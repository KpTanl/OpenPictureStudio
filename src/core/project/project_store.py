from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from concurrent.futures import CancelledError
from dataclasses import dataclass
from pathlib import Path

from .document import ProjectDocument
from .assets import ProjectAssetError

logger = logging.getLogger(__name__)


class ProjectChangedError(ValueError):
    pass


def check_cancelled(is_cancelled):
    if is_cancelled():
        raise CancelledError()


def file_stamp(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def read_bytes(path, is_cancelled):
    chunks = []
    with path.open("rb") as stream:
        while True:
            check_cancelled(is_cancelled)
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def hash_file(path, is_cancelled):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            check_cancelled(is_cancelled)
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def remove_temporary(path):
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("未能清理项目临时文件：%s", path)


def prepare_bytes(path, payload, is_cancelled):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            for offset in range(0, len(payload), 1 << 20):
                check_cancelled(is_cancelled)
                stream.write(payload[offset:offset + (1 << 20)])
            stream.flush()
            os.fsync(stream.fileno())
        check_cancelled(is_cancelled)
        return Path(temporary)
    except BaseException:
        remove_temporary(Path(temporary))
        raise


def atomic_write(path: Path, payload: bytes, *, is_cancelled=lambda: False):
    temporary = prepare_bytes(path, payload, is_cancelled)
    try:
        check_cancelled(is_cancelled)
        os.replace(temporary, path)
    finally:
        remove_temporary(temporary)


def clear_pending(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("暂未清理项目恢复日志：%s", path)


class ProjectStore:
    def _read(self, payload, root, is_cancelled):
        check_cancelled(is_cancelled)
        document = ProjectDocument.from_dict(json.loads(payload))
        checked_workflows = set()
        checked_artifacts = set()
        from .assets import validate_asset
        for digest, asset in document.assets.items():
            check_cancelled(is_cancelled)
            relative = Path(asset["path"])
            location = (root / relative).resolve()
            if relative.is_absolute() or ".." in relative.parts or not location.is_relative_to(root):
                raise ValueError("资产路径越界")
            validate_asset(location, digest, asset["size"], is_cancelled)
        for revision in document.revisions:
            check_cancelled(is_cancelled)
            digest = revision["workflow_hash"]
            snapshot = root / "workflows" / f"{digest}.json"
            if digest not in checked_workflows:
                if hash_file(snapshot, is_cancelled) != digest:
                    raise ValueError("持久化工作流损坏")
                checked_workflows.add(digest)
            for artifact in revision["artifact_refs"]:
                relative = Path(artifact["path"])
                location = (root / relative).resolve()
                if relative.is_absolute() or ".." in relative.parts or not location.is_relative_to(root):
                    raise ValueError("产物路径越界")
                key = (location, artifact["hash"])
                if key not in checked_artifacts:
                    if hash_file(location, is_cancelled) != artifact["hash"]:
                        raise ValueError("历史产物校验失败")
                    checked_artifacts.add(key)
        check_cancelled(is_cancelled)
        return document

    def save(self, path: str, document: ProjectDocument) -> None:
        prepared = self.prepare(path, document)
        try:
            self.publish(prepared)
        finally:
            prepared.close()

    def prepare(self, path: str, document: ProjectDocument, *, expected_hash=None,
                is_cancelled=lambda: False):
        from .revision_store import RevisionStore
        check_cancelled(is_cancelled)
        target = Path(path).resolve()
        if target.suffix.lower() != ".opsproj":
            raise ValueError("项目扩展名必须为 .opsproj")
        root = target.with_suffix(".opsdata")
        previous_stamp = file_stamp(target)
        previous = read_bytes(target, is_cancelled) if previous_stamp is not None else None
        if file_stamp(target) != previous_stamp:
            raise ProjectChangedError("项目在读取期间已被修改")
        if expected_hash is not None and (
            previous is None or hashlib.sha256(previous).hexdigest() != expected_hash
        ):
            raise ProjectChangedError("项目在保存期间已被修改")
        if previous is not None:
            self._read(previous, root, is_cancelled)
        payload = json.dumps(document.to_dict(), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        ProjectDocument.from_dict(json.loads(payload))
        snapshots = RevisionStore(str(root / "workflows"))
        for workflow in document.workflows.values():
            check_cancelled(is_cancelled)
            from ..contracts import WorkflowGraph
            snapshots.write_workflow(WorkflowGraph.from_dict(workflow).workflow_hash(), workflow,
                                     is_cancelled=is_cancelled)
        self._read(payload, root, is_cancelled)
        temporary = prepare_bytes(target, payload, is_cancelled)
        journal_temporary = None
        try:
            if previous is not None:
                journal_temporary = prepare_bytes(root / "journal" / "pending.json", previous, is_cancelled)
            return PreparedProject(target, hashlib.sha256(payload).hexdigest(), previous_stamp,
                                   temporary, journal_temporary)
        except BaseException:
            remove_temporary(temporary)
            remove_temporary(journal_temporary)
            raise

    def publish(self, prepared):
        target = prepared.target
        if file_stamp(target) != prepared.previous_stamp:
            raise ProjectChangedError("项目在保存期间已被修改")
        pending = target.with_suffix(".opsdata") / "journal" / "pending.json"
        if prepared.journal_temporary is not None:
            os.replace(prepared.journal_temporary, pending)
        os.replace(prepared.temporary, target)
        clear_pending(pending)

    def prepare_load(self, path: str, *, is_cancelled=lambda: False):
        check_cancelled(is_cancelled)
        target = Path(path).resolve()
        root = target.with_suffix(".opsdata")
        pending = root / "journal" / "pending.json"
        previous_stamp = file_stamp(target)
        recovery = None
        try:
            payload = read_bytes(target, is_cancelled)
            document = self._read(payload, root, is_cancelled)
        except ProjectAssetError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            if not pending.exists():
                raise
            payload = read_bytes(pending, is_cancelled)
            document = self._read(payload, root, is_cancelled)
            recovery = prepare_bytes(target, payload, is_cancelled)
        try:
            check_cancelled(is_cancelled)
            if file_stamp(target) != previous_stamp:
                raise ProjectChangedError("项目在打开期间已被修改")
            return LoadedProject(target, document, hashlib.sha256(payload).hexdigest(),
                                 previous_stamp, recovery)
        except BaseException:
            remove_temporary(recovery)
            raise

    def publish_load(self, loaded):
        target = loaded.target
        if file_stamp(target) != loaded.previous_stamp:
            raise ProjectChangedError("项目在打开期间已被修改")
        if loaded.recovery is not None:
            os.replace(loaded.recovery, target)
        pending = target.with_suffix(".opsdata") / "journal" / "pending.json"
        clear_pending(pending)

    def load(self, path: str) -> ProjectDocument:
        loaded = self.prepare_load(path)
        try:
            self.publish_load(loaded)
            return loaded.document
        finally:
            loaded.close()

    def copy_artifact(self, source, target, expected_hash, *, is_cancelled=lambda: False, replace_corrupt=False):
        check_cancelled(is_cancelled)
        if target.exists():
            if source != target and hash_file(source, is_cancelled) != expected_hash:
                raise ValueError("历史产物校验失败")
            if hash_file(target, is_cancelled) == expected_hash:
                return
            if not replace_corrupt:
                raise ValueError("历史产物校验失败")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as output, source.open("rb") as original:
                while True:
                    check_cancelled(is_cancelled)
                    chunk = original.read(1 << 20)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != expected_hash:
                raise ValueError("历史产物校验失败")
            check_cancelled(is_cancelled)
            os.replace(temporary, target)
        finally:
            remove_temporary(Path(temporary))


@dataclass
class PreparedProject:
    target: Path
    file_hash: str
    previous_stamp: tuple | None
    temporary: Path
    journal_temporary: Path | None

    def close(self):
        remove_temporary(self.temporary)
        remove_temporary(self.journal_temporary)


@dataclass
class LoadedProject:
    target: Path
    document: ProjectDocument
    file_hash: str
    previous_stamp: tuple | None
    recovery: Path | None

    def close(self):
        remove_temporary(self.recovery)
