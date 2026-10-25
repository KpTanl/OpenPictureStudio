from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import CancelledError
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Condition
import logging
from time import monotonic


logger = logging.getLogger(__name__)


class _PreparationAbandoned(Exception):
    pass


@dataclass
class _Preparation:
    subscribers: dict = field(default_factory=dict)
    snapshot: object = None
    error: BaseException | None = None
    ready: bool = False
    borrowers: int = 0
    hold: object = None


class BeautyPreparationService:
    def __init__(self, resources=None):
        self._resources = resources
        self._condition = Condition()
        self._entries = OrderedDict()
        self._closed = False

    def _trim(self, *, all_idle=False):
        size = sum(entry.snapshot.nbytes for entry in self._entries.values() if entry.snapshot is not None)
        for key, entry in tuple(self._entries.items()):
            if not entry.ready or entry.subscribers or entry.borrowers:
                continue
            if all_idle or entry.error is not None or len(self._entries) > 8 or size > 8 * 1024 * 1024:
                self._entries.pop(key)
                if entry.snapshot is not None:
                    size -= entry.snapshot.nbytes
                if entry.hold is not None:
                    entry.hold.close()

    @contextmanager
    def borrow_cached(self, key, cancelled):
        with self._condition:
            if self._closed or cancelled():
                raise CancelledError()
            entry = self._entries.get(key)
            available = entry is not None and entry.ready and entry.error is None
            if available:
                entry.borrowers += 1
                self._entries.move_to_end(key)
        try:
            yield entry.snapshot if available else None
        finally:
            if available:
                with self._condition:
                    entry.borrowers -= 1
                    self._trim(all_idle=self._closed)

    @contextmanager
    def borrow(self, key, loader, cancelled):
        while True:
            try:
                with self._borrow(key, loader, cancelled) as snapshot:
                    yield snapshot
                return
            except _PreparationAbandoned:
                if cancelled() or self._closed:
                    raise CancelledError() from None

    @contextmanager
    def _borrow(self, key, loader, cancelled):
        started = monotonic()
        token = object()
        with self._condition:
            if self._closed or cancelled():
                raise CancelledError()
            entry = self._entries.get(key)
            if entry is not None and entry.ready and entry.error is not None:
                self._entries.pop(key)
                entry = None
            leader = entry is None
            if leader:
                entry = self._entries[key] = _Preparation()
            self._entries.move_to_end(key)
            entry.subscribers[token] = cancelled

        def abandoned():
            with self._condition:
                callbacks = tuple(entry.subscribers.values())
                closed = self._closed
            return closed or not callbacks or all(callback() for callback in callbacks)

        try:
            if leader:
                try:
                    snapshot = loader(abandoned)
                    if abandoned():
                        raise CancelledError()
                    if self._resources is not None:
                        try:
                            entry.hold = self._resources.hold_memory(snapshot.nbytes, abandoned)
                        except MemoryError:
                            with self._condition:
                                self._trim(all_idle=True)
                            entry.hold = self._resources.hold_memory(snapshot.nbytes, abandoned)
                    entry.snapshot = snapshot
                except BaseException as error:
                    entry.error = error
                finally:
                    with self._condition:
                        entry.ready = True
                        self._condition.notify_all()
            else:
                with self._condition:
                    while not entry.ready:
                        if cancelled() or self._closed:
                            raise CancelledError()
                        self._condition.wait(0.05)
            with self._condition:
                if cancelled() or self._closed:
                    raise CancelledError()
                if entry.error is not None:
                    if isinstance(entry.error, CancelledError):
                        raise _PreparationAbandoned() from None
                    raise entry.error
                entry.borrowers += 1
            logger.debug("美颜准备 cache=%s status=%s faces=%s skipped=%s duration_ms=%.1f",
                         "cold" if leader else "warm", entry.snapshot.status, entry.snapshot.processed,
                         entry.snapshot.skipped, (monotonic() - started) * 1000)
            try:
                yield entry.snapshot
            finally:
                with self._condition:
                    entry.borrowers -= 1
        finally:
            with self._condition:
                entry.subscribers.pop(token, None)
                self._trim(all_idle=self._closed)

    def close(self):
        with self._condition:
            self._closed = True
            self._trim(all_idle=True)
            self._condition.notify_all()
