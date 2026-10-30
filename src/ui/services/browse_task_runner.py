from concurrent.futures import ThreadPoolExecutor
import logging
from threading import Event, Lock

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ...application.browse_service import BrowseRequest

logger = logging.getLogger(__name__)


class BrowseTaskRunner(QObject):
    finished = Signal(object, object)
    busy_changed = Signal(bool)
    _completed = Signal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self._service = service
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-browse")
        self._active = None
        self._pending = None
        self._generation = 0
        self._lock = Lock()
        self._outcome = None
        self._closed = Event()
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    @property
    def is_busy(self):
        return self._active is not None or self._pending is not None

    def submit(self, *, list_version, selection_revision, paths=(), directory=None,
               append=True, target=None, origin="files"):
        if self._closed.is_set():
            return None
        self.cancel()
        request = BrowseRequest(self._generation, list_version, selection_revision,
                                tuple(paths), directory, append, target, origin)
        if self._active is None:
            self._active = request
            self._pool.submit(self._run, request)
        else:
            self._pending = request
        self.busy_changed.emit(True)
        return request

    def cancel(self):
        self._generation += 1
        if self._active is not None:
            self._active.cancellation.set()
        if self._pending is not None:
            self._pending.cancellation.set()
        self._pending = None

    def _run(self, request):
        result = self._service.prepare(request)
        logger.debug("浏览列表 request=%s origin=%s count=%s elapsed_ms=%s cancelled=%s success=%s",
                     request.request_id, request.origin, len(result.data.paths) if result.success else 0,
                     result.data.elapsed_ms if result.success else None,
                     request.cancellation.is_set(), result.success)
        with self._lock:
            if self._closed.is_set():
                return
            self._outcome = (request, result)
        try:
            self._completed.emit()
        except RuntimeError:
            self.shutdown()

    @Slot()
    def _deliver(self):
        with self._lock:
            outcome, self._outcome = self._outcome, None
        if outcome is None:
            return
        request, result = outcome
        try:
            if (not self._closed.is_set() and not request.cancellation.is_set()
                    and request.request_id == self._generation):
                self.finished.emit(request, result)
        finally:
            self._active = self._pending
            self._pending = None
            if self._active is not None and not self._closed.is_set():
                self._pool.submit(self._run, self._active)
            self.busy_changed.emit(self.is_busy)

    def shutdown(self):
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            self._outcome = None
        self.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)
