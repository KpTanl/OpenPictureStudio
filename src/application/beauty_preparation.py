from contextlib import contextmanager
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

    def _key(self, key):
        return self._store.key('preparation', key)

    def ready(self, key):
        return self._store.ready(self._key(key))

    @contextmanager
    def borrow_cached(self, key, cancelled):
        lease = self._store.cached(self._key(key), cancelled)
        try:
            yield lease.value if lease is not None else None
        finally:
            if lease is not None:
                lease.close()

    @contextmanager
    def borrow(self, key, loader, cancelled):
        execution = current_execution.get()
        priority = execution.priority if execution is not None else self._compute.scheduling()[0]
        priority = getattr(cancelled, "priority", priority)
        with self._compute.suspend(cancelled):
            lease = self._store.acquire(self._key(key), loader, cancelled, priority)
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
