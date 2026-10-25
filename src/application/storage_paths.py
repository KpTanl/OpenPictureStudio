from __future__ import annotations

from contextlib import contextmanager
import copy
import errno
import json
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from threading import RLock
import uuid

from ..utils.user_paths import application_root

logger = logging.getLogger(__name__)
_lock = RLock()
_settings_cache = None
LOCATION_KEYS = ("data", "thumbnails", "edit_cache", "models", "runtimes", "logs",
                 "artifacts", "audit", "transforms", "plugins")
TEMP_KEYS = ("artifacts", "audit", "transforms", "plugins")


def bootstrap_root():
    return application_root("LOCALAPPDATA") / ".storage"


def plain(path):
    info = Path(path).lstat()
    return not (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))


def check_plain_path(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.exists() and not plain(parent):
            raise ValueError("storage.error.link")


@contextmanager
def file_lock(path, *, blocking=False, is_cancelled=lambda: False):
    path = Path(path)
    check_plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not plain(path):
        raise ValueError("storage.error.link")
    stream = path.open("a+b")
    try:
        stream.seek(0)
        while True:
            if is_cancelled():
                raise InterruptedError("storage.cancelled")
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if not blocking or error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                time.sleep(0.05)
        yield stream
    finally:
        stream.close()


def read_settings():
    global _settings_cache
    path = bootstrap_root() / "storage.json"
    if not path.exists():
        return {"version": 1, "locations": {}, "limits": {}, "projects": [], "jobs": [], "retired": [], "results": []}
    try:
        info = path.stat()
        signature = str(path), info.st_ino, info.st_mtime_ns, info.st_size
        if _settings_cache is not None and _settings_cache[0] == signature:
            return _settings_cache[1]
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("storage.error.configuration") from error
    if (not isinstance(value, dict) or value.get("version") != 1
            or any(not isinstance(value.get(key), dict) for key in ("locations", "limits"))
            or any(not isinstance(value.get(key), list) for key in ("projects", "jobs", "retired", "results"))):
        raise ValueError("storage.error.configuration")
    _settings_cache = signature, value
    return value


def _write_settings(value, *, is_cancelled=lambda: False):
    root = bootstrap_root()
    root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        if is_cancelled():
            raise InterruptedError("storage.cancelled")
        os.replace(temporary, root / "storage.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


def update_settings(change, *, is_cancelled=lambda: False, timeout=None):
    deadline = None if timeout is None else time.monotonic() + timeout
    def cancelled():
        if is_cancelled():
            raise InterruptedError("storage.cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("storage.error.lock_timeout")
        return False
    cancelled()
    while not _lock.acquire(timeout=0.05):
        cancelled()
    try:
        cancelled()
        with file_lock(bootstrap_root() / "settings.lock", blocking=True, is_cancelled=cancelled):
            if is_cancelled():
                raise InterruptedError("storage.cancelled")
            value = copy.deepcopy(read_settings())
            result = change(value)
            _write_settings(value, is_cancelled=is_cancelled)
            return result
    finally:
        _lock.release()


def storage_path(kind, default):
    return Path(read_settings()["locations"].get(kind) or default).absolute()


def default_data_root():
    return application_root("LOCALAPPDATA")


def data_root():
    return storage_path("data", default_data_root())


def temporary_root(kind):
    base = Path(tempfile.gettempdir())
    root = base / "OpenPictureStudio-work"
    return storage_path(kind, root / kind)


def register_project(path):
    path = str(Path(path).absolute())
    try:
        def change(value):
            if path not in value["projects"]:
                value["projects"].append(path)
        update_settings(change)
    except (OSError, ValueError):
        logger.warning("项目位置登记失败：%s", path, exc_info=True)


class StorageSession:
    def __init__(self, *, cancelled=lambda: False, progress=lambda kind: None):
        self._gate = file_lock(bootstrap_root() / "gate.lock", blocking=True)
        self._gate.__enter__()
        self._lease = None
        self._gate_active = True
        self._cancelled, self._progress = cancelled, progress
        try:
            busy = False
            for path in (bootstrap_root() / "sessions").glob("*.lock"):
                try:
                    with file_lock(path):
                        pass
                    path.unlink()
                except OSError:
                    busy = True
            self._busy = busy
            if not busy:
                from .storage_service import execute_pending
                execute_pending(phase="locations", cancelled=cancelled, progress=progress)
            path = bootstrap_root() / "sessions" / (uuid.uuid4().hex + ".lock")
            self._path = path
            self._lease = file_lock(path)
            self._lease.__enter__()
        except BaseException:
            self.close()
            raise

    def ready(self):
        try:
            if not self._busy:
                from .storage_service import execute_pending
                execute_pending(phase="data", cancelled=self._cancelled, progress=self._progress)
        finally:
            self._release_gate()

    def _release_gate(self):
        if self._gate_active:
            self._gate.__exit__(None, None, None)
            self._gate_active = False

    def close(self):
        self._release_gate()
        if self._lease is not None:
            self._lease.__exit__(None, None, None)
            self._lease = None
            self._path.unlink(missing_ok=True)
