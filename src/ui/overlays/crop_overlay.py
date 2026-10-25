"""Crop overlay for interactive rectangular cropping on ImageViewer."""

from typing import Dict, Optional, Tuple
from PyQt6.QtCore import Qt, QRect, QPoint
from PyQt6.QtGui import QPainter, QPen, QMouseEvent
from resources.styles.semantic_colors import OVERLAY_COLORS
from ..theme import to_qcolor
from .base_overlay import ViewerOverlay


class CropOverlay(ViewerOverlay):
    """画在 ImageViewer 上的矩形裁剪选区覆盖层。

    采用归一化相对坐标系 (0.0 ~ 1.0)，在窗口缩放、自适应变换和高 DPI 缩放时
    永远与底层图像像素完全贴合，毫无错位。
    """

    HANDLE_SIZE = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        # [l, t, r, b] 归一化选区
        self._norm = [0.1, 0.1, 0.9, 0.9]
        self._aspect_ratio: Optional[float] = None
        self._drag_mode: Optional[str] = None
        self._drag_start_pos = QPoint()
        self._drag_start_norm = [0.1, 0.1, 0.9, 0.9]

    @property
    def norm_rect(self) -> list[float]:
        return list(self._norm)

    @property
    def aspect_ratio(self) -> Optional[float]:
        return self._aspect_ratio

    def restore_geometry(self, rect=None, aspect_ratio=None):
        self._norm = list(rect or (0, 0, 1, 1))
        self._aspect_ratio = aspect_ratio
        self.update_view()

    def set_aspect_ratio(self, ratio: Optional[float | str], image_width: int = 1, image_height: int = 1) -> None:
        """设置宽高比约束（如 1.0, '1:1', 16/9 等，None 为自由裁切）。"""
        if isinstance(ratio, str):
            if ":" in ratio:
                parts = ratio.split(":")
                try:
                    ratio = float(parts[0]) / float(parts[1])
                except (ValueError, ZeroDivisionError):
                    ratio = None
            else:
                try:
                    ratio = float(ratio)
                except ValueError:
                    ratio = None
        self._aspect_ratio = ratio
        if ratio and ratio > 0 and image_width > 0 and image_height > 0:
            # 调整归一化选区以满足宽高比
            l, t, r, b = self._norm
            cur_w = (r - l) * image_width
            cur_h = (b - t) * image_height

            target_w = cur_h * ratio
            if target_w <= (r - l) * image_width:
                cx = (l + r) / 2.0
                norm_half_w = (target_w / image_width) / 2.0
                self._norm[0] = max(0.0, cx - norm_half_w)
                self._norm[2] = min(1.0, cx + norm_half_w)
            else:
                target_h = cur_w / ratio
                cy = (t + b) / 2.0
                norm_half_h = (target_h / image_height) / 2.0
                self._norm[1] = max(0.0, cy - norm_half_h)
                self._norm[3] = min(1.0, cy + norm_half_h)

        self.geometry_changed.emit()

    def is_dragging(self) -> bool:
        return self._drag_mode is not None

    def cancel_interaction(self) -> None:
        if self._drag_mode is not None:
            self._drag_mode = None
            self.drag_finished.emit()

    def get_selection_rect(self, image_rect: QRect) -> QRect:
        """根据当前归一化选区与图像绘制区域计算视口像素矩形。"""
        l, t, r, b = self._norm
        x = image_rect.left() + int(l * image_rect.width())
        y = image_rect.top() + int(t * image_rect.height())
        w = int((r - l) * image_rect.width())
        h = int((b - t) * image_rect.height())
        return QRect(x, y, max(1, w), max(1, h))

    get_pixel_rect = get_selection_rect

    def get_crop_box(self, orig_w: int, orig_h: int) -> Tuple[int, int, int, int]:
        """获取在原图物理坐标系中的 (left, top, right, bottom)。"""
        l, t, r, b = self._norm
        x1 = max(0, min(orig_w - 1, int(round(l * orig_w))))
        y1 = max(0, min(orig_h - 1, int(round(t * orig_h))))
        x2 = max(x1 + 1, min(orig_w, int(round(r * orig_w))))
        y2 = max(y1 + 1, min(orig_h, int(round(b * orig_h))))
        return (x1, y1, x2, y2)

    def paint(self, painter: QPainter, image_rect: QRect) -> None:
        if image_rect.width() <= 0 or image_rect.height() <= 0:
            return

        sel = self.get_selection_rect(image_rect)

        painter.save()
        # 1. 绘制暗色半透明遮罩
        mask_color = to_qcolor(OVERLAY_COLORS["scrim"])
        painter.fillRect(image_rect.left(), image_rect.top(), image_rect.width(), sel.top() - image_rect.top(), mask_color)
        painter.fillRect(image_rect.left(), sel.bottom() + 1, image_rect.width(), image_rect.bottom() - sel.bottom(), mask_color)
        painter.fillRect(image_rect.left(), sel.top(), sel.left() - image_rect.left(), sel.height(), mask_color)
        painter.fillRect(sel.right() + 1, sel.top(), image_rect.right() - sel.right(), sel.height(), mask_color)

        # 2. 九宫格构图辅助线
        guide_pen = QPen(to_qcolor(OVERLAY_COLORS["guide"]), 1, Qt.PenStyle.DashLine)
        painter.setPen(guide_pen)
        x_step = sel.width() / 3.0
        y_step = sel.height() / 3.0
        painter.drawLine(int(sel.left() + x_step), sel.top(), int(sel.left() + x_step), sel.bottom())
        painter.drawLine(int(sel.left() + x_step * 2), sel.top(), int(sel.left() + x_step * 2), sel.bottom())
        painter.drawLine(sel.left(), int(sel.top() + y_step), sel.right(), int(sel.top() + y_step))
        painter.drawLine(sel.left(), int(sel.top() + y_step * 2), sel.right(), int(sel.top() + y_step * 2))

        # 3. 选框亮白边框
        border_pen = QPen(to_qcolor(OVERLAY_COLORS["frame"]), 2)
        painter.setPen(border_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(sel)

        # 4. 8 个调节手柄
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
            self._drag_start_norm = list(self._norm)
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

        # 计算增量
        dx = (event.pos().x() - self._drag_start_pos.x()) / float(image_rect.width())
        dy = (event.pos().y() - self._drag_start_pos.y()) / float(image_rect.height())
        sl, st, sr, sb = self._drag_start_norm

        min_span = 0.05
        if self._drag_mode == "move":
            w = sr - sl
            h = sb - st
            nl = max(0.0, min(1.0 - w, sl + dx))
            nt = max(0.0, min(1.0 - h, st + dy))
            self._norm = [nl, nt, nl + w, nt + h]
        elif self._drag_mode == "se":
            self._norm[2] = max(sl + min_span, min(1.0, sr + dx))
            self._norm[3] = max(st + min_span, min(1.0, sb + dy))
        elif self._drag_mode == "nw":
            self._norm[0] = min(sr - min_span, max(0.0, sl + dx))
            self._norm[1] = min(sb - min_span, max(0.0, st + dy))
        elif self._drag_mode == "ne":
            self._norm[2] = max(sl + min_span, min(1.0, sr + dx))
            self._norm[1] = min(sb - min_span, max(0.0, st + dy))
        elif self._drag_mode == "sw":
            self._norm[0] = min(sr - min_span, max(0.0, sl + dx))
            self._norm[3] = max(st + min_span, min(1.0, sb + dy))
        elif self._drag_mode == "e":
            self._norm[2] = max(sl + min_span, min(1.0, sr + dx))
        elif self._drag_mode == "w":
            self._norm[0] = min(sr - min_span, max(0.0, sl + dx))
        elif self._drag_mode == "s":
            self._norm[3] = max(st + min_span, min(1.0, sb + dy))
        elif self._drag_mode == "n":
            self._norm[1] = min(sb - min_span, max(0.0, st + dy))

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
