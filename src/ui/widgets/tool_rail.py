"""工具栏：编辑工作区最左侧的一列入口，每个入口是图标加文字。

只接收入口与状态，用户点击发出 ``entry_requested``；``set_current()``、``set_available()``、
``set_modified()`` 是程序回填，不发出信号。外观由全局样式表按对象名决定，已修改的圆点在绘制时读取主题。
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPaintEvent
from PySide6.QtWidgets import QFrame, QToolButton, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_SM, SPACING_XS, TOOL_RAIL_WIDTH, px
from ..icons import themed_icon
from ..theme import get_theme_tokens, to_qcolor

_BUTTON_HEIGHT = 54
_ICON_SIZE = 22
_DOT_RADIUS = 3


class _RailButton(QToolButton):
    def __init__(self, icon_name: str, text: str, parent: QWidget):
        super().__init__(parent)
        self._modified = False
        self.setObjectName("toolRailButton")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setIcon(themed_icon(icon_name, checked_role="accent"))
        self.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self.setFixedSize(TOOL_RAIL_WIDTH - 2 * px(SPACING_SM), _BUTTON_HEIGHT)
        self.setText(text)

    def modified(self) -> bool:
        return self._modified

    def set_modified(self, modified: bool) -> None:
        if modified != self._modified:
            self._modified = modified
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if not self._modified:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(to_qcolor(get_theme_tokens()["accent"]))
        offset = px(SPACING_SM)
        painter.drawEllipse(QPointF(self.width() - offset, offset), _DOT_RADIUS, _DOT_RADIUS)
        painter.end()


class ToolRail(QFrame):
    entry_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("toolRail")
        self.setFixedWidth(TOOL_RAIL_WIDTH)
        self._buttons: dict[str, _RailButton] = {}
        self._current = ""
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(px(SPACING_SM), px(SPACING_SM), px(SPACING_SM), px(SPACING_SM))
        self._layout.setSpacing(px(SPACING_XS))
        self._layout.addStretch(1)

    def set_entries(self, entries: Sequence[tuple[str, str, str]]) -> None:
        """设置入口，每项为 (入口标识, 图标名, 文字)。"""
        for button in self._buttons.values():
            self._layout.removeWidget(button)
            button.deleteLater()
        self._buttons = {}
        for index, (entry_id, icon_name, text) in enumerate(entries):
            button = _RailButton(icon_name, text, self)
            button.clicked.connect(lambda _checked=False, name=entry_id: self._on_clicked(name))
            self._layout.insertWidget(index, button)
            self._buttons[entry_id] = button
        self.set_current(self._current)

    def entry_ids(self) -> tuple[str, ...]:
        return tuple(self._buttons)

    def button(self, entry_id: str) -> QToolButton:
        return self._buttons[entry_id]

    def set_entry_text(self, entry_id: str, text: str) -> None:
        self._buttons[entry_id].setText(text)

    def current(self) -> str:
        return self._current

    def set_current(self, entry_id: str) -> None:
        self._current = entry_id
        for name, button in self._buttons.items():
            button.setChecked(name == entry_id)

    def set_available(self, entry_ids: Iterable[str]) -> None:
        available = set(entry_ids)
        for name, button in self._buttons.items():
            button.setVisible(name in available)

    def modified(self) -> frozenset[str]:
        return frozenset(name for name, button in self._buttons.items() if button.modified())

    def set_modified(self, entry_ids: Iterable[str]) -> None:
        modified = set(entry_ids)
        for name, button in self._buttons.items():
            button.set_modified(name in modified)

    def _on_clicked(self, entry_id: str) -> None:
        # 选中态由所属方回填：请求被拒绝时仍停在原来的入口
        self.set_current(self._current)
        self.entry_requested.emit(entry_id)
