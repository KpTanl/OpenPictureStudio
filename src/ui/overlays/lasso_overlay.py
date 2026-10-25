"""Lasso overlay for interactive polygonal and freehand selection on ImageViewer."""

from typing import Any, Dict, List, Optional, Tuple
from PyQt6.QtCore import Qt, QRect
from PyQt6.QtGui import QPainter, QPen, QMouseEvent, QPainterPath
from resources.styles.semantic_colors import OVERLAY_COLORS
from ..theme import to_qcolor
from .base_overlay import ViewerOverlay


class LassoOverlay(ViewerOverlay):
    """画在 ImageViewer 上的套索选区多边形绘制覆盖层。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # [(x, y), ...] in 0.0 ~ 1.0 normalized image coordinates
        self.draft_points: List[Tuple[float, float]] = []
        self.regions: List[Dict[str, Any]] = []
        self._current_mouse_pos: Optional[Tuple[float, float]] = None

    @property
    def is_closed(self) -> bool:
        return len(self.regions) > 0

    def add_point(self, x: float, y: float) -> None:
        self.draft_points.append((x, y))

    def close_selection(self) -> bool:
        """把正在画的轮廓闭合成一个区域；不足三个点时不动，返回是否闭合了。"""
        if len(self.draft_points) < 3:
            return False
        self.regions.append({"points": list(self.draft_points), "op": "add"})
        self.draft_points.clear()
        self._current_mouse_pos = None
        self.geometry_changed.emit()
        self.update_view()
        return True

    def clear(self) -> None:
        self.draft_points.clear()
        self.regions.clear()
        self.geometry_changed.emit()

    clear_selection = clear

    def get_pixel_regions(self, iw: int, ih: int) -> List[Dict[str, Any]]:
        result = []
        for reg in self.regions:
            pts = reg.get("points", [])
            pix_pts = [(int(x * iw), int(y * ih)) for x, y in pts]
            result.append({"points": pix_pts, "op": reg.get("op", "add")})
        if len(self.draft_points) >= 3:
            pix_pts = [(int(x * iw), int(y * ih)) for x, y in self.draft_points]
            result.append({"points": pix_pts, "op": "add"})
        return result

    def paint(self, painter: QPainter, image_rect: QRect) -> None:
        if image_rect.width() <= 0 or image_rect.height() <= 0:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 1. 绘制已闭合区域
        for reg in self.regions:
            pts = reg.get("points", [])
            if len(pts) < 3:
                continue
            path = QPainterPath()
            sx = image_rect.left() + int(pts[0][0] * image_rect.width())
            sy = image_rect.top() + int(pts[0][1] * image_rect.height())
            path.moveTo(sx, sy)
            for x, y in pts[1:]:
                path.lineTo(image_rect.left() + int(x * image_rect.width()), image_rect.top() + int(y * image_rect.height()))
            path.closeSubpath()

            painter.fillPath(path, to_qcolor(OVERLAY_COLORS["selection_fill"]))
            painter.setPen(QPen(to_qcolor(OVERLAY_COLORS["selection_stroke"]), 2, Qt.PenStyle.DashLine))
            painter.drawPath(path)

        # 2. 绘制当前绘制中的多边形
        if self.draft_points:
            draft_pen = QPen(to_qcolor(OVERLAY_COLORS["draft_stroke"]), 2)
            painter.setPen(draft_pen)

            prev_pt = None
            for idx, (x, y) in enumerate(self.draft_points):
                px = image_rect.left() + int(x * image_rect.width())
                py = image_rect.top() + int(y * image_rect.height())
                # 绘制顶点节点
                painter.setBrush(to_qcolor(OVERLAY_COLORS["handle"]))
                painter.drawEllipse(px - 3, py - 3, 6, 6)

                if prev_pt is not None:
                    painter.drawLine(prev_pt[0], prev_pt[1], px, py)
                prev_pt = (px, py)

            # 绘制连接到鼠标当前位置的辅助线
            if self._current_mouse_pos is not None and prev_pt is not None:
                mx = image_rect.left() + int(self._current_mouse_pos[0] * image_rect.width())
                my = image_rect.top() + int(self._current_mouse_pos[1] * image_rect.height())
                painter.setPen(QPen(to_qcolor(OVERLAY_COLORS["draft_hint"]), 1, Qt.PenStyle.DashLine))
                painter.drawLine(prev_pt[0], prev_pt[1], mx, my)

        painter.restore()

    def handle_mouse_press(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if image_rect.width() <= 0 or image_rect.height() <= 0:
            return False

        if event.button() == Qt.MouseButton.LeftButton:
            x_frac = (event.pos().x() - image_rect.left()) / float(image_rect.width())
            y_frac = (event.pos().y() - image_rect.top()) / float(image_rect.height())
            x_frac = max(0.0, min(1.0, x_frac))
            y_frac = max(0.0, min(1.0, y_frac))

            # 若靠近起点（容差 12 像素），则闭合多边形
            if len(self.draft_points) >= 3:
                start_x = image_rect.left() + int(self.draft_points[0][0] * image_rect.width())
                start_y = image_rect.top() + int(self.draft_points[0][1] * image_rect.height())
                if abs(event.pos().x() - start_x) <= 12 and abs(event.pos().y() - start_y) <= 12:
                    return self.close_selection()

            self.draft_points.append((x_frac, y_frac))
            self.geometry_changed.emit()
            self.update_view()
            return True

        elif event.button() == Qt.MouseButton.RightButton:
            # 右键闭合当前多边形
            return self.close_selection()

        return False

    def handle_key_press(self, key: int) -> bool:
        """退格删掉正在画的轮廓的上一个点，Esc 放弃整条轮廓；都不碰已经闭合的区域。"""
        if not self.draft_points or key not in (Qt.Key.Key_Backspace, Qt.Key.Key_Escape):
            return False
        if key == Qt.Key.Key_Backspace:
            self.draft_points.pop()
        else:
            self.draft_points.clear()
        if not self.draft_points:
            self._current_mouse_pos = None
        self.geometry_changed.emit()
        self.update_view()
        return True

    def handle_mouse_move(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if image_rect.width() <= 0 or image_rect.height() <= 0:
            return False

        if self.draft_points:
            x_frac = (event.pos().x() - image_rect.left()) / float(image_rect.width())
            y_frac = (event.pos().y() - image_rect.top()) / float(image_rect.height())
            self._current_mouse_pos = (max(0.0, min(1.0, x_frac)), max(0.0, min(1.0, y_frac)))
            self.update_view()
            return True
        return False
