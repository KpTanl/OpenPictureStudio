"""列表行：可单选的一列行，用于选项列表与导航。

每行可带左侧图标与右侧说明文字；选中行为强调色浅底。自绘，取值在绘制时读取当前主题。
方向键移动选中项，回车或空格等同双击。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QEvent, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QIcon, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from resources.styles.theme_variables import (CONTROL_HEIGHT, FONT_SIZE_BODY, FONT_SIZE_CAPTION, RADIUS_CONTROL,
                                              SPACING_MD, SPACING_SM, SPACING_XS, px)
from ..icons import themed_icon
from ..theme import get_theme_tokens, to_qcolor

_ROW_HEIGHT = CONTROL_HEIGHT + px(SPACING_XS)
_ICON_SIZE = 18


class GroupedOptionRow(QWidget):
    """列表中的一行。"""

    clicked = pyqtSignal(int)
    double_clicked = pyqtSignal(int)

    def __init__(
        self,
        index: int,
        key: str,
        title: str,
        subtitle: str = "",
        parent: Optional[QWidget] = None,
        *,
        icon: str = "",
    ):
        super().__init__(parent)
        self._index = index
        self._key = key
        self._title = title
        self._subtitle = subtitle
        self._icon: Optional[QIcon] = themed_icon(icon, checked_role="accent") if icon else None
        self._checked = False
        self._hovered = False
        self._pressed = False
        self._focused = False

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(_ROW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    @property
    def index(self) -> int:
        return self._index

    @property
    def key(self) -> str:
        return self._key

    @property
    def title(self) -> str:
        return self._title

    def set_title(self, title: str) -> None:
        if self._title != title:
            self._title = title
            self.update()

    def set_subtitle(self, subtitle: str) -> None:
        if self._subtitle != subtitle:
            self._subtitle = subtitle
            self.update()

    def set_checked(self, checked: bool) -> None:
        if self._checked != checked:
            self._checked = checked
            self.update()

    def is_checked(self) -> bool:
        return self._checked

    def set_focused(self, focused: bool) -> None:
        """所在列表获得键盘焦点时，选中行显示焦点描边。"""
        if self._focused != focused:
            self._focused = focused
            self.update()

    def enterEvent(self, event: QEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            was_pressed = self._pressed
            self._pressed = False
            self.update()
            if was_pressed and self.rect().contains(event.position().toPoint()):
                self.clicked.emit(self._index)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self._index)
        super().mouseDoubleClickEvent(event)

    def _background(self) -> str:
        if self._checked:
            return "accent_soft"
        if self._pressed:
            return "well"
        if self._hovered:
            return "control"
        return ""

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        tokens = get_theme_tokens()
        radius = px(RADIUS_CONTROL)
        bounds = QRectF(self.rect())

        background = self._background()
        if background:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(to_qcolor(tokens[background]))
            painter.drawRoundedRect(bounds, radius, radius)
        if self._checked and self._focused:
            painter.setPen(QPen(to_qcolor(tokens["accent"]), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(bounds.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)

        left = px(SPACING_MD)
        right = self.width() - px(SPACING_MD)
        if self._icon is not None:
            mode = QIcon.Mode.Active if self._hovered or self._pressed else QIcon.Mode.Normal
            state = QIcon.State.On if self._checked else QIcon.State.Off
            self._icon.paint(painter, QRect(left, (self.height() - _ICON_SIZE) // 2, _ICON_SIZE, _ICON_SIZE),
                             Qt.AlignmentFlag.AlignCenter, mode, state)
            left += _ICON_SIZE + px(SPACING_SM)

        if self._subtitle:
            caption = QFont(self.font())
            caption.setPixelSize(px(FONT_SIZE_CAPTION))
            painter.setFont(caption)
            painter.setPen(to_qcolor(tokens["text_caption"]))
            width = painter.fontMetrics().horizontalAdvance(self._subtitle)
            painter.drawText(QRect(right - width, 0, width, self.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._subtitle)
            right -= width + px(SPACING_SM)

        font = QFont(self.font())
        font.setPixelSize(px(FONT_SIZE_BODY))
        font.setWeight(font.Weight.DemiBold if self._checked else font.Weight.Normal)
        painter.setFont(font)
        if self._checked:
            painter.setPen(to_qcolor(tokens["accent"]))
        else:
            painter.setPen(to_qcolor(tokens["text_primary" if self._hovered or self._pressed else "text_secondary"]))
        title = painter.fontMetrics().elidedText(self._title, Qt.TextElideMode.ElideRight, max(0, right - left))
        painter.drawText(QRect(left, 0, max(0, right - left), self.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        painter.end()


class GroupedOptionCard(QWidget):
    """一列可单选的行。"""

    selection_changed = pyqtSignal(int, str)
    item_double_clicked = pyqtSignal(int, str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._rows: list[GroupedOptionRow] = []
        self._checked_index: int = 0

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)

    def add_item(
        self,
        key: str,
        title: str,
        subtitle: str = "",
        default_checked: bool = False,
        *,
        icon: str = "",
    ) -> int:
        idx = len(self._rows)
        row = GroupedOptionRow(idx, key, title, subtitle, self, icon=icon)
        row.clicked.connect(self._on_row_clicked)
        row.double_clicked.connect(self._on_row_double_clicked)
        row.set_focused(self.hasFocus())
        self._rows.append(row)
        self._layout.addWidget(row)

        if default_checked or (idx == 0 and self._checked_index == 0):
            self._checked_index = idx
            row.set_checked(True)
        return idx

    def set_items(self, items: list[tuple[str, str]]) -> None:
        """批量设置选项，格式为 [(key, title), ...]"""
        self.clear()
        for i, (key, title) in enumerate(items):
            self.add_item(key, title, default_checked=(i == self._checked_index))

    def clear(self) -> None:
        for row in self._rows:
            self._layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        self._checked_index = 0

    def set_item_text(self, index: int, title: str, subtitle: str = "") -> None:
        if 0 <= index < len(self._rows):
            self._rows[index].set_title(title)
            if subtitle:
                self._rows[index].set_subtitle(subtitle)

    def set_checked_index(self, index: int) -> None:
        if 0 <= index < len(self._rows):
            if self._checked_index != index:
                old_idx = self._checked_index
                self._checked_index = index
                if 0 <= old_idx < len(self._rows):
                    self._rows[old_idx].set_checked(False)
                self._rows[index].set_checked(True)
                self.selection_changed.emit(index, self._rows[index].key)

    def set_checked_key(self, key: str) -> None:
        for i, row in enumerate(self._rows):
            if row.key == key:
                self.set_checked_index(i)
                return

    def clear_selection(self) -> None:
        if 0 <= self._checked_index < len(self._rows):
            self._rows[self._checked_index].set_checked(False)
        self._checked_index = -1

    def checked_index(self) -> int:
        return self._checked_index

    def checked_key(self) -> str:
        if 0 <= self._checked_index < len(self._rows):
            return self._rows[self._checked_index].key
        return ""

    def checked_widget(self) -> Optional[QWidget]:
        if 0 <= self._checked_index < len(self._rows):
            return self._rows[self._checked_index]
        return None

    def item_count(self) -> int:
        return len(self._rows)

    def _on_row_clicked(self, index: int) -> None:
        self.set_checked_index(index)

    def _on_row_double_clicked(self, index: int) -> None:
        self.set_checked_index(index)
        if 0 <= index < len(self._rows):
            self.item_double_clicked.emit(index, self._rows[index].key)

    def _set_rows_focused(self, focused: bool) -> None:
        for row in self._rows:
            row.set_focused(focused)

    def focusInEvent(self, event) -> None:
        self._set_rows_focused(True)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._set_rows_focused(False)
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Up:
            if self._checked_index > 0:
                self.set_checked_index(self._checked_index - 1)
            event.accept()
        elif key == Qt.Key.Key_Down:
            if self._checked_index < len(self._rows) - 1:
                self.set_checked_index(self._checked_index + 1)
            event.accept()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if 0 <= self._checked_index < len(self._rows):
                self.item_double_clicked.emit(self._checked_index, self._rows[self._checked_index].key)
            event.accept()
        else:
            super().keyPressEvent(event)
