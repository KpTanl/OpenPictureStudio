"""图库窗口：左侧集合，中间当前集合的照片表，右侧关键词；以及智能集合的条件窗口。

中间顶部的主操作随所选集合变化：全部照片是“加入图库”，普通集合是“加入浏览区选中照片”，
智能集合是“编辑条件…”。评分、标记、定位等只作用于表里选中的照片，在表下方的操作条与右键菜单里。
图库本身的核验、备份、恢复在左下角。业务都经 runner 的命令完成，窗口只投影状态。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QSize, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                             QLineEdit, QProxyStyle, QScrollArea, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate,
                             QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from resources.styles.theme_variables import (CONTROL_HEIGHT, SPACING_LG, SPACING_MD, SPACING_SM, SPACING_XL,
                                              SPACING_XS, px)
from ...application.photo_library_store import RULE_VALUES
from ..base_window import BaseDialog
from ..icons import themed_icon
from ..theme import get_theme_tokens, to_qcolor
from ..widgets.apple_button import AppleButton, AppleIconButton, ApplePrimaryButton
from ..widgets.apple_choice import AppleCheckBox
from ..widgets.apple_menu import AppleMenu
from ..widgets.empty_state import EmptyState
from ..widgets.grouped_list import GroupedOptionCard
from ..widgets.management_page import management_label
from ..widgets.message_dialog import DialogButton, ask_choice, confirm_destructive, show_confirm
from ..widgets.notice_bar import NoticeBar
from ..widgets.segmented_control import AppleSegmentedControl
from ..widgets.setting_row import SettingRow, SettingSection
from .name_dialog import ask_name

# 集合列表里“全部照片”的键；集合 ID 是 UUID，不会与它相同
_ALL = "*"
_SIDEBAR_WIDTH = 220
_SIDEBAR_MIN_WIDTH = 180
# 文件夹放最后并占剩下的宽度，窗口窄时先收窄它（完整路径在悬停提示里）；其余各列定宽，放不下时省略中间
_COLUMNS = ("photo", "rating", "mark", "keywords", "availability", "folder")
_COLUMN_WIDTHS = {"photo": 140, "rating": 84, "mark": 84, "keywords": 120, "availability": 104}
_TARGET_KINDS = ("photo", "directory", "backup_out", "backup_in")


def _plain(text):
    return text.rstrip("…").rstrip(".")


def _menu_button(parent):
    """点了弹出菜单的按钮：文字右侧一个向下的箭头。"""
    button = AppleButton(parent=parent)
    button.setIcon(themed_icon("chevron-down"))
    button.setIconSize(QSize(14, 14))
    button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    return button


def _rating_text(tr, value):
    return "★" * value if value else tr("library.rating_none")


def _open_menu(menu, anchor):
    menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
    menu.deleteLater()


class _PlainRows(QProxyStyle):
    """照片表不画系统的行底：Windows 11 样式会在其中给每一格左侧各画一道选中标记。"""

    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QStyle.PrimitiveElement.PE_PanelItemViewRow:
            super().drawPrimitive(element, option, painter, widget)


class _RowDelegate(QStyledItemDelegate):
    """选中行的浅底在这里铺，不交给系统样式画。"""

    def paint(self, painter, option, index):
        option = QStyleOptionViewItem(option)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, to_qcolor(get_theme_tokens()["accent_soft"]))
            option.state &= ~QStyle.StateFlag.State_Selected
        option.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, option, index)


def _pane_header(parent, title):
    """侧栏与正文顶部一行：粗体标题，右侧放图标按钮。"""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(px(SPACING_XS))
    label = management_label(parent, "managementSubheading", wrap=False)
    label.setText(title)
    row.addWidget(label, 1)
    return row, label


class LibraryRuleDialog(BaseDialog):
    """智能集合的条件。name 给出时是新建，顶部带名称输入。符合数量随勾选即时更新。"""

    def __init__(self, runner, translate, rules=None, parent=None, *, name=None):
        super().__init__(parent=parent, title_key="library.smart", min_width=560, min_height=360,
                         default_width=720, default_height=680)
        self.runner, self.tr = runner, translate
        self.setWindowTitle(_plain(translate("library.new_smart_collection" if name is not None else "library.edit_rules")))
        rules = rules or {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_LG))
        layout.setSpacing(px(SPACING_MD))
        self.name = None
        if name is not None:
            caption = management_label(self, "settingRowTitle")
            caption.setText(translate("library.name"))
            layout.addWidget(caption)
            self.name = QLineEdit(name, self)
            self.name.setMaxLength(120)
            layout.addWidget(self.name)
            layout.addSpacing(px(SPACING_SM))
        hint = management_label(self, "managementCaption")
        hint.setText(translate("library.rules_hint"))
        layout.addWidget(hint)
        scroll = QScrollArea(self)
        scroll.setObjectName("libraryRules")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(px(SPACING_LG))
        section = SettingSection(content)
        section.set_title(translate("library.conditions"))
        self.fields = {}
        for key, values in RULE_VALUES.items():
            row = section.add_row(SettingRow(section))
            row.set_text(translate("library.rule." + key))
            self.fields[key] = []
            for value in values:
                text = _rating_text(translate, value) if key == "rating" else translate("library.rule_value." + str(value).lower())
                check = AppleCheckBox(text, row)
                check.setChecked(value in rules.get(key, []))
                check.clicked.connect(self.preview)
                row.add_control(check)
                self.fields[key].append((value, check))
        body.addWidget(section)
        keywords = SettingSection(content)
        keywords.set_title(translate("library.keywords"))
        names = list(runner.service.keywords.values())
        include = keywords.add_row(SettingRow(keywords))
        include.set_text(translate("library.include_keywords"))
        self.mode = AppleSegmentedControl(include)
        for value in ("all", "any"):
            self.mode.add_segment(translate("library." + value), value)
        self.mode.setCurrentIndex(1 if rules.get("keyword_mode") == "any" else 0)
        self.mode.currentIndexChanged.connect(self.preview)
        include.add_control(self.mode)
        self.include = self._keyword_grid(keywords, names, rules.get("keywords", []))
        exclude = keywords.add_row(SettingRow(keywords))
        exclude.set_text(translate("library.exclude_keywords"))
        self.exclude = self._keyword_grid(keywords, names, rules.get("exclude_keywords", []))
        if not names:
            for row in (include, exclude):
                row.set_text(row.title(), translate("library.no_keywords"))
            self.mode.setEnabled(False)
        body.addWidget(keywords)
        body.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, px(SPACING_SM), 0, 0)
        footer.setSpacing(px(SPACING_SM))
        self.count = management_label(self, "managementCaption")
        footer.addWidget(self.count, 1)
        cancel = AppleButton(translate("common.cancel"), self)
        cancel.clicked.connect(self.reject)
        self.confirm = ApplePrimaryButton(translate("library.create" if name is not None else "common.save"), self)
        self.confirm.clicked.connect(self.accept)
        self.confirm.setDefault(True)
        footer.addWidget(cancel)
        footer.addWidget(self.confirm)
        layout.addLayout(footer)
        if self.name is not None:
            self.name.textChanged.connect(lambda text: self.confirm.setEnabled(bool(text.strip())))
            self.confirm.setEnabled(bool(name.strip()))
            self.name.setFocus()
        self.preview()

    def _keyword_grid(self, section, keywords, checked):
        holder = QWidget(section)
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, px(SPACING_MD))
        grid.setHorizontalSpacing(px(SPACING_LG))
        grid.setVerticalSpacing(px(SPACING_XS))
        checks = []
        for index, keyword in enumerate(keywords):
            check = AppleCheckBox(keyword["name"], holder)
            check.setChecked(keyword["keyword_id"] in checked)
            check.clicked.connect(self.preview)
            grid.addWidget(check, index // 3, index % 3)
            checks.append((keyword["keyword_id"], check))
        holder.setVisible(bool(keywords))
        section.add_widget(holder)
        return checks

    def collection_name(self):
        return self.name.text() if self.name is not None else ""

    def rules(self):
        rules = {key: [value for value, check in fields if check.isChecked()] for key, fields in self.fields.items()}
        rules = {key: values for key, values in rules.items() if values}
        for key, checks in (("keywords", self.include), ("exclude_keywords", self.exclude)):
            values = [identity for identity, check in checks if check.isChecked()]
            if values:
                rules[key] = values
        rules["keyword_mode"] = self.mode.currentData()
        return rules

    def preview(self):
        result = self.runner.service.evaluate(self.rules())
        self.count.setText(self.tr("library.preview_count").format(count=result["count"], pending=result["pending_count"]))


class PhotoLibraryDialog(BaseDialog):
    photo_requested = Signal(str)

    def __init__(self, runner, translate, parent=None):
        super().__init__(parent=parent, title_key="library.title", min_width=1060, min_height=600,
                         default_width=1160, default_height=760, modal=False)
        self.runner, self.tr = runner, translate
        self._focused = False
        self.offset = 0
        self.limit = 100
        self._scope = ""
        self._collection = ""
        self._generation = 0
        self._pending = 0
        self._collection_signature = None
        self._keyword_signature = None
        self._ids, self._result = [], None
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("librarySplitter")
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_collections())
        self.splitter.addWidget(self._build_photos())
        self.splitter.addWidget(self._build_keywords())
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([_SIDEBAR_WIDTH, 1160 - 2 * _SIDEBAR_WIDTH, _SIDEBAR_WIDTH])
        root.addWidget(self.splitter)
        runner.changed.connect(self.refresh)
        runner.evaluated.connect(lambda: self.refresh() if self.isVisible() else None)
        runner.failed.connect(self._show_failure)
        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    # 版式
    # ------------------------------------------------------------------
    def _pane(self):
        pane = QWidget(self)
        pane.setObjectName("libraryPane")
        pane.setMinimumWidth(_SIDEBAR_MIN_WIDTH)
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        inner = QVBoxLayout()
        inner.setContentsMargins(px(SPACING_LG), px(SPACING_LG), px(SPACING_LG), px(SPACING_MD))
        inner.setSpacing(px(SPACING_MD))
        layout.addLayout(inner, 1)
        return pane, layout, inner

    @staticmethod
    def _list_scroll(parent, card, empty=None):
        scroll = QScrollArea(parent)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        layout.addWidget(card)
        if empty is not None:
            layout.addWidget(empty)
        layout.addStretch()
        scroll.setWidget(content)
        return scroll

    def _footer(self, pane, layout):
        footer = QFrame(pane)
        footer.setObjectName("libraryFooter")
        row = QHBoxLayout(footer)
        row.setContentsMargins(px(SPACING_LG), px(SPACING_MD), px(SPACING_LG), px(SPACING_MD))
        row.setSpacing(px(SPACING_SM))
        layout.addWidget(footer)
        return row

    def _build_collections(self):
        pane, layout, inner = self._pane()
        header, self.collections_title = _pane_header(pane, "")
        self.new_collection_button = AppleIconButton(themed_icon("plus"), parent=pane)
        self.new_collection_button.clicked.connect(self._new_collection_menu)
        header.addWidget(self.new_collection_button)
        inner.addLayout(header)
        self.collections = GroupedOptionCard(pane)
        self.collections.selection_changed.connect(lambda _index, key: self.collection_changed(key))
        inner.addWidget(self._list_scroll(pane, self.collections), 1)
        footer = self._footer(pane, layout)
        self.verify_button = AppleButton(parent=pane)
        self.verify_button.clicked.connect(lambda: self.runner.scan())
        footer.addWidget(self.verify_button, 1)
        self.library_more = AppleIconButton(themed_icon("dots"), parent=pane)
        self.library_more.clicked.connect(self._library_menu)
        footer.addWidget(self.library_more)
        return pane

    def _build_photos(self):
        body = QWidget(self)
        body.setObjectName("libraryPane")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_LG), px(SPACING_XL), px(SPACING_LG))
        layout.setSpacing(px(SPACING_MD))
        header = QHBoxLayout()
        header.setSpacing(px(SPACING_SM))
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = management_label(body, "managementTitle")
        self.caption = management_label(body, "managementCaption")
        titles.addWidget(self.title)
        titles.addWidget(self.caption)
        header.addLayout(titles, 1)
        self.primary = ApplePrimaryButton(parent=body)
        self.primary.clicked.connect(self._primary_action)
        self.browse_button = AppleButton(parent=body)
        self.browse_button.clicked.connect(self.browse)
        self.collection_more = AppleIconButton(themed_icon("dots"), parent=body)
        self.collection_more.clicked.connect(self._collection_menu)
        for widget in (self.primary, self.browse_button, self.collection_more):
            header.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)
        self.recovery_notice = NoticeBar("warning", parent=body)
        self.recovery_notice.action_triggered.connect(self.recovery)
        self.error_notice = NoticeBar("warning", parent=body)
        self.info_notice = NoticeBar("info", parent=body)
        for notice in (self.recovery_notice, self.error_notice, self.info_notice):
            notice.hide()
            layout.addWidget(notice)
        box = QFrame(body)
        box.setObjectName("listBox")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(1, 1, 1, 1)
        self.stack = QStackedWidget(box)
        self.photos = QTableWidget(0, len(_COLUMNS), self.stack)
        self.photos.setObjectName("libraryTable")
        self.photos.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.photos.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.photos.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.photos.setShowGrid(False)
        self.photos.setWordWrap(False)
        self.photos.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.photos.setItemDelegate(_RowDelegate(self.photos))
        rows = _PlainRows()
        rows.setParent(self.photos)
        self.photos.setStyle(rows)
        self.photos.verticalHeader().hide()
        self.photos.verticalHeader().setDefaultSectionSize(CONTROL_HEIGHT + px(SPACING_SM))
        columns = self.photos.horizontalHeader()
        columns.setHighlightSections(False)
        columns.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for index, key in enumerate(_COLUMNS):
            if key == "folder":
                columns.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
            else:
                columns.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
                self.photos.setColumnWidth(index, _COLUMN_WIDTHS[key])
        self.photos.doubleClicked.connect(self.open_photo)
        self.photos.itemSelectionChanged.connect(self._project_actions)
        self.photos.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.photos.customContextMenuRequested.connect(self._photo_context_menu)
        self.empty = EmptyState("photo", self.stack)
        self.stack.addWidget(self.photos)
        self.stack.addWidget(self.empty)
        box_layout.addWidget(self.stack)
        layout.addWidget(box, 1)
        bar = QHBoxLayout()
        bar.setSpacing(px(SPACING_SM))
        self.selection_label = management_label(body, "managementCaption", wrap=False)
        bar.addWidget(self.selection_label)
        bar.addSpacing(px(SPACING_XS))
        self.rate_button = _menu_button(body)
        self.rate_button.clicked.connect(lambda: _open_menu(self._rating_menu(), self.rate_button))
        self.mark_button = _menu_button(body)
        self.mark_button.clicked.connect(lambda: _open_menu(self._mark_menu(), self.mark_button))
        self.remove_member = AppleButton(parent=body)
        self.remove_member.clicked.connect(lambda: self.members("remove"))
        self.up = AppleIconButton(themed_icon("chevron-up"), parent=body)
        self.up.clicked.connect(lambda: self.reorder(-1))
        self.down = AppleIconButton(themed_icon("chevron-down"), parent=body)
        self.down.clicked.connect(lambda: self.reorder(1))
        self.photo_more = AppleIconButton(themed_icon("dots"), parent=body)
        self.photo_more.clicked.connect(lambda: _open_menu(self._photo_menu(), self.photo_more))
        for widget in (self.rate_button, self.mark_button, self.remove_member, self.up, self.down, self.photo_more):
            bar.addWidget(widget)
        bar.addStretch()
        self.pager = QWidget(body)
        pager = QHBoxLayout(self.pager)
        pager.setContentsMargins(0, 0, 0, 0)
        pager.setSpacing(px(SPACING_XS))
        self.previous = AppleIconButton(themed_icon("chevron-left"), parent=self.pager)
        self.previous.clicked.connect(lambda: self.page(-1))
        self.position = management_label(self.pager, "managementCaption", wrap=False)
        self.next = AppleIconButton(themed_icon("chevron-right"), parent=self.pager)
        self.next.clicked.connect(lambda: self.page(1))
        for widget in (self.previous, self.position, self.next):
            pager.addWidget(widget)
        bar.addWidget(self.pager)
        layout.addLayout(bar)
        return body

    def _build_keywords(self):
        pane, layout, inner = self._pane()
        header, self.keywords_title = _pane_header(pane, "")
        self.keyword_more = AppleIconButton(themed_icon("dots"), parent=pane)
        self.keyword_more.clicked.connect(lambda: _open_menu(self._keyword_menu(), self.keyword_more))
        self.new_keyword_button = AppleIconButton(themed_icon("plus"), parent=pane)
        self.new_keyword_button.clicked.connect(self.new_keyword)
        header.addWidget(self.keyword_more)
        header.addWidget(self.new_keyword_button)
        inner.addLayout(header)
        self.keywords = GroupedOptionCard(pane)
        self.keywords.selection_changed.connect(lambda _index, _key: self._project_actions())
        self.keywords_empty = management_label(pane, "managementCaption")
        inner.addWidget(self._list_scroll(pane, self.keywords, self.keywords_empty), 1)
        footer = self._footer(pane, layout)
        footer.setDirection(QHBoxLayout.Direction.TopToBottom)
        self.add_keyword = AppleButton(parent=pane)
        self.add_keyword.clicked.connect(lambda: self.assign_keyword(True))
        self.remove_keyword = AppleButton(parent=pane)
        self.remove_keyword.clicked.connect(lambda: self.assign_keyword(False))
        footer.addWidget(self.add_keyword)
        footer.addWidget(self.remove_keyword)
        return pane

    def showEvent(self, event):
        super().showEvent(event)
        if not self._focused:
            self.photos.setFocus()
            self._focused = True

    def retranslate(self):
        self.setWindowTitle(self.tr("library.title"))
        self.photos.setHorizontalHeaderLabels([self.tr("library." + key) for key in _COLUMNS])
        self.collections_title.setText(self.tr("library.collections"))
        self.keywords_title.setText(self.tr("library.keywords"))
        self.new_collection_button.setToolTip(_plain(self.tr("library.new_collection")))
        self.new_keyword_button.setToolTip(_plain(self.tr("library.new_keyword")))
        self.keyword_more.setToolTip(self.tr("library.keyword_actions"))
        self.collection_more.setToolTip(self.tr("library.collection_actions"))
        self.library_more.setToolTip(self.tr("library.library_actions"))
        self.photo_more.setToolTip(self.tr("library.photo_actions"))
        self.verify_button.setText(self.tr("library.verify"))
        self.browse_button.setText(self.tr("library.browse"))
        self.rate_button.setText(self.tr("library.rating"))
        self.mark_button.setText(self.tr("library.mark"))
        self.remove_member.setText(self.tr("library.remove_members"))
        self.up.setToolTip(self.tr("library.up"))
        self.down.setToolTip(self.tr("library.down"))
        self.previous.setToolTip(self.tr("library.previous"))
        self.next.setToolTip(self.tr("library.next"))
        self.add_keyword.setText(self.tr("library.add_keyword"))
        self.add_keyword.setToolTip(self.tr("library.add_keyword_hint"))
        self.remove_keyword.setText(self.tr("library.remove_keyword"))
        self.recovery_notice.set_action_text(self.tr("library.recovery"))
        self.keywords_empty.setText(self.tr("library.no_keywords"))

    def retranslate_ui(self):
        super().retranslate_ui()
        self.retranslate()
        self.refresh()

    # ------------------------------------------------------------------
    # 状态投影
    # ------------------------------------------------------------------
    def _show_failure(self, error):
        self.error_notice.set_text(self.tr(error))
        self.error_notice.show()

    def _show_message(self, text):
        self.info_notice.set_text(text)
        self.info_notice.setVisible(bool(text))

    def command(self, operation, values, callback=None):
        generation = self._generation
        values.setdefault("scope", self._scope)
        self._pending += 1
        self._show_message("")
        self._project_header()
        def done(value, error):
            if generation != self._generation:
                return
            self._pending = max(0, self._pending - 1)
            failure = error or self.runner.service.error
            if failure:
                self._show_failure(failure)
            else:
                self.error_notice.hide()
            self._project_header()
            if not error and callback:
                callback(value)
        self.runner.command(operation, values, done, cancelled=lambda: generation != self._generation)

    def hideEvent(self, event):
        self._generation += 1
        self._pending = 0
        super().hideEvent(event)

    def refresh(self):
        service = self.runner.service
        self._scope = service.scope
        if not service.loaded and service.error:
            self._show_failure("library.unavailable")
        if self._collection and self._collection not in service.collections:
            self._collection = ""
        self._fill_collections()
        self._fill_keywords()
        self._result = service.collection_result(self._collection) if self._collection else None
        ids = self._result["photos"] if self._result else list(service.photos)
        selected = set(self.selected())
        self.offset = min(self.offset, max(0, ((len(ids) - 1) // self.limit) * self.limit))
        shown = ids[self.offset:self.offset + self.limit]
        blocked = self.photos.blockSignals(True)
        self.photos.clearSelection()
        self.photos.setRowCount(len(shown))
        for row, identity in enumerate(shown):
            photo = service.photos[identity]
            path = photo["current_path"] or photo["last_known_path"]
            names = ", ".join(service.keywords[k]["name"] for k in photo["keywords"] if k in service.keywords)
            values = {"photo": Path(path).name, "rating": _rating_text(self.tr, photo["rating"]) if photo["rating"] else "",
                      "mark": "" if photo["mark"] == "unmarked" else self.tr("library.rule_value." + photo["mark"]),
                      "keywords": names, "availability": self.tr("library.rule_value." + service.state(photo)["availability"]),
                      "folder": str(Path(path).parent)}
            for column, key in enumerate(_COLUMNS):
                item = QTableWidgetItem(values[key])
                item.setData(Qt.ItemDataRole.UserRole, identity)
                if key in ("photo", "folder"):
                    item.setToolTip(path)
                elif key == "keywords" and names:
                    item.setToolTip(names)
                self.photos.setItem(row, column, item)
            if identity in selected:
                self.photos.selectionModel().select(self.photos.model().index(row, 0),
                                                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self.photos.blockSignals(blocked)
        self._ids = ids
        self.stack.setCurrentWidget(self.photos if ids else self.empty)
        self.pager.setVisible(len(ids) > self.limit)
        self.position.setText(f"{self.offset + 1 if ids else 0}–{self.offset + len(shown)} / {len(ids)}")
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(self.offset + self.limit < len(ids))
        pending = len(service.pending)
        self.recovery_notice.set_text(self.tr("library.recovery_pending", count=pending))
        self.recovery_notice.setVisible(bool(pending))
        self._project_header()
        self._project_actions()

    def _fill_collections(self):
        service = self.runner.service
        items = [(_ALL, self.tr("library.all_photos"), str(len(service.photos)), "photo")]
        for entry in service.collections.values():
            smart = entry["kind"] == "smart"
            items.append((entry["collection_id"], entry["name"], "" if smart else str(len(entry["members"])),
                          "adjustments-horizontal" if smart else "stack-2"))
        self._fill(self.collections, items, self._collection or _ALL, "_collection_signature")

    def _fill_keywords(self):
        service = self.runner.service
        counts = {}
        for photo in service.photos.values():
            for keyword in photo["keywords"]:
                counts[keyword] = counts.get(keyword, 0) + 1
        items = [(entry["keyword_id"], entry["name"], str(counts.get(entry["keyword_id"], 0)), "")
                 for entry in service.keywords.values()]
        self._fill(self.keywords, items, self.keywords.checked_key(), "_keyword_signature")
        self.keywords_empty.setVisible(not items)

    def _fill(self, card, items, selected, signature_name):
        """列表内容变了才重建；保留原来选中的一项，程序回填不发出选择信号。"""
        blocked = card.blockSignals(True)
        if items != getattr(self, signature_name):
            card.clear()
            for key, title, subtitle, icon in items:
                card.add_item(key, title, subtitle, icon=icon)
            setattr(self, signature_name, items)
        if selected:
            card.set_checked_key(selected)
        card.blockSignals(blocked)

    def _entry(self):
        return self.runner.service.collections.get(self._collection)

    def _project_header(self):
        service = self.runner.service
        entry = self._entry()
        ids = self._ids
        if entry is None:
            parts = [self.tr("library.photo_count", count=len(ids))]
        elif entry["kind"] == "ordinary":
            parts = [self.tr("library.kind.ordinary"), self.tr("library.photo_count", count=len(ids))]
        else:
            result = self._result
            parts = [self.tr("library.kind.smart"),
                     self.tr("library.preview_count").format(count=result["count"], pending=result["pending_count"])]
        missing = sum(service.availability.get(identity) == "missing" for identity in ids)
        if missing:
            parts.append(self.tr("library.missing_count", count=missing))
        if self._pending:
            parts.append(self.tr("library.working"))
        self.title.setText(entry["name"] if entry else self.tr("library.all_photos"))
        self.caption.setText(" · ".join(parts))
        kind = entry["kind"] if entry else ""
        self.primary.setText(self.tr("library.edit_rules" if kind == "smart" else "library.add_members" if kind else "library.register"))
        self.primary.setToolTip("" if kind == "smart" else self.tr("library.add_members_hint" if kind else "library.register_hint"))
        self.collection_more.setVisible(entry is not None)
        empty = ("library.empty_smart" if kind == "smart" else "library.empty_collection" if kind else "library.empty_library")
        self.empty.set_text(self.tr(empty), self.tr(empty + "_hint"))

    def _project_actions(self):
        entry = self._entry()
        ordinary = entry is not None and entry["kind"] == "ordinary"
        ids = self.selected()
        self.selection_label.setText(self.tr("library.selection_count", count=len(ids)) if ids else self.tr("library.no_selection"))
        for widget in (self.rate_button, self.mark_button, self.photo_more):
            widget.setEnabled(bool(ids))
        for widget in (self.remove_member, self.up, self.down):
            widget.setVisible(ordinary)
        self.remove_member.setEnabled(bool(ids))
        position = entry["members"].index(ids[0]) if ordinary and len(ids) == 1 and ids[0] in entry["members"] else -1
        self.up.setEnabled(position > 0)
        self.down.setEnabled(0 <= position < len(entry["members"]) - 1 if ordinary else False)
        keyword = self.keywords.checked_key()
        self.add_keyword.setEnabled(bool(keyword))
        self.remove_keyword.setEnabled(bool(keyword) and bool(ids))
        self.keyword_more.setEnabled(bool(keyword))

    @staticmethod
    def _selected(card):
        return card.checked_key()

    def selected(self):
        return [self.photos.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
                for index in self.photos.selectionModel().selectedRows() if self.photos.item(index.row(), 0)]

    def collection_changed(self, key):
        self._collection = "" if key == _ALL else key
        self.offset = 0
        self.refresh()

    def page(self, delta):
        self.offset = max(0, self.offset + delta * self.limit)
        self.refresh()

    # ------------------------------------------------------------------
    # 菜单
    # ------------------------------------------------------------------
    def _rating_menu(self, parent=None):
        menu = AppleMenu(parent or self)
        for value in range(6):
            action = menu.addAction(_rating_text(self.tr, value))
            action.triggered.connect(lambda _checked=False, value=value: self.metadata(rating=value))
        return menu

    def _mark_menu(self, parent=None):
        menu = AppleMenu(parent or self)
        for key, value in (("keep", "keep"), ("reject", "reject"), ("unmark", "unmarked")):
            action = menu.addAction(self.tr("library." + key))
            action.triggered.connect(lambda _checked=False, value=value: self.metadata(mark=value))
        return menu

    def _photo_menu(self, *, full=False):
        menu = AppleMenu(self)
        ids = self.selected()
        single = len(ids) == 1
        if full:
            for title, submenu in (("library.rating", self._rating_menu(menu)), ("library.mark", self._mark_menu(menu))):
                submenu.setTitle(self.tr(title))
                menu.addMenu(submenu)
            menu.addSeparator()
        action = menu.addAction(self.tr("library.open"))
        action.setEnabled(single and self.runner.service.availability.get(ids[0]) == "available" if single else False)
        action.triggered.connect(self.open_photo)
        for key, directory in (("relocate", False), ("search_folder", True)):
            action = menu.addAction(self.tr("library." + key))
            action.setEnabled(single)
            action.triggered.connect(lambda _checked=False, directory=directory: self.relocate(directory))
        entry = self._entry()
        if full and entry is not None and entry["kind"] == "ordinary":
            menu.addSeparator()
            action = menu.addAction(self.tr("library.remove_members"))
            action.triggered.connect(lambda: self.members("remove"))
        return menu

    def _photo_context_menu(self, position):
        index = self.photos.indexAt(position)
        if not index.isValid():
            return
        if not self.photos.selectionModel().isRowSelected(index.row(), index.parent()):
            self.photos.selectRow(index.row())
        menu = self._photo_menu(full=True)
        menu.exec(self.photos.viewport().mapToGlobal(position))
        menu.deleteLater()

    def _new_collection_menu(self):
        menu = AppleMenu(self)
        for key, smart in (("new_collection", False), ("new_smart_collection", True)):
            action = menu.addAction(self.tr("library." + key))
            action.triggered.connect(lambda _checked=False, smart=smart: self.new_collection(smart))
        _open_menu(menu, self.new_collection_button)

    def _collection_menu(self):
        entry = self._entry()
        if entry is None:
            return
        menu = AppleMenu(self)
        action = menu.addAction(self.tr("library.rename"))
        action.triggered.connect(self.rename_collection)
        if entry["kind"] == "smart":
            action = menu.addAction(self.tr("library.edit_rules"))
            action.triggered.connect(self.edit_rules)
        menu.addSeparator()
        action = menu.addAction(self.tr("library.delete_collection"))
        action.triggered.connect(self.delete_collection)
        _open_menu(menu, self.collection_more)

    def _keyword_menu(self):
        menu = AppleMenu(self)
        action = menu.addAction(self.tr("library.rename"))
        action.triggered.connect(self.rename_keyword)
        action = menu.addAction(self.tr("library.delete_keyword"))
        action.triggered.connect(self.delete_keyword)
        return menu

    def _library_menu(self):
        menu = AppleMenu(self)
        action = menu.addAction(self.tr("library.register_new"))
        action.triggered.connect(lambda: self.register(True))
        targets = AppleMenu(_plain(self.tr("library.select_target")), menu)
        for kind in _TARGET_KINDS:
            action = targets.addAction(self.tr("library.target." + kind))
            action.triggered.connect(lambda _checked=False, kind=kind: self.select_target(kind))
        menu.addMenu(targets)
        action = menu.addAction(self.tr("library.recovery"))
        action.setEnabled(bool(self.runner.service.pending))
        action.triggered.connect(self.recovery)
        menu.addSeparator()
        for key, callback in (("backup", self.backup), ("restore", self.restore)):
            action = menu.addAction(self.tr("library." + key))
            action.triggered.connect(lambda _checked=False, callback=callback: callback())
        _open_menu(menu, self.library_more)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _primary_action(self):
        entry = self._entry()
        if entry is None:
            self.register()
        elif entry["kind"] == "smart":
            self.edit_rules()
        else:
            self.members("add")

    def _browser_ready(self):
        if self.runner.service.browser.selected:
            return True
        self._show_failure("library.browser_empty")
        return False

    def register(self, new_content=False):
        if not self._browser_ready():
            return
        if new_content and not show_confirm(self, _plain(self.tr("library.register_new")), self.tr("library.confirm_new"),
                                            _plain(self.tr("library.register_new"))):
            return
        browser = self.runner.service.browser
        self.command("register_library_photos", {"scope": browser.scope, "new_content": bool(new_content)})

    def metadata(self, **values):
        ids = self.selected()
        if ids:
            self.command("edit_library_metadata", {"photos": ids, **values})

    def text(self, key, value="", action=""):
        return ask_name(self, _plain(self.tr("library." + key)), self.tr("library.name"), value, action)

    def new_keyword(self):
        name = self.text("new_keyword", action=self.tr("library.create"))
        if name is not None:
            self.command("manage_library_keyword", {"action": "create", "name": name})

    def rename_keyword(self):
        identity = self._selected(self.keywords)
        if identity:
            name = self.text("rename", self.runner.service.keywords[identity]["name"], self.tr("common.save"))
            if name is not None:
                self.command("manage_library_keyword", {"action": "rename", "keyword": identity, "name": name})

    def delete_keyword(self):
        identity = self._selected(self.keywords)
        if not identity:
            return
        count = sum(identity in photo["keywords"] for photo in self.runner.service.photos.values())
        if confirm_destructive(self, _plain(self.tr("library.delete_keyword")), self.tr("library.confirm_keyword").format(count=count),
                               self.tr("library.delete")):
            self.command("manage_library_keyword", {"action": "delete", "keyword": identity})

    def assign_keyword(self, add):
        identity = self._selected(self.keywords)
        if identity:
            if self.selected():
                self.metadata(**{"add" if add else "remove": [identity]})
            elif add and self._browser_ready():
                browser = self.runner.service.browser
                self.command("register_library_photos", {"scope": browser.scope},
                    lambda result: self.command("edit_library_metadata", {"photos": result["photos"], "add": [identity]}))

    def new_collection(self, smart=False):
        if smart:
            dialog = LibraryRuleDialog(self.runner, self.tr, parent=self, name="")
            accepted = dialog.exec()
            name, rules = dialog.collection_name(), dialog.rules()
            dialog.deleteLater()
            if not accepted:
                return
        else:
            name, rules = self.text("new_collection", action=self.tr("library.create")), {}
            if name is None:
                return
        self.command("manage_photo_collection", {"action": "create", "name": name, "kind": "smart" if smart else "ordinary", "rules": rules})

    def rename_collection(self):
        entry = self._entry()
        if entry:
            name = self.text("rename", entry["name"], self.tr("common.save"))
            if name is not None:
                self.command("manage_photo_collection", {"action": "rename", "collection": self._collection, "name": name})

    def edit_rules(self):
        entry = self._entry()
        if entry and entry["kind"] == "smart":
            dialog = LibraryRuleDialog(self.runner, self.tr, entry["rules"], self)
            accepted = dialog.exec()
            rules = dialog.rules()
            dialog.deleteLater()
            if accepted:
                self.command("manage_photo_collection", {"action": "rules", "collection": self._collection, "rules": rules})

    def delete_collection(self):
        if self._collection and confirm_destructive(self, _plain(self.tr("library.delete_collection")),
                                                    self.tr("library.confirm_collection"), self.tr("library.delete")):
            self.command("manage_photo_collection", {"action": "delete", "collection": self._collection})

    def members(self, action):
        entry = self._entry()
        if entry and entry["kind"] == "ordinary":
            ids = self.selected()
            if action == "add":
                if not self._browser_ready():
                    return
                browser = self.runner.service.browser
                collection = self._collection
                self.command("register_library_photos", {"scope": browser.scope},
                    lambda result: self.command("manage_photo_collection", {"action": "add", "collection": collection, "members": result["photos"]}))
                return
            if ids:
                self.command("manage_photo_collection", {"action": action, "collection": self._collection, "members": ids})

    def reorder(self, delta):
        entry = self._entry()
        ids = self.selected()
        if entry and entry["kind"] == "ordinary" and len(ids) == 1:
            members = list(entry["members"])
            position = members.index(ids[0])
            target = position + delta
            if 0 <= target < len(members):
                members[position], members[target] = members[target], members[position]
                self.command("manage_photo_collection", {"action": "reorder", "collection": self._collection, "members": members})

    def browse(self):
        self.command("show_photo_collection", {"collection": self._collection})

    def select_target(self, kind):
        label = self.tr("library.target." + kind)
        if kind == "directory":
            path = QFileDialog.getExistingDirectory(self, label)
        elif kind == "backup_out":
            path = QFileDialog.getSaveFileName(self, label, "library-backup.sqlite3", "SQLite (*.sqlite3)")[0]
        else:
            path = QFileDialog.getOpenFileName(self, label)[0]
        if path:
            self.runner.target(path, kind)
            self._show_message(self.tr("library.target_selected").format(name=path))

    def open_photo(self):
        ids = self.selected()
        if len(ids) == 1:
            photo = self.runner.service.photos[ids[0]]
            if self.runner.service.availability.get(ids[0]) == "available":
                self.photo_requested.emit(photo["current_path"])

    def _choose_candidate(self, rows):
        dialog = BaseDialog(title_default=_plain(self.tr("library.relocate")), min_width=520, min_height=0, parent=self)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_LG))
        layout.setSpacing(px(SPACING_MD))
        message = management_label(dialog, "dialogMessage")
        message.setText(self.tr("library.relocate_choose"))
        layout.addWidget(message)
        card = GroupedOptionCard(dialog)
        for index, row in enumerate(rows):
            card.add_item(str(index), row["new_path"])
        layout.addWidget(card)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, px(SPACING_SM), 0, 0)
        actions.addStretch()
        cancel = AppleButton(self.tr("common.cancel"), dialog)
        cancel.clicked.connect(dialog.reject)
        confirm = ApplePrimaryButton(self.tr("common.ok"), dialog)
        confirm.clicked.connect(dialog.accept)
        confirm.setDefault(True)
        actions.addWidget(cancel)
        actions.addWidget(confirm)
        layout.addLayout(actions)
        accepted = dialog.exec()
        index = int(card.checked_key() or 0)
        dialog.deleteLater()
        return index if accepted else None

    def relocate(self, directory=False):
        ids = self.selected()
        if len(ids) != 1:
            return
        path = QFileDialog.getExistingDirectory(self, self.tr("library.search_folder")) if directory else QFileDialog.getOpenFileName(self, self.tr("library.relocate"))[0]
        if not path:
            return
        target = self.runner.target(path, "directory" if directory else "photo")
        def candidates(result):
            rows = result["candidates"]
            if not rows:
                self._show_failure(result["rejected"][0]["reason"] if result.get("rejected") else "library.no_candidates")
                return
            index = 0
            if len(rows) > 1:
                index = self._choose_candidate(rows)
                if index is None:
                    return
            row = rows[index]
            message = self.tr("library.confirm_relocation").format(old=row["old_path"], new=row["new_path"])
            choice = ask_choice(self, _plain(self.tr("library.relocate")), message,
                                (DialogButton("cancel", self.tr("common.cancel")),
                                 DialogButton("confirm", self.tr("library.relocate_action"), "primary")),
                                default="confirm", escape="cancel")
            if choice == "confirm":
                self.command("confirm_photo_relocation", {"proposal": row["proposal"]})
            else:
                self.command("cancel_photo_relocation", {"proposal": row["proposal"]})
        self.command("prepare_photo_relocation", {"photo": ids[0], "target": target}, candidates)

    def backup(self):
        path = QFileDialog.getSaveFileName(self, self.tr("library.backup"), "library-backup.sqlite3", "SQLite (*.sqlite3)")[0]
        if path:
            self.command("backup_photo_library", {"target": self.runner.target(path, "backup_out")})

    def restore(self):
        path = QFileDialog.getOpenFileName(self, self.tr("library.restore"), "", "SQLite (*.sqlite3)")[0]
        if not path:
            return
        target = self.runner.target(path, "backup_in")
        def inspected(result):
            if confirm_destructive(self, _plain(self.tr("library.restore")), self.tr("library.confirm_restore_counts").format(count=result["photos"]),
                                   _plain(self.tr("library.restore"))):
                self.command("restore_photo_library", {"target": target, "confirmation": "restore"})
        self.command("inspect_library_backup", {"target": target}, inspected)

    def recovery(self):
        for row in tuple(self.runner.service.pending):
            payload = row["payload"]
            message = self.tr("library.confirm_recovery").format(old=payload["old_path"], new=payload["new_path"])
            if show_confirm(self, _plain(self.tr("library.recovery")), message, self.tr("library.recovery_action")):
                try:
                    self.runner.retain_recovery_drafts(row["operation_id"])
                except ValueError as error:
                    self._show_failure(str(error))
                return
