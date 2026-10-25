from __future__ import annotations

import logging
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import perf_counter

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage

from ...application.result_types import CoreResult, ErrorCode
from ...application.studio_source_service import StudioLoadQueue

logger = logging.getLogger(__name__)


def source_qimage(image):
    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    try:
        return QImage(rgba.tobytes(), rgba.width, rgba.height, rgba.width * 4,
                      QImage.Format.Format_RGBA8888).copy()
    finally:
        if rgba is not image:
            rgba.close()


class StudioSourceRunner(QObject):
    finished = pyqtSignal(object, object, object)
    preview_ready = pyqtSignal(object, object, object)
    prefetch_failed = pyqtSignal()
    _completed = pyqtSignal()

    def __init__(self, service, preview_cache, parent=None):
        super().__init__(parent)
        self._service = service
        self._cache = preview_cache
        self._prefetch_paths = deque()
        self._queue = StudioLoadQueue()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-source")
        self._lock = Lock()
        self._result = None
        self._closed = Event()
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def submit(self, path, session_id=None, *, preview_key=None, initial_workflow=None, preview_workflow=None):
        if self._closed.is_set():
            return
        self._prefetch_paths.clear()
        request = self._queue.submit(path, session_id, preview_key=preview_key,
                                      initial_workflow=initial_workflow, preview_workflow=preview_workflow)
        selected = request or self._queue.pending or self._queue.active
        if selected is not None and selected.preview_key is not None:
            frame = self._cache.peek(selected.preview_key)
            if frame is not None and not selected.cancellation.is_set():
                self._emit_cached_frame(selected, frame)
        if request is not None:
            self._pool.submit(self._run, request)

    def cancel(self):
        self._queue.cancel()
        self._prefetch_paths.clear()

    def prefetch(self, targets):
        if self._closed.is_set():
            return
        active = self._queue.active
        targets = tuple(targets)
        self._prefetch_paths = deque(target for target in targets if not (
            active is not None and active.prefetch and not active.cancellation.is_set()
            and target.path == active.path and target.workflow == active.initial_workflow))
        if active is None:
            self._start_prefetch()
        elif active.prefetch and not any(target.path == active.path and target.workflow == active.initial_workflow
                                         for target in targets):
            active.cancellation.set()

    def pause_prefetch(self):
        self._prefetch_paths.clear()
        active = self._queue.active
        if active is not None and active.prefetch:
            active.cancellation.set()

    def _start_prefetch(self):
        if self._prefetch_paths and not self._closed.is_set():
            target = self._prefetch_paths.popleft()
            request = self._queue.submit(target.path, prefetch=True, preview_key=target.preview_key,
                                          initial_workflow=target.workflow)
            if request is not None:
                self._pool.submit(self._run, request)

    def _show_cached(self, request):
        key = request.preview_key
        if key is None or request.cancellation.is_set() or not key.matches_file():
            return
        try:
            frame = self._cache.get(key)
        except OSError:
            logger.exception("OPS preview cache read failed")
            return
        if frame is not None and not request.cancellation.is_set():
            self._emit_cached_frame(request, frame)

    def _emit_cached_frame(self, request, frame):
        width, height = frame.size
        qimage = QImage(frame.rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        self.preview_ready.emit(request, qimage, frame.reference_size)

    def _run(self, request):
        started = perf_counter()
        logger.debug("OPS source load started: request=%s reuse_session=%s",
                     request.request_id, request.session_id is not None)
        try:
            if request.prefetch:
                result = self._service.prepare_preview(request, self._cache)
            else:
                self._show_cached(request)
                result = self._service.prepare(request)
        except Exception as error:
            logger.exception("OPS source worker failed")
            result = CoreResult.from_exception(error, message_key="error.source.decode_failed")
        qimage = None
        if result.success and result.data is not None:
            try:
                if not request.cancellation.is_set():
                    qimage = source_qimage(result.data.image)
                    checked = self._service.validate_prepared(result.data)
                    if not checked.success:
                        result.data.close()
                        result = checked
                        qimage = None
            except Exception as error:
                result.data.close()
                result = CoreResult.from_exception(error, code=ErrorCode.DECODE_FAILED,
                                                   message_key="error.source.decode_failed")
        if request.cancellation.is_set() and result.success:
            if result.data is not None:
                result.data.close()
            result = CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            qimage = None
        logger.debug("OPS source load finished: request=%s elapsed_ms=%.1f result=%s",
                     request.request_id, (perf_counter() - started) * 1000,
                     "ready" if result.success else result.error.code)
        with self._lock:
            closed = self._closed.is_set()
            if not closed:
                self._result = (request, result, qimage)
        if closed:
            if result.success and result.data is not None:
                result.data.close()
            return
        try:
            self._completed.emit()
        except RuntimeError:
            self.shutdown()

    @pyqtSlot()
    def _deliver(self):
        with self._lock:
            outcome, self._result = self._result, None
        if outcome is None:
            return
        request, result, qimage = outcome
        try:
            if not self._closed.is_set() and not request.cancellation.is_set():
                failed = not result.success and result.error.code != ErrorCode.CANCELLED
                if failed:
                    error = result.error
                    logger.warning("OPS source load failed: request=%s prefetch=%s image=%r code=%s "
                                   "key=%s exception=%s message=%r",
                                   request.request_id, request.prefetch, os.path.basename(request.path),
                                   error.code, error.message_key, error.details.get("exception", ""),
                                   error.details.get("message", ""))
                if request.prefetch:
                    if failed:
                        self.prefetch_failed.emit()
                else:
                    self.finished.emit(request, result, qimage)
        finally:
            if result.success and result.data is not None:
                result.data.close()
            next_request = self._queue.finish(request)
            if next_request is not None and not self._closed.is_set():
                self._pool.submit(self._run, next_request)
            elif not self._closed.is_set():
                self._start_prefetch()

    def shutdown(self):
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            outcome, self._result = self._result, None
        self.cancel()
        if outcome is not None and outcome[1].success and outcome[1].data is not None:
            outcome[1].data.close()
        self._pool.submit(self._close_cache)
        self._pool.shutdown(wait=False)

    def _close_cache(self):
        try:
            self._cache.close()
        except OSError:
            logger.exception("OPS preview cache cleanup failed")
