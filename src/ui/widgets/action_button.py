"""绑定到动作的图标按钮：点击即触发动作，可用状态、悬停提示和快捷键随动作变化。"""

from __future__ import annotations

import re
from typing import Optional

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QWidget

from resources.styles.theme_variables import TOOL_BUTTON_SIZE
from ..icons import themed_icon
from .apple_button import AppleIconButton
from .tooltip import set_tooltip

_ICON_SIZE = 20


def plain_action_text(text: str) -> str:
    """去掉动作文字里的助记符、省略号和末尾括号里的助记键，得到按钮与提示用的文字。"""
    cleaned = text.replace("&", "").replace("...", "").replace("…", "").strip()
    return re.sub(r"\([A-Za-z0-9]\)$", "", cleaned).strip()


def shortcut_text(action: QAction) -> str:
    return action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)


class ActionIconButton(AppleIconButton):
    def __init__(self, action: QAction, icon_name: str, parent: Optional[QWidget] = None, *,
                 size: int = TOOL_BUTTON_SIZE):
        super().__init__(themed_icon(icon_name), size, parent)
        self._action = action
        self.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self.clicked.connect(action.trigger)
        action.changed.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        self.setEnabled(self._action.isEnabled())
        text = plain_action_text(self._action.text())
        self.setAccessibleName(text)
        set_tooltip(self, text, shortcut_text(self._action))
