"""弹出菜单与下拉选择。

外观由全局样式表决定；勾选标记与箭头取自图标资源，随主题自动换色。
AppleDropDown 兼容 QComboBox 的常用接口，可在右侧显示一段说明文字（如资源状态），也可以给每一项各加说明。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence
from PySide6.QtCore import (
    QElapsedTimer,
    QEvent,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QPaintEvent,
    QPixmap,
)
from PySide6.QtWidgets import (
    QMenu,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QToolTip,
    QWidget,
)

from resources.styles.theme_variables import CONTROL_HEIGHT, SPACING_SM, px
from ..icons import themed_icon
from ..theme import get_theme_tokens, to_qcolor

_ICON = 16
# 菜单因点击按钮而收起时，这次点击随后会再送到按钮；在此时间内的按下不再弹出，点一下才能收起
_REOPEN_GUARD_MS = 150


def _create_blank_icon(size: int = 16) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    return QIcon(pixmap)


class AppleMenu(QMenu):
    """圆角弹出菜单。"""

    def __init__(self, *args, **kwargs):
        title = ""
        parent = None
        if len(args) == 1:
            if isinstance(args[0], str):
                title = args[0]
            elif isinstance(args[0], QWidget) or args[0] is None:
                parent = args[0]
            else:
                title = str(args[0])
        elif len(args) >= 2:
            title = args[0]
            parent = args[1]

        if "title" in kwargs:
            title = kwargs.pop("title")
        if "parent" in kwargs:
            parent = kwargs.pop("parent")

        if parent is not None:
            super().__init__(title, parent, **kwargs)
        elif title:
            super().__init__(title, **kwargs)
        else:
            super().__init__(**kwargs)

        self.setObjectName("appleMenu")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)


class AppleDropDown(QPushButton):
    """下拉选择，接口与 QComboBox 的常用部分一致。"""

    currentIndexChanged = Signal(int)
    currentTextChanged = Signal(str)
    activated = Signal(int)

    _MIN_VISIBLE_CHARS = 4

    def __init__(self, parent: Optional[QWidget] = None, flat: bool = False):
        super().__init__(parent)
        self.setObjectName("appleDropDown")
        self._flat = flat
        self._note = ""
        self.setProperty("flat", flat)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        if not self._flat:
            self.setMinimumHeight(CONTROL_HEIGHT)

        self._items: list[dict[str, Any]] = []
        self._current_index: int = -1

        self._menu = AppleMenu(parent=self)
        self._menu.aboutToHide.connect(self._on_menu_about_to_hide)
        self._menu_hidden = QElapsedTimer()
        self.clicked.connect(self.show_popup)

    def set_note(self, text: str) -> None:
        """在箭头左侧显示一段说明文字，例如所选资源的状态；空字符串表示不显示。"""
        self._note = text
        self.updateGeometry()
        self.update()

    def note(self) -> str:
        return self._note

    def setItemNote(self, index: int, note: str) -> None:
        """给一项加一段说明（如资源状态）：列表里显示在该项右侧，选中它时显示在箭头左侧。"""
        if 0 <= index < len(self._items) and self._items[index]["note"] != note:
            self._items[index]["note"] = note
            self._sync_action_text(index)
            if index == self._current_index:
                self.updateGeometry()
                self.update()

    def itemNote(self, index: int) -> str:
        return self._items[index]["note"] if 0 <= index < len(self._items) else ""

    def _sync_action_text(self, index: int) -> None:
        item = self._items[index]
        # 制表符之后的部分由菜单画在右侧一栏
        item["action"].setText(item["text"] + ("	" + item["note"] if item["note"] else ""))

    def _shown_note(self) -> str:
        """按钮上显示的说明：整个下拉的说明优先，其次是所选项的说明。"""
        return self._note or self.itemNote(self._current_index)

    def _on_menu_about_to_hide(self) -> None:
        self._menu_hidden.start()
        self._set_open(False)

    def _set_open(self, is_open: bool) -> None:
        """展开期间按钮保持激活外观。"""
        if bool(self.property("open")) != is_open:
            self.setProperty("open", is_open)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    def show_popup(self) -> None:
        """在按钮下方弹出选项，并高亮当前项，方向键从当前项开始移动。"""
        if not self._items:
            return
        self._refresh_action_icons()
        btn_width = self.width()
        menu_hint = self._menu.sizeHint()
        self._menu.setMinimumWidth(max(btn_width, menu_hint.width()))
        global_pos = self.mapToGlobal(QPoint(0, self.height() + 4))
        self._set_open(True)
        self._menu.popup(global_pos)
        if 0 <= self._current_index < len(self._items):
            self._menu.setActiveAction(self._items[self._current_index]["action"])

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """按下即弹出；菜单刚因这次点击收起时不再弹出。"""
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            super().mousePressEvent(event)
            return
        event.accept()
        if self._menu_hidden.isValid() and self._menu_hidden.elapsed() < _REOPEN_GUARD_MS:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.show_popup()

    def showPopup(self) -> None:
        self.show_popup()

    def hidePopup(self) -> None:
        self._menu.close()

    def _refresh_action_icons(self) -> None:
        check_icon = themed_icon("check", role="accent")
        blank_icon = _create_blank_icon()

        for i, item in enumerate(self._items):
            action: Optional[QAction] = item.get("action")
            if action is None:
                continue
            custom_icon: Optional[QIcon] = item.get("custom_icon")
            if custom_icon is not None and not custom_icon.isNull():
                action.setIcon(custom_icon)
            else:
                if i == self._current_index:
                    action.setIcon(check_icon)
                else:
                    action.setIcon(blank_icon)

    def addItem(self, text: str, userData: Any = None, icon: Optional[QIcon] = None) -> None:
        index = len(self._items)
        action = QAction(text, self._menu)
        action.setData(userData)
        action.triggered.connect(lambda checked, idx=index: self._on_action_triggered(idx))
        self._menu.addAction(action)

        self._items.append({
            "text": text,
            "note": "",
            "data": userData,
            "custom_icon": icon,
            "action": action,
            "is_separator": False,
        })

        if self._current_index < 0:
            self.setCurrentIndex(0)

    def addItems(self, texts: Sequence[str]) -> None:
        for t in texts:
            self.addItem(t)

    def addSeparator(self) -> None:
        sep_action = self._menu.addSeparator()
        self._items.append({
            "text": "",
            "note": "",
            "data": None,
            "custom_icon": None,
            "action": sep_action,
            "is_separator": True,
        })

    def _on_action_triggered(self, index: int) -> None:
        if 0 <= index < len(self._items):
            self.setCurrentIndex(index)
            self.activated.emit(index)

    def setCurrentIndex(self, index: int) -> None:
        if index < 0 or index >= len(self._items):
            if not self._items:
                self._current_index = -1
                self.setText("")
            return

        if self._items[index].get("is_separator"):
            return

        old_index = self._current_index
        self._current_index = index
        selected_item = self._items[index]
        self.setText(selected_item["text"])
        self._refresh_action_icons()

        if old_index != index:
            self.currentIndexChanged.emit(index)
            self.currentTextChanged.emit(selected_item["text"])

    def setCurrentText(self, text: str) -> None:
        idx = self.findText(text)
        if idx >= 0:
            self.setCurrentIndex(idx)

    def currentIndex(self) -> int:
        return self._current_index

    def currentText(self) -> str:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index]["text"]
        return ""

    def currentData(self, role: int = Qt.ItemDataRole.UserRole) -> Any:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index]["data"]
        return None

    def count(self) -> int:
        return sum(1 for item in self._items if not item.get("is_separator"))

    def findData(self, data: Any, role: int = Qt.ItemDataRole.UserRole, flags: Qt.MatchFlag = Qt.MatchFlag.MatchExactly) -> int:
        for i, item in enumerate(self._items):
            if not item.get("is_separator") and item.get("data") == data:
                return i
        return -1

    def findText(self, text: str, flags: Qt.MatchFlag = Qt.MatchFlag.MatchExactly) -> int:
        for i, item in enumerate(self._items):
            if not item.get("is_separator") and item.get("text") == text:
                return i
        return -1

    def itemText(self, index: int) -> str:
        if 0 <= index < len(self._items):
            return self._items[index]["text"]
        return ""

    def itemData(self, index: int, role: int = Qt.ItemDataRole.UserRole) -> Any:
        if 0 <= index < len(self._items):
            return self._items[index]["data"]
        return None

    def setItemText(self, index: int, text: str) -> None:
        if 0 <= index < len(self._items):
            self._items[index]["text"] = text
            self._sync_action_text(index)
            if index == self._current_index:
                self.setText(text)

    def setItemEnabled(self, index: int, enabled: bool) -> None:
        if 0 <= index < len(self._items):
            self._items[index]["action"].setEnabled(enabled)

    def setItemData(self, index: int, data: Any, role: int = Qt.ItemDataRole.UserRole) -> None:
        if 0 <= index < len(self._items):
            self._items[index]["data"] = data
            action = self._items[index].get("action")
            if action:
                action.setData(data)

    def clear(self) -> None:
        self._menu.clear()
        self._items.clear()
        self._current_index = -1
        self.setText("")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Down,
                           Qt.Key.Key_Up, Qt.Key.Key_F4):
            self.show_popup()
            event.accept()
            return
        super().keyPressEvent(event)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        if self._flat:
            return hint
        metrics = self.fontMetrics()
        chrome = hint.width() - metrics.horizontalAdvance(self.text())
        floor = chrome + metrics.horizontalAdvance("…") + self._MIN_VISIBLE_CHARS * metrics.averageCharWidth()
        return QSize(min(hint.width(), floor), hint.height())

    def _button_option(self) -> QStyleOptionButton:
        """空间不足时文本右侧省略；sizeHint 仍按完整文本计算。"""
        option = QStyleOptionButton()
        self.initStyleOption(option)
        width = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self).width()
        note = self._shown_note()
        if note:
            width -= self.fontMetrics().horizontalAdvance(note) + px(SPACING_SM)
        # 按钮会把 & 当作助记符，所选项的文字按原样显示
        option.text = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, max(0, width)).replace("&", "&&")
        return option

    def event(self, event: QEvent) -> bool:
        if (event.type() == QEvent.Type.ToolTip and not self._flat and not self.toolTip()
                and self._button_option().text.replace("&&", "&") != self.text()):
            QToolTip.showText(event.globalPos(), self.text(), self)
            return True
        return super().event(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, self._button_option())
        arrow = QRect(self.width() - _ICON - px(SPACING_SM), (self.height() - _ICON) // 2, _ICON, _ICON)
        mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
        themed_icon("chevron-down", role="text_caption").paint(painter, arrow, Qt.AlignmentFlag.AlignCenter, mode)
        note = self._shown_note()
        if note:
            tokens = get_theme_tokens()
            painter.setPen(to_qcolor(tokens["text_caption" if self.isEnabled() else "text_disabled"]))
            painter.drawText(QRect(0, 0, arrow.left() - px(SPACING_SM), self.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, note)
        painter.end()
