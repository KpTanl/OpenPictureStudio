from __future__ import annotations

import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLineEdit, QScrollArea, QVBoxLayout, QWidget

from resources.styles.theme_variables import CONTROL_HEIGHT_COMPACT, SPACING_MD, SPACING_SM, px
from ..icons import themed_icon
from .apple_button import AppleGhostButton, AppleIconButton
from .apple_menu import AppleDropDown
from .management_page import ManagementList, ManagementListGroup, ManagementListItem


def _when(timestamp: float) -> str:
    """会话最后更新的时间：今天写时分，今年写月日，更早写年月日。"""
    moment, now = time.localtime(timestamp), time.localtime()
    if moment[:3] == now[:3]:
        return time.strftime("%H:%M", moment)
    return time.strftime("%m-%d" if moment.tm_year == now.tm_year else "%Y-%m-%d", moment)


class ConversationList(QWidget):
    selected = pyqtSignal(str)
    new_requested = pyqtSignal(bool)
    manage_requested = pyqtSignal()
    back_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("assistantConversationList")
        self._state, self._tr, self._filter = {}, None, "photo"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_MD), px(SPACING_MD), px(SPACING_MD), px(SPACING_MD))
        layout.setSpacing(px(SPACING_SM))
        top = QHBoxLayout()
        self.back = AppleIconButton(themed_icon("chevron-left"), CONTROL_HEIGHT_COMPACT, self)
        self.back.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.back.clicked.connect(self.back_requested)
        self.new = AppleGhostButton(parent=self)
        self.new.clicked.connect(lambda: self.new_requested.emit(False))
        self.more = AppleIconButton(themed_icon("dots"), CONTROL_HEIGHT_COMPACT, self)
        self.more.clicked.connect(self.manage_requested)
        top.addWidget(self.back)
        top.addWidget(self.new, 1)
        top.addWidget(self.more)
        layout.addLayout(top)
        self.search = QLineEdit(self)
        self.search.textEdited.connect(lambda _text: self._project())
        layout.addWidget(self.search)
        self.filter = AppleDropDown(self, flat=True)
        self.filter.activated.connect(self._filter_changed)
        layout.addWidget(self.filter)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.list = ManagementList(scroll)
        self.list.selection_changed.connect(self.selected)
        scroll.setWidget(self.list)
        layout.addWidget(scroll, 1)
        self.general = AppleGhostButton(parent=self)
        self.general.clicked.connect(lambda: self.new_requested.emit(True))
        layout.addWidget(self.general)

    def set_state(self, state, tr, *, show_back=True):
        previous = self._state.get("current")
        self._state, self._tr = state, tr
        record = state.get("conversation") or {}
        if previous != state.get("current") and record.get("object_key", "") != state.get("photo", "") and not record.get("archived"):
            self._filter = "all"
        self.back.setVisible(show_back)
        self.back.setAccessibleName(tr("assistant.back_to_chat"))
        self.new.setText(tr("assistant.new_chat"))
        self.more.setAccessibleName(tr("assistant.conversation_actions"))
        self.search.setPlaceholderText(tr("assistant.search_conversations"))
        self.general.setText(tr("assistant.new_general"))
        self.filter.clear()
        for key in ("photo", "all", "archived"):
            self.filter.addItem(tr("assistant.filter." + key), key)
        self.filter.setCurrentIndex(("photo", "all", "archived").index(self._filter))
        self._project()

    def _filter_changed(self, index):
        self._filter = self.filter.itemData(index)
        self._project()

    def _project(self):
        if not self._tr:
            return
        query = self.search.text().casefold().strip()
        records = [record for record in self._state.get("conversations", ())
                   if (record["archived"] if self._filter == "archived" else not record["archived"])
                   and (self._filter != "photo" or record["object_key"] == self._state.get("photo", ""))
                   and (not query or query in (record["title"] + "\n" + record["search_text"]).casefold())]
        groups = []
        for pinned in (True, False):
            items = tuple(ManagementListItem(record["id"], record["title"] or self._tr("assistant.new_chat"),
                                            (self._tr("assistant.unread") + " · " if record["unread"] else "")
                                            + (record["photo_name"] or self._tr("assistant.general_conversation"))
                                            + " · " + _when(record["updated"]),
                                            "star" if pinned else "sparkles")
                          for record in records if record["pinned"] == pinned)
            if items:
                groups.append(ManagementListGroup("pinned" if pinned else "recent",
                                                  self._tr("assistant.pinned" if pinned else "assistant.recent"), items))
        self.list.set_groups(groups, preferred_key=self._state.get("current", ""))
