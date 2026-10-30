from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QImage

from ...application.image_load_queue import ImageLoadQueue
from ...application.result_types import CoreOperationError, CoreResult
from ...core.contracts.result import ErrorCode, memory_error_key
from .studio_preview_runner import SampledRender

logger = logging.getLogger(__name__)


class DetailResources:
    def __init__(self):
        self.detail = None
        self.image = None
        self.source = None
        self.source_identity = None

    def clear_frame(self):
        self.image = None
        detail, self.detail = self.detail, None
        if detail is not None:
            detail.close()

    def release_source(self):
        source, self.source = self.source, None
        self.source_identity = None
        if source is not None:
            source.close()

    def adopt(self, detail, image, identity):
        self.clear_frame()
        self.release_source()
        self.detail, self.image = detail, image
        self.source = detail.take_source()
        self.source_identity = identity

    def close(self):
        self.clear_frame()
        self.release_source()


class StudioDetailRunner(QObject):
    ready = Signal(object, object)
    _completed = Signal(object, object)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self._service = service
        self._queue = ImageLoadQueue()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-detail")
        self._pending = None
        self._lock = Lock()
        self._packets = {}
        self._closed = False
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def submit(self, preview):
        if self._closed:
            return
        self._pending = preview
        for job in self._queue.replace([(preview.generation,)]):
            self._start(job)

    def _start(self, job):
        preview, self._pending = self._pending, None
        self._pool.submit(self._run, job, preview)

    def cancel(self):
        self._queue.replace([])
        self._pending = None
        with self._lock:
            packets, self._packets = self._packets, {}
        for packet in packets.values():
            packet.close()

    def _run(self, job, preview):
        result, image, error = None, None, None
        cancelled = lambda: job.cancellation.is_set() or preview.is_cancelled()
        try:
            result = self._service.render_preview(preview.session, use_proxy=False,
                allow_intermediate=False, keep_full_display=True, priority=1,
                source_identity=preview.context.source_identity,
                source_preview=preview.context.source_preview, is_cancelled=cancelled)
            if result is not None and result.detail is not None and not cancelled():
                frame = result.detail.frame
                width, height = frame.size
                image = QImage(frame.rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
                if image.isNull():
                    raise MemoryError()
        except MemoryError as exc:
            error = CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(exc)).error
        except CoreOperationError as exc:
            error = exc.error
            logger.warning("OPS 清晰画面失败 request=%s code=%s key=%s",
                           preview.generation, error.code, error.message_key)
        except Exception as exc:
            error = CoreResult.from_exception(exc, message_key="studio.detail_failed").error
            logger.exception("OPS 清晰画面计算失败")
        packet = SampledRender(image, None, result, error)
        with self._lock:
            accepted = not self._closed and not cancelled()
            if accepted:
                self._packets[job.request_id] = packet
        if not accepted:
            packet.close()
        try:
            self._completed.emit(job, preview)
        except RuntimeError:
            with self._lock:
                pending = self._packets.pop(job.request_id, None)
            if pending is not None:
                pending.close()

    @Slot(object, object)
    def _deliver(self, job, preview):
        with self._lock:
            packet = self._packets.pop(job.request_id, None)
        try:
            if (packet is not None and not self._closed and not job.cancellation.is_set()
                    and not preview.is_cancelled()):
                self.ready.emit(preview, packet)
        finally:
            if packet is not None:
                packet.close()
            for following in self._queue.finish(job):
                if not self._closed:
                    self._start(following)

    def shutdown(self):
        with self._lock:
            self._closed = True
        self.cancel()
        self._queue.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
