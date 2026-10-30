"""滑块：细滑轨加圆钮，自绘，取值来自当前主题。

- 填充可从任意原点开始，双向参数从中点向两侧填充
- 可指定带颜色的渐变滑轨（如色温、饱和度），此时不画填充
- 点滑轨跳到该处并可继续拖动；双击圆钮恢复默认值
- 只有用户操作才发出 valueEdited，程序赋值不触发
"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QKeyEvent, QLinearGradient, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QSlider, QWidget

from ..theme import get_theme_tokens, to_qcolor
from .value_editing import ValueEditMixin

_KNOB = 16.0
_TRACK = 4.0
_HEIGHT = 20


class AppleSlider(QSlider, ValueEditMixin):
    """水平滑块。"""

    resetRequested = Signal()
    valueEdited = Signal(int)

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        parent: Optional[QWidget] = None,
        default_value: Optional[int] = None,
    ):
        super().__init__(orientation, parent)
        self.setObjectName("appleSlider")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._default_value = default_value
        self._fill_origin: Optional[int] = None
        self._track_colors: tuple[str, ...] = ()
        self._drag_offset = 0.0

    @property
    def default_value(self) -> Optional[int]:
        return self._default_value

    @default_value.setter
    def default_value(self, val: Optional[int]) -> None:
        self._default_value = val

    def set_fill_origin(self, value: Optional[int]) -> None:
        """填充的起点；None 表示从最小值一端开始。"""
        self._fill_origin = value
        self.update()

    def set_track_colors(self, colors: Sequence[str] = ()) -> None:
        """渐变滑轨的色标，自左向右均匀分布；为空时用主题的滑轨色并画填充。"""
        self._track_colors = tuple(colors)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(120, _HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return QSize(int(_KNOB) * 3, _HEIGHT)

    def _position(self, value: int) -> float:
        span = max(1.0, self.width() - _KNOB)
        extent = self.maximum() - self.minimum()
        ratio = (value - self.minimum()) / extent if extent > 0 else 0.0
        return _KNOB / 2 + span * (1.0 - ratio if self.invertedAppearance() else ratio)

    def _handle_rect(self) -> QRectF:
        center = self._position(self.sliderPosition())
        return QRectF(center - _KNOB / 2, self.height() / 2 - _KNOB / 2, _KNOB, _KNOB)

    def _value_at(self, x: float) -> int:
        span = max(1.0, self.width() - _KNOB)
        ratio = min(1.0, max(0.0, (x - _KNOB / 2) / span))
        if self.invertedAppearance():
            ratio = 1.0 - ratio
        return self.minimum() + round(ratio * (self.maximum() - self.minimum()))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        previous = self.value()
        x = event.position().x()
        handle = self._handle_rect()
        on_handle = handle.adjusted(-2, -2, 2, 2).contains(event.position())
        self._drag_offset = x - handle.center().x() if on_handle else 0.0
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.setSliderDown(True)
        if not on_handle:
            self.setSliderPosition(self._value_at(x))
        event.accept()
        self._emit_value_edited(previous)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not (self.isSliderDown() and event.buttons() & Qt.MouseButton.LeftButton):
            event.ignore()
            return
        previous = self.value()
        self.setSliderPosition(self._value_at(event.position().x() - self._drag_offset))
        event.accept()
        self._emit_value_edited(previous)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            event.accept()
        else:
            event.ignore()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if (event.button() == Qt.MouseButton.LeftButton and self._default_value is not None
                and self._handle_rect().adjusted(-2, -2, 2, 2).contains(event.position())):
            self.edit_value(self._default_value)
            self.resetRequested.emit()
            event.accept()
            return
        self.mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        previous = self.value()
        super().keyPressEvent(event)
        self._emit_value_edited(previous)

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()

    def event(self, event):
        if (event.type() == QEvent.Type.ShortcutOverride
                and event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier)
                and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
                                    Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Home, Qt.Key.Key_End)):
            event.accept()
            return True
        return super().event(event)

    def paintEvent(self, event) -> None:
        if self.orientation() != Qt.Orientation.Horizontal:
            super().paintEvent(event)
            return
        tokens = get_theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.4)

        middle = self.height() / 2
        track = QRectF(_KNOB / 2, middle - _TRACK / 2, max(1.0, self.width() - _KNOB), _TRACK)
        painter.setPen(Qt.PenStyle.NoPen)
        if self._track_colors:
            gradient = QLinearGradient(QPointF(track.left(), 0), QPointF(track.right(), 0))
            last = max(1, len(self._track_colors) - 1)
            for index, color in enumerate(self._track_colors):
                gradient.setColorAt(index / last, to_qcolor(color))
            painter.setBrush(gradient)
            painter.drawRoundedRect(track, _TRACK / 2, _TRACK / 2)
        else:
            painter.setBrush(to_qcolor(tokens["track"]))
            painter.drawRoundedRect(track, _TRACK / 2, _TRACK / 2)
            origin = self.minimum() if self._fill_origin is None else self._fill_origin
            start, stop = sorted((self._position(origin), self._position(self.sliderPosition())))
            if stop - start >= 0.5:
                painter.setBrush(to_qcolor(tokens["accent"]))
                painter.drawRoundedRect(QRectF(start, track.top(), stop - start, _TRACK), _TRACK / 2, _TRACK / 2)

        knob = self._handle_rect().adjusted(0.5, 0.5, -0.5, -0.5)
        if self.hasFocus():
            painter.setPen(QPen(to_qcolor(tokens["accent"]), 2.0))
        else:
            painter.setPen(QPen(to_qcolor(tokens["knob_border"]), 1.0))
        painter.setBrush(to_qcolor(tokens["knob_pressed" if self.isSliderDown() else "knob"]))
        painter.drawEllipse(knob)
        painter.end()
