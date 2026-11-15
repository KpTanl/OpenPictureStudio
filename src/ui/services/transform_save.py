from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, Signal

from ...core.raw_loader import load_image

logger = logging.getLogger(__name__)


class PendingImage:
    """看图模式里一次旋转或翻转的结果。

    原图的解码与变换都慢，界面线程只登记、不取用；第一个调用 result 的后台线程算一次，
    之后都拿同一份。连续变换时 source 是上一次的 PendingImage，否则是原图路径。
    """

    def __init__(self, source, transform):
        self._source, self._transform = source, transform
        self._lock = Lock()
        self._image = None

    @property
    def ready(self) -> bool:
        return self._image is not None

    def result(self) -> Image.Image:
        with self._lock:
            if self._image is None:
                source = self._source
                image = source.result() if isinstance(source, PendingImage) else load_image(source)
                self._image = self._transform(image)
                self._source = None
            return self._image


@dataclass(frozen=True)
class TransformSaveRequest:
    source_path: str
    source_revision: int
    generation: int
    target_path: Path


class _TransformSaveSignals(QObject):
    finished = Signal(object, str)


class TransformSaveWorker(QRunnable):
    def __init__(self, image: PendingImage, request: TransformSaveRequest, output_service, *, workspace=None):
        super().__init__()
        self._output_service = output_service
        self._image = image
        self._request = request
        self.signals = _TransformSaveSignals()
        self._workspace = workspace

    def run(self) -> None:
        error = ""
        try:
            from contextlib import nullcontext
            image = self._image.result()
            budget = self._workspace.reserve(image.width * image.height * 8 + 1024 ** 2) if self._workspace else nullcontext()
            with budget:
                self._output_service.save(image, self._request.target_path)
        except Exception as exc:
            logger.exception(
                "Failed to save transformed image to %s", self._request.target_path
            )
            error = str(exc)
        self.signals.finished.emit(self._request, error)
