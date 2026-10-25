from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .contracts import ArtifactRef
from .project.project_store import atomic_write


class ArtifactStore:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, payload: bytes, *, media_type="image/png", kind="render", producer_node=""):
        extensions = {"image/png": ".png", "image/tiff": ".tiff"}
        digest = hashlib.sha256(payload).hexdigest()
        path = self.root / (digest + extensions[media_type])
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("已有产物校验失败")
        else:
            atomic_write(path, payload)
        return ArtifactRef(hash=digest, kind=kind, media_type=media_type,
                           producer_node=producer_node, path=str(path))

    def read(self, ref):
        path = Path(ref.path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("产物路径越界")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != ref.hash:
            raise ValueError("产物哈希校验失败")
        return payload

    def put_file(self, source, *, media_type, kind, producer_node=""):
        digest = hashlib.sha256()
        directory = Path(tempfile.mkdtemp(dir=self.root, prefix="commit-"))
        temporary = directory / "render.tmp"
        try:
            with open(temporary, "wb") as output, open(source, "rb") as input_file:
                for chunk in iter(lambda: input_file.read(1 << 20), b""):
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            value = digest.hexdigest()
            path = directory / (value + {"image/png": ".png", "image/tiff": ".tiff"}[media_type])
            os.replace(temporary, path)
            return ArtifactRef(hash=value, kind=kind, media_type=media_type,
                               producer_node=producer_node, path=str(path))
        except Exception:
            temporary.unlink(missing_ok=True)
            directory.rmdir()
            raise

    def discard_unpublished(self, ref):
        path = Path(ref.path).resolve()
        if path.parent.parent != self.root or not path.parent.name.startswith("commit-"):
            raise ValueError("只能清理本次未发布的提交产物")
        path.unlink(missing_ok=True)
        path.parent.rmdir()
