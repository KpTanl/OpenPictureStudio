from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from PyQt6.QtCore import QObject, QRunnable, Qt, pyqtSignal
from PyQt6.QtGui import QImage

from ...application.image_load_queue import ImageLoadQueue
from ...application.result_types import CoreError, CoreOperationError, CoreResult, ErrorCode
from ...core.contracts.result import memory_error_key
from ...application.services import PreviewResult, SamplingHandle
from ...application.studio_preview_cache import PreviewFrame
from ...application.studio_preview_queue import StudioPreviewRequest

logger = logging.getLogger(__name__)


class _RenderSignals(QObject):
    finished = pyqtSignal(object, object, object, object)
    sampled_finished = pyqtSignal(object)


@dataclass
class SampledRender:
    image: QImage | None
    frame: PreviewFrame | None
    result: PreviewResult | None
    error: CoreError | None
    detail_image: QImage | None = None

    def close(self):
        result, self.result = self.result, None
        self.image = None
        self.frame = None
        self.detail_image = None
        if result is not None:
            result.close()


@dataclass
class SamplingResources:
    sampling: SamplingHandle | None = None
    delivery: SamplingDelivery | None = None

    def close(self):
        sampling, self.sampling = self.sampling, None
        delivery, self.delivery = self.delivery, None
        if delivery is not None:
            delivery.close()
        if sampling is not None:
            sampling.close()


class SamplingDelivery:
    def __init__(self, request: StudioPreviewRequest):
        self._request = request
        self._lock = Lock()
        self._closed = False
        self._result: SampledRender | None = None

    @property
    def request(self):
        return self._request

    def publish(self, result: SampledRender):
        with self._lock:
            accepted = not self._closed and not self._request.is_cancelled()
            if accepted:
                self._result = result
        if not accepted:
            result.close()
        return accepted

    def take(self):
        with self._lock:
            result, self._result = self._result, None
            return result

    def close(self):
        with self._lock:
            self._closed = True
            result, self._result = self._result, None
        if result is not None:
            result.close()

    def __del__(self):
        self.close()


class StudioPreviewWorker(QRunnable):
    def __init__(self, request, service, delivery=None):
        super().__init__()
        self.request = request
        self.service = service
        self.signals = _RenderSignals()
        self.delivery = delivery

    def run(self):
        started = monotonic()
        image, frame, error, detail_image = None, None, None, None
        image_started = None
        result = None
        try:
            request = self.request
            options = dict(use_proxy=request.context.preview_mode != "full", editing_tool=request.context.editing_tool,
                is_cancelled=request.is_cancelled, source_identity=request.context.source_identity,
                source_preview=request.context.source_preview)
            if request.temporary_workflow is not None:
                options["temporary_workflow"] = request.temporary_workflow
            result = self.service.render_preview(request.session,
                sampling_request=request.sampling_request, keep_full_display=request.keep_full_display,
                allow_intermediate=request.sampling_request is None, **options)
            rendered = result.display if result is not None else None
            image_started = monotonic()
            if rendered is not None and not request.is_cancelled():
                width, height = rendered.size
                reference = result.full_size if result is not None and request.context.preview_target.mode != "full" else request.reference_size
                frame = PreviewFrame(rendered.rgba, rendered.size, reference,
                                     result.full_size if result is not None else None,
                                     result.effect_status if result is not None else (),
                                     result.input_size if result is not None else None,
                                     result.input_is_proxy if result is not None else None)
                image = QImage(frame.rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
                if result.detail is not None:
                    try:
                        full = result.detail.frame
                        width, height = full.size
                        detail_image = (image if request.context.preview_mode == "full" and full.size == frame.size else
                            QImage(full.rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy())
                        if detail_image.isNull():
                            raise MemoryError()
                    except MemoryError as exc:
                        result.take_detail().close()
                        detail_image = None
                        result.detail_error = CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(exc)).error
        except CoreOperationError as exc:
            error = exc.error
            logger.warning("OPS 预览失败 request=%s tool=%s code=%s key=%s details=%s",
                self.request.generation, self.request.context.editing_tool,
                error.code, error.message_key, error.details)
        except Exception as exc:
            error = CoreResult.from_exception(exc, message_key="studio.render_failed").error
            logger.exception("OPS 预览计算失败")
        finished_at = monotonic()
        logger.debug("OPS 预览 request=%s session=%s tool=%s workflow=%s state=%s source=%s queue_ms=%.1f render_ms=%.1f qimage_ms=%s cancelled=%s failed=%s",
            self.request.generation, self.request.context.session_id, self.request.context.editing_tool,
            self.request.session.workflow.workflow_hash()[:12], self.request.session.state_version,
            self.request.context.source_identity.file_hash[:12],
            (started - self.request.requested_at) * 1000, (finished_at - started) * 1000,
            round((finished_at - image_started) * 1000, 1) if image_started is not None else None,
            self.request.is_cancelled(), error is not None)
        try:
            if self.delivery is not None:
                self.delivery.publish(SampledRender(image, frame, result, error, detail_image))
                self.signals.sampled_finished.emit(self.request)
            else:
                self.signals.finished.emit(self.request, image, frame, error)
        except RuntimeError:
            if self.delivery is not None:
                self.delivery.close()
            logger.debug("OPS 预览接收方已关闭")
        finally:
            if self.delivery is None and result is not None:
                result.close()


class StudioHistogramRunner(QObject):
    ready = pyqtSignal(object, object)
    _completed = pyqtSignal(object, object, object, object)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self._service = service
        self._queue = ImageLoadQueue()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-histogram")
        self._closed = False
        self._pending_frame = None
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def submit(self, request, frame):
        if not self._closed:
            self._pending_frame = (request, frame)
            for job in self._queue.replace([(request.generation,)]):
                self._start(job)

    def _start(self, job):
        request, frame = self._pending_frame
        self._pending_frame = None
        self._pool.submit(self._run, job, request, frame)

    def cancel(self):
        self._queue.replace([])
        self._pending_frame = None

    def _run(self, job, request, frame):
        started = monotonic()
        histogram, error = None, None
        try:
            if not job.cancellation.is_set() and not request.is_cancelled():
                histogram = self._service.histogram(frame)
        except Exception as exc:
            error = exc
            logger.exception("OPS 直方图计算失败")
        logger.debug("OPS 直方图 request=%s duration_ms=%.1f cancelled=%s failed=%s",
                     request.generation, (monotonic() - started) * 1000,
                     job.cancellation.is_set() or request.is_cancelled(), error is not None)
        try:
            self._completed.emit(job, request, histogram, error)
        except RuntimeError:
            logger.debug("OPS 直方图接收方已关闭")

    def _deliver(self, job, request, histogram, error):
        try:
            if (not self._closed and not job.cancellation.is_set()
                    and not request.is_cancelled() and histogram is not None and error is None):
                self.ready.emit(request, histogram)
        finally:
            following = self._queue.finish(job)
            if not self._closed:
                for pending in following:
                    self._start(pending)

    def shutdown(self):
        self._closed = True
        self._queue.close()
        self._pending_frame = None
        self._pool.shutdown(wait=False, cancel_futures=True)
