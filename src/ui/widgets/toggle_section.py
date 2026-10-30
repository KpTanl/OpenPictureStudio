"""带开关的分组：开关表示这一组做不做，打开后才显示它的内容；标题下可以有一行说明。

只有用户操作发出 ``toggled_by_user``；``set_checked()`` 是程序回填，不发出。内容由使用方放入，
分组只负责在开关打开时显示它。相邻分组之间的细线在全局样式表里。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_SM, SPACING_XS, px
from .toggle_switch import LabeledToggle


class ToggleSection(QFrame):
    toggled_by_user = Signal(bool)

    def __init__(self, title: str = "", description: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("toggleSection")
        self._expanded = False
        self._header = LabeledToggle(title, description, switch_on_left=True, parent=self)
        self._header.setDescriptionWordWrap(True)
        self._header.toggled.connect(self._on_toggled)
        self._content = QWidget(self)
        self._content_layout = QVBoxLayout(self._content)
        # 内容与标题左对齐：让出开关与间距的宽度
        indent = self._header.switch.sizeHint().width() + px(SPACING_SM)
        self._content_layout.setContentsMargins(indent, px(SPACING_XS), 0, px(SPACING_SM))
        self._content_layout.setSpacing(px(SPACING_SM))
        self._content.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, px(SPACING_XS), 0, px(SPACING_XS))
        layout.setSpacing(0)
        layout.addWidget(self._header)
        layout.addWidget(self._content)

    @property
    def header(self) -> LabeledToggle:
        return self._header

    def title(self) -> str:
        return self._header.text()

    def set_title(self, text: str) -> None:
        self._header.setText(text)

    def description(self) -> str:
        return self._header.description()

    def set_description(self, text: str) -> None:
        self._header.setDescription(text)

    def is_checked(self) -> bool:
        return self._header.isChecked()

    def set_checked(self, checked: bool) -> None:
        """程序回填：不发出 toggled_by_user。"""
        self._header.set_checked_silent(checked)
        self._sync_content()

    def set_switch_enabled(self, enabled: bool) -> None:
        """开关不可用时标题与说明仍照常显示，用来写明原因。"""
        self._header.switch.setEnabled(enabled)

    def is_switch_enabled(self) -> bool:
        return self._header.switch.isEnabled()

    def set_content_held(self, held: bool) -> None:
        """held 为真时内容保持显示，即使开关已经关闭；用于拖动尚未结束的时刻。"""
        self._expanded = held
        self._sync_content()

    def add_widget(self, widget: QWidget) -> None:
        self._content_layout.addWidget(widget)

    def is_content_visible(self) -> bool:
        return not self._content.isHidden()

    def _on_toggled(self, checked: bool) -> None:
        self._sync_content()
        self.toggled_by_user.emit(checked)

    def _sync_content(self) -> None:
        self._content.setVisible(self._header.isChecked() or self._expanded)
