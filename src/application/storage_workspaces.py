from __future__ import annotations

from contextlib import contextmanager
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import shutil
import tempfile
from threading import BoundedSemaphore, Lock
import uuid
import weakref

from .storage_paths import check_plain_path, file_lock, plain, read_settings, temporary_root

logger = logging.getLogger(__name__)
_cleanup_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-workspace-cleanup")
_cleanup_finalized = SimpleQueue()
_cleanup_slots = BoundedSemaphore(32)
_cleanup_pending = deque()
_cleanup_lock = Lock()
_cleanup_running = False


def tree_size(root):
    total = 0
    def failed(error):
        raise error
    for parent, directories, files in os.walk(root, followlinks=False, onerror=failed):
        directories[:] = [name for name in directories if plain(Path(parent) / name)]
        for name in files:
            path = Path(parent) / name
            if not plain(path):
                raise ValueError("storage.error.link")
            total += path.stat().st_size
    return total


def _finish(root, token, lease, references):
    try:
        if references is None:
            return
        if plain(root) and json.loads((root / "owner.json").read_text(encoding="utf-8"))["token"] == token:
            from .storage_service import contains
            if any(contains(root, path) for path in references):
                return
        else:
            return
    except Exception:
        logger.warning("工作目录引用核验失败，保留目录：%s", root, exc_info=True)
        return
    finally:
        lease.__exit__(None, None, None)
    try:
        if plain(root) and json.loads((root / "owner.json").read_text(encoding="utf-8"))["token"] == token:
            shutil.rmtree(root)
    except (OSError, ValueError, KeyError):
        logger.debug("工作目录回收未完成：%s", root, exc_info=True)
    except Exception:
        logger.exception("工作目录回收失败：%s", root)


def _drain_cleanup():
    global _cleanup_pending, _cleanup_running
    while True:
        entries = deque()
        with _cleanup_lock:
            if not _cleanup_pending:
                _cleanup_running = False
                return
            entries, _cleanup_pending = _cleanup_pending, entries
        try:
            from .storage_service import StorageService
            references = StorageService().reference_paths()
        except Exception:
            logger.warning("工作目录引用核验失败，保留本批目录", exc_info=True)
            references = None
        for root, token, lease in entries:
            try:
                _finish(root, token, lease, references)
            except Exception:
                logger.exception("工作目录收尾失败：%s", root)
            finally:
                _cleanup_slots.release()


def _schedule_finish(root, token, lease):
    global _cleanup_pending, _cleanup_running
    entry = (root, token, lease)
    if _cleanup_slots.acquire(blocking=False):
        with _cleanup_lock:
            _cleanup_pending.append(entry)
            if _cleanup_running:
                return
            _cleanup_running = True
        try:
            _cleanup_pool.submit(_drain_cleanup)
            return
        except RuntimeError:
            entries = deque()
            with _cleanup_lock:
                entries, _cleanup_pending = _cleanup_pending, entries
                _cleanup_running = False
            for root, token, lease in entries:
                _cleanup_slots.release()
                lease.__exit__(None, None, None)
                logger.debug("工作目录保留供后续维护：%s", root)
            return
    lease.__exit__(None, None, None)
    logger.debug("工作目录保留供后续维护：%s", root)


def _schedule_cleanup():
    while True:
        try:
            entry = _cleanup_finalized.get_nowait()
        except Empty:
            return
        _schedule_finish(*entry)


class ManagedWorkspace:
    def __init__(self, kind):
        _schedule_cleanup()
        self.kind = kind
        self.root = temporary_root(kind)
        check_plain_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        if not plain(self.root):
            raise ValueError("storage.error.link")
        path = Path(tempfile.mkdtemp(prefix="work-", dir=self.root))
        token = uuid.uuid4().hex
        (path / "owner.json").write_text(json.dumps({"version": 1, "kind": kind, "pid": os.getpid(), "token": token}), encoding="utf-8")
        lease = file_lock(path / "owner.lock")
        lease.__enter__()
        self.name = str(path)
        self._finalizer = weakref.finalize(self, _cleanup_finalized.put, (path, token, lease))

    @contextmanager
    def reserve(self, size):
        limit = read_settings()["limits"].get(self.kind, 0) * 1024 ** 2
        if limit:
            with file_lock(self.root / "budget.lock", blocking=True):
                occupied = tree_size(self.root)
                if occupied + size > limit:
                    from .storage_service import StorageService
                    for entry in StorageService().clean_candidates(self.kind):
                        path = Path(entry["id"])
                        with file_lock(path / "owner.lock"):
                            token = json.loads((path / "owner.json").read_text(encoding="utf-8"))["token"]
                            if token != entry["token"]:
                                continue
                        shutil.rmtree(path)
                    occupied = tree_size(self.root)
                if occupied + size > limit:
                    raise ValueError("storage.error.quota")
                yield
        else:
            yield

    def cleanup(self):
        self._finalizer()
        _schedule_cleanup()

    def __enter__(self):
        return self.name

    def __exit__(self, *_):
        self.cleanup()
