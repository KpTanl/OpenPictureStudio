import logging
from threading import Event

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot

logger = logging.getLogger(__name__)


class _CacheSignals(QObject):
    finished = Signal(object, object)


class _CacheWorker(QRunnable):
    def __init__(self, operation):
        super().__init__()
        self._operation = operation
        self.signals = _CacheSignals()
        self.completed = Event()

    def run(self):
        result, error = None, None
        try:
            result = self._operation()
        except Exception as exc:
            error = exc
            logger.exception("缓存维护失败")
        try:
            self.signals.finished.emit(result, error)
        except RuntimeError:
            logger.debug("缓存维护接收方已释放")
        finally:
            self.completed.set()


class CacheMaintenanceRunner(QObject):
    state_changed = Signal()
    clear_finished = Signal(int, str)
    maintenance_failed = Signal(str)

    def __init__(self, cache, version, parent=None, *, message_keys):
        super().__init__(parent)
        self._cache = cache
        self._version = version
        self._message_keys = dict(message_keys)
        self._worker = None
        self._query_worker = None
        self._status = None
        self._operation_name = ""
        self._pending_limit = None
        self._revision = 0
        self._closed = False

    @property
    def is_busy(self):
        return self._worker is not None

    @property
    def can_clear(self):
        return not self._closed and not self.is_busy and self._cache.is_writer

    @property
    def status_snapshot(self):
        return self._status

    @property
    def directory(self):
        return str(self._cache.directory)

    @property
    def limit_mib(self):
        return self._cache.max_bytes // 1024 ** 2

    def _start(self, name, operation):
        if self._closed or self.is_busy:
            return
        self._revision += 1
        self._operation_name = name
        self._worker = _CacheWorker(lambda: (operation(), self._cache.status()))
        self._worker.signals.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self.state_changed.emit()
        QThreadPool.globalInstance().start(self._worker)

    def initialize(self):
        self._start("initialize", lambda: self._cache.prepare(self._version))

    def query_status(self):
        if self._closed or self._query_worker is not None:
            return
        revision = self._revision
        self._query_worker = _CacheWorker(lambda: (revision, self._cache.status()))
        self._query_worker.signals.finished.connect(self._query_finished, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(self._query_worker)

    def set_limit(self, max_bytes):
        if self._closed:
            return
        if self.is_busy:
            self._pending_limit = max_bytes
            return
        self._start("limit", lambda: self._cache.set_limit(max_bytes))

    def clear(self):
        if not self.can_clear:
            return
        self._start("clear", self._cache.clear)

    @Slot(object, object)
    def _finished(self, result, error):
        self._worker = None
        if self._closed:
            return
        name, self._operation_name = self._operation_name, ""
        self._revision += 1
        value = None
        if error is None:
            value, self._status = result
        self.state_changed.emit()
        if name == "clear":
            self.clear_finished.emit(value or 0, self._message_keys[name] if error is not None else "")
        elif error is not None:
            self.maintenance_failed.emit(self._message_keys[name])
        if self._pending_limit is not None:
            limit, self._pending_limit = self._pending_limit, None
            self.set_limit(limit)

    @Slot(object, object)
    def _query_finished(self, result, error):
        self._query_worker = None
        if self._closed:
            return
        if error is not None:
            self.maintenance_failed.emit(self._message_keys["query"])
            return
        revision, status = result
        if revision != self._revision:
            self.query_status()
        elif status != self._status:
            self._status = status
            self.state_changed.emit()

    def shutdown(self):
        self._closed = True
        self._pending_limit = None

    def wait_for_idle(self):
        for worker in (self._worker, self._query_worker):
            if worker is not None:
                worker.completed.wait()
