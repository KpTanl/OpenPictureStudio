"""证件照的裁切框：锁在规格的宽高比上，框里画人像参考线。

坐标是相对画布上那幅画面（这一步的输入）的比例 [左, 上, 右, 下]。比例按画面的像素计算，拖动时由画面在画布上
的宽高换算，所以框在任何缩放下都保持规格的形状。框的位置只由面板经 ``set_frame()`` 回填；用户拖动时发出
``geometry_changed``，一次拖动的首尾由 ``drag_started``、``drag_finished`` 标出。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QMouseEvent, QPainter, QPainterPath, QPen

from resources.styles.semantic_colors import OVERLAY_COLORS
from ...core.tools.id_photo import PORTRAIT_GUIDE
from ..theme import to_qcolor
from .base_overlay import ViewerOverlay

# 框的高度至少是画面高度的这么多
_MIN_HEIGHT = 0.08


class IDPhotoOverlay(ViewerOverlay):
    HANDLE_SIZE = 9

    def __init__(self, parent=None):
        super().__init__(parent)
        self._norm = [0.0, 0.0, 1.0, 1.0]
        self._ratio = 1.0
        self._head_label = ""
        self._chin_label = ""
        self._drag_mode: Optional[str] = None
        self._drag_start = QPoint()
        self._drag_norm = list(self._norm)

    @property
    def norm_rect(self) -> list[float]:
        return list(self._norm)

    def set_frame(self, rect, ratio: float) -> None:
        """程序回填框的位置与规格的宽高比，不发出信号。"""
        self._norm = [float(value) for value in rect]
        self._ratio = float(ratio)
        self.update_view()

    def set_labels(self, head: str, chin: str) -> None:
        self._head_label, self._chin_label = head, chin
        self.update_view()

    def is_dragging(self) -> bool:
        return self._drag_mode is not None

    def cancel_interaction(self) -> None:
        if self._drag_mode is not None:
            self._drag_mode = None
            self.drag_finished.emit()

    def _selection(self, image_rect: QRect) -> QRectF:
        left, top, right, bottom = self._norm
        return QRectF(image_rect.left() + left * image_rect.width(), image_rect.top() + top * image_rect.height(),
                      (right - left) * image_rect.width(), (bottom - top) * image_rect.height())

    def _corners(self, selection: QRectF) -> dict[str, QPointF]:
        return {"nw": selection.topLeft(), "ne": selection.topRight(),
                "sw": selection.bottomLeft(), "se": selection.bottomRight()}

    def paint(self, painter: QPainter, image_rect: QRect) -> None:
        if image_rect.width() <= 0 or image_rect.height() <= 0:
            return
        selection = self._selection(image_rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 框外压暗：一整块带洞的遮罩，分块填会在拼缝处叠出细线
        scrim = QPainterPath()
        scrim.setFillRule(Qt.FillRule.OddEvenFill)
        scrim.addRect(QRectF(image_rect))
        scrim.addRect(selection)
        painter.fillPath(scrim, to_qcolor(OVERLAY_COLORS["scrim"]))

        # 头顶线、下巴线与头部范围
        head_top = selection.top() + selection.height() * PORTRAIT_GUIDE["head_top_ratio"]
        chin = selection.top() + selection.height() * PORTRAIT_GUIDE["chin_ratio"]
        head_width = selection.width() * PORTRAIT_GUIDE["head_width_ratio"]
        painter.setPen(QPen(to_qcolor(OVERLAY_COLORS["portrait_guide"]), 1.2, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(selection.left(), head_top), QPointF(selection.right(), head_top))
        painter.drawLine(QPointF(selection.left(), chin), QPointF(selection.right(), chin))
        painter.drawRect(QRectF(selection.center().x() - head_width / 2, head_top, head_width, chin - head_top))
        painter.setPen(to_qcolor(OVERLAY_COLORS["portrait_label"]))
        metrics = painter.fontMetrics()
        if self._head_label:
            painter.drawText(QPointF(selection.left() + 6, head_top - 4), self._head_label)
        if self._chin_label:
            painter.drawText(QPointF(selection.left() + 6, chin + metrics.ascent() + 2), self._chin_label)

        painter.setPen(QPen(to_qcolor(OVERLAY_COLORS["frame"]), 2))
        painter.drawRect(selection)
        size = self.HANDLE_SIZE
        painter.setPen(QPen(to_qcolor(OVERLAY_COLORS["handle"]), 1))
        painter.setBrush(to_qcolor(OVERLAY_COLORS["handle"]))
        for point in self._corners(selection).values():
            painter.drawRect(QRectF(point.x() - size / 2, point.y() - size / 2, size, size))
        painter.restore()

    def _hit_test(self, position: QPoint, selection: QRectF) -> Optional[str]:
        reach = self.HANDLE_SIZE + 4
        for name, point in self._corners(selection).items():
            if abs(position.x() - point.x()) <= reach / 2 + 1 and abs(position.y() - point.y()) <= reach / 2 + 1:
                return name
        return "move" if selection.contains(QPointF(position)) else None

    def handle_mouse_press(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if event.button() != Qt.MouseButton.LeftButton or image_rect.width() <= 0 or image_rect.height() <= 0:
            return False
        mode = self._hit_test(event.pos(), self._selection(image_rect))
        if mode is None:
            return False
        self._drag_mode = mode
        self._drag_start = event.pos()
        self._drag_norm = list(self._norm)
        self.drag_started.emit()
        return True

    def handle_mouse_move(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if image_rect.width() <= 0 or image_rect.height() <= 0:
            return False
        if self._drag_mode is None:
            mode = self._hit_test(event.pos(), self._selection(image_rect))
            cursors = {"nw": Qt.CursorShape.SizeFDiagCursor, "se": Qt.CursorShape.SizeFDiagCursor,
                       "ne": Qt.CursorShape.SizeBDiagCursor, "sw": Qt.CursorShape.SizeBDiagCursor,
                       "move": Qt.CursorShape.SizeAllCursor}
            self.set_cursor(cursors.get(mode, Qt.CursorShape.ArrowCursor))
            return False

        dx = (event.pos().x() - self._drag_start.x()) / image_rect.width()
        dy = (event.pos().y() - self._drag_start.y()) / image_rect.height()
        left, top, right, bottom = self._drag_norm
        if self._drag_mode == "move":
            width, height = right - left, bottom - top
            left = min(max(0.0, left + dx), 1.0 - width)
            top = min(max(0.0, top + dy), 1.0 - height)
            self._norm = [left, top, left + width, top + height]
        else:
            self._norm = self._resized(self._drag_mode, dx, dy, image_rect.width() / image_rect.height())
        self.geometry_changed.emit()
        self.update_view()
        return True

    def _resized(self, corner: str, dx: float, dy: float, image_aspect: float) -> list[float]:
        """拖一个角：对角不动，按规格的宽高比收放，收在画面以内，不小于最小高度。"""
        left, top, right, bottom = self._drag_norm
        # 框在比例坐标里的宽高比：像素宽高比除以画面的宽高比
        span = self._ratio / image_aspect
        anchor_x = right if corner in ("nw", "sw") else left
        anchor_y = bottom if corner in ("nw", "ne") else top
        width = (right - left) + (-dx if corner in ("nw", "sw") else dx)
        height = (bottom - top) + (-dy if corner in ("nw", "ne") else dy)
        height = max(height, width / span)
        room_x = anchor_x if corner in ("nw", "sw") else 1.0 - anchor_x
        room_y = anchor_y if corner in ("nw", "ne") else 1.0 - anchor_y
        height = max(_MIN_HEIGHT, min(height, room_y, room_x / span))
        width = height * span
        new_left = anchor_x - width if corner in ("nw", "sw") else anchor_x
        new_top = anchor_y - height if corner in ("nw", "ne") else anchor_y
        return [new_left, new_top, new_left + width, new_top + height]

    def handle_mouse_release(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if self._drag_mode is None:
            return False
        self._drag_mode = None
        self.set_cursor(Qt.CursorShape.ArrowCursor)
        self.drag_finished.emit()
        return True
