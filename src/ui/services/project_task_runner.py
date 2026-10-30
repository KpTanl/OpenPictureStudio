from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ...application.project_requests import ProjectRequest
from ...application.result_types import CoreResult, ErrorCode

logger = logging.getLogger(__name__)


class ProjectTaskRunner(QObject):
    finished = Signal(object, object)
    _completed = Signal()

    def __init__(self, core, parent=None):
        super().__init__(parent)
        self._core = core
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-project")
        self._active = None
        self._pending = None
        self._generation = 0
        self._sequence = 0
        self._closed = Event()
        self._lock = Lock()
        self._result = None
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def submit(self, purpose, path, *, snapshot=None, session_id=None, source_id=None,
               state_version=None, source_path=None):
        if self._closed.is_set():
            return None
        self.cancel()
        with self._lock:
            if self._closed.is_set():
                return None
            self._sequence += 1
            request = ProjectRequest(self._sequence, self._generation, purpose, path,
                                     session_id, source_id, state_version, source_path, snapshot)
            start = self._active is None
            if start:
                self._active = request
                self._pool.submit(self._run, request)
            else:
                self._pending = request
        return request

    def cancel(self):
        with self._lock:
            self._generation += 1
            if self._active is not None:
                self._active.cancellation.cancel()
            request, self._pending = self._pending, None
        if request is not None:
            request.cancellation.cancel()
            self._log_terminal(request, CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled"))

    @staticmethod
    def _log_terminal(request, result):
        logger.debug("项目任务：task_id=%s source_id=%s session_id=%s state_version=%s generation=%s "
                     "purpose=%s stage=finished elapsed_ms=%.1f result=%s",
                     request.request_id, request.source_id, request.session_id, request.state_version,
                     request.generation, request.purpose, (monotonic() - request.queued_at) * 1000,
                     "success" if result.success else result.error.code)

    def _discard_open(self, request, result):
        if request.purpose == "open" and result.success:
            self._core.close_session(result.data.session_id)

    def _run(self, request):
        started = monotonic()
        logger.debug("项目任务：task_id=%s source_id=%s session_id=%s state_version=%s generation=%s "
                     "purpose=%s stage=started wait_ms=%.1f",
                     request.request_id, request.source_id, request.session_id, request.state_version,
                     request.generation, request.purpose, (started - request.queued_at) * 1000)
        try:
            if request.cancellation.is_cancelled() or self._closed.is_set():
                result = CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            elif request.purpose == "open":
                result = self._core.open_project(request.path, source_path=request.source_path,
                                                 cancellation=request.cancellation, request=request)
            elif request.purpose == "check":
                result = self._core.project_capabilities(request.session_id,
                                                         is_cancelled=request.cancellation.is_cancelled)
            else:
                result = self._core.save_project(request.session_id, request.path,
                                                 snapshot=request.snapshot, cancellation=request.cancellation,
                                                 request=request)
        except Exception as exc:
            logger.exception("项目后台任务失败：task_id=%s", request.request_id)
            result = CoreResult.from_exception(exc, message_key="studio.project_failed")
        self._log_terminal(request, result)
        with self._lock:
            closed = self._closed.is_set()
            if not closed:
                self._result = request, result
        if closed:
            self._discard_open(request, result)
            return
        try:
            self._completed.emit()
        except RuntimeError:
            self.shutdown()

    @Slot()
    def _deliver(self):
        with self._lock:
            outcome, self._result = self._result, None
            if outcome is not None:
                self._active, self._pending = self._pending, None
                next_request = self._active
                current = outcome[0].generation == self._generation
        if outcome is None:
            return
        request, result = outcome
        try:
            if self._closed.is_set():
                self._discard_open(request, result)
            elif not current:
                if request.purpose in ("save", "exit_save") and result.success:
                    self.finished.emit(request, result)
                else:
                    self._discard_open(request, result)
            else:
                self.finished.emit(request, result)
        finally:
            with self._lock:
                if next_request is not None and not self._closed.is_set():
                    self._pool.submit(self._run, next_request)

    def shutdown(self):
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            outcome, self._result = self._result, None
        self.cancel()
        if outcome is not None:
            self._discard_open(*outcome)
        self._pool.shutdown(wait=False)
