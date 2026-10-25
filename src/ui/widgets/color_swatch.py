"""色块按钮：显示一种颜色，点击后由使用方去改它。

颜色是数据，由 ``set_color()`` 回填；描边与焦点色在绘制时取自当前主题。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from resources.styles.theme_variables import CONTROL_HEIGHT_COMPACT, RADIUS_SMALL, px
from ..theme import get_theme_tokens, to_qcolor

_WIDTH = 44


class ColorSwatch(QAbstractButton):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._color = (255, 255, 255)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def color(self) -> tuple[int, int, int]:
        return self._color

    def set_color(self, color) -> None:
        """程序回填，red、green、blue 各 0 到 255；不发出信号。"""
        color = tuple(int(channel) for channel in color)
        if color != self._color:
            self._color = color
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(_WIDTH, CONTROL_HEIGHT_COMPACT)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event) -> None:
        tokens = get_theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        focused = self.hasFocus()
        border = to_qcolor(tokens["accent" if focused else "hairline_solid"])
        painter.setPen(QPen(border, 2 if focused else 1))
        painter.setBrush(QColor(*self._color))
        if not self.isEnabled():
            painter.setOpacity(0.4)
        radius = px(RADIUS_SMALL)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), radius, radius)
        painter.end()
