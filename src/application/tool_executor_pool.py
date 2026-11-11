from __future__ import annotations

import logging
import time
from collections import OrderedDict
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition

from .image_resources import queue_order

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    tool_id: str
    executor: object = None
    busy: bool = True
    retired: bool = False
    digests: frozenset = frozenset()


@dataclass(eq=False)
class _Request:
    key: object
    tool_id: str
    priority: object
    created: float
    digests: frozenset
    ready: object
    cancelled: object


class ExecutorReservation:
    def __init__(self, pool, request, entry, created):
        self.pool, self.request, self.entry, self.created = pool, request, entry, created

    def initialize(self, factory):
        if self.created:
            self.entry.executor = factory()
        return self.entry.executor

    def close(self, *, failed=False):
        pool, self.pool = self.pool, None
        if pool is None:
            return
        with pool._condition:
            self.entry.retired |= failed or self.entry.executor is None
            dispose = self.entry.retired or pool._closed
            if not dispose:
                self.entry.busy = False
            pool._condition.notify_all()
        if dispose:
            pool._dispose(self.request.key, self.entry)


class ToolExecutorPool:
    def __init__(self, capacity=3):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("执行实例容量必须为正整数")
        self._capacity = capacity
        self._entries = OrderedDict()
        self._condition = Condition()
        self._closed = False
        self._waiting = []
        self._cleanup = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-model-release")

    def _dispose(self, key, entry):
        try:
            if entry.executor is not None:
                entry.executor.close()
        except Exception:
            logger.exception("工具执行实例释放失败 tool=%s", entry.tool_id)
        finally:
            entry.executor = None
            with self._condition:
                if self._entries.get(key) is entry:
                    del self._entries[key]
                self._condition.notify_all()

    @contextmanager
    def request(self, key, tool_id, *, priority=0, digests=frozenset(),
                ready=lambda: True, cancelled=lambda: False):
        request = _Request(key, tool_id, priority, time.monotonic(), digests, ready, cancelled)
        with self._condition:
            if self._closed:
                raise CancelledError()
            self._waiting.append(request)
        try:
            yield request
        finally:
            with self._condition:
                self._waiting.remove(request)
                self._condition.notify_all()

    def _available(self, request):
        entry = self._entries.get(request.key)
        if entry is not None:
            return not entry.busy and not entry.retired
        return len(self._entries) < self._capacity or any(not item.busy for item in self._entries.values())

    def try_reserve(self, request):
        victim = None
        with self._condition:
            waiting = tuple(self._waiting)
        now = time.monotonic()
        order = {item: queue_order(item.priority, item.created, now) for item in waiting
                 if not item.cancelled() and item.ready()}
        with self._condition:
            if self._closed:
                raise CancelledError()
            present = request.key in self._entries
            candidates = [item for item in self._waiting if item in order and self._available(item)
                          and (item.key == request.key if present else item.key not in self._entries)]
            first = min(candidates, key=order.get) if candidates else None
            if first is not request:
                return None
            entry = self._entries.get(request.key)
            created = entry is None
            if created and len(self._entries) >= self._capacity:
                protected = {item.key for item in self._waiting if item in order and order[item] < order[request]}
                victim = next((item for item in self._entries.items()
                               if not item[1].busy and item[0] not in protected), None)
                if victim is None:
                    return None
                victim[1].busy = victim[1].retired = True
            elif created:
                entry = _Entry(request.tool_id, digests=request.digests)
                self._entries[request.key] = entry
            if victim is None:
                entry.busy = True
                self._entries.move_to_end(request.key)
                return ExecutorReservation(self, request, entry, created)
        self._dispose(*victim)
        return None

    @contextmanager
    def borrow(self, key, tool_id, factory, is_cancelled, *, digests=frozenset(), priority=0):
        with self.request(key, tool_id, priority=priority, digests=digests, cancelled=is_cancelled) as request:
            reservation = None
            while reservation is None:
                if is_cancelled():
                    raise CancelledError()
                reservation = self.try_reserve(request)
                if reservation is None:
                    with self._condition:
                        self._condition.wait(0.05)
            failed = True
            try:
                started = time.monotonic()
                executor = reservation.initialize(factory)
                logger.debug("工具执行实例 tool=%s created=%s wait_ms=%.1f initialize_ms=%.1f",
                             tool_id, reservation.created, (started - request.created) * 1000,
                             (time.monotonic() - started) * 1000)
                if is_cancelled() or reservation.entry.retired:
                    raise CancelledError()
                yield executor
                if is_cancelled() or reservation.entry.retired:
                    raise CancelledError()
                failed = False
            finally:
                reservation.close(failed=failed)

    def invalidate(self, tool_ids=None):
        with self._condition:
            for key, entry in tuple(self._entries.items()):
                if tool_ids is not None and entry.tool_id not in tool_ids:
                    continue
                entry.retired = True
                if not entry.busy:
                    entry.busy = True
                    self._cleanup.submit(self._dispose, key, entry)
            self._condition.notify_all()

    def trim_idle(self):
        with self._condition:
            victims = [(key, entry) for key, entry in self._entries.items() if not entry.busy]
            for _key, entry in victims:
                entry.busy, entry.retired = True, True
        for key, entry in victims:
            self._dispose(key, entry)

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self.invalidate()
        self._cleanup.shutdown(wait=False)

    def invalidate_resources(self, digests):
        with self._condition:
            if self._closed:
                return
            for key, entry in tuple(self._entries.items()):
                if entry.digests.isdisjoint(digests):
                    continue
                entry.retired = True
                if not entry.busy:
                    entry.busy = True
                    self._cleanup.submit(self._dispose, key, entry)
            self._condition.notify_all()
