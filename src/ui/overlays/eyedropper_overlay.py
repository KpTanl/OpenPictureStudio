from __future__ import annotations

from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtGui import QMouseEvent

from .base_overlay import ViewerOverlay


class EyedropperOverlay(ViewerOverlay):
    """取色覆盖层：报告图像显示矩形内的归一化点击位置。"""

    picked = Signal(float, float)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._active = False
        self._cursor_applied = False

    @property
    def active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self._apply_cursor()

    def set_viewer(self, viewer) -> None:
        self._restore_cursor()
        super().set_viewer(viewer)
        self._apply_cursor()

    def _apply_cursor(self) -> None:
        if self._viewer is None:
            return
        if self._active:
            self.set_cursor(Qt.CursorShape.CrossCursor)
            self._cursor_applied = True
        else:
            self._restore_cursor()

    def _restore_cursor(self) -> None:
        if self._cursor_applied and self._viewer is not None and hasattr(self._viewer, "unsetCursor"):
            self._viewer.unsetCursor()
        self._cursor_applied = False

    def handle_mouse_press(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if not self._active:
            return False
        if event.button() == Qt.MouseButton.LeftButton:
            position = event.position().toPoint()
            if image_rect.contains(position) and image_rect.width() > 0 and image_rect.height() > 0:
                x = (position.x() - image_rect.x()) / image_rect.width()
                y = (position.y() - image_rect.y()) / image_rect.height()
                self.picked.emit(x, y)
        return True

    def handle_mouse_move(self, event: QMouseEvent, image_rect: QRect) -> bool:
        return self._active

    def handle_mouse_release(self, event: QMouseEvent, image_rect: QRect) -> bool:
        return self._active
