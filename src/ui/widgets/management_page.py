from dataclasses import dataclass

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                            QSplitter, QVBoxLayout, QWidget)

from resources.styles.theme_variables import INFO_SIDEBAR_WIDTH, SPACING_MD, SPACING_LG, SPACING_XL, px
from .grouped_list import GroupedOptionCard
from .notice_bar import NoticeBar
from .status_badge import StatusBadge


def status_kind(state):
    if state in ("ready", "no_resources", "installed", "bundled", "available", "active"):
        return "success"
    if state in ("queued", "discovering", "downloading", "verifying", "waiting", "uninstalling", "cleaning"):
        return "active"
    if state in ("missing", "disabled", "cancelled"):
        return "pending"
    return "warning"


def management_label(parent, object_name="", *, selectable=False, wrap=True):
    label = QLabel(parent)
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    policy = label.sizePolicy()
    policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored if wrap else QSizePolicy.Policy.Preferred)
    label.setSizePolicy(policy)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


@dataclass(frozen=True)
class ManagementListItem:
    key: str
    title: str
    subtitle: str = ""
    icon: str = ""


@dataclass(frozen=True)
class ManagementListGroup:
    key: str
    title: str
    items: tuple[ManagementListItem, ...]


class ManagementList(QWidget):
    selection_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._signature = ()
        self._groups = []
        self._selected = ""
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(px(SPACING_LG))

    def selected_key(self):
        return self._selected

    def selected_widget(self):
        return next((card.checked_widget() for _, card in self._groups if card.checked_key() == self._selected), None)

    def set_groups(self, groups, preferred_key=None):
        signature = tuple((group.key, tuple((item.key, item.icon) for item in group.items)) for group in groups)
        selected = self._selected if preferred_key is None else preferred_key
        changed = signature != self._signature
        previous = self._selected
        if signature != self._signature:
            while self._layout.count():
                item = self._layout.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            self._groups = []
            for group in groups:
                holder = QWidget(self)
                layout = QVBoxLayout(holder)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(px(SPACING_MD))
                title = management_label(holder, "managementGroupTitle")
                layout.addWidget(title)
                card = GroupedOptionCard(holder)
                for item in group.items:
                    card.add_item(item.key, item.title, item.subtitle, icon=item.icon)
                card.selection_changed.connect(lambda index, key: self._select(key, notify=True))
                layout.addWidget(card)
                self._layout.addWidget(holder)
                self._groups.append((title, card))
            # 条目从上往下排；放进可伸缩的滚动区时多出的高度留在末尾，不把各组拉开。
            self._layout.addStretch(1)
            self._signature = signature
        keys = [item.key for group in groups for item in group.items]
        for (title, card), group in zip(self._groups, groups):
            title.setText(group.title)
            title.setVisible(bool(group.title))
            for index, item in enumerate(group.items):
                card.set_item_text(index, item.title, item.subtitle)
        self._select(selected if selected in keys else next(iter(keys), ""), notify=False)
        return changed or previous != self._selected

    def select_key(self, key):
        if any(key == item_key for _, items in self._signature for item_key, _ in items):
            self._select(key, notify=True)
            return True
        return False

    def _select(self, key, *, notify):
        changed = key != self._selected
        self._selected = key
        for (title, card), (_, items) in zip(self._groups, self._signature):
            blocked = card.blockSignals(True)
            if any(item_key == key for item_key, _ in items):
                card.set_checked_key(key)
            else:
                card.clear_selection()
            card.blockSignals(blocked)
        if changed and notify:
            self.selection_changed.emit(key)


class ManagementInfo(QWidget):
    def __init__(self, keys, parent=None):
        super().__init__(parent)
        self._rows = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for key in keys:
            row = QFrame(self)
            row.setObjectName("managementInfoRow")
            line = QHBoxLayout(row)
            line.setContentsMargins(0, px(SPACING_MD), 0, px(SPACING_MD))
            line.setSpacing(px(SPACING_LG))
            title = management_label(row, "managementCaption")
            value = management_label(row, "managementValue", selectable=True)
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            line.addWidget(title, 1)
            line.addWidget(value, 3)
            layout.addWidget(row)
            self._rows[key] = (row, title, value)

    def set_row(self, key, title, value, *, visible=True):
        row, label, text = self._rows[key]
        label.setText(title)
        text.setText(value)
        row.setVisible(visible)


class ManagementPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("managementPage")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        layout.addWidget(self.splitter)
        navigation = QWidget(self)
        navigation.setObjectName("managementNavigation")
        navigation.setMinimumWidth(INFO_SIDEBAR_WIDTH // 2)
        self.navigation_layout = QVBoxLayout(navigation)
        self.navigation_layout.setContentsMargins(px(SPACING_MD), px(SPACING_LG), px(SPACING_MD), px(SPACING_LG))
        self.navigation_layout.setSpacing(px(SPACING_MD))
        self.navigation = ManagementList(navigation)
        self.navigation_layout.addWidget(self.navigation)
        self.navigation_layout.addStretch()
        self.navigation_scroll = QScrollArea(self)
        self.navigation_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.navigation_scroll.setWidgetResizable(True)
        self.navigation_scroll.setWidget(navigation)
        self.navigation_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.navigation.selection_changed.connect(self.ensure_selection_visible)
        self.splitter.addWidget(self.navigation_scroll)
        detail = QWidget(self)
        detail.setObjectName("managementDetail")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(0)
        self.scroll = QScrollArea(detail)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(self.scroll)
        content.setObjectName("managementContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_XL))
        content_layout.setSpacing(px(SPACING_LG))
        header = QHBoxLayout()
        titles = QVBoxLayout()
        self.title = management_label(content, "managementTitle")
        self.subtitle = management_label(content, "managementCaption")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        self.header_actions = QHBoxLayout()
        header.addLayout(self.header_actions)
        self.badge = StatusBadge(parent=content)
        header.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        content_layout.addLayout(header)
        self._notices = {kind: NoticeBar(kind, parent=content) for kind in ("info", "warning")}
        for notice in self._notices.values():
            notice.hide()
            content_layout.addWidget(notice)
        self.body_layout = QVBoxLayout()
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(px(SPACING_LG))
        content_layout.addLayout(self.body_layout)
        content_layout.addStretch()
        self.scroll.setWidget(content)
        detail_layout.addWidget(self.scroll, 1)
        footer = QFrame(detail)
        footer.setObjectName("managementActions")
        self.footer_layout = QVBoxLayout(footer)
        self.footer_layout.setContentsMargins(px(SPACING_XL), px(SPACING_LG), px(SPACING_XL), px(SPACING_LG))
        self.footer_layout.setSpacing(px(SPACING_MD))
        detail_layout.addWidget(footer)
        self.splitter.addWidget(detail)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([INFO_SIDEBAR_WIDTH, INFO_SIDEBAR_WIDTH * 2])

    def set_notice(self, text="", kind="info"):
        for name, notice in self._notices.items():
            notice.setVisible(bool(text) and kind == name)
            if kind == name:
                notice.set_text(text)

    def select_key(self, key):
        selected = self.navigation.select_key(key)
        if selected:
            self.ensure_selection_visible()
        return selected

    def ensure_selection_visible(self):
        selected = self.navigation.selected_widget()
        if selected is not None:
            self.navigation_scroll.widget().layout().activate()
            self.navigation_scroll.ensureWidgetVisible(selected)

    def replace_body(self, widget):
        while self.body_layout.count():
            previous = self.body_layout.takeAt(0).widget()
            if previous is not None:
                previous.deleteLater()
        self.body_layout.addWidget(widget)
