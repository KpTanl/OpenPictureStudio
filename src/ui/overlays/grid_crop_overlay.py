"""Grid crop overlay for interactive multi-grid cropping on ImageViewer."""

from typing import List, Optional, Tuple
from PySide6.QtCore import Qt, QRect, QPoint
from PySide6.QtGui import QPainter, QPen, QMouseEvent
from resources.styles.semantic_colors import OVERLAY_COLORS
from ..theme import to_qcolor
from .base_overlay import ViewerOverlay


class GridCropOverlay(ViewerOverlay):
    """画在 ImageViewer 上的九宫格/多格切图辅助线覆盖层。"""

    GRAB_TOL = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.h_lines: List[float] = [1.0 / 3.0, 2.0 / 3.0]
        self.v_lines: List[float] = [1.0 / 3.0, 2.0 / 3.0]
        self._dragging_line: Optional[Tuple[str, int]] = None

    def split_evenly(self, rows: int, cols: int) -> None:
        """平均分割为 rows × cols。"""
        self.restore_geometry(rows, cols)
        self.geometry_changed.emit()

    def restore_geometry(self, rows: int, cols: int, h_lines=None, v_lines=None) -> None:
        rows = max(1, min(10, rows))
        cols = max(1, min(10, cols))
        self.h_lines = [i / float(rows) for i in range(1, rows)] if h_lines is None else list(h_lines)
        self.v_lines = [j / float(cols) for j in range(1, cols)] if v_lines is None else list(v_lines)

    def set_grid(self, rows: int, cols: int) -> None:
        self.split_evenly(rows, cols)

    @property
    def row_splits(self) -> List[float]:
        return self.h_lines

    @property
    def col_splits(self) -> List[float]:
        return self.v_lines

    def get_grid_regions(self, width: int, height: int) -> List[QRect]:
        h_stops = [0] + [int(f * height) for f in self.h_lines] + [height]
        v_stops = [0] + [int(f * width) for f in self.v_lines] + [width]
        regions = []
        for i in range(len(h_stops) - 1):
            for j in range(len(v_stops) - 1):
                regions.append(QRect(v_stops[j], h_stops[i], v_stops[j+1] - v_stops[j], h_stops[i+1] - h_stops[i]))
        return regions

    def paint(self, painter: QPainter, image_rect: QRect) -> None:
        if image_rect.width() <= 0 or image_rect.height() <= 0:
            return

        painter.save()
        pen = QPen(to_qcolor(OVERLAY_COLORS["grid_line"]), 2, Qt.PenStyle.DashLine)
        painter.setPen(pen)

        # 绘制横线
        for frac in self.h_lines:
            y = image_rect.top() + int(frac * image_rect.height())
            painter.drawLine(image_rect.left(), y, image_rect.right(), y)

        # 绘制竖线
        for frac in self.v_lines:
            x = image_rect.left() + int(frac * image_rect.width())
            painter.drawLine(x, image_rect.top(), x, image_rect.bottom())

        # 绘制外边框
        outer_pen = QPen(to_qcolor(OVERLAY_COLORS["grid_frame"]), 2)
        painter.setPen(outer_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(image_rect)

        painter.restore()

    def _hit_test(self, pos: QPoint, image_rect: QRect) -> Optional[Tuple[str, int]]:
        for idx, frac in enumerate(self.h_lines):
            y = image_rect.top() + int(frac * image_rect.height())
            if abs(pos.y() - y) <= self.GRAB_TOL and image_rect.left() <= pos.x() <= image_rect.right():
                return ("h", idx)
        for idx, frac in enumerate(self.v_lines):
            x = image_rect.left() + int(frac * image_rect.width())
            if abs(pos.x() - x) <= self.GRAB_TOL and image_rect.top() <= pos.y() <= image_rect.bottom():
                return ("v", idx)
        return None

    def handle_mouse_press(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if event.button() != Qt.MouseButton.LeftButton or image_rect.width() <= 0:
            return False
        hit = self._hit_test(event.pos(), image_rect)
        if hit is not None:
            self._dragging_line = hit
            return True
        return False

    def handle_mouse_move(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if image_rect.width() <= 0 or image_rect.height() <= 0:
            return False

        if self._dragging_line is None:
            hit = self._hit_test(event.pos(), image_rect)
            if hit is not None:
                self.set_cursor(Qt.CursorShape.SizeVerCursor if hit[0] == "h" else Qt.CursorShape.SizeHorCursor)
            else:
                self.set_cursor(Qt.CursorShape.ArrowCursor)
            return False

        direction, idx = self._dragging_line
        if direction == "h":
            frac = (event.pos().y() - image_rect.top()) / float(image_rect.height())
            self.h_lines[idx] = max(0.05, min(0.95, frac))
        else:
            frac = (event.pos().x() - image_rect.left()) / float(image_rect.width())
            self.v_lines[idx] = max(0.05, min(0.95, frac))

        self.geometry_changed.emit()
        self.update_view()
        return True

    def handle_mouse_release(self, event: QMouseEvent, image_rect: QRect) -> bool:
        if self._dragging_line is not None:
            self._dragging_line = None
            self.set_cursor(Qt.CursorShape.ArrowCursor)
            return True
        return False
