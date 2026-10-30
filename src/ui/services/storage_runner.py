from concurrent.futures import ThreadPoolExecutor
from threading import Event

from PySide6.QtCore import QObject, Qt, Signal, Slot


class StorageRunner(QObject):
    finished = Signal(str, object, str)
    busy_changed = Signal(bool)
    limits_changed = Signal()
    _completed = Signal(str, object, str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.busy = False
        self._closed = False
        self._cancelled = Event()
        self.service.cancelled = self._cancelled.is_set
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-storage")
        self._operation = ""
        self._pending_limits = {}
        self._writing_limits = {}
        self._limits_failed = False
        self._flush_callback = None
        self._discard_callback = None
        self._discarding_limits = False
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def submit(self, name, operation):
        if self._closed or self.busy:
            return
        self._cancelled.clear()
        self.busy = True
        self._operation = name
        self.busy_changed.emit(True)
        self._pool.submit(self._run, name, operation)

    def _run(self, name, operation):
        result, error = None, ""
        try:
            result = operation()
        except InterruptedError:
            error = "storage.cancelled"
        except Exception as exc:
            error = str(exc)
        try:
            self._completed.emit(name, result, error)
        except RuntimeError:
            pass

    @Slot(str, object, str)
    def _deliver(self, name, result, error):
        self.busy = False
        if not self._closed:
            if name == "limit":
                written, self._writing_limits = self._writing_limits, {}
                if error and not self._discarding_limits:
                    for key, value in written.items():
                        self._pending_limits.setdefault(key, value)
                    self._limits_failed = True
                self.limits_changed.emit()
            self.busy_changed.emit(self.busy)
            self.finished.emit(name, result, error)
            if self._discarding_limits:
                self._finish_discard()
                return
            if name == "limit" and error:
                self._finish_flush(False)
            else:
                self._start_limits()
                if not self.has_pending_limits:
                    self._finish_flush(True)

    def refresh(self):
        self.submit("scan", lambda: self.service.scan(self._cancelled.is_set))

    def cancel(self):
        self._cancelled.set()

    @property
    def cancellable(self):
        return self.busy and self._operation not in ("schedule", "cancel_jobs", "register", "forget", "limit")

    def persist_limits(self, values):
        if self._closed or self._discarding_limits:
            return
        self._pending_limits.update(values)
        self._limits_failed = False
        self._start_limits()
        self.limits_changed.emit()

    @property
    def has_pending_limits(self):
        return bool(self._pending_limits or self._writing_limits)

    @property
    def limits_failed(self):
        return self._limits_failed

    @property
    def saving_limits(self):
        return self.busy and self._operation == "limit"

    @property
    def discarding_limits(self):
        return self._discarding_limits

    def _start_limits(self):
        if self._closed or self.busy or self._limits_failed or self._discarding_limits or not self._pending_limits:
            return
        from ...application.storage_paths import update_settings
        values, self._pending_limits = self._pending_limits, {}
        self._writing_limits = values
        self.submit("limit", lambda: update_settings(lambda settings: settings["limits"].update(values),
                                                     is_cancelled=self._cancelled.is_set, timeout=5.0))

    def flush_limits(self, callback):
        self._flush_callback = callback
        self._limits_failed = False
        if self.cancellable:
            self.cancel()
        self._start_limits()
        if not self.has_pending_limits:
            self._finish_flush(True)

    def _finish_flush(self, success):
        callback, self._flush_callback = self._flush_callback, None
        if callback is not None:
            callback(success)

    def discard_limits(self, callback):
        if self._closed or self._discarding_limits:
            return
        self._pending_limits.clear()
        self._flush_callback = None
        self._discard_callback = callback
        self._discarding_limits = True
        self._limits_failed = False
        if self.busy and self._operation == "limit":
            self.cancel()
        else:
            self._finish_discard()
        self.limits_changed.emit()

    def _finish_discard(self):
        self._discarding_limits = False
        self.limits_changed.emit()
        callback, self._discard_callback = self._discard_callback, None
        if callback is not None:
            callback()

    def shutdown(self):
        self._closed = True
        self._flush_callback = None
        self._discard_callback = None
        self.cancel()
        self._pool.shutdown(wait=self.busy and self._operation in ("schedule", "cancel_jobs", "register", "forget"), cancel_futures=False)
