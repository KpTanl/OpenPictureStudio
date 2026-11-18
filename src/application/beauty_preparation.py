from contextlib import contextmanager
from threading import Lock
from ..core.contracts.execution import current_execution
from .shared_stages import SharedStageStore


class BeautyPreparationService:
    def __init__(self, resources=None, compute=None, *, store=None):
        self._owns_store = store is None
        self._owns_resources = store is None and resources is None
        self._owns_compute = store is None and compute is None
        if store is None:
            from .image_resources import ComputeBudget, ImageResourcePool, ImageResourcePolicy
            compute = compute or ComputeBudget(2)
            resources = resources or ImageResourcePool(ImageResourcePolicy())
            store = SharedStageStore(resources, compute)
        self._store = store
        self._compute = store.compute
        self._links = {}
        self._lock = Lock()

    def _key(self, key):
        return self._store.key('preparation', key)

    def ready(self, key):
        return self._store.ready(self._key(key))

    def link(self, alias, key):
        """分析按 alias 取准备结果时，若渲染按 key 算好的还在缓存里，直接用它。"""
        with self._lock:
            self._links.pop(alias, None)
            self._links[alias] = key
            if len(self._links) > 64:
                del self._links[next(iter(self._links))]

    @contextmanager
    def borrow(self, key, loader, cancelled):
        execution = current_execution.get()
        priority = execution.priority if execution is not None else self._compute.scheduling()[0]
        priority = getattr(cancelled, "priority", priority)
        with self._lock:
            linked = self._links.get(key)
        with self._compute.suspend(cancelled):
            lease = ((self._store.cached(self._key(linked), cancelled) if linked is not None else None)
                     or self._store.acquire(self._key(key), loader, cancelled, priority))
            try:
                yield lease.value
            finally:
                lease.close()

    def close(self):
        if self._owns_store:
            self._store.close()
        if self._owns_resources:
            self._store.resources.close()
        if self._owns_compute:
            self._compute.close()
