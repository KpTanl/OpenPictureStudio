from __future__ import annotations

import logging
import time
from collections import OrderedDict
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    tool_id: str
    executor: object = None
    busy: bool = True
    retired: bool = False
    digests: frozenset = frozenset()


class ToolExecutorPool:
    def __init__(self, capacity=3):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("执行实例容量必须为正整数")
        self._capacity = capacity
        self._entries = OrderedDict()
        self._condition = Condition()
        self._closed = False
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
    def borrow(self, key, tool_id, factory, is_cancelled, *, digests=frozenset()):
        created = False
        while True:
            if is_cancelled():
                raise CancelledError()
            victim = None
            with self._condition:
                if self._closed:
                    raise CancelledError()
                entry = self._entries.get(key)
                if entry is not None and not entry.busy and not entry.retired:
                    entry.busy = True
                    self._entries.move_to_end(key)
                    break
                if entry is None and len(self._entries) < self._capacity:
                    entry = _Entry(tool_id, digests=digests)
                    self._entries[key] = entry
                    created = True
                    break
                if entry is None:
                    victim = next(((old_key, old_entry) for old_key, old_entry in self._entries.items()
                                   if not old_entry.busy), None)
                    if victim is not None:
                        victim[1].busy = True
                        victim[1].retired = True
                if victim is None:
                    self._condition.wait(0.05)
            if victim is not None:
                self._dispose(*victim)
        try:
            if created:
                started = time.perf_counter()
                entry.executor = factory()
                logger.debug("工具执行实例创建 tool=%s duration_ms=%.1f", tool_id,
                             (time.perf_counter() - started) * 1000)
            else:
                logger.debug("工具执行实例复用 tool=%s", tool_id)
            if is_cancelled() or entry.retired:
                raise CancelledError()
            yield entry.executor
            if is_cancelled() or entry.retired:
                raise CancelledError()
        except BaseException:
            entry.retired = True
            raise
        finally:
            with self._condition:
                dispose = entry.retired or self._closed
                if not dispose:
                    entry.busy = False
                    self._condition.notify_all()
            if dispose:
                self._dispose(key, entry)

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
