import logging
from dataclasses import dataclass
from contextlib import nullcontext
from concurrent.futures import CancelledError
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ...application.image_load_queue import ImageLoadQueue
from ...application.result_types import CoreResult, CoreOperationError
from .studio_source_runner import source_qimage

logger = logging.getLogger(__name__)


@dataclass
class OriginalDisplay:
    preview: object
    full: object
    size: tuple[int, int]
    memory: object = None
    full_memory: object = None
    preview_proxy_input: bool = True

    def close(self):
        self.preview = self.full = None
        memory, self.memory = self.memory, None
        if memory is not None:
            memory.close()
        memory, self.full_memory = self.full_memory, None
        if memory is not None:
            memory.close()


class StudioOriginalRunner(QObject):
    ready = Signal(object, object, object)
    source_ready = Signal(object, object)
    _source_completed = Signal(object)
    _completed = Signal(object, object, object)

    def __init__(self, sources, parent=None, *, complete_source=None):
        super().__init__(parent)
        self._sources = sources
        self._complete_source = complete_source
        self._queue = ImageLoadQueue()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-original")
        self._closed = False
        self._source_lock = Lock()
        self._prepared_sources = {}
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._source_completed.connect(self._deliver_source, Qt.ConnectionType.QueuedConnection)

    def submit(self, session_id, identity, *, full=True, proxy_input=True):
        for request in self._queue.replace([(session_id, identity, full, proxy_input)]):
            self._pool.submit(self._run, request)

    def cancel(self):
        self._queue.replace([])

    def _run(self, request):
        started = monotonic()
        image, error = None, None
        memory = full_memory = None
        try:
            full = request.key[2]
            proxy_input = request.key[3] and not full
            if self._complete_source is not None:
                prepared = self._complete_source(*request.key[:2], is_cancelled=request.cancellation.is_set)
                if not prepared.success:
                    raise CoreOperationError(prepared.error)
                if prepared.data is not None:
                    with self._source_lock:
                        if self._closed or request.cancellation.is_set():
                            prepared.data.close()
                            raise CancelledError()
                        self._prepared_sources[id(request)] = prepared
                    try:
                        self._source_completed.emit(request)
                    except RuntimeError:
                        with self._source_lock:
                            pending = self._prepared_sources.pop(id(request), None)
                        if pending is not None:
                            pending.data.close()
                        raise CancelledError()
            width, height = self._sources.dimensions(request.key[1].path)
            scale = min(1, 1600 / max(width, height))
            pw, ph = max(1, round(width * scale)), max(1, round(height * scale))
            memory = self._sources.resources.hold_memory(pw * ph * 16, request.cancellation.is_set)
            if full:
                full_memory = self._sources.resources.hold_memory(width * height * 16, request.cancellation.is_set)
            working = (self._sources.resources.working_memory(width * height * 8, request.cancellation.is_set)
                       if not proxy_input else nullcontext())
            with working:
                if not proxy_input:
                    original = self._sources.load_original_display(request.key[1],
                        is_cancelled=request.cancellation.is_set)
                    size = original.size
                else:
                    original, size = self._sources.load_preview(request.key[1].path, max_side=1600,
                        is_cancelled=request.cancellation.is_set)
                try:
                    if not request.cancellation.is_set():
                        full_image = source_qimage(original) if full else None
                        original.thumbnail((1600, 1600))
                        preview = source_qimage(original)
                        image = OriginalDisplay(preview, full_image if full else (
                            preview if (preview.width(), preview.height()) == size else None), size,
                            memory, full_memory, proxy_input)
                        memory = full_memory = None
                finally:
                    original.close()
        except Exception as exc:
            if not request.cancellation.is_set():
                logger.exception("OPS 原图对比准备失败")
                error = (exc.error if isinstance(exc, CoreOperationError) else
                         CoreResult.from_exception(exc, message_key="studio.original_failed").error)
        finally:
            if memory is not None:
                memory.close()
            if full_memory is not None:
                full_memory.close()
        logger.debug("OPS 原图对比 session=%s duration_ms=%.1f cancelled=%s failed=%s",
                     request.key[0], (monotonic() - started) * 1000,
                     request.cancellation.is_set(), error is not None)
        try:
            self._completed.emit(request, image, error)
        except RuntimeError:
            if image is not None:
                image.close()
            logger.debug("OPS 原图对比接收方已关闭")

    @Slot(object, object, object)
    def _deliver(self, request, image, error):
        try:
            if not self._closed and not request.cancellation.is_set():
                self.ready.emit(request.key[:2], image, error)
            elif image is not None:
                image.close()
        finally:
            for following in self._queue.finish(request):
                self._pool.submit(self._run, following)

    @Slot(object)
    def _deliver_source(self, request):
        with self._source_lock:
            result = self._prepared_sources.pop(id(request), None)
        if result is None:
            return
        try:
            if not self._closed and not request.cancellation.is_set():
                self.source_ready.emit(request.key, result)
        finally:
            if result.data is not None:
                result.data.close()
                result.data = None

    def shutdown(self):
        with self._source_lock:
            self._closed = True
            pending, self._prepared_sources = tuple(self._prepared_sources.values()), {}
        for result in pending:
            result.data.close()
            result.data = None
        self._queue.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
