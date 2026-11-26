from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import CancelledError
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from threading import Condition, Event, RLock, local, get_ident
from typing import Callable
import logging
import time
from ..core.contracts.result import MEMORY_AVAILABLE_KEY, MEMORY_BUDGET_KEY
from ..utils.log_levels import TRACE
from .memory_settings import (DISPLAY_CACHE_BYTES, FALLBACK_LIMIT_BYTES, PREVIEW_CACHE_BYTES, PREVIEW_CACHE_ITEMS,
                              PROXY_CACHE_BYTES, SOURCE_CACHE_BYTES, SOURCE_CACHE_ITEMS, MemorySettings, resolve)
from .system_profile import available_logical_count, available_memory, memory_status, physical_core_count

logger = logging.getLogger(__name__)


def queue_order(priority, created, now):
    value = priority() if callable(priority) else priority
    return value - (now - created) / 2, created

# 可以在运行中替换的内存字段
MEMORY_FIELDS = frozenset({"memory_bytes", "memory_seat_bytes", "memory_reserve_bytes", "source_cache_bytes",
                           "proxy_cache_bytes", "display_cache_bytes", "preview_cache_bytes",
                           "source_cache_items", "preview_cache_items"})


@dataclass(frozen=True)
class ImageResourcePolicy:
    """内存预算与并发额度。

    memory_bytes 是已登记用量的上限。memory_seat_bytes 是座位：合计用量在座位以内的请求直接放行，
    超出的还要核对当前可用内存，available_memory() 减去本次请求不得低于 memory_reserve_bytes，否则拒绝。
    座位缺省等于上限，即不核对。
    """

    memory_bytes: int = FALLBACK_LIMIT_BYTES
    memory_seat_bytes: int | None = None
    memory_reserve_bytes: int = 0
    available_memory: Callable[[], int | None] | None = None
    source_cache_bytes: int = SOURCE_CACHE_BYTES
    proxy_cache_bytes: int = PROXY_CACHE_BYTES
    display_cache_bytes: int = DISPLAY_CACHE_BYTES
    compute_slots: int = 2
    operator_threads: int | None = None
    source_cache_items: int = SOURCE_CACHE_ITEMS
    thumbnail_slots: int = 1
    thumbnail_pending: int = 96
    interactive_threads: int | None = None
    thread_capacity: int | None = None
    preview_cache_bytes: int = PREVIEW_CACHE_BYTES
    preview_cache_items: int = PREVIEW_CACHE_ITEMS

    def __post_init__(self):
        if self.memory_seat_bytes is None:
            object.__setattr__(self, "memory_seat_bytes", self.memory_bytes)
        for name in ("memory_bytes", "memory_seat_bytes", "memory_reserve_bytes",
                     "source_cache_bytes", "proxy_cache_bytes", "display_cache_bytes", "preview_cache_bytes",
                     "compute_slots", "source_cache_items", "preview_cache_items",
                     "thumbnail_slots", "thumbnail_pending"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError("资源预算必须为非负整数")
        if not self.memory_bytes or not self.compute_slots or not self.thumbnail_slots:
            raise ValueError("内存预算和执行槽必须大于零")
        if self.memory_seat_bytes > self.memory_bytes:
            raise ValueError("座位不能超过预算上限")
        if self.thread_capacity is None:
            object.__setattr__(self, "thread_capacity", available_logical_count())
        if type(self.thread_capacity) is not int or self.thread_capacity < 1:
            raise ValueError("线程总额度必须为正整数")
        if self.operator_threads is None:
            object.__setattr__(self, "operator_threads",
                               max(1, min(8, self.thread_capacity // self.compute_slots)))
        if (type(self.operator_threads) is not int
                or not 1 <= self.operator_threads <= self.thread_capacity):
            raise ValueError("内部计算线程数必须为总额度内的正整数")
        if self.interactive_threads is None:
            object.__setattr__(self, "interactive_threads", self.operator_threads)
        if type(self.interactive_threads) is not int or self.interactive_threads < 1:
            raise ValueError("前台计算线程数必须为正整数")


def system_resource_policy():
    """按本机的处理器与物理内存装配策略。内存按默认偏好解析；座位向系统登记之后才有，这里不给。"""
    logical = available_logical_count()
    physical = physical_core_count(logical)
    status = memory_status()
    budget = resolve(MemorySettings(), status.total_bytes if status is not None else None)
    return ImageResourcePolicy(thread_capacity=logical, interactive_threads=min(physical, 8),
                               thumbnail_slots=max(1, min(4, physical - 1)),
                               memory_bytes=budget.limit_bytes, memory_seat_bytes=0,
                               memory_reserve_bytes=budget.reserve_bytes, available_memory=available_memory)


@dataclass(frozen=True)
class ComputeLease:
    threads: int
    heavy: bool


@dataclass(frozen=True)
class ComputeRequestContext:
    threads: int


@dataclass(frozen=True)
class ComputeUsage:
    thread_capacity: int
    used_threads: int
    active: int
    background: int
    thumbnails: int
    foreground: int
    waiting: int


@dataclass(frozen=True, eq=False)
class _ComputeRequest:
    priority: int
    created: float
    role: str
    threads: int
    minimum: int

    @property
    def heavy(self):
        return self.role in ("operator", "foreground")

    @property
    def current_priority(self):
        return self.priority() if callable(self.priority) else self.priority


@dataclass
class _ComputeState:
    request: _ComputeRequest
    lease: ComputeLease
    active: bool = True
    foreground: bool = False


class ComputeBudget:
    def __init__(self, slots, thread_capacity=None, *, operator_threads=1):
        if type(slots) is not int or slots < 1:
            raise ValueError("计算槽数必须为正整数")
        if thread_capacity is None:
            thread_capacity = available_logical_count()
        if type(thread_capacity) is not int or thread_capacity < 1:
            raise ValueError("线程总额度必须为正整数")
        if type(operator_threads) is not int or not 1 <= operator_threads <= thread_capacity:
            raise ValueError("内部线程需求超过总额度")
        self._slots = slots
        self._thread_capacity = thread_capacity
        self._operator_threads = operator_threads
        self._condition = Condition()
        self._active = 0
        self._active_background = 0
        self._active_foreground = 0
        self._active_thumbnails = 0
        self._used_threads = 0
        self._waiting = []
        self._blocked = {}
        self._local = local()
        self._closed = False

    @contextmanager
    def borrow(self, is_cancelled=lambda: False, priority=0, *, role="operator", threads=None,
               minimum_threads=1, admit=None):
        if role not in ("operator", "foreground", "thumbnail", "quick_preview"):
            raise ValueError("计算任务角色无效")
        if threads is None:
            threads = self._operator_threads if role in ("operator", "foreground") else 1
        if type(threads) is not int or threads < 1:
            raise ValueError("线程需求必须为正整数")
        if role in ("thumbnail", "quick_preview") and threads != 1:
            raise ValueError("轻量任务仅领取一个线程额度")
        minimum = minimum_threads if role == "foreground" else threads
        if type(minimum) is not int or not 1 <= minimum <= min(threads, self._thread_capacity):
            raise ValueError("最小线程需求超过可用额度")
        ticket = _ComputeRequest(priority, time.monotonic(), role, threads, minimum)
        if is_cancelled():
            raise CancelledError()
        state = getattr(self._local, "state", None)
        if state is not None and state.active:
            with self._condition:
                if self._closed:
                    raise CancelledError()
            if minimum > state.lease.threads or ticket.heavy and not state.lease.heavy:
                raise ValueError("嵌套计算需求超过已领取额度")
            if admit is not None and not admit():
                raise RuntimeError("实例准入必须在归还外层算力后执行")
            yield state.lease
            return
        lease = self._acquire(ticket, is_cancelled, admit)
        state = _ComputeState(ticket, lease, foreground=self._local.foreground)
        self._local.state = state
        try:
            if is_cancelled():
                raise CancelledError()
            yield lease
        finally:
            self._local.state = None
            if state.active:
                self._release(ticket, state.lease, foreground=state.foreground)

    def _acquire(self, ticket, is_cancelled, admit=None):
        with self._condition:
            if self._closed:
                raise CancelledError()
            self._waiting.append(ticket)
        try:
            while True:
                if is_cancelled():
                    raise CancelledError()
                with self._condition:
                    waiting = tuple(self._waiting)
                priorities = {item: item.current_priority for item in waiting}
                with self._condition:
                    if self._closed:
                        raise CancelledError()
                    now = time.monotonic()
                    foreground = self._active_foreground or any(item.heavy and priorities.get(item, 1) <= 0
                                                               for item in self._waiting)
                    candidates = [item for item in self._waiting
                                  if item in priorities and self._blocked.get(item, 0) <= now
                                  and (not item.heavy or priorities[item] <= 0 or not self._active_background)
                                  and (item.role != "thumbnail" or not foreground or not self._active_thumbnails)]
                    first = min(candidates, key=lambda item: queue_order(priorities[item], item.created, now)) if candidates else None
                    available = self._thread_capacity - self._used_threads
                    if (first is ticket and available >= ticket.minimum
                            and (not ticket.heavy or self._active < self._slots)):
                        granted = min(ticket.threads, available) if ticket.role == "foreground" else ticket.threads
                        self._waiting.remove(ticket)
                        self._used_threads += granted
                        self._active += ticket.heavy
                        granted_foreground = ticket.heavy and priorities[ticket] <= 0
                        self._active_background += ticket.heavy and not granted_foreground
                        self._active_foreground += granted_foreground
                        self._active_thumbnails += ticket.role == "thumbnail"
                        active = self._active
                        used = self._used_threads
                    else:
                        self._condition.wait(0.05)
                        continue
                lease = ComputeLease(granted, ticket.heavy)
                try:
                    accepted = admit is None or admit()
                except BaseException:
                    self._release(ticket, lease, foreground=granted_foreground)
                    raise
                if accepted:
                    self._local.foreground = granted_foreground
                    logger.log(TRACE, "计算预算准入 role=%s priority=%s threads=%s used=%s capacity=%s wait_ms=%.1f active=%s slots=%s",
                               ticket.role, priorities[ticket], granted, used, self._thread_capacity,
                               (time.monotonic() - ticket.created) * 1000, active, self._slots)
                    return lease
                self._release(ticket, lease, foreground=granted_foreground)
                with self._condition:
                    self._waiting.append(ticket)
                    self._blocked[ticket] = time.monotonic() + 0.05
        finally:
            with self._condition:
                self._blocked.pop(ticket, None)
                if ticket in self._waiting:
                    self._waiting.remove(ticket)
                self._condition.notify_all()

    def _release(self, ticket, lease, *, foreground):
        with self._condition:
            self._used_threads -= lease.threads
            self._active -= ticket.heavy
            self._active_background -= ticket.heavy and not foreground
            self._active_foreground -= foreground
            self._active_thumbnails -= ticket.role == "thumbnail"
            active, used = self._active, self._used_threads
            self._condition.notify_all()
        logger.log(TRACE, "计算预算释放 role=%s threads=%s active=%s used=%s", ticket.role, lease.threads, active, used)

    def scheduling(self):
        state = getattr(self._local, "state", None)
        return ((state.request.priority, state.request.role) if state is not None
                else getattr(self._local, "scheduling", (0, "operator")))

    @contextmanager
    def request_context(self, is_cancelled=lambda: False, priority=0, *, role="operator", threads=None,
                        minimum_threads=1):
        if is_cancelled():
            raise CancelledError()
        previous = getattr(self._local, "scheduling", (0, "operator"))
        self._local.scheduling = priority, role
        try:
            yield ComputeRequestContext(threads or self._operator_threads)
        finally:
            self._local.scheduling = previous

    @contextmanager
    def suspend(self, is_cancelled=lambda: False):
        state = getattr(self._local, "state", None)
        if state is None or not state.active:
            yield
            return
        self._release(state.request, state.lease, foreground=state.foreground)
        state.active = False
        previous = getattr(self._local, "scheduling", (0, "operator"))
        self._local.scheduling = state.request.priority, state.request.role
        self._local.state = None
        try:
            yield
        finally:
            try:
                if not is_cancelled() and not self._closed:
                    restore = replace(state.request, threads=state.lease.threads, minimum=state.lease.threads)
                    state.lease = self._acquire(restore, is_cancelled)
                    state.foreground = self._local.foreground
                    state.active = True
                    self._local.state = state
            finally:
                self._local.scheduling = previous

    def snapshot(self):
        with self._condition:
            return ComputeUsage(self._thread_capacity, self._used_threads, self._active,
                                self._active_background, self._active_thumbnails,
                                self._active_foreground, len(self._waiting))

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()


@dataclass
class _Entry:
    category: str
    size: int
    done: Event = field(default_factory=Event)
    subscribers: dict = field(default_factory=dict)
    value: object = None
    error: BaseException | None = None
    borrowers: int = 0
    owner: int = field(default_factory=get_ident)
    buffers: dict = field(default_factory=dict)
    retain: bool = True


class ImageLease:
    def __init__(self, pool, key, entry):
        self._pool, self._key, self._entry = pool, key, entry

    @property
    def value(self):
        if self._entry is None:
            raise RuntimeError("图像借用已释放")
        return self._entry.value

    def close(self):
        if self._entry is not None:
            entry, self._entry = self._entry, None
            self._pool.release(self._key, entry)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class MemoryHold:
    def __init__(self, pool, size, owner=None):
        self._pool = pool
        self._size = size
        self._lock = RLock()
        self._owner = owner

    def close(self):
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.release_memory(self._size, owner=self._owner)

    def split(self, size):
        with self._lock:
            pool = self._pool
            if pool is None:
                raise RuntimeError("内存预约已释放")
            size = min(size, self._size)
            with pool._condition:
                self._size -= size
                if self._owner is not None:
                    pool._working_owners[self._owner] -= size
                    if not pool._working_owners[self._owner]:
                        pool._working_owners.pop(self._owner)
                if not self._size:
                    self._owner = None
            return MemoryHold(pool, size)

    def consume(self, pool):
        with self._lock:
            if self._pool is not pool:
                return 0
            self._pool = None
            pool._reserved -= self._size
            if self._owner is not None:
                pool._working_owners[self._owner] -= self._size
                if not pool._working_owners[self._owner]:
                    pool._working_owners.pop(self._owner)
            return self._size

    def __del__(self):
        self.close()


class ImageResourcePool:
    """同一缓冲区只记账一次；在途订阅和借用均阻止淘汰。"""

    def __init__(self, policy):
        self.policy = policy
        self._lock = RLock()
        self._entries = OrderedDict()
        self._closed = False
        self._reserved = 0
        self._working_owners = {}
        self._condition = Condition(self._lock)
        self._limits = self._retention(policy)

    @staticmethod
    def _retention(policy):
        """各分类空闲缓存的留存额度：字节数与条数，条数为 None 表示不限。"""
        return {"source": (policy.source_cache_bytes, policy.source_cache_items),
                "proxy": (policy.proxy_cache_bytes, None),
                "display": (policy.display_cache_bytes, None),
                "preview": (policy.preview_cache_bytes, policy.preview_cache_items),
                "intermediate": (min(2 * 1024 ** 3, policy.memory_bytes // 3), 48)}

    def retention_limit(self, category):
        with self._lock:
            return self._limits[category][0]

    def _costs(self):
        sizes = dict.fromkeys(self._limits, 0)
        counts = dict.fromkeys(self._limits, 0)
        known = set()
        for entry in self._entries.values():
            counts[entry.category] += 1
            cost = max(0, entry.size - sum(entry.buffers.values()))
            for identity, size in entry.buffers.items():
                if identity not in known:
                    cost += size
                    known.add(identity)
            sizes[entry.category] += cost
        return sizes, counts

    def update_memory(self, **changes):
        """运行中替换内存策略的字段。已持有的用量不追溯：让出超出新留存与新上限的空闲缓存，后续新增申请按新策略准入。"""
        if not changes.keys() <= MEMORY_FIELDS:
            raise ValueError("只能替换内存策略的字段")
        with self._condition:
            self.policy = replace(self.policy, **changes)
            self._limits = self._retention(self.policy)
            self._trim()
            self._condition.notify_all()

    def _remove(self, key):
        entry = self._entries.pop(key)
        close = getattr(entry.value, "close", None)
        if close:
            close()

    def _admit(self, required):
        """新增用量的准入；估算为零的新申请同样要过。释放与收尾只调用 _trim()，不会因为预算而失败。"""
        total = self._trim(required)
        if total + required > self.policy.memory_bytes:
            raise MemoryError(MEMORY_BUDGET_KEY)
        if required and not self._room(total, required):
            # 座位以外、可用内存又不够时，先让出全部可淘汰的缓存，再看一次
            if not self._room(self._trim(required, everything=True), required):
                raise MemoryError(MEMORY_AVAILABLE_KEY)

    def _room(self, total, required):
        """合计用量在座位以内直接放行；以外要求本次请求之后系统的可用内存仍不低于保留量，读不到可用内存时不拦。"""
        if total + required <= self.policy.memory_seat_bytes:
            return True
        probe = self.policy.available_memory
        available = probe() if probe is not None else None
        return available is None or available - required >= self.policy.memory_reserve_bytes

    def _trim(self, required=0, *, everything=False):
        """淘汰空闲的缓存并返回剩余的已登记用量；everything 为真时空闲的全部让出。"""
        sizes, counts = self._costs()
        total = sum(sizes.values()) + self._reserved
        for key, entry in list(self._entries.items()):
            if not entry.done.is_set() or entry.borrowers or entry.subscribers:
                continue
            size_limit, count_limit = self._limits[entry.category]
            if (everything or self._closed or entry.error is not None or not entry.retain
                    or total + required > self.policy.memory_bytes
                    or sizes[entry.category] > size_limit
                    or count_limit is not None and counts[entry.category] > count_limit):
                self._remove(key)
                sizes, counts = self._costs()
                total = sum(sizes.values()) + self._reserved
        return total

    def _can_wait(self):
        owner = get_ident()
        if self._working_owners.get(owner, 0) or any(
                not entry.done.is_set() and entry.size > 0 and entry.owner == owner
                for entry in self._entries.values()):
            return False
        return (any(not entry.done.is_set() and entry.size > 0 and entry.owner != owner
                    for entry in self._entries.values())
                or any(thread != owner for thread in self._working_owners))

    def acquire(self, key, category, estimate, loader, size_of, is_cancelled=lambda: False):
        if is_cancelled():
            raise CancelledError()
        token = object()
        while True:
            if is_cancelled():
                raise CancelledError()
            if callable(estimate):
                with self._lock:
                    candidate = self._entries.get(key)
                    needs_estimate = candidate is None or isinstance(candidate.error, CancelledError)
                if needs_estimate:
                    estimate = estimate()
            with self._condition:
                if self._closed:
                    raise CancelledError()
                entry = self._entries.get(key)
                if entry is not None and entry.done.is_set() and isinstance(entry.error, CancelledError):
                    self._entries.pop(key)
                    entry = None
                leader = entry is None
                if leader:
                    if callable(estimate):
                        continue
                    try:
                        self._admit(estimate)
                    except MemoryError:
                        if estimate > self.policy.memory_bytes or not self._can_wait():
                            raise
                        self._condition.wait(0.05)
                        continue
                    entry = _Entry(category, estimate)
                    self._entries[key] = entry
                self._entries.move_to_end(key)
                entry.subscribers[token] = is_cancelled
                break

        def abandoned():
            with self._lock:
                callbacks = tuple(entry.subscribers.values())
                closed = self._closed
            return closed or not callbacks or all(callback() for callback in callbacks)

        try:
            if leader:
                started = time.monotonic()
                try:
                    value = loader(abandoned)
                    size = size_of(value)
                    with self._lock:
                        from ..core.execution_memo import buffers
                        entry.value, entry.size, entry.buffers = value, size, buffers(value)
                        self._admit(0)
                except BaseException as error:
                    close = getattr(locals().get("value"), "close", None)
                    if close:
                        close()
                    entry.error, entry.size = error, 0
                    entry.value, entry.buffers = None, {}
                finally:
                    entry.done.set()
                    with self._condition:
                        self._condition.notify_all()
                # 失败的照常写进日志，成功的每帧都有，只在 TRACE 下写
                logger.log(logging.DEBUG if entry.error is not None else TRACE,
                           "图像资源 category=%s source=%s duration_ms=%.1f bytes=%s failed=%s",
                           category, getattr(key[0], "file_hash", "")[:12],
                           (time.monotonic() - started) * 1000, entry.size, entry.error is not None)
            else:
                logger.log(TRACE, "图像资源复用 category=%s source=%s ready=%s", category,
                           getattr(key[0], "file_hash", "")[:12], entry.done.is_set())
                while not entry.done.wait(0.05):
                    if is_cancelled():
                        raise CancelledError()
            if is_cancelled() or self._closed:
                raise CancelledError()
            if entry.error is not None:
                raise entry.error
            with self._lock:
                entry.borrowers += 1
            return ImageLease(self, key, entry)
        finally:
            with self._lock:
                entry.subscribers.pop(token, None)
                self._trim()

    def borrow_cached(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if self._closed or entry is None or not entry.done.is_set() or entry.error is not None:
                return None
            entry.borrowers += 1
            self._entries.move_to_end(key)
            return ImageLease(self, key, entry)

    def store(self, key, category, value, size):
        """登记一个已经算好的缓存项。准入不过时不登记、不等待，返回是否登记；同一个键正在使用时保留原有的。"""
        with self._condition:
            if self._closed:
                return False
            entry = self._entries.get(key)
            if entry is not None:
                if not entry.done.is_set() or entry.borrowers or entry.subscribers:
                    self._entries.move_to_end(key)
                    return True
                self._remove(key)
            try:
                self._admit(size)
            except MemoryError:
                return False
            entry = self._entries[key] = _Entry(category, size, value=value)
            from ..core.execution_memo import buffers
            entry.buffers = buffers(value)
            entry.done.set()
            self._trim()
            return key in self._entries

    def additional_bytes(self, value):
        from ..core.execution_memo import buffers
        candidate = buffers(value)
        with self._lock:
            known = {identity for entry in self._entries.values() for identity in entry.buffers}
            return sum(size for identity, size in candidate.items() if identity not in known)

    def publish_shared(self, key, value, *, retain=True):
        from ..core.execution_memo import buffers, freeze
        owned = getattr(value, "execution_ownership", None)
        stored = replace(value, execution_ownership=None) if hasattr(value, "execution_ownership") else value
        storage = buffers(stored)
        size = sum(storage.values())
        if not size:
            size = getattr(stored, "nbytes", 4096)
        with self._condition:
            if self._closed:
                raise CancelledError()
            existing = self._entries.get(key)
            if existing is not None and existing.done.is_set() and existing.error is None:
                existing.borrowers += 1
                self._entries.move_to_end(key)
                return ImageLease(self, key, existing)
            if owned is not None:
                owned.release_credit(self)
            entry = _Entry("intermediate", size, value=freeze(stored), borrowers=1, buffers=storage, retain=retain)
            entry.done.set()
            self._entries[key] = entry
            try:
                self._admit(0)
            except BaseException:
                self._entries.pop(key)
                raise
            self._condition.notify_all()
            return ImageLease(self, key, entry)

    def contains(self, key):
        with self._lock:
            entry = self._entries.get(key)
            return not self._closed and entry is not None and entry.done.is_set() and entry.error is None

    def discard(self, key, *, value=None):
        """淘汰指定条目，已有借用释放后回收。"""
        with self._condition:
            entry = self._entries.get(key)
            if entry is None or (value is not None and entry.value is not value):
                return
            entry.retain = False
            if entry.done.is_set() and not entry.borrowers and not entry.subscribers:
                self._remove(key)
            self._condition.notify_all()

    def try_hold_memory(self, size):
        if type(size) is not int or size < 0:
            raise ValueError("内存持有量必须为非负整数")
        with self._condition:
            if self._closed:
                raise CancelledError()
            self._admit(size)
            self._reserved += size
            return MemoryHold(self, size)

    def try_working_memory(self, size, *, retained=False):
        if type(size) is not int or size < 0:
            raise ValueError("工作内存必须为非负整数")
        owner = None if retained else get_ident()
        with self._condition:
            if self._closed:
                raise CancelledError()
            try:
                self._admit(size)
            except MemoryError:
                if size > self.policy.memory_bytes or not self._can_wait():
                    raise
                return None
            self._reserved += size
            if owner is not None:
                self._working_owners[owner] = self._working_owners.get(owner, 0) + size
            return MemoryHold(self, size, owner)

    def hold_memory(self, size, is_cancelled=lambda: False):
        if type(size) is not int or size < 0:
            raise ValueError("内存持有量必须为非负整数")
        while True:
            if is_cancelled():
                raise CancelledError()
            with self._condition:
                if self._closed:
                    raise CancelledError()
                try:
                    self._admit(size)
                except MemoryError:
                    if size > self.policy.memory_bytes or not self._can_wait():
                        raise
                    self._condition.wait(0.05)
                    continue
                self._reserved += size
                return MemoryHold(self, size)

    def release_memory(self, size, *, owner=None):
        with self._condition:
            self._reserved -= size
            if owner is not None:
                self._working_owners[owner] -= size
                if not self._working_owners[owner]:
                    self._working_owners.pop(owner)
            self._trim()
            self._condition.notify_all()

    @contextmanager
    def working_memory(self, size, is_cancelled=lambda: False, *, fallback_sizes=()):
        owner = get_ident()
        sizes = (size, *fallback_sizes)
        while True:
            if is_cancelled():
                raise CancelledError()
            with self._condition:
                if self._closed:
                    raise CancelledError()
                reason = MEMORY_BUDGET_KEY
                for candidate in sizes:
                    try:
                        self._admit(candidate)
                    except MemoryError as error:
                        reason = str(error)
                        continue
                    size = candidate
                    break
                else:
                    if sizes[-1] > self.policy.memory_bytes or not self._can_wait():
                        raise MemoryError(reason)
                    self._condition.wait(0.05)
                    continue
                self._reserved += size
                self._working_owners[owner] = self._working_owners.get(owner, 0) + size
                break
        try:
            yield size
        finally:
            with self._condition:
                self._reserved -= size
                self._working_owners[owner] -= size
                if not self._working_owners[owner]:
                    self._working_owners.pop(owner)
                self._trim()
                self._condition.notify_all()

    def release(self, key, entry):
        with self._lock:
            entry.borrowers -= 1
            self._trim()
            self._condition.notify_all()
            if logger.isEnabledFor(TRACE):
                logger.log(TRACE, "图像资源释放 %s", self.usage())

    def close(self):
        with self._lock:
            self._closed = True
            self._trim()

    def usage(self):
        with self._lock:
            return {"bytes": sum(self._costs()[0].values()),
                    "reserved_bytes": self._reserved,
                    "borrowers": sum(entry.borrowers for entry in self._entries.values()),
                    "entries": len(self._entries)}
