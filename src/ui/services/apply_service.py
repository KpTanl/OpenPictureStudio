"""Apply/save target resolution shared by workspaces.

Owns the "where do I write the rendered result?" prompt and target path
resolution so workspaces reuse one implementation instead of reaching into
the main window.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

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

    def resolve_target(self, target, old_path, *, default_ext=".png"):
        old_ext = os.path.splitext(old_path)[1].lower()
        if target == ApplyTarget.CANCEL:
            return None
        if target == ApplyTarget.OVERWRITE and old_ext not in IMAGE_EXTS:
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
