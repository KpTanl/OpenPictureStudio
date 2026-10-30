"""空状态：一块区域里还没有内容时，居中显示图标、一句话、可选的说明和一到两个操作。

操作以键声明，点按钮只发出 ``action_triggered``；文字由使用方给出。``drop_target`` 为拖放区域预留留白，
使用方通过 ``set_drag_active()`` 记录拖放状态；``on_canvas`` 表示它占的是画布的位置，铺画布底色。
文字外观在全局样式表里，图标在绘制时读取当前主题。
"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_LG, SPACING_MD, SPACING_SM, SPACING_XL, SPACING_XS, px
from ..icons import themed_icon
from .apple_button import AppleButton, ApplePrimaryButton

_ICON_SIZE = 40
_MAX_ACTIONS = 2
# 文字一行的最大宽度，放不下时换行；宿主更窄时随宿主收窄
_TEXT_WIDTH = 360


class _Icon(QWidget):
    def __init__(self, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._name = name
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_name(self, name: str) -> None:
        if name != self._name:
            self._name = name
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(_ICON_SIZE, _ICON_SIZE)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        themed_icon(self._name, role="text_caption").paint(painter, QRect(0, 0, _ICON_SIZE, _ICON_SIZE))
        painter.end()


class EmptyState(QWidget):
    action_triggered = Signal(str)

    def __init__(self, icon: str, parent: Optional[QWidget] = None, *,
                 actions: Sequence[str] = (), drop_target: bool = False, on_canvas: bool = False):
        super().__init__(parent)
        if len(actions) > _MAX_ACTIONS or len(set(actions)) != len(actions):
            raise ValueError(f"空状态最多 {_MAX_ACTIONS} 个互不相同的操作: {tuple(actions)!r}")
        self.setObjectName("emptyState")
        self.setProperty("onCanvas", on_canvas)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._card = QWidget(self)
        self._card.setProperty("dragActive", False)
        horizontal, vertical = (3 * px(SPACING_XL), 2 * px(SPACING_XL)) if drop_target else (0, 0)
        self._card.setMaximumWidth(_TEXT_WIDTH + 2 * horizontal)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_XL))
        center = QHBoxLayout()
        center.addStretch(1)
        center.addWidget(self._card, 1000)
        center.addStretch(1)
        outer.addStretch(1)
        outer.addLayout(center)
        outer.addStretch(1)

        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(horizontal, vertical, horizontal, vertical)
        layout.setSpacing(px(SPACING_SM))
        self._icon = _Icon(icon, self._card)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(px(SPACING_XS))
        self._title = QLabel(self._card)
        self._title.setObjectName("emptyStateTitle")
        self._description = QLabel(self._card)
        self._description.setObjectName("emptyStateText")
        for label in (self._title, self._description):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            layout.addWidget(label)
        self._description.hide()

        self._buttons: dict[str, AppleButton] = {}
        self._actions = QWidget(self._card)
        row = QHBoxLayout(self._actions)
        row.setContentsMargins(0, px(SPACING_MD), 0, 0)
        row.setSpacing(px(SPACING_SM))
        row.addStretch(1)
        for index, key in enumerate(actions):
            button = (ApplePrimaryButton if index == 0 else AppleButton)("", self._actions)
            button.setMinimumWidth(px(SPACING_LG) * 6)
            button.clicked.connect(lambda _checked=False, key=key: self.action_triggered.emit(key))
            self._buttons[key] = button
            row.addWidget(button)
        row.addStretch(1)
        layout.addWidget(self._actions)
        self._actions.setVisible(bool(actions))

    def set_icon(self, name: str) -> None:
        self._icon.set_name(name)

    def title(self) -> str:
        return self._title.text()

    def description(self) -> str:
        return self._description.text()

    def set_text(self, title: str, description: str = "") -> None:
        self._title.setText(title)
        self._description.setText(description)
        self._description.setVisible(bool(description))

    def action_button(self, key: str) -> AppleButton:
        return self._buttons[key]

    def set_action_text(self, key: str, text: str) -> None:
        self._buttons[key].setText(text)

    def is_drag_active(self) -> bool:
        return bool(self._card.property("dragActive"))

    def set_drag_active(self, active: bool) -> None:
        """记录这块区域是否处于拖入状态。"""
        if bool(active) == self.is_drag_active():
            return
        self._card.setProperty("dragActive", bool(active))
