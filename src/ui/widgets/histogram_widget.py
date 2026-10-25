"""直方图：亮度主体层叠加红、绿、蓝三个通道，带四分位参考线与两端的裁切指示。只读显示。

底色、参考线与亮度层在绘制时读取当前主题；三个通道的颜色表示数据含义，取自语义颜色模块。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QPainter, QColor, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget, QSizePolicy

from resources.styles.semantic_colors import CHANNEL_COLORS
from resources.styles.theme_variables import RADIUS_SMALL, px
from ..theme import get_theme_tokens, to_qcolor

_BINS = 256
_HEIGHT = 88
# 各层的不透明度：亮度层在下，三个通道叠在上面
_LUMA_ALPHA = 0.50
_CHANNEL_ALPHA = 0.40
# 单个通道在最暗或最亮一格的像素占比超过它即视为裁切
_CLIP_SHARE = 0.005
_CLIP_SIZE = 8
_CLIP_MARGIN = 3


def _smooth(hist, kernel: int = 5) -> list[float]:
    """箱式平滑，让相邻两格过渡更柔和。"""
    half = kernel // 2
    count = len(hist)
    out = []
    for index in range(count):
        window = hist[max(0, index - half):min(count, index + half + 1)]
        out.append(sum(window) / len(window))
    return out


class HistogramWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._layers: dict[str, list[float]] = {}
        self._clipped = ((False, False, False), (False, False, False))
        self._display_cap = 0.0

    def has_data(self) -> bool:
        return bool(self._layers)

    def set_histogram_data(self, data: dict) -> None:
        """data 含 r、g、b、luma 四组 256 格的计数。"""
        channels = {name: list(data.get(name) or [0] * _BINS) for name in ("luma", "r", "g", "b")}
        self._layers = {name: _smooth(hist) for name, hist in channels.items()}
        total = max(1, sum(channels["r"]))
        self._clipped = tuple(tuple(channels[name][edge] > total * _CLIP_SHARE for name in ("r", "g", "b"))
                              for edge in (0, _BINS - 1))
        self._update_display_cap()
        self.update()

    def clear(self) -> None:
        """没有数据：只留底色与参考线。"""
        self._layers = {}
        self._clipped = ((False, False, False), (False, False, False))
        self._display_cap = 0.0
        self.update()

    def _graph_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

    def _update_display_cap(self) -> None:
        """纵向的满刻度取靠前但不是最高的峰值，压住离群的尖峰；数据变化时缓慢跟随，避免小幅调整时画面跳动。"""
        values = sorted((value for layer in self._layers.values() for value in layer), reverse=True)
        target = max(1.0, values[int(len(values) * 0.006)]) if values else 1.0
        if self._display_cap <= 0:
            self._display_cap = target
            return
        factor = 0.20 if target > self._display_cap else 0.08
        self._display_cap = self._display_cap * (1.0 - factor) + target * factor

    def _draw_layer(self, painter: QPainter, rect: QRectF, hist: list[float], color: QColor, scale: float) -> None:
        step = rect.width() / (_BINS - 1)
        cap = max(1.0, self._display_cap)
        points = [QPointF(rect.left(), rect.bottom())]
        for index, count in enumerate(hist):
            height = min(1.0, max(0.0, count / cap)) ** 0.62 * rect.height() * scale
            points.append(QPointF(rect.left() + index * step, rect.bottom() - height))
        points.append(QPointF(rect.right(), rect.bottom()))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF(points))

    def _draw_clipping(self, painter: QPainter, rect: QRectF, idle: QColor) -> None:
        """左上角是暗部裁切，右上角是亮部裁切；颜色是被裁通道叠加后的颜色，没有裁切时是不起眼的底色。"""
        painter.setPen(Qt.PenStyle.NoPen)
        corners = ((rect.left() + _CLIP_MARGIN, 1), (rect.right() - _CLIP_MARGIN, -1))
        for (x, direction), clipped in zip(corners, self._clipped):
            painter.setBrush(QColor(*(255 if flag else 30 for flag in clipped)) if any(clipped) else idle)
            top = rect.top() + _CLIP_MARGIN
            painter.drawPolygon(QPolygonF([QPointF(x, top), QPointF(x + direction * _CLIP_SIZE, top),
                                           QPointF(x, top + _CLIP_SIZE)]))

    def paintEvent(self, event):
        tokens = get_theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._graph_rect()
        radius = px(RADIUS_SMALL)
        painter.setPen(QPen(to_qcolor(tokens["hairline"]), 1.0))
        painter.setBrush(to_qcolor(tokens["well"]))
        painter.drawRoundedRect(rect, radius, radius)
        for quarter in (0.25, 0.5, 0.75):
            x = rect.left() + rect.width() * quarter
            painter.drawLine(QPointF(x, rect.top() + 1), QPointF(x, rect.bottom() - 1))

        if self._layers:
            inner = rect.adjusted(1, 1, -1, 0)
            painter.setClipRect(inner)
            luma = to_qcolor(tokens["text_secondary"])
            luma.setAlphaF(_LUMA_ALPHA)
            self._draw_layer(painter, inner, self._layers["luma"], luma, 0.96)
            for name in ("r", "g", "b"):
                color = to_qcolor(CHANNEL_COLORS[name])
                color.setAlphaF(_CHANNEL_ALPHA)
                self._draw_layer(painter, inner, self._layers[name], color, 0.92)
            painter.setClipping(False)
        self._draw_clipping(painter, rect, to_qcolor(tokens["track"]))
        painter.end()
