from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, Signal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransformSaveRequest:
    source_path: str
    source_revision: int
    generation: int
    target_path: Path


class _TransformSaveSignals(QObject):
    finished = Signal(object, str)


class TransformSaveWorker(QRunnable):
    def __init__(self, image: Image.Image, request: TransformSaveRequest, output_service, *, workspace=None):
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
            budget = self._workspace.reserve(self._image.width * self._image.height * 8 + 1024 ** 2) if self._workspace else nullcontext()
            with budget:
                self._output_service.save(self._image, self._request.target_path)
        except Exception as exc:
            logger.exception(
                "Failed to save transformed image to %s", self._request.target_path
            )
            error = str(exc)
        self.signals.finished.emit(self._request, error)
