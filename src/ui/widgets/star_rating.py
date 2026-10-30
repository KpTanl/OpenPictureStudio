"""评分：五颗星。点第 N 颗评为 N 星，再点当前的星级清除；悬停时预览。

自绘，取值在绘制时读取当前主题。用户操作发出 ``rating_edited``；``set_rating()`` 回填不发出。
"""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from resources.styles.theme_variables import SPACING_XS, px
from ..theme import get_theme_tokens, to_qcolor

MAX_RATING = 5
_STAR_SIZE = 20


def _star_path(bounds: QRectF) -> QPainterPath:
    center = bounds.center()
    outer = bounds.width() / 2.0
    inner = outer * 0.42
    path = QPainterPath()
    for index in range(10):
        radius = outer if index % 2 == 0 else inner
        angle = -math.pi / 2 + index * math.pi / 5
        point = QPointF(center.x() + radius * math.cos(angle), center.y() + radius * math.sin(angle))
        if index == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    return path


class StarRating(QWidget):
    rating_edited = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._rating = 0
        self._hover = 0
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setMouseTracking(True)

    def rating(self) -> int:
        return self._rating

    def set_rating(self, rating: int) -> None:
        rating = max(0, min(MAX_RATING, int(rating)))
        if rating != self._rating:
            self._rating = rating
            self.update()

    def _edit(self, rating: int) -> None:
        rating = max(0, min(MAX_RATING, rating))
        if rating != self._rating:
            self._rating = rating
            self.update()
            self.rating_edited.emit(rating)

    def _step(self) -> int:
        return _STAR_SIZE + px(SPACING_XS)

    def _star_at(self, x: float) -> int:
        return max(1, min(MAX_RATING, int(x // self._step()) + 1))

    def sizeHint(self) -> QSize:
        return QSize(MAX_RATING * self._step() - px(SPACING_XS), _STAR_SIZE)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        hover = self._star_at(event.position().x())
        if hover != self._hover:
            self._hover = hover
            self.update()

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = 0
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            star = self._star_at(event.position().x())
            self._edit(0 if star == self._rating else star)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Up):
            self._edit(self._rating + 1)
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Down):
            self._edit(self._rating - 1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def focusInEvent(self, event) -> None:
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.update()
        super().focusOutEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tokens = get_theme_tokens()
        enabled = self.isEnabled()
        shown = self._hover if self._hover and enabled else self._rating
        filled = to_qcolor(tokens["rating" if enabled else "text_disabled"])
        outline = to_qcolor(tokens["accent" if self.hasFocus() else "text_caption" if enabled else "text_disabled"])
        for index in range(MAX_RATING):
            bounds = QRectF(index * self._step(), 0, _STAR_SIZE, _STAR_SIZE).adjusted(1.5, 1.5, -1.5, -1.5)
            path = _star_path(bounds)
            if index < shown:
                painter.setPen(QPen(filled, 1.4))
                painter.setBrush(filled)
            else:
                painter.setPen(QPen(outline, 1.4))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        painter.end()
