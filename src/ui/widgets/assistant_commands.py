from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QFrame, QListWidget, QListWidgetItem, QVBoxLayout

from resources.styles.theme_variables import ASSISTANT_COMMAND_WIDTH, CONTROL_HEIGHT, SPACING_XS, px


COMMANDS = (("agent", "assistant.mode.agent"), ("plan", "assistant.mode.plan"),
            ("model", "assistant.command.model"), ("effort", "assistant.thinking"),
            ("context", "assistant.context_title"), ("stop", "assistant.stop"))


class AssistantCommandMenu(QFrame):
    selected = Signal(str)

    def __init__(self, editor):
        super().__init__(editor, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("assistantCommandMenu")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._editor, self._rows, self._signature = editor, [], None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XS), px(SPACING_XS), px(SPACING_XS), px(SPACING_XS))
        self.items = QListWidget(self)
        self.items.setObjectName("assistantCommandList")
        self.items.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.items.itemClicked.connect(lambda item: self._choose(item.data(Qt.ItemDataRole.UserRole)))
        layout.addWidget(self.items)
        editor.installEventFilter(self)
        editor.textEdited.connect(self._filter)
        editor.textChanged.connect(self._programmed)

    def set_state(self, identity, translate, *, effort, running):
        rows = [(name, translate(key), name != "effort" or effort, name != "stop" or running)
                for name, key in COMMANDS]
        signature = (identity, tuple(rows))
        if signature != self._signature:
            self.hide()
            self._signature = signature
            self._rows = rows

    def _programmed(self):
        if self._editor._projecting:
            self.hide()

    def _filter(self, text):
        match = re.match(r"^/([^\s]*)", text)
        if not match or self._editor.textCursor().position() > match.end() or self._editor._composing:
            self.hide()
            return
        query = match[1].casefold()
        rows = [row for row in self._rows if query in row[0].casefold() or query in row[1].casefold()]
        self.items.clear()
        for name, label, effort, running in rows:
            item = QListWidgetItem("/" + name + "   " + label, self.items)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setSizeHint(QSize(0, CONTROL_HEIGHT))
            if not effort or not running:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        if not rows:
            self.hide()
            return
        self._move(1, initial=True)
        screen = self._editor.screen().availableGeometry()
        width = min(ASSISTANT_COMMAND_WIDTH, self._editor.width(), screen.width())
        height = min(len(rows) * CONTROL_HEIGHT + px(SPACING_XS) * 2, screen.height())
        origin = self._editor.mapToGlobal(QPoint(0, 0))
        left = max(screen.left(), min(origin.x(), screen.right() - width + 1))
        top = origin.y() - height - px(SPACING_XS)
        if top < screen.top():
            top = min(screen.bottom() - height + 1, origin.y() + self._editor.height())
        self.setGeometry(left, max(screen.top(), top), width, height)
        self.show()

    def _move(self, direction, *, initial=False):
        count = self.items.count()
        if not count:
            return
        start = -1 if initial else self.items.currentRow()
        for offset in range(1, count + 1):
            index = (start + direction * offset) % count
            if self.items.item(index).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.items.setCurrentRow(index)
                return
        self.items.setCurrentRow(-1)

    def _choose(self, name):
        row = next((row for row in self._rows if row[0] == name), None)
        if row is None or not row[2] or not row[3]:
            return
        match = re.match(r"^/[^\s]*\s?", self._editor.toPlainText())
        if match:
            cursor = self._editor.textCursor()
            cursor.beginEditBlock()
            cursor.setPosition(0)
            cursor.setPosition(match.end(), QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            cursor.endEditBlock()
            self._editor.setTextCursor(cursor)
        self.hide()
        self._editor.setFocus()
        self.selected.emit(name)

    def eventFilter(self, watched, event):
        if watched is self._editor and event.type() == QEvent.Type.KeyPress and not self._editor._composing:
            key = event.key()
            if event.modifiers() == Qt.KeyboardModifier.NoModifier:
                if self.isVisible() and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                    self._move(-1 if key == Qt.Key.Key_Up else 1)
                    return True
                if self.isVisible() and key == Qt.Key.Key_Escape:
                    self.hide()
                    return True
                if self.isVisible() and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                    item = self.items.currentItem()
                    if item:
                        self._choose(item.data(Qt.ItemDataRole.UserRole))
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    if self._editor.toPlainText().strip() == "/":
                        self._filter(self._editor.toPlainText())
                        return True
                    match = re.match(r"^/([^\s]+)(?:\s|$)", self._editor.toPlainText())
                    if match and any(row[0] == match[1] for row in self._rows):
                        self._choose(match[1])
                        return True
        if self.isVisible():
            if event.type() == QEvent.Type.MouseButtonPress:
                point = event.globalPosition().toPoint()
                editor_origin = self._editor.mapToGlobal(QPoint(0, 0))
                editor_rect = self._editor.rect().translated(editor_origin)
                if not self.geometry().contains(point) and not editor_rect.contains(point):
                    self.hide()
            elif watched is self._editor and event.type() in (QEvent.Type.Hide, QEvent.Type.FocusOut):
                self.hide()
            elif watched is self._editor.window() and event.type() == QEvent.Type.WindowDeactivate:
                self.hide()
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        QApplication.instance().installEventFilter(self)

    def hideEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)
