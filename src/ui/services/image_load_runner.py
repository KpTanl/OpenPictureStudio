from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Lock
from time import perf_counter
import logging

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QImage

from ...application.image_load_queue import ImageLoadQueue
from ...utils.log_levels import TRACE

logger = logging.getLogger(__name__)


class ImageLoadRunner(QObject):
    ready = Signal(object, object, object)
    display_ready = Signal(object, object, object, object)
    quick_ready = Signal(object, object, object)
    _completed = Signal()

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
        logger.log(TRACE, "图像队列 thumbnail=%s generation=%s active=%s pending=%s",
                   self._thumbnail, self._queue.generation, len(self._queue.active), len(self._queue.pending))

    @staticmethod
    def _display_qimage(image, cancelled, rows=256):
        """把解码好的图按条带直接写进 QImage，格式就是 Qt 绘制用的那种。取消时返回 None。

        整幅转成字节串再复制，一亿像素的图每一步都占着解释器锁约 0.1 秒，界面线程拿到后还要再转一次格式，
        卡住界面 0.2 秒。按条带写，每次占锁十几毫秒，界面线程拿到后不用再转。"""
        if image.mode not in ("RGB", "RGBA"):
            with image.convert("RGBA") as converted:
                return ImageLoadRunner._display_qimage(converted, cancelled, rows)
        opaque = image.mode == "RGB"
        qimage = QImage(image.width, image.height,
                        QImage.Format.Format_RGB32 if opaque else QImage.Format.Format_ARGB32)
        if qimage.isNull():
            raise MemoryError()
        view, line = memoryview(qimage.bits()), qimage.bytesPerLine()
        for top in range(0, image.height, rows):
            if cancelled():
                return None
            bottom = min(image.height, top + rows)
            with image.crop((0, top, image.width, bottom)) as strip:
                view[top * line:bottom * line] = strip.tobytes("raw", "BGRX" if opaque else "BGRA")
            if opaque:
                # Pillow 把填充字节写成 0，Qt 要求这种格式的第四个字节是 255，否则直接绘制时整幅是黑的
                np.frombuffer(view, np.uint8, (bottom - top) * line, top * line)[3::4] = 255
        del view
        # 带透明通道的图由 Qt 做预乘，结果与原来逐位相同
        return qimage if opaque else qimage.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

    def _run(self, request):
        started = perf_counter()
        qimage, error, reference_size = None, None, None
        try:
            path, stamp, size = request.key.path, request.key.stamp, request.key.size
            if self._thumbnail:
                image = self._service.thumbnail(path, size, expected_stamp=stamp,
                    is_cancelled=request.cancellation.is_set, priority=2)
            elif request.key.stage == "quick":
                # RAW 取内嵌的相机预览；其余格式带着显示区域大小来，取一张屏幕大小的缩小图
                preview = (self._service.quick_preview(path, expected_stamp=stamp,
                               is_cancelled=request.cancellation.is_set) if size is None else
                           self._service.screen_preview(path, size, expected_stamp=stamp,
                               is_cancelled=request.cancellation.is_set))
                image, reference_size = preview if preview is not None else (None, None)
            else:
                image, reference_size = self._service.viewer_image(path,
                    raw_display_mode=request.key.raw_display_mode,
                    non_raw_display_mode=request.key.non_raw_display_mode, expected_stamp=stamp,
                    is_cancelled=request.cancellation.is_set, keep_opaque=True)
            try:
                if image is not None and not request.cancellation.is_set():
                    if self._thumbnail:
                        qimage = QImage(image.tobytes(), image.width, image.height, image.width * 4,
                                        QImage.Format.Format_RGBA8888).copy()
                    else:
                        qimage = self._display_qimage(image, request.cancellation.is_set)
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

    @Slot()
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
