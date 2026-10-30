"""按钮组件：六种用途加紧凑尺寸。

外观全部由全局样式表按对象名决定，随主题切换自动更新；本模块只定义种类、尺寸与交互属性。
- AppleBrandButton        主操作（品牌渐变），每个窗口只用于唯一的主操作
- ApplePrimaryButton      确认（强调色）
- AppleButton             次要（描边）
- AppleGhostButton        文字
- AppleIconButton         图标
- AppleDestructiveButton  危险
- AppleCompactButton      紧凑尺寸的文字按钮，用于行内与标签位置
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontMetrics, QIcon
from PySide6.QtWidgets import QPushButton, QSizePolicy, QWidget

from resources.styles.theme_variables import CONTROL_HEIGHT, CONTROL_HEIGHT_COMPACT, TOOL_BUTTON_SIZE


def bold_text_extra(button: QPushButton) -> int:
    """选中态文字加粗后多出的宽度。样式表里的字重不参与尺寸计算，由选中时加粗的按钮自己留出。"""
    bold = QFont(button.font())
    bold.setWeight(QFont.Weight.DemiBold)
    regular = button.fontMetrics().horizontalAdvance(button.text())
    return max(0, QFontMetrics(bold).horizontalAdvance(button.text()) - regular)


class AppleButton(QPushButton):
    """次要按钮（描边）。"""

    OBJECT_NAME = "appleButton"

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setObjectName(self.OBJECT_NAME)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(CONTROL_HEIGHT)


class AppleBrandButton(AppleButton):
    """主操作按钮（品牌渐变）。"""

    OBJECT_NAME = "appleBrandButton"


class ApplePrimaryButton(AppleButton):
    """确认按钮（强调色）。"""

    OBJECT_NAME = "applePrimaryButton"


class AppleDestructiveButton(AppleButton):
    """危险按钮，用于删除、清除等不可撤回的操作。"""

    OBJECT_NAME = "appleDestructiveButton"


class AppleGhostButton(AppleButton):
    """文字按钮，可设为可选中以表示开启状态。"""

    OBJECT_NAME = "appleGhostButton"


class AppleCompactButton(AppleButton):
    """紧凑尺寸的文字按钮。"""

    OBJECT_NAME = "appleCompactButton"

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setFixedHeight(CONTROL_HEIGHT_COMPACT)


class AppleIconButton(AppleButton):
    """方形图标按钮。"""

    OBJECT_NAME = "appleIconButton"

    def __init__(self, icon: Optional[QIcon] = None, size: int = TOOL_BUTTON_SIZE,
                 parent: Optional[QWidget] = None):
        super().__init__("", parent)
        self.setFixedSize(size, size)
        if icon is not None:
            self.setIcon(icon)
