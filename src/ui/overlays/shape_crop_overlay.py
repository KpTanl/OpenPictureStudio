"""Shape crop overlay for interactive geometric shape cropping on ImageViewer."""

from typing import Dict, Optional
from PyQt6.QtCore import Qt, QRect, QPoint
from PyQt6.QtGui import QPainter, QPen, QMouseEvent, QPainterPath
from resources.styles.semantic_colors import OVERLAY_COLORS
from ..theme import to_qcolor
from .base_overlay import ViewerOverlay


class ShapeCropOverlay(ViewerOverlay):
    """画在 ImageViewer 上的几何形状遮罩裁剪覆盖层。"""

    HANDLE_SIZE = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        # [x, y, w, h] in 0.0~1.0 normalized image coordinates
        self.norm_rect = [0.15, 0.15, 0.7, 0.7]
        self.shape_type = "circle"  # "circle", "ellipse", "round_rect", "rect"
        self.corner_radius = 20.0
        self._drag_mode: Optional[str] = None
        self._drag_start_pos = QPoint()
        self._drag_start_rect = [0.15, 0.15, 0.7, 0.7]

    def set_shape_type(self, stype: str) -> None:
        self.shape_type = stype
        self.geometry_changed.emit()

    def set_corner_radius(self, r: float) -> None:
        self.corner_radius = r
        self.geometry_changed.emit()

    def is_dragging(self) -> bool:
        return self._drag_mode is not None

    def cancel_interaction(self) -> None:
        if self._drag_mode is not None:
            self._drag_mode = None
            self.drag_finished.emit()

    def get_selection_rect(self, image_rect: QRect) -> QRect:
        x, y, w, h = self.norm_rect
        px = image_rect.left() + int(x * image_rect.width())
        py = image_rect.top() + int(y * image_rect.height())
        pw = int(w * image_rect.width())
        ph = int(h * image_rect.height())
        return QRect(px, py, max(10, pw), max(10, ph))

    def paint(self, painter: QPainter, image_rect: QRect) -> None:
        if image_rect.width() <= 0 or image_rect.height() <= 0:
            return

        sel = self.get_selection_rect(image_rect)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 1. 构建形状路径
        shape_path = QPainterPath()
        if self.shape_type == "circle":
            side = min(sel.width(), sel.height())
            cx = sel.left() + sel.width() / 2.0
            cy = sel.top() + sel.height() / 2.0
            shape_path.addEllipse(cx - side / 2.0, cy - side / 2.0, side, side)
        elif self.shape_type == "ellipse":
            shape_path.addEllipse(sel.left(), sel.top(), sel.width(), sel.height())
        elif self.shape_type == "round_rect":
            r = min(self.corner_radius, sel.width() / 2.0, sel.height() / 2.0)
            shape_path.addRoundedRect(sel.left(), sel.top(), sel.width(), sel.height(), r, r)
        else:
            shape_path.addRect(sel.left(), sel.top(), sel.width(), sel.height())

        # 2. 全图暗色遮罩扣去形状区域
        full_path = QPainterPath()
        full_path.addRect(image_rect.left(), image_rect.top(), image_rect.width(), image_rect.height())
        mask_path = full_path.subtracted(shape_path)

        painter.fillPath(mask_path, to_qcolor(OVERLAY_COLORS["scrim"]))

        # 3. 形状高亮边框
        pen = QPen(to_qcolor(OVERLAY_COLORS["frame"]), 2)
        painter.setPen(pen)
        painter.drawPath(shape_path)

        # 4. 8 个外框调整手柄
        hs = self.HANDLE_SIZE
        painter.setPen(QPen(to_qcolor(OVERLAY_COLORS["handle"]), 1))
        painter.setBrush(to_qcolor(OVERLAY_COLORS["handle"]))
        handles = self._get_handles(sel)
        for pt in handles.values():
            painter.drawRect(pt.x() - hs // 2, pt.y() - hs // 2, hs, hs)

        painter.restore()

    def _get_handles(self, sel: QRect) -> Dict[str, QPoint]:
        cx = sel.left() + sel.width() // 2
        cy = sel.top() + sel.height() // 2
        return {
            "nw": QPoint(sel.left(), sel.top()),
            "n": QPoint(cx, sel.top()),
            "ne": QPoint(sel.right(), sel.top()),
            "e": QPoint(sel.right(), cy),
            "se": QPoint(sel.right(), sel.bottom()),
            "s": QPoint(cx, sel.bottom()),
            "sw": QPoint(sel.left(), sel.bottom()),
            "w": QPoint(sel.left(), cy),
        }

    def _hit_test(self, pos: QPoint, sel: QRect) -> Optional[str]:
        hs = self.HANDLE_SIZE + 4
        for name, pt in self._get_handles(sel).items():
            if QRect(pt.x() - hs // 2, pt.y() - hs // 2, hs, hs).contains(pos):
                return name
        if sel.contains(pos):
            return "move"
        return None

    def handle_mouse_press(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if event.button() != Qt.MouseButton.LeftButton or image_rect.width() <= 0:
            return False
        sel = self.get_selection_rect(image_rect)
        mode = self._hit_test(event.pos(), sel)
        if mode is not None:
            self._drag_mode = mode
            self._drag_start_pos = event.pos()
            self._drag_start_rect = list(self.norm_rect)
            self.drag_started.emit()
            return True
        return False

    def handle_mouse_move(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if image_rect.width() <= 0 or image_rect.height() <= 0:
            return False

        if self._drag_mode is None:
            sel = self.get_selection_rect(image_rect)
            mode = self._hit_test(event.pos(), sel)
            cursor_map = {
                "nw": Qt.CursorShape.SizeFDiagCursor,
                "se": Qt.CursorShape.SizeFDiagCursor,
                "ne": Qt.CursorShape.SizeBDiagCursor,
                "sw": Qt.CursorShape.SizeBDiagCursor,
                "n": Qt.CursorShape.SizeVerCursor,
                "s": Qt.CursorShape.SizeVerCursor,
                "w": Qt.CursorShape.SizeHorCursor,
                "e": Qt.CursorShape.SizeHorCursor,
                "move": Qt.CursorShape.SizeAllCursor,
            }
            if mode in cursor_map:
                self.set_cursor(cursor_map[mode])
            else:
                self.set_cursor(Qt.CursorShape.ArrowCursor)
            return False

        dx = (event.pos().x() - self._drag_start_pos.x()) / float(image_rect.width())
        dy = (event.pos().y() - self._drag_start_pos.y()) / float(image_rect.height())
        sx, sy, sw, sh = self._drag_start_rect

        min_s = 0.05
        if self._drag_mode == "move":
            nx = max(0.0, min(1.0 - sw, sx + dx))
            ny = max(0.0, min(1.0 - sh, sy + dy))
            self.norm_rect = [nx, ny, sw, sh]
        elif self._drag_mode == "se":
            nw = max(min_s, min(1.0 - sx, sw + dx))
            nh = max(min_s, min(1.0 - sy, sh + dy))
            self.norm_rect = [sx, sy, nw, nh]
        elif self._drag_mode == "nw":
            nx = max(0.0, min(sx + sw - min_s, sx + dx))
            ny = max(0.0, min(sy + sh - min_s, sy + dy))
            self.norm_rect = [nx, ny, sx + sw - nx, sy + sh - ny]
        elif self._drag_mode == "ne":
            ny = max(0.0, min(sy + sh - min_s, sy + dy))
            nw = max(min_s, min(1.0 - sx, sw + dx))
            self.norm_rect = [sx, ny, nw, sy + sh - ny]
        elif self._drag_mode == "sw":
            nx = max(0.0, min(sx + sw - min_s, sx + dx))
            nh = max(min_s, min(1.0 - sy, sh + dy))
            self.norm_rect = [nx, sy, sx + sw - nx, nh]
        elif self._drag_mode == "n":
            ny = max(0.0, min(sy + sh - min_s, sy + dy))
            self.norm_rect = [sx, ny, sw, sy + sh - ny]
        elif self._drag_mode == "s":
            nh = max(min_s, min(1.0 - sy, sh + dy))
            self.norm_rect = [sx, sy, sw, nh]
        elif self._drag_mode == "w":
            nx = max(0.0, min(sx + sw - min_s, sx + dx))
            self.norm_rect = [nx, sy, sx + sw - nx, sh]
        elif self._drag_mode == "e":
            nw = max(min_s, min(1.0 - sx, sw + dx))
            self.norm_rect = [sx, sy, nw, sh]

        self.geometry_changed.emit()
        self.update_view()
        return True

    def handle_mouse_release(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if self._drag_mode is not None:
            self._drag_mode = None
            self.set_cursor(Qt.CursorShape.ArrowCursor)
            self.drag_finished.emit()
            return True
        return False
