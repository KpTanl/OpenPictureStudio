from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Condition
from concurrent.futures import CancelledError
from time import monotonic
from collections import OrderedDict
import hashlib
import logging

from ..core.contracts.workflow import _canonical_json
from ..core.execution_memo import current_memo, memo_cancellation, freeze, key_data

logger = logging.getLogger(__name__)


class _RetryStage(Exception):
    pass


class _DirectLease:
    def __init__(self, value):
        self.value = value

    def close(self):
        self.value = None


@dataclass
class _Stage:
    subscribers: dict = field(default_factory=dict)
    producer: object = None
    error: BaseException | None = None
    ready: bool = False
    # 已经有订阅者拿到了结果：之后别的订阅者再取消，这份结果也有人在用，不再丢弃
    delivered: bool = False


class SharedStageStore:
    def __init__(self, resources, compute):
        self.resources, self.compute = resources, compute
        self._condition = Condition()
        self._entries = {}
        self._closed = False
        self._hot = OrderedDict()

    def hot(self, family, identity):
        family, identity = self.key("hot", family), self.key("hot-value", identity)
        with self._condition:
            previous = self._hot.get(family)
            self._hot[family] = identity
            self._hot.move_to_end(family)
            while len(self._hot) > 128:
                self._hot.popitem(last=False)
        return previous is None or previous == identity

    @staticmethod
    def key(name, identity):
        return "stage", name, hashlib.sha256(_canonical_json(key_data(identity)).encode("utf-8")).hexdigest()

    def ready(self, key):
        if self.resources.contains(key):
            return True
        with self._condition:
            entry = self._entries.get(key)
            return entry is not None and entry.ready and entry.error is None

    def cached(self, key, cancelled):
        if cancelled() or self._closed:
            raise CancelledError()
        return self.resources.borrow_cached(key)

    def acquire(self, key, load, cancelled, priority=0, *, retain=True, optional=False, transient=False):
        while True:
            if transient and (cancelled() or self._closed):
                raise CancelledError()
            cached = None if transient else self.cached(key, cancelled)
            if cached is not None:
                logger.debug("阶段命中 name=%s key=%s", key[1], key[-1][:12])
                return cached
            token = object()
            with self._condition:
                if self._closed:
                    raise CancelledError()
                entry = self._entries.get(key)
                leader = entry is None
                if leader:
                    entry = self._entries[key] = _Stage()
                entry.subscribers[token] = cancelled, priority

            def abandoned():
                with self._condition:
                    callbacks = tuple(item[0] for item in entry.subscribers.values())
                    closed = self._closed
                return closed or not callbacks or all(callback() for callback in callbacks)

            def shared_priority():
                with self._condition:
                    subscribers = tuple(entry.subscribers.values())
                priorities = [value() if callable(value) else value for abort, value in subscribers if not abort()]
                return min(priorities, default=0)

            abandoned.priority = shared_priority
            started = monotonic()
            try:
                if leader:
                    try:
                        entry.producer = None if transient else self.cached(key, abandoned)
                        if entry.producer is None:
                            value = load(abandoned)
                            if abandoned():
                                raise CancelledError()
                            try:
                                entry.producer = (_DirectLease(value) if transient else
                                                  self.resources.publish_shared(key, value, retain=retain))
                            except MemoryError:
                                if not optional:
                                    raise
                                entry.error = _RetryStage()
                                return _DirectLease(value)
                    except BaseException as error:
                        entry.error = error.with_traceback(None)
                    finally:
                        with self._condition:
                            entry.ready = True
                            if entry.error is not None and self._entries.get(key) is entry:
                                self._entries.pop(key)
                            self._condition.notify_all()
                else:
                    with self.compute.suspend(cancelled):
                        with self._condition:
                            while not entry.ready:
                                if cancelled() or self._closed:
                                    raise CancelledError()
                                self._condition.wait(0.05)
                if cancelled() or self._closed:
                    raise CancelledError()
                if isinstance(entry.error, (CancelledError, _RetryStage)):
                    continue
                if entry.error is not None:
                    raise entry.error
                result = (_DirectLease(entry.producer.value) if transient else self.resources.borrow_cached(key))
                if result is not None:
                    with self._condition:
                        entry.delivered = True
                    logger.debug("阶段交付 name=%s key=%s leader=%s wait_ms=%.1f", key[1], key[-1][:12], leader,
                                 (monotonic() - started) * 1000)
                    return result
            finally:
                producer = None
                discard = abandoned()
                with self._condition:
                    entry.subscribers.pop(token, None)
                    discard = discard and not entry.delivered
                    if not entry.subscribers and entry.ready:
                        if self._entries.get(key) is entry:
                            self._entries.pop(key)
                        producer, entry.producer = entry.producer, None
                if producer is not None:
                    producer.close()
                    if discard and not transient:
                        self.resources.discard(key)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class MemoScope:
    def __init__(self, store, namespace, input_identity, cancelled, priority, enabled=True):
        self.store, self.namespace, self.input_identity = store, namespace, input_identity
        self.cancelled, self.priority, self.enabled = cancelled, priority, enabled
        self.domain = ()
        self.retain = True
        self._leases = []

    def memo(self, name, key, load):
        if not self.enabled:
            return load()
        identity = self.store.key("memo:" + name, (self.namespace, self.domain, key))
        def calculate(abandoned):
            token = memo_cancellation.set(abandoned)
            try:
                return freeze(load())
            finally:
                memo_cancellation.reset(token)
        lease = self.store.acquire(identity, calculate, self.cancelled, self.priority, optional=True, retain=self.retain)
        self._leases.append(lease)
        return lease.value

    @contextmanager
    def activate(self):
        token = current_memo.set(self)
        try:
            yield self
        finally:
            current_memo.reset(token)
            for lease in reversed(self._leases):
                lease.close()
            self._leases.clear()
