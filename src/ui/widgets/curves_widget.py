"""
交互式曲线编辑控件 — 支持 RGB / R / G / B 四通道的曲线调整。

用户可在 256×256 坐标系上点击添加控制点、拖拽移动、双击删除。
使用 Monotone Cubic Hermite Spline 生成平滑曲线。
底色、网格与总曲线在绘制时读取当前主题；红、绿、蓝三条曲线的颜色表示数据含义，取自语义颜色模块。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, Qt, QPointF, QRectF, Signal
from PySide6.QtGui import QPainter, QPen, QColor, QPainterPath, QFont
from PySide6.QtWidgets import QWidget, QSizePolicy

from ..theme import get_theme_tokens, to_qcolor
from resources.styles.semantic_colors import CHANNEL_COLORS
from resources.styles.theme_variables import FONT_FAMILIES, FONT_SIZE_CAPTION, RADIUS_SMALL, px

logger = logging.getLogger(__name__)

_HANDLE_RADIUS = 5
_HIT_RADIUS = 12
_HEIGHT = 180

CURVE_CHANNELS = ("rgb", "r", "g", "b")


class CurvesWidget(QWidget):
    """交互式曲线编辑组件。"""

    curves_changed = Signal()
    interaction_started = Signal()
    interaction_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(_HEIGHT)
        self.setMouseTracking(True)

        self._active_channel = "rgb"
        self._points: dict[str, list[tuple[int, int]]] = {
            channel: [(0, 0), (255, 255)] for channel in CURVE_CHANNELS}

        self._dragging_idx = -1
        self._hover_idx = -1

        self._margin = 8

    def get_curves(self) -> dict[str, list[tuple[int, int]]]:
        return {k: list(v) for k, v in self._points.items()}

    def is_interacting(self):
        return self._dragging_idx >= 0

    def _finish_interaction(self):
        if self._dragging_idx >= 0:
            self._dragging_idx = -1
            self.interaction_finished.emit()

    def event(self, event):
        if event.type() in (QEvent.Type.UngrabMouse, QEvent.Type.Hide, QEvent.Type.WindowDeactivate):
            self._finish_interaction()
        return super().event(event)

    def set_curves(self, curves: dict[str, list[tuple[int, int]]]) -> None:
        for ch in CURVE_CHANNELS:
            if ch in curves and curves[ch]:
                self._points[ch] = list(curves[ch])
            else:
                self._points[ch] = [(0, 0), (255, 255)]
        self.update()

    def active_channel(self) -> str:
        return self._active_channel

    def set_active_channel(self, channel: str) -> None:
        """切换正在编辑的那条曲线；这是界面状态，不改变任何曲线。"""
        if channel in CURVE_CHANNELS and channel != self._active_channel:
            self._finish_interaction()
            self._active_channel = channel
            self._hover_idx = -1
            self.update()

    def reset_channel(self, channel: str | None = None) -> None:
        ch = channel or self._active_channel
        self._points[ch] = [(0, 0), (255, 255)]
        self.update()
        self.curves_changed.emit()

    def reset_all(self) -> None:
        for ch in self._points:
            self._points[ch] = [(0, 0), (255, 255)]
        self.update()
        self.curves_changed.emit()

    def _graph_rect(self) -> QRectF:
        m = self._margin
        return QRectF(m, m, self.width() - 2 * m, self.height() - 2 * m)

    def _val_to_pos(self, x: int, y: int) -> QPointF:
        r = self._graph_rect()
        return QPointF(r.left() + x / 255.0 * r.width(), r.bottom() - y / 255.0 * r.height())

    def _pos_to_val(self, pos: QPointF) -> tuple[int, int]:
        r = self._graph_rect()
        x = int((pos.x() - r.left()) / r.width() * 255.0 + 0.5)
        y = int((r.bottom() - pos.y()) / r.height() * 255.0 + 0.5)
        return max(0, min(255, x)), max(0, min(255, y))

    def _find_point_at(self, pos: QPointF) -> int:
        pts = self._points[self._active_channel]
        for i, (x, y) in enumerate(pts):
            pp = self._val_to_pos(x, y)
            dx = pos.x() - pp.x()
            dy = pos.y() - pp.y()
            if dx * dx + dy * dy <= _HIT_RADIUS * _HIT_RADIUS:
                return i
        return -1

    @staticmethod
    def _channel_color(channel: str, tokens: dict) -> QColor:
        return to_qcolor(tokens["text_primary"] if channel == "rgb" else CHANNEL_COLORS[channel])

    def _draw_background(self, painter: QPainter, tokens: dict) -> None:
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = px(RADIUS_SMALL)
        hairline = to_qcolor(tokens["hairline"])
        painter.setPen(QPen(hairline, 1.0))
        painter.setBrush(to_qcolor(tokens["well"]))
        painter.drawRoundedRect(r, radius, radius)

        r = self._graph_rect()
        painter.setPen(QPen(hairline, 1, Qt.PenStyle.DotLine))
        for i in range(1, 4):
            frac = i / 4.0
            x = r.left() + frac * r.width()
            painter.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            y = r.bottom() - frac * r.height()
            painter.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

        painter.setPen(QPen(to_qcolor(tokens["text_caption"]), 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.top()))

    def _build_curve_path(self, channel: str) -> QPainterPath:
        from ...core.color_adjuster import ColorAdjuster

        pts = self._points[channel]
        if len(pts) < 2:
            return QPainterPath()

        lut = ColorAdjuster._build_curve_lut(pts)

        path = QPainterPath()
        first = self._val_to_pos(0, lut[0])
        path.moveTo(first)
        for i in range(1, 256):
            p = self._val_to_pos(i, lut[i])
            path.lineTo(p)
        return path

    def _draw_curve(self, painter: QPainter, channel: str, tokens: dict) -> None:
        is_active = channel == self._active_channel
        pen_color = self._channel_color(channel, tokens)
        pen_color.setAlphaF(1.0 if is_active else 0.35)
        pen = QPen(pen_color, 2.0 if is_active else 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        path = self._build_curve_path(channel)
        if not path.isEmpty():
            painter.drawPath(path)

    def _draw_points(self, painter: QPainter, tokens: dict) -> None:
        """控制点是面板底色的圆，描边是这条曲线的颜色；指着或拖着的那个填满曲线的颜色。"""
        pts = self._points[self._active_channel]
        color = self._channel_color(self._active_channel, tokens)
        surface = to_qcolor(tokens["surface"])

        for i, (x, y) in enumerate(pts):
            pos = self._val_to_pos(x, y)
            engaged = i == self._hover_idx or i == self._dragging_idx
            radius = _HANDLE_RADIUS + (1 if engaged else 0)
            painter.setPen(QPen(color, 2.0))
            painter.setBrush(color if engaged else surface)
            painter.drawEllipse(pos, radius, radius)

    def _draw_value_hint(self, painter: QPainter, tokens: dict) -> None:
        if self._dragging_idx < 0:
            return
        pts = self._points[self._active_channel]
        if self._dragging_idx >= len(pts):
            return
        x, y = pts[self._dragging_idx]
        pos = self._val_to_pos(x, y)

        font = QFont()
        font.setFamilies(list(FONT_FAMILIES))
        font.setPixelSize(px(FONT_SIZE_CAPTION))
        painter.setFont(font)
        painter.setPen(to_qcolor(tokens["text_primary"]))

        text = f"({x}, {y})"
        tr = self._graph_rect()
        tx = pos.x() + 10
        ty = pos.y() - 10
        if tx + 60 > tr.right():
            tx = pos.x() - 60
        if ty < tr.top():
            ty = pos.y() + 20

        painter.drawText(QPointF(tx, ty), text)

    def paintEvent(self, event):
        super().paintEvent(event)
        tokens = get_theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        self._draw_background(painter, tokens)

        for ch in ("r", "g", "b", "rgb"):
            if ch != self._active_channel:
                self._draw_curve(painter, ch, tokens)
        self._draw_curve(painter, self._active_channel, tokens)
        self._draw_points(painter, tokens)
        self._draw_value_hint(painter, tokens)

        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self._find_point_at(event.position())
            if idx >= 0:
                self._dragging_idx = idx
                self.interaction_started.emit()
            else:
                r = self._graph_rect()
                if r.contains(event.position()):
                    x, y = self._pos_to_val(event.position())
                    x = max(1, min(254, x))
                    pts = self._points[self._active_channel]
                    insert_idx = 1
                    for i in range(1, len(pts) - 1):
                        if pts[i][0] < x:
                            insert_idx = i + 1
                        else:
                            break
                    pts.insert(insert_idx, (x, y))
                    self._dragging_idx = insert_idx
                    self.interaction_started.emit()
                    self.update()
                    self.curves_changed.emit()

    def mouseMoveEvent(self, event):
        if not event.buttons() & Qt.MouseButton.LeftButton:
            self._finish_interaction()
        if self._dragging_idx >= 0:
            pts = self._points[self._active_channel]
            x, y = self._pos_to_val(event.position())
            is_endpoint = self._dragging_idx == 0 or self._dragging_idx == len(pts) - 1
            if is_endpoint:
                pts[self._dragging_idx] = (pts[self._dragging_idx][0], y)
            else:
                prev_x = pts[self._dragging_idx - 1][0] if self._dragging_idx > 0 else 0
                next_x = pts[self._dragging_idx + 1][0] if self._dragging_idx < len(pts) - 1 else 255
                x = max(prev_x + 1, min(next_x - 1, x))
                pts[self._dragging_idx] = (x, y)
            self.update()
            self.curves_changed.emit()
        else:
            idx = self._find_point_at(event.position())
            if idx != self._hover_idx:
                self._hover_idx = idx
                self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._finish_interaction()
            self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._finish_interaction()
            idx = self._find_point_at(event.position())
            pts = self._points[self._active_channel]
            is_endpoint = idx == 0 or idx == len(pts) - 1
            if idx >= 0 and not is_endpoint and len(pts) > 2:
                pts.pop(idx)
                self.update()
                self.curves_changed.emit()
