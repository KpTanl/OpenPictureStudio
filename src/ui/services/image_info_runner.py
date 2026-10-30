from concurrent.futures import CancelledError, ThreadPoolExecutor
import logging

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ...application.image_load_queue import ImageLoadQueue
from ...application.image_info import ImageInfoQuery, ImageInfoRequest, ImageInfoResult
from ...core.contracts.result import CoreError, ErrorCode, SourceChangedError

logger = logging.getLogger(__name__)


class ImageInfoRunner(QObject):
    """后台读取并交付当前文件版本的信息快照。"""
    started = Signal(object)
    ready = Signal(object)
    _completed = Signal(object, object)

    def __init__(self, read_info, file_stamp, parent=None):
        super().__init__(parent)
        self._read_info = read_info
        self._file_stamp = file_stamp
        self._queue = ImageLoadQueue()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-info")
        self._closed = False
        self._generation = 0
        self._latest_request = None
        self._cached = None
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def request(self, query: ImageInfoQuery):
        if self._closed:
            return
        if not query.path:
            self.cancel()
            self._cached = None
            return
        self._generation += 1
        try:
            stamp = self._file_stamp(query.path)
        except OSError:
            stamp = None
        request = ImageInfoRequest(self._generation, query.path, stamp, query.details)
        self._latest_request = request
        self.started.emit(request)
        cached = self._cached
        if (not query.force and cached is not None and stamp is not None
                and cached.request.path == request.path and cached.request.file_stamp == stamp
                and (not query.details or cached.info.details)):
            self._queue.replace([])
            self.ready.emit(ImageInfoResult(request, cached.info))
            return
        self._cached = None
        if stamp is None:
            self._queue.replace([])
            self.ready.emit(ImageInfoResult(request, error=CoreError(
                ErrorCode.SOURCE_NOT_FOUND, "photo_info.read_failed", retryable=True)))
            return
        for job in self._queue.replace([(request,)]):
            self._pool.submit(self._run, job)

    def cancel(self):
        self._latest_request = None
        self._queue.replace([])

    def _run(self, job):
        request = job.key[0]
        result = ImageInfoResult(request)
        try:
            if not job.cancellation.is_set():
                info = self._read_info(request.path, details=request.details,
                                       is_cancelled=job.cancellation.is_set)
                if self._file_stamp(request.path) != request.file_stamp:
                    result = ImageInfoResult(request, error=CoreError(
                        ErrorCode.SOURCE_CHANGED, "photo_info.source_changed", retryable=True))
                else:
                    result = ImageInfoResult(request, info)
        except CancelledError:
            pass
        except SourceChangedError:
            result = ImageInfoResult(request, error=CoreError(
                ErrorCode.SOURCE_CHANGED, "photo_info.source_changed", retryable=True))
        except MemoryError:
            result = ImageInfoResult(request, error=CoreError(
                ErrorCode.DECODE_FAILED, "photo_info.memory_failed", retryable=True))
        except Exception:
            logger.exception("图片信息读取失败：%s", request.path)
            result = ImageInfoResult(request, error=CoreError(
                ErrorCode.DECODE_FAILED, "photo_info.read_failed", retryable=True))
        try:
            self._completed.emit(job, result)
        except RuntimeError:
            logger.debug("图片信息接收方已关闭")

    @Slot(object, object)
    def _deliver(self, job, result):
        try:
            if (self._closed or job.cancellation.is_set()
                    or result.request != self._latest_request):
                return
            if result.info is not None:
                try:
                    valid = self._file_stamp(result.request.path) == result.request.file_stamp
                except OSError:
                    valid = False
                if not valid:
                    result = ImageInfoResult(result.request, error=CoreError(
                        ErrorCode.SOURCE_CHANGED, "photo_info.source_changed", retryable=True))
                else:
                    self._cached = result
            self.ready.emit(result)
        finally:
            following = self._queue.finish(job)
            if not self._closed:
                for pending in following:
                    self._pool.submit(self._run, pending)

    def shutdown(self):
        self._closed = True
        self._latest_request = None
        self._cached = None
        self._queue.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
