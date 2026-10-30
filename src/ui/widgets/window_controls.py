"""窗口按钮：最小化、最大化或还原、关闭，放在顶栏最右侧，代替系统标题栏上的那三个。

只发出请求信号，不操作窗口。最大化按钮上的鼠标消息归系统（悬停时才有分屏布局），它的悬停与按下由
``set_maximize_state()`` 给出；其余两个是普通按钮。图形自己画，颜色在绘制时读取当前主题。
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QSizePolicy, QWidget

from ..theme import get_theme_tokens, to_qcolor
from .tooltip import set_tooltip

BUTTON_WIDTH = 46
_GLYPH = 10
_RESTORE_OFFSET = 2
MINIMIZE, MAXIMIZE, RESTORE, CLOSE = "minimize", "maximize", "restore", "close"


class _ControlButton(QAbstractButton):
    def __init__(self, kind: str, parent: QWidget):
        super().__init__(parent)
        self._kind = kind
        self._external: Optional[tuple[bool, bool]] = None
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def kind(self) -> str:
        return self._kind

    def set_kind(self, kind: str) -> None:
        if kind != self._kind:
            self._kind = kind
            self.update()

    def set_external_state(self, hovered: bool, pressed: bool) -> None:
        """悬停与按下由外部给出，用于鼠标消息归系统的按钮。"""
        if (hovered, pressed) != self._external:
            self._external = (hovered, pressed)
            self.update()

    def state(self) -> tuple[bool, bool]:
        if self._external is not None and any(self._external):
            return self._external
        return self.underMouse() and self.isEnabled(), self.isDown()

    def sizeHint(self) -> QSize:
        return QSize(BUTTON_WIDTH, BUTTON_WIDTH)

    def paintEvent(self, event: QPaintEvent) -> None:
        tokens = get_theme_tokens()
        hovered, pressed = self.state()
        painter = QPainter(self)
        if hovered or pressed:
            if self._kind == CLOSE:
                background = tokens["danger_hover" if pressed else "danger"]
            else:
                background = tokens["track" if pressed else "control"]
            painter.fillRect(self.rect(), to_qcolor(background))
        if self._kind == CLOSE and (hovered or pressed):
            color = tokens["on_danger"]
        else:
            color = tokens["text_primary" if hovered or pressed else "text_secondary"]
        painter.setPen(QPen(to_qcolor(color), 1))
        # 图形的线落在像素中心，1 像素宽的线才不发虚
        left = (self.width() - _GLYPH) // 2 + 0.5
        top = (self.height() - _GLYPH) // 2 + 0.5
        if self._kind == MINIMIZE:
            y = top + _GLYPH // 2
            painter.drawLine(QPointF(left, y), QPointF(left + _GLYPH, y))
        elif self._kind == MAXIMIZE:
            painter.drawRect(QRectF(left, top, _GLYPH, _GLYPH))
        elif self._kind == RESTORE:
            side = _GLYPH - _RESTORE_OFFSET
            back = QRectF(left + _RESTORE_OFFSET, top, side, side)
            front = QRectF(left, top + _RESTORE_OFFSET, side, side)
            painter.drawLine(back.topLeft(), back.topRight())
            painter.drawLine(back.topRight(), back.bottomRight())
            painter.drawLine(back.topLeft(), QPointF(back.left(), front.top()))
            painter.drawLine(back.bottomRight(), QPointF(front.right(), back.bottom()))
            painter.drawRect(front)
        else:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.drawLine(QPointF(left, top), QPointF(left + _GLYPH, top + _GLYPH))
            painter.drawLine(QPointF(left + _GLYPH, top), QPointF(left, top + _GLYPH))
        painter.end()


class WindowControls(QWidget):
    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    def __init__(self, translate: Callable[[str, str], str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._translate = translate
        self._maximized = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._minimize = _ControlButton(MINIMIZE, self)
        self._maximize = _ControlButton(MAXIMIZE, self)
        self._close = _ControlButton(CLOSE, self)
        for button, signal in ((self._minimize, self.minimize_requested), (self._maximize, self.maximize_requested),
                               (self._close, self.close_requested)):
            button.clicked.connect(signal.emit)
            layout.addWidget(button)
        self.retranslate_ui()

    def maximize_button(self) -> QWidget:
        return self._maximize

    def is_maximized(self) -> bool:
        return self._maximized

    def set_maximized(self, maximized: bool) -> None:
        """窗口是否处于最大化：决定中间那个按钮是“最大化”还是“还原”。"""
        self._maximized = bool(maximized)
        self._maximize.set_kind(RESTORE if maximized else MAXIMIZE)
        self.retranslate_ui()

    def set_maximize_state(self, hovered: bool, pressed: bool) -> None:
        self._maximize.set_external_state(hovered, pressed)

    def retranslate_ui(self) -> None:
        tr = self._translate
        texts = ((self._minimize, tr("window.minimize", "Minimize")),
                 (self._maximize, tr("window.restore", "Restore") if self._maximized
                  else tr("window.maximize", "Maximize")),
                 (self._close, tr("window.close", "Close")))
        for button, text in texts:
            button.setAccessibleName(text)
            set_tooltip(button, text)
