"""开关按钮：带图标和文字、有选中态的描边按钮，用于“照片信息”“胶片条”这类显示开关。

外观由全局样式表按对象名决定。用户点击发出 ``clicked(bool)``；``setChecked()`` 回填不发出该信号。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QPushButton, QSizePolicy, QWidget

from resources.styles.theme_variables import CONTROL_HEIGHT
from ..icons import themed_icon
from .apple_button import bold_text_extra

_ICON_SIZE = 18


class ToggleButton(QPushButton):
    def __init__(self, text: str = "", icon_name: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setObjectName("toggleButton")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(CONTROL_HEIGHT)
        self.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        if icon_name:
            self.set_icon_name(icon_name)

    def set_icon_name(self, name: str) -> None:
        self.setIcon(themed_icon(name, checked_role="accent"))

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(hint.width() + bold_text_extra(self), hint.height())

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()
