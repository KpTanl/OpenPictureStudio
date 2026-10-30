"""标签：胶囊形的小按钮，可单独使用（如快捷提示词），也可成组单选（如比例、背景）。

外观由全局样式表按对象名决定。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QGridLayout, QPushButton, QSizePolicy, QWidget

from resources.styles.theme_variables import CONTROL_HEIGHT_COMPACT, SPACING_SM, px
from .apple_button import bold_text_extra


class Chip(QPushButton):
    def __init__(self, text: str = "", parent: Optional[QWidget] = None, *, checkable: bool = False):
        super().__init__(text, parent)
        self.setObjectName("chip")
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(CONTROL_HEIGHT_COMPACT)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(hint.width() + (bold_text_extra(self) if self.isCheckable() else 0), hint.height())

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


class ChipGroup(QWidget):
    """一组单选标签。

    columns 大于 0 时按该列数等宽排成网格，否则排成一行并靠左。
    只有用户点击发出 ``current_changed``；``set_current()`` 回填不发出，也可以回填成全部不选中。
    """

    current_changed = Signal(str)

    def __init__(self, columns: int = 0, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._columns = columns
        self._chips: dict[str, Chip] = {}
        self._current = ""
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.buttonClicked.connect(self._on_clicked)
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(px(SPACING_SM))

    def set_items(self, items: list[tuple[str, str]]) -> None:
        """重设全部标签，格式为 [(key, 文字), ...]；原选中项仍在时保持选中。"""
        current = self.current()
        for chip in self._chips.values():
            self._group.removeButton(chip)
            self._layout.removeWidget(chip)
            chip.deleteLater()
        if self._columns <= 0:
            self._layout.setColumnStretch(len(self._chips), 0)
        self._chips = {}
        self._current = ""
        for index, (key, text) in enumerate(items):
            chip = Chip(text, self, checkable=True)
            self._chips[key] = chip
            self._group.addButton(chip)
            if self._columns > 0:
                self._layout.addWidget(chip, index // self._columns, index % self._columns)
            else:
                self._layout.addWidget(chip, 0, index)
        if self._columns <= 0:
            self._layout.setColumnStretch(len(items), 1)
        if current in self._chips:
            self.set_current(current)

    def set_text(self, key: str, text: str) -> None:
        self._chips[key].setText(text)

    def set_item_enabled(self, key: str, enabled: bool, reason: str = "") -> None:
        """单独启用或禁用一个标签；禁用时的原因写在悬停提示里。"""
        chip = self._chips[key]
        chip.setEnabled(enabled)
        chip.setToolTip("" if enabled else reason)

    def is_item_enabled(self, key: str) -> bool:
        return self._chips[key].isEnabled()

    def current(self) -> str:
        return self._current

    def set_current(self, key: str) -> None:
        """程序回填。空字符串表示当前取值不属于任何一个标签，全部不选中。"""
        if key and key not in self._chips:
            raise KeyError(f"未知的标签: {key!r}")
        self._current = key
        # 单选组里不能直接取消选中，回填期间暂时放开
        self._group.setExclusive(False)
        for name, chip in self._chips.items():
            chip.setChecked(name == key)
        self._group.setExclusive(True)

    def _on_clicked(self, button) -> None:
        key = next(key for key, chip in self._chips.items() if chip is button)
        if key != self._current:
            self._current = key
            self.current_changed.emit(key)
