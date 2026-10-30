from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import logging

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from ...application.draft_persistence import DraftSaveRequest
from ...application.result_types import CoreResult, ErrorCode

logger = logging.getLogger(__name__)


class DraftPersistenceRunner(QObject):
    ready = Signal()
    failed = Signal(object)
    saved = Signal()
    persisted = Signal(int)
    _completed = Signal(object, object)

    def __init__(self, service, state_store, parent=None):
        super().__init__(parent)
        self._service = service
        self._store = state_store.drafts
        self._loaded = False
        self.load_error = None
        self._closed = False
        self._dirty_paths = set()
        self._baseline = {}
        self._saved_version = self._store.version
        self._active = None
        self._pending = None
        self._flush_callback = None
        self._version_waiters = []
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-drafts")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._submit_latest)
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        state_store.stash_changed.connect(self._changed)
        self._start("load", None)

    @property
    def loaded(self):
        return self._loaded

    def _changed(self, path):
        if self._closed:
            return
        if not self._loaded:
            self._dirty_paths.add(path)
        else:
            self._timer.start()

    def _start(self, kind, request):
        if kind == "load":
            self.load_error = None
        else:
            request = replace(request, baseline=self._baseline)
        self._active = (kind, request)
        self._pool.submit(self._run, kind, request)

    def _run(self, kind, request):
        try:
            result = self._service.load() if kind == "load" else self._service.save(request)
        except Exception as error:
            logger.exception("草稿任务失败")
            result = CoreResult.from_exception(error, message_key="studio.draft_save_failed")
        try:
            self._completed.emit((kind, request), result)
        except RuntimeError:
            logger.debug("草稿接收方已关闭")

    def _submit_latest(self):
        if self._closed or not self._loaded or self._store.version == self._saved_version:
            return
        request = DraftSaveRequest(self._store.version, self._store.snapshot(), self._baseline)
        if (self._active is not None and self._active[0] == "save"
                and self._active[1].version == request.version):
            return
        if self._active is None:
            self._start("save", request)
        else:
            self._pending = request

    @Slot(object, object)
    def _deliver(self, task, result):
        self._active = None
        if self._closed:
            return
        kind, request = task
        if result.success:
            if kind == "load":
                self._baseline = result.data
                current = self._store.snapshot()
                merged = {path: params for path, params in result.data.items()
                          if path not in self._dirty_paths}
                merged.update(current)
                dirty = bool(self._dirty_paths)
                self._store.restore(merged)
                self._dirty_paths.clear()
                self._loaded = True
                self._saved_version = -1 if dirty else self._store.version
                self._deliver_versions()
                self.ready.emit()
                if dirty:
                    self._timer.start()
            else:
                self._baseline = request.drafts
                self._saved_version = request.version
                self.persisted.emit(request.version)
                self._deliver_versions()
                if self._saved_version == self._store.version:
                    self.saved.emit()
        else:
            if kind == "load":
                self.load_error = result.error
            self._pending = None
            self._timer.stop()
            self.failed.emit(result.error)
            self._deliver_versions(result)
            self._finish_flush(result)
            return
        pending, self._pending = self._pending, None
        if pending is not None:
            self._start("save", pending)
        elif self._flush_callback is not None:
            if self._saved_version == self._store.version:
                self._finish_flush(CoreResult.ok())
            else:
                self._submit_latest()

    def _deliver_versions(self, error=None):
        remaining = []
        for version, callback in self._version_waiters:
            if error is not None or self._loaded and self._saved_version >= version:
                result = error if error is not None else CoreResult.ok(self._saved_version)
                QTimer.singleShot(0, self, lambda callback=callback, result=result: callback(result))
            else:
                remaining.append((version, callback))
        self._version_waiters = remaining

    def wait_for_version(self, version, callback):
        if self._closed:
            callback(CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled"))
            return
        self._version_waiters.append((version, callback))
        self._deliver_versions()
        if self._version_waiters:
            self.retry()

    def _finish_flush(self, result):
        callback, self._flush_callback = self._flush_callback, None
        if callback is not None:
            QTimer.singleShot(0, self, lambda: callback(result))

    def retry(self):
        if self._closed:
            return
        self._timer.stop()
        if self._loaded:
            self._submit_latest()
        elif self._active is None:
            self._start("load", None)

    def flush(self, callback):
        self._flush_callback = callback
        self._timer.stop()
        if self._loaded and self._saved_version == self._store.version:
            self._finish_flush(CoreResult.ok())
        else:
            self.retry()

    def shutdown(self):
        self._closed = True
        self._deliver_versions(CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled"))
        self._timer.stop()
        self._pending = None
        self._flush_callback = None
        self._pool.shutdown(wait=False, cancel_futures=True)
