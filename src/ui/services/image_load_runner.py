from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Lock
from time import perf_counter
import logging

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QImage

from ...application.image_load_queue import ImageLoadQueue

logger = logging.getLogger(__name__)


class ImageLoadRunner(QObject):
    ready = pyqtSignal(object, object, object)
    display_ready = pyqtSignal(object, object, object, object)
    quick_ready = pyqtSignal(object, object, object)
    _completed = pyqtSignal()

    def __init__(self, source_service, *, thumbnail=False, parent=None):
        super().__init__(parent)
        self._service = source_service
        self._thumbnail = thumbnail
        policy = source_service.policy
        slots = policy.thumbnail_slots if thumbnail else 1
        self._queue = ImageLoadQueue(slots, policy.thumbnail_pending if thumbnail else 1)
        self._pool = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="ops-image")
        self._quick_queue = None if thumbnail else ImageLoadQueue(1, 1)
        self._quick_pool = None if thumbnail else ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-quick")
        self._lock = Lock()
        self._results = []
        self._closed = False
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def replace(self, keys):
        keys = list(keys)
        for request in self._queue.replace(key for key in keys if key.stage != "quick"):
            self._pool.submit(self._run, request)
        if self._quick_queue is not None:
            for request in self._quick_queue.replace(key for key in keys if key.stage == "quick"):
                self._quick_pool.submit(self._run, request)
        logger.debug("图像队列 thumbnail=%s generation=%s active=%s pending=%s",
                     self._thumbnail, self._queue.generation, len(self._queue.active), len(self._queue.pending))

    def _run(self, request):
        started = perf_counter()
        qimage, error, reference_size = None, None, None
        try:
            path, stamp, size = request.key.path, request.key.stamp, request.key.size
            if self._thumbnail:
                image = self._service.thumbnail(path, size, expected_stamp=stamp,
                    is_cancelled=request.cancellation.is_set, priority=2)
            elif request.key.stage == "quick":
                preview = self._service.quick_preview(path, expected_stamp=stamp,
                    is_cancelled=request.cancellation.is_set)
                image, reference_size = preview if preview is not None else (None, None)
            else:
                image, reference_size = self._service.viewer_image(path,
                    raw_display_mode=request.key.raw_display_mode,
                    non_raw_display_mode=request.key.non_raw_display_mode, expected_stamp=stamp,
                    is_cancelled=request.cancellation.is_set)
            try:
                if image is not None and not request.cancellation.is_set():
                    qimage = QImage(image.tobytes(), image.width, image.height, image.width * 4,
                                    QImage.Format.Format_RGBA8888).copy()
            finally:
                if image is not None:
                    image.close()
        except CancelledError:
            pass
        except Exception as exc:
            error = exc
            logger.exception("图像加载失败：%s", request.key.path)
        logger.debug("图像任务 thumbnail=%s stage=%s request=%s elapsed_ms=%.1f cancelled=%s failed=%s",
                     self._thumbnail, request.key.stage, request.request_id, (perf_counter() - started) * 1000,
                     request.cancellation.is_set(), error is not None)
        with self._lock:
            if self._closed:
                return
            self._results.append((request, qimage, error, reference_size))
        try:
            self._completed.emit()
        except RuntimeError:
            self.shutdown()

    def _deliver(self):
        with self._lock:
            results, self._results = self._results, []
        for request, image, error, reference_size in results:
            quick = request.key.stage == "quick"
            try:
                if not self._closed and not request.cancellation.is_set():
                    if quick:
                        self.quick_ready.emit(request, image, reference_size)
                    elif self._thumbnail:
                        self.ready.emit(request, image, error)
                    else:
                        self.display_ready.emit(request, image, error, reference_size)
            finally:
                queue = self._quick_queue if quick else self._queue
                pool = self._quick_pool if quick else self._pool
                for following in queue.finish(request):
                    pool.submit(self._run, following)

    def shutdown(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._results.clear()
        self._queue.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._quick_queue is not None:
            self._quick_queue.close()
            self._quick_pool.shutdown(wait=False, cancel_futures=True)
