from __future__ import annotations

import os
import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from .result_types import CoreResult, ErrorCode

logger = logging.getLogger(__name__)


class OperationCancellation:
    def __init__(self):
        self._lock = RLock()
        self._cancelled = False
        self._published = False

    def cancel(self):
        with self._lock:
            if self._published:
                return False
            self._cancelled = True
            return True

    def mark_published(self):
        with self._lock:
            self._published = True

    def is_cancelled(self):
        with self._lock:
            return self._cancelled

    @contextmanager
    def publication(self):
        with self._lock:
            yield not self._cancelled


class OutputWriter:
    def __init__(self):
        self._lock = RLock()
        self._targets = {}

    @contextmanager
    def reserve(self, path):
        target = Path(path).resolve()
        key = os.path.normcase(str(target))
        ticket = object()
        with self._lock:
            state = self._targets.setdefault(key, {"count": 0, "latest": None})
            state["count"] += 1
            state["latest"] = ticket
        try:
            yield target, key, ticket
        finally:
            with self._lock:
                state["count"] -= 1
                if not state["count"]:
                    self._targets.pop(key)

    def is_current(self, reservation):
        with self._lock:
            state = self._targets.get(reservation[1])
            return state is not None and state["latest"] is reservation[2]

    @contextmanager
    def publication(self, reservation, publication):
        with self._lock, publication() as valid:
            yield valid and (reservation is None or self.is_current(reservation))

    def write(self, reservation, *, ext, overwrite, encode, validate, publication, on_published=None):
        target, _, _ = reservation
        if target.exists() and not overwrite:
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.export.exists")
        if not self.is_current(reservation):
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=ext)
        os.close(fd)
        try:
            encode(temporary)
            validate(temporary)
            with open(temporary, "rb+") as stream:
                os.fsync(stream.fileno())
            size = os.path.getsize(temporary)
            with self.publication(reservation, publication) as valid:
                if not valid:
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                if overwrite:
                    os.replace(temporary, target)
                else:
                    os.link(temporary, target)
                if on_published is not None:
                    on_published()
            return CoreResult.ok(size)
        except FileExistsError:
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.export.exists")
        finally:
            if os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    logger.exception("未能清理导出临时文件：%s", temporary)
