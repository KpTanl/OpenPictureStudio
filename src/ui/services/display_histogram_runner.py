from concurrent.futures import CancelledError, ThreadPoolExecutor
import logging
import math

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QImage

from ...application.display_histogram import HISTOGRAM_SAMPLE_PIXELS, DisplayHistogramRequest
from ...application.image_load_queue import ImageLoadQueue
from ...core.contracts.image_display import DEFAULT_NON_RAW_DISPLAY_MODE

logger = logging.getLogger(__name__)


def _sample_frame(image: QImage) -> tuple[tuple[int, int], bytes]:
    """把显示画面按等间隔取样缩到统计所需的像素数以内，转成直通透明度的 RGBA8 字节。在后台线程调用。"""
    pixels = image.width() * image.height()
    if pixels > HISTOGRAM_SAMPLE_PIXELS:
        step = math.ceil(math.sqrt(pixels / HISTOGRAM_SAMPLE_PIXELS))
        image = image.scaled(max(1, image.width() // step), max(1, image.height() // step),
                             Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    # RGBA8888 每像素四字节，行与行之间没有填充
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return (image.width(), image.height()), bytes(image.constBits())


class DisplayHistogramRunner(QObject):
    """后台计算看图画面的直方图，只交付最新一次请求的结果。"""

    started = Signal(object)
    ready = Signal(object, object)
    _completed = Signal(object, object, object)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self._service = service
        self._queue = ImageLoadQueue()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-histogram")
        self._closed = False
        self._generation = 0
        self._latest = None
        self._pending = None
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def request(self, path: str, stamp, raw_display_mode, image: QImage, *,
                non_raw_display_mode=DEFAULT_NON_RAW_DISPLAY_MODE) -> None:
        """image 是这个文件版本当前显示的终态画面；此前的请求随之作废。"""
        if self._closed:
            return
        self._generation += 1
        request = DisplayHistogramRequest(self._generation, path, tuple(stamp), raw_display_mode,
                                          non_raw_display_mode)
        self._latest = request
        self._pending = (request, image)
        self.started.emit(request)
        for job in self._queue.replace([(request.generation,)]):
            self._start(job)

    def cancel(self) -> None:
        self._latest = None
        self._pending = None
        self._queue.replace([])

    def _start(self, job) -> None:
        if self._pending is None:
            self._queue.finish(job)
            return
        request, image = self._pending
        self._pending = None
        self._pool.submit(self._run, job, request, image)

    def _run(self, job, request, image) -> None:
        histogram = None
        try:
            if not job.cancellation.is_set():
                histogram = self._service.histogram(lambda: _sample_frame(image),
                                                    is_cancelled=job.cancellation.is_set)
        except CancelledError:
            pass
        except Exception:
            logger.exception("看图直方图计算失败：%s", request.path)
        try:
            self._completed.emit(job, request, histogram)
        except RuntimeError:
            logger.debug("看图直方图接收方已关闭")

    @Slot(object, object, object)
    def _deliver(self, job, request, histogram) -> None:
        try:
            if (not self._closed and not job.cancellation.is_set() and request == self._latest
                    and histogram is not None):
                self.ready.emit(request, histogram)
        finally:
            following = self._queue.finish(job)
            if not self._closed:
                for pending in following:
                    self._start(pending)

    def shutdown(self) -> None:
        self._closed = True
        self._latest = None
        self._pending = None
        self._queue.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
