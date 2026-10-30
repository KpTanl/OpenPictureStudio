"""Apply/save workflow shared by OPS and OPS.

Owns the "where do I write the rendered result?" decision and the atomic file
write, so both workspaces reuse one implementation instead of reaching into the
main window.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PIL import Image
from PySide6.QtWidgets import QFileDialog, QWidget

from ..dialogs.apply_target_dialog import ApplyTarget, ask_apply_target

IMAGE_EXTS = (".png", ".webp", ".bmp", ".tiff", ".tif", ".jpg", ".jpeg")


class ApplyService:
    def __init__(self, parent: Optional[QWidget], tr: Callable[..., str], *, output_service=None):
        from ...application.image_output_service import ImageOutputService
        self.output_service = output_service or ImageOutputService()
        self._parent = parent
        self._tr = tr

    # ---- prompt ----
    def ask_target(self) -> ApplyTarget:
        return ask_apply_target(self._parent, self._tr)

    # ---- helpers ----
    @staticmethod
    def has_alpha(image: Image.Image) -> bool:
        return image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )

    @classmethod
    def default_ext(cls, image: Image.Image, old_path: str) -> str:
        ext = os.path.splitext(old_path)[1].lower()
        if cls.has_alpha(image):
            return ".png"
        return ext if ext in IMAGE_EXTS else ".png"

    def save_image_to_path(self, image: Image.Image, path: str, quality: int = 95) -> None:
        from .background_operation import save_image_background
        save_image_background(self._parent, self.output_service, image, path, quality=quality)

    # ---- main entry ----
    def resolve_and_save(
        self, image: Image.Image, target: ApplyTarget, old_path: str
    ) -> Optional[str]:
        """Write ``image`` per the chosen target; return the final path or None."""
        default_ext = self.default_ext(image, old_path)
        final_path = self.resolve_target(target, old_path, default_ext=default_ext,
                                         has_alpha=self.has_alpha(image))
        if final_path is not None:
            self.save_image_to_path(image, final_path)
        return final_path

    def resolve_target(self, target, old_path, *, default_ext=".png", has_alpha=False):
        old_ext = os.path.splitext(old_path)[1].lower()
        if target == ApplyTarget.CANCEL:
            return None
        if target == ApplyTarget.OVERWRITE and (
            old_ext not in IMAGE_EXTS or (has_alpha and old_ext in (".jpg", ".jpeg", ".bmp"))
        ):
            target = ApplyTarget.SAVE_AS

        if target == ApplyTarget.OVERWRITE:
            final_path = old_path
        else:  # SAVE_AS
            start = os.path.splitext(old_path)[0] + "_edited" + default_ext
            chosen, _ = QFileDialog.getSaveFileName(
                self._parent,
                self._tr("apply.save_as.title", "Save As"),
                start,
                self._tr(
                    "apply.save_as.filter",
                    "Images (*.png *.jpg *.jpeg *.webp *.tif *.tiff *.bmp)",
                ),
            )
            if not chosen:
                return None
            final_path = chosen
            if not os.path.splitext(final_path)[1]:
                final_path += default_ext

        return final_path
