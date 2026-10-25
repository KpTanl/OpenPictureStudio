"""编辑栈：这张照片现在依次做了哪几步。每一行是一步：图标、名称、一句摘要，右侧是重置这一步的按钮。

只接收要显示的行并发出请求：点一行发出 ``stage_requested``，点行尾的按钮发出 ``reset_requested``；
程序用 ``set_rows()`` 回填不发出。行是自绘的，取值在绘制时读取当前主题。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from PyQt6.QtCore import QEvent, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QIcon, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from resources.styles.theme_variables import (CONTROL_HEIGHT, CONTROL_HEIGHT_COMPACT, FONT_SIZE_BODY,
                                              FONT_SIZE_CAPTION, RADIUS_CONTROL, SPACING_MD, SPACING_SM,
                                              SPACING_XS, px)
from ..icons import themed_icon
from ..theme import get_theme_tokens, to_qcolor
from .apple_button import AppleIconButton
from .collapsible_card import CollapsibleCard
from .tooltip import set_tooltip

_ROW_HEIGHT = CONTROL_HEIGHT + px(SPACING_XS)
_ICON_SIZE = 18


@dataclass(frozen=True)
class StackRow:
    """编辑栈里的一步。current 表示侧栏现在显示的正是这一步的工具。"""

    key: str
    title: str
    summary: str = ""
    icon: str = ""
    current: bool = False
    resettable: bool = True


class _StageRow(QWidget):
    clicked = pyqtSignal(str)
    reset_clicked = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._row = StackRow("", "")
        self._icon: Optional[QIcon] = None
        self._hovered = False
        self._pressed = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedHeight(_ROW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, px(SPACING_XS), 0)
        layout.addStretch(1)
        self.reset_button = AppleIconButton(themed_icon("rotate"), CONTROL_HEIGHT_COMPACT, self)
        self.reset_button.setIconSize(QSize(16, 16))
        self.reset_button.clicked.connect(lambda: self.reset_clicked.emit(self._row.key))
        layout.addWidget(self.reset_button)

    def row(self) -> StackRow:
        return self._row

    def set_row(self, row: StackRow) -> None:
        if row.icon != self._row.icon:
            self._icon = themed_icon(row.icon, checked_role="accent") if row.icon else None
        self._row = row
        self.reset_button.setVisible(row.resettable)
        self.setAccessibleName(" ".join(text for text in (row.title, row.summary) if text))
        self.update()

    def enterEvent(self, event: QEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hovered = self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pressed, self._pressed = self._pressed, False
            self.update()
            if pressed and self.rect().contains(event.position().toPoint()):
                self.clicked.emit(self._row.key)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self._row.key)
            event.accept()
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.update()
        super().focusOutEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        tokens = get_theme_tokens()
        radius = px(RADIUS_CONTROL)
        enabled = self.isEnabled()
        current = self._row.current
        frame = QRectF(self.rect()).adjusted(0, 1, 0, -1)

        background = "well" if self._pressed else "control" if current or self._hovered else ""
        if background and enabled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(to_qcolor(tokens[background]))
            painter.drawRoundedRect(frame, radius, radius)
        if self.hasFocus():
            painter.setPen(QPen(to_qcolor(tokens["accent"]), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(frame.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)

        left = px(SPACING_SM)
        right = (self.reset_button.x() if self._row.resettable else self.width()) - px(SPACING_SM)
        if self._icon is not None:
            mode = (QIcon.Mode.Disabled if not enabled
                    else QIcon.Mode.Active if self._hovered or self._pressed else QIcon.Mode.Normal)
            self._icon.paint(painter, QRect(left, (self.height() - _ICON_SIZE) // 2, _ICON_SIZE, _ICON_SIZE),
                             Qt.AlignmentFlag.AlignCenter, mode, QIcon.State.On if current else QIcon.State.Off)
            left += _ICON_SIZE + px(SPACING_SM)

        font = QFont(self.font())
        font.setPixelSize(px(FONT_SIZE_BODY))
        font.setWeight(QFont.Weight.DemiBold if current else QFont.Weight.Normal)
        title_width = min(max(0, right - left), QFontMetrics(font).horizontalAdvance(self._row.title) + 1)
        if self._row.summary:
            caption = QFont(self.font())
            caption.setPixelSize(px(FONT_SIZE_CAPTION))
            painter.setFont(caption)
            painter.setPen(to_qcolor(tokens["text_caption" if enabled else "text_disabled"]))
            # 名称优先完整显示，摘要用剩下的宽度
            available = max(0, right - left - title_width - px(SPACING_SM))
            summary = painter.fontMetrics().elidedText(self._row.summary, Qt.TextElideMode.ElideRight, available)
            painter.drawText(QRect(right - available, 0, available, self.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, summary)
        painter.setFont(font)
        role = ("text_disabled" if not enabled
                else "text_primary" if current or self._hovered or self._pressed else "text_secondary")
        painter.setPen(to_qcolor(tokens[role]))
        title = painter.fontMetrics().elidedText(self._row.title, Qt.TextElideMode.ElideRight, title_width)
        painter.drawText(QRect(left, 0, title_width, self.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        painter.end()


class EditStackView(QWidget):
    stage_requested = pyqtSignal(str)
    reset_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("editStack")
        self._rows: list[_StageRow] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_MD), px(SPACING_SM), px(SPACING_MD), 0)
        layout.setSpacing(0)
        self._card = CollapsibleCard("", self)
        self._list = QVBoxLayout()
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(0)
        self._card.add_layout(self._list)
        self._empty = QLabel(self._card)
        self._empty.setObjectName("mutedText")
        self._empty.setWordWrap(True)
        self._card.add_widget(self._empty)
        layout.addWidget(self._card)
        self._reset_tip = ""

    def rows(self) -> tuple[StackRow, ...]:
        return tuple(row.row() for row in self._rows)

    def row_widgets(self) -> tuple[QWidget, ...]:
        return tuple(self._rows)

    def is_expanded(self) -> bool:
        return self._card.is_expanded()

    def set_expanded(self, expanded: bool) -> None:
        self._card.set_expanded(expanded)

    def set_rows(self, rows: Sequence[StackRow]) -> None:
        """程序回填，按处理顺序自上而下。行数不变时原位更新。"""
        rows = tuple(rows)
        while len(self._rows) > len(rows):
            row = self._rows.pop()
            self._list.removeWidget(row)
            row.deleteLater()
        while len(self._rows) < len(rows):
            row = _StageRow(self._card)
            row.clicked.connect(self.stage_requested)
            row.reset_clicked.connect(self.reset_requested)
            set_tooltip(row.reset_button, self._reset_tip)
            row.reset_button.setAccessibleName(self._reset_tip)
            self._rows.append(row)
            self._list.addWidget(row)
        for widget, row in zip(self._rows, rows):
            widget.set_row(row)
        self._empty.setVisible(not rows)

    def set_texts(self, title: str, empty: str, reset_tip: str) -> None:
        self._card.set_title(title)
        self._empty.setText(empty)
        self._reset_tip = reset_tip
        for row in self._rows:
            set_tooltip(row.reset_button, reset_tip)
            row.reset_button.setAccessibleName(reset_tip)
