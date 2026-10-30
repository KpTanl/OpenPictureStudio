"""提示条：一行说明，左侧是图标，右侧可带一个操作。

两种：warning 警告、info 信息。底色与圆角由全局样式表按 ``kind`` 属性决定，图标在绘制时读取当前主题。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QPainter, QPaintEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from resources.styles.theme_variables import SPACING_MD, SPACING_SM, SPACING_XS, px
from ..icons import themed_icon
from .apple_button import AppleGhostButton

# 种类 -> (图标名, 图标取值的键)
_KINDS = {
    "warning": ("alert-triangle", "warning"),
    "info": ("info-circle", "accent"),
}
_ICON_SIZE = 18


class NoticeBar(QFrame):
    action_triggered = Signal()
    dismissed = Signal()

    def __init__(self, kind: str = "info", text: str = "", action_text: str = "",
                 parent: Optional[QWidget] = None, *, closable: bool = False):
        super().__init__(parent)
        if kind not in _KINDS:
            raise ValueError(f"未知的提示种类: {kind!r}")
        self._kind = kind
        self.setObjectName("noticeBar")
        self.setProperty("kind", kind)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(SPACING_MD) + _ICON_SIZE + px(SPACING_SM), px(SPACING_SM),
                                  px(SPACING_SM), px(SPACING_SM))
        layout.setSpacing(px(SPACING_SM))
        self._label = QLabel(text, self)
        self._label.setObjectName("noticeBarText")
        self._label.setWordWrap(True)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._label, 1)
        self._action = AppleGhostButton(action_text, self)
        self._action.clicked.connect(self.action_triggered.emit)
        self._action.setVisible(bool(action_text))
        layout.addWidget(self._action)
        layout.addSpacing(px(SPACING_XS))
        self.close_button = AppleGhostButton("×", self) if closable else None
        if self.close_button is not None:
            self.close_button.setAutoDefault(False)
            self.close_button.clicked.connect(self.dismissed.emit)
            layout.addWidget(self.close_button)

    @property
    def action_button(self) -> AppleGhostButton:
        return self._action

    def kind(self) -> str:
        return self._kind

    def text(self) -> str:
        return self._label.text()

    def set_text(self, text: str) -> None:
        self._label.setText(text)

    def set_action_text(self, text: str) -> None:
        self._action.setText(text)
        self._action.setVisible(bool(text))

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        icon_name, role = _KINDS[self._kind]
        painter = QPainter(self)
        themed_icon(icon_name, role=role).paint(
            painter, QRect(px(SPACING_MD), (self.height() - _ICON_SIZE) // 2, _ICON_SIZE, _ICON_SIZE))
        painter.end()
