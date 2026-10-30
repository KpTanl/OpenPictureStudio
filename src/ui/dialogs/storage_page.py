from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QFileDialog, QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget
from resources.styles.theme_variables import DIALOG_SIZES, SPACING_LG, SPACING_MD, SPACING_SM, SPACING_XL, SPACING_XS, px

from ...application.storage_paths import (LOCATION_KEYS, TEMP_KEYS, data_root, read_settings,
                                         register_project, update_settings)
from ..base_window import BaseDialog
from ..icons import themed_icon
from ..widgets.apple_button import (AppleCompactButton, AppleDestructiveButton, AppleGhostButton,
                                    AppleIconButton, ApplePrimaryButton)
from ..widgets.apple_choice import AppleCheckBox
from ..widgets.apple_menu import AppleMenu
from ..widgets.apple_spin_box import AppleSpinBox
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.empty_state import EmptyState
from ..widgets.management_page import management_label
from ..widgets.message_dialog import DialogButton, ask_choice, confirm_destructive, show_confirm
from ..widgets.notice_bar import NoticeBar
from ..widgets.setting_row import SettingRow, SettingSection


def size_text(size):
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KiB"
    return f"{size / 1024 ** 3:.2f} GiB" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.2f} MiB"


def open_path(path):
    path = Path(path)
    if path.is_file() or not path.exists():
        path = path.parent
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class StorageListDialog(BaseDialog):
    selection_changed = Signal()
    PAGE_SIZE = 50

    def __init__(self, title, translate, parent=None, *, note="", selectable=False, empty_key="storage.empty",
                 location=""):
        """location 给出时，各条目同在这个位置：位置只在列表上方写一次，条目不再各带打开位置的按钮。"""
        size = DIALOG_SIZES["storage"]
        super().__init__(title_default=title, min_width=size["min_w"], min_height=size["min_h"],
                         default_width=size["default_w"], default_height=size["default_h"], parent=parent)
        self.tr = translate
        self._entries, self._checked, self._page = [], set(), 0
        self._checks = {}
        self._entries_set = False
        self._selectable = selectable
        self._empty_key = empty_key
        self._sized = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_XL))
        layout.setSpacing(px(SPACING_MD))
        self.note = management_label(self)
        self.note.setText(note)
        self.note.setVisible(bool(note))
        layout.addWidget(self.note)
        self.location = QWidget(self)
        place = QHBoxLayout(self.location)
        place.setContentsMargins(0, 0, 0, 0)
        place.setSpacing(px(SPACING_SM))
        self.location_text = management_label(self.location, "managementCaption", selectable=True)
        self.location_text.setText(translate("storage.location", path=location))
        place.addWidget(self.location_text, 1)
        locate = AppleIconButton(themed_icon("folder-open"), parent=self.location)
        locate.setToolTip(translate("storage.open"))
        locate.clicked.connect(lambda _checked=False: open_path(location))
        place.addWidget(locate, 0, Qt.AlignmentFlag.AlignVCenter)
        self.location.setVisible(bool(location))
        layout.addWidget(self.location)
        self.card = QFrame(self)
        self.card.setObjectName("listBox")
        card = QVBoxLayout(self.card)
        card.setContentsMargins(1, 1, 1, 1)
        card.setSpacing(0)
        self.header = QFrame(self.card)
        self.header.setObjectName("listBoxHeader")
        header = QHBoxLayout(self.header)
        header.setContentsMargins(px(SPACING_LG), px(SPACING_SM), px(SPACING_LG), px(SPACING_SM))
        header.setSpacing(px(SPACING_MD))
        self.select_all = AppleCheckBox(translate("storage.select_all"), self.header)
        self.select_all.clicked.connect(lambda _checked=False: self._select_all())
        self.selection_count = management_label(self.header, "managementCaption", wrap=False)
        header.addWidget(self.select_all)
        header.addStretch()
        header.addWidget(self.selection_count)
        self.header.hide()
        card.addWidget(self.header)
        self.scroll = QScrollArea(self.card)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget(self.scroll)
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(px(SPACING_LG), 0, px(SPACING_LG), 0)
        self.body.setSpacing(0)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.content)
        card.addWidget(self.scroll, 1)
        layout.addWidget(self.card, 1)
        self.pager = QWidget(self)
        pager = QHBoxLayout(self.pager)
        pager.setContentsMargins(0, 0, 0, 0)
        self.previous = AppleIconButton(themed_icon("chevron-left"), parent=self.pager)
        self.next = AppleIconButton(themed_icon("chevron-right"), parent=self.pager)
        self.position = management_label(self.pager, "managementCaption", wrap=False)
        self.previous.clicked.connect(lambda: self.change_page(-1))
        self.next.clicked.connect(lambda: self.change_page(1))
        pager.addStretch()
        pager.addWidget(self.previous)
        pager.addWidget(self.position)
        pager.addWidget(self.next)
        layout.addWidget(self.pager)
        self.pager.hide()
        self.actions = QHBoxLayout()
        self.actions.setContentsMargins(0, px(SPACING_XS), 0, 0)
        self.actions.setSpacing(px(SPACING_SM))
        self.actions.addStretch()
        layout.addLayout(self.actions)

    def add_action(self, text, callback, *, role="secondary"):
        button = {"secondary": AppleGhostButton, "primary": ApplePrimaryButton,
                  "danger": AppleDestructiveButton}[role](text, self)
        button.setAutoDefault(False)
        button.clicked.connect(lambda _checked=False: callback())
        self.actions.addWidget(button)
        return button

    def selected_records(self):
        return [entry[0] for index, entry in enumerate(self._entries) if index in self._checked]

    def set_entries(self, entries):
        entries = list(entries)
        if self._entries_set and entries == self._entries:
            self.selection_changed.emit()
            return
        was_empty = not self._entries
        selected = {entry.get("id") for entry in self.selected_records() if entry.get("id")}
        self._entries = entries
        self._entries_set = True
        self._checked = {index for index, entry in enumerate(entries) if entry[0].get("id") in selected}
        self._page = min(self._page, max(0, (len(entries) - 1) // self.PAGE_SIZE))
        self.change_page(0)
        self.update_selection()
        self.selection_changed.emit()
        if self.isVisible() and was_empty != (not entries):
            self.fit_content()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._sized:
            self.fit_content()
            self.scroll.setFocus()
            self._sized = True

    def fit_content(self):
        if self.isMaximized() or self.isFullScreen():
            return
        self.layout().activate()
        width = self.scroll.viewport().width()
        body_height = self.body.heightForWidth(width) if self.body.hasHeightForWidth() else self.body.sizeHint().height()
        chrome = self.height() - self.scroll.viewport().height()
        maximum = min(DIALOG_SIZES["storage"]["default_h"], self.screen().availableGeometry().height() - 2 * px(SPACING_XL))
        self.resize(self.width(), max(self.minimumHeight(), min(maximum, chrome + body_height)))

    def change_page(self, offset):
        self._page += offset
        while self.body.count():
            item = self.body.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        self._checks = {}
        start = self._page * self.PAGE_SIZE
        for index in range(start, min(start + self.PAGE_SIZE, len(self._entries))):
            record, title, description, path = self._entries[index]
            leading = QWidget(self.content)
            line = QHBoxLayout(leading)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(px(SPACING_MD))
            if self._selectable:
                check = AppleCheckBox("", leading)
                check.setAccessibleName(title)
                check.setChecked(index in self._checked)
                check.clicked.connect(lambda checked, index=index: self._select(index, checked))
                line.addWidget(check, 0, Qt.AlignmentFlag.AlignVCenter)
                self._checks[index] = check
            labels = QVBoxLayout()
            labels.setContentsMargins(0, 0, 0, 0)
            labels.setSpacing(px(SPACING_XS) // 2)
            name = management_label(leading, "settingRowTitle", selectable=True)
            name.setText(title)
            labels.addWidget(name)
            if description:
                caption = management_label(leading, "settingRowDescription", selectable=True)
                caption.setText(description)
                labels.addWidget(caption)
            line.addLayout(labels, 1)
            row = SettingRow(self.content, leading=leading)
            row.setProperty("first", index == start)
            if path:
                locate = AppleIconButton(themed_icon("folder-open"), parent=row)
                locate.setToolTip(self.tr("storage.open"))
                locate.clicked.connect(lambda _checked=False, path=path: open_path(path))
                row.add_control(locate)
            self.body.addWidget(row)
        if not self._entries:
            empty = EmptyState("folder-open", self.content)
            empty.set_text(self.tr(self._empty_key))
            self.body.addWidget(empty)
        self.pager.setVisible(len(self._entries) > self.PAGE_SIZE)
        self.position.setText(self.tr("storage.page_count", current=self._page + 1,
                                     total=max(1, (len(self._entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)))
        self.previous.setEnabled(self._page > 0)
        self.next.setEnabled(start + self.PAGE_SIZE < len(self._entries))
        self.previous.setToolTip(self.tr("storage.previous"))
        self.next.setToolTip(self.tr("storage.next"))
        self.scroll.verticalScrollBar().setValue(0)

    def update_selection(self):
        records = self.selected_records()
        if records and any("bytes" in entry[0] for entry in self._entries):
            text = self.tr("storage.selection_size", count=len(records),
                           size=size_text(sum(record.get("bytes", 0) for record in records)))
        else:
            text = self.tr("storage.selection_count", count=len(records))
        self.selection_count.setText(text)
        self.select_all.setCheckState(Qt.CheckState.Unchecked if not records else
                                      Qt.CheckState.Checked if len(records) == len(self._entries) else
                                      Qt.CheckState.PartiallyChecked)
        self.header.setVisible(self._selectable and len(self._entries) > 1)

    def _select(self, index, checked):
        if checked:
            self._checked.add(index)
        else:
            self._checked.discard(index)
        self.update_selection()
        self.selection_changed.emit()

    def _select_all(self):
        """全选覆盖所有页；已经全选时再点一次清空。"""
        everything = len(self._checked) == len(self._entries)
        self._checked = set() if everything else set(range(len(self._entries)))
        for index, check in self._checks.items():
            check.setChecked(index in self._checked)
        self.update_selection()
        self.selection_changed.emit()


class StoragePage(QWidget):
    manage_requested = Signal(str)
    discard_limits_requested = Signal()
    OWNER_KEYS = ("models", "runtimes", "assistant", "attachments", "styles", "library", "exports")
    CLEAN_KEYS = (*TEMP_KEYS, "drafts", "mask_assets", "projects", "logs", "backups", "wallpaper",
                  "presets", "model_downloads", "runtime_downloads")
    GROUPS = (
        ("caches", ("thumbnails", "edit_cache")),
        ("records", ("drafts", "mask_assets", "projects", "library", "backups", "exports",
                     "assistant", "attachments", "styles", "presets")),
        ("work", TEMP_KEYS),
        ("resources", ("models", "model_downloads", "runtimes", "runtime_downloads", "legacy_models")),
        ("other", ("logs", "wallpaper", "originals", "outputs", "legacy_temp", "settings", "configuration", "other")),
    )

    def __init__(self, runner, translate, parent=None, *, preferences=None, header_actions=None):
        super().__init__(parent)
        self.runner, self.tr = runner, translate
        self._items, self._rows, self._buttons = [], {}, {}
        self._cache_keys, self._cache_actions = set(), {}
        self._pending_limits, self._limits = {}, {}
        self._limits_close_pending = False
        self._refreshed, self._job_count, self._total = "", 0, 0
        self._scanned = False
        self._limits_timer = QTimer(self)
        self._limits_timer.setSingleShot(True)
        self._limits_timer.setInterval(350)
        self._limits_timer.timeout.connect(self.flush_limits)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_LG))
        self.summary = SettingRow(self)
        self._buttons["refresh"] = AppleCompactButton(parent=self.summary)
        self._buttons["refresh"].clicked.connect(self.refresh)
        self._buttons["cancel_scan"] = AppleCompactButton(parent=self.summary)
        self._buttons["cancel_scan"].clicked.connect(lambda: self.runner.cancel())
        self.more = AppleIconButton(themed_icon("dots"), parent=self.summary)
        self.more.clicked.connect(self.show_menu)
        for button in (self._buttons["refresh"], self._buttons["cancel_scan"], self.more):
            if header_actions is None:
                self.summary.add_control(button)
            else:
                header_actions.addWidget(button)
        layout.addWidget(self.summary)
        self.notice = NoticeBar("warning", parent=self)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.limits_notice = NoticeBar("info", parent=self)
        self.limits_notice.action_triggered.connect(self.discard_limits_requested)
        self.limits_notice.hide()
        layout.addWidget(self.limits_notice)
        self.cache_layout = QVBoxLayout()
        self.cache_layout.setContentsMargins(0, 0, 0, 0)
        self.cache_layout.setSpacing(px(SPACING_LG))
        layout.addLayout(self.cache_layout)
        self._groups = {}
        for name, keys in self.GROUPS:
            card = CollapsibleCard("", self, is_expanded=name in ("caches", "records"))
            self._groups[name] = card
            layout.addWidget(card)
            card.hide()
        self.rules = CollapsibleCard("", self, is_expanded=False)
        self.limits_section = SettingSection(self.rules)
        self.rules.add_widget(self.limits_section)
        layout.addWidget(self.rules)
        limits = (read_settings() if preferences is None else preferences)["limits"]
        for key in (*TEMP_KEYS, "log_bytes", "log_count", "backup_days"):
            row = SettingRow(self)
            control = AppleSpinBox(row)
            control.setRange(1024 if key == "log_bytes" else 0 if key in (*TEMP_KEYS, "backup_days") else 1,
                             2147483647 if key == "log_bytes" else 100 if key == "log_count" else 1048576)
            control.setKeyboardTracking(False)
            control.setValue(limits.get(key, 2_000_000 if key == "log_bytes" else 3 if key == "log_count" else 0))
            control.setEnabled(runner is not None)
            control.valueEdited.connect(lambda value, key=key: self.set_limit(key, value))
            row.add_control(control)
            self._limits[key] = row
            if key not in TEMP_KEYS:
                self.limits_section.add_row(row)
            else:
                row.hide()
        if runner is not None:
            runner.finished.connect(self.finished)
            runner.busy_changed.connect(self.busy_changed)
            runner.limits_changed.connect(self._update_limits_notice)
        self.retranslate()

    def add_cache_section(self, key, section, usage_row):
        self._cache_keys.add(key)
        self.cache_layout.addWidget(section)
        button = AppleIconButton(themed_icon("dots"), parent=usage_row)
        button.clicked.connect(lambda: self.show_category_menu(key, button))
        usage_row.add_control(button)
        self._cache_actions[key] = button
        self.project()

    def retranslate(self):
        self._buttons["refresh"].setText(self.tr("storage.refresh"))
        self._buttons["cancel_scan"].setText(self.tr("storage.cancel_scan"))
        self.more.setToolTip(self.tr("storage.more"))
        for name, card in self._groups.items():
            card.set_title(self.tr("storage.group." + name))
        for key, button in self._cache_actions.items():
            button.setToolTip(self.title(key) + " · " + self.tr("storage.more"))
        self.rules.set_title(self.tr("storage.rules"))
        for key, row in self._limits.items():
            row.set_text(self.tr("storage.limit." + key), self.tr("storage.limit_hint." + ("temp" if key in TEMP_KEYS else key)))
        self.limits_notice.set_action_text(self.tr("storage.discard_limits_close"))
        self._update_limits_notice()
        self.project()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.has_pending_limits:
            self.refresh()

    def hideEvent(self, event):
        if self.runner is not None and self.runner.cancellable:
            self.runner.cancel()
        super().hideEvent(event)

    def refresh(self):
        if self.runner is not None:
            self.summary.set_message()
            self.runner.refresh()

    def show_menu(self):
        menu = AppleMenu(self)
        for key, callback in (("register", self.register), ("backup_all", lambda: self.backup("data")),
                              ("move_data", lambda: self.move("data")), ("pending", self.pending),
                              ("history", self.history), ("cancel_jobs", self.cancel_jobs)):
            if key in ("pending", "cancel_jobs"):
                menu.addSeparator()
            action = menu.addAction(self.tr("storage." + key))
            action.setEnabled(self.runner is not None and not self.runner.busy)
            action.triggered.connect(lambda _checked=False, callback=callback: callback())
        menu.exec(self.more.mapToGlobal(self.more.rect().bottomLeft()))
        menu.deleteLater()

    def busy_changed(self, busy):
        self._buttons["refresh"].setEnabled(not busy and self.runner is not None)
        self._buttons["cancel_scan"].setVisible(bool(self.runner is not None and self.runner.cancellable))
        self.more.setEnabled(not busy and self.runner is not None)
        self.project()
        self._update_limits_notice()

    def _update_limits_notice(self):
        discarding = self.runner is not None and self.runner.discarding_limits
        failed = self.runner is not None and self.runner.limits_failed
        key = ("storage.discarding_limits" if discarding else
               "storage.limits_unsaved" if failed else
               "storage.saving_limits")
        self.limits_notice.set_text(self.tr(key))
        self.limits_notice.setVisible(discarding or self.has_pending_limits and (failed or self._limits_close_pending))
        self.limits_notice.action_button.setEnabled(not discarding)
        for row in self._limits.values():
            for control in row.findChildren(AppleSpinBox):
                control.setEnabled(self.runner is not None and not discarding)

    def set_limits_close_pending(self, pending):
        self._limits_close_pending = pending
        self._update_limits_notice()

    def title(self, key):
        return self.tr("storage.kind.retired") if key.startswith("retired:") else self.tr("storage.kind." + key)

    def item_caption(self, item):
        state = self.tr("storage.partial" if item.errors else "storage.complete")
        return self.tr("storage.item_usage", size=size_text(item.size), count=item.files, state=state)

    def project(self):
        self.summary.set_text(self.tr("storage.used_total", size=size_text(self._total)) if self._scanned else self.tr("storage.overview"),
                              self.tr("storage.saving_limits") if self.runner is not None and self.runner.saving_limits else
                              self.tr("storage.scanning") if self.runner is not None and self.runner.busy else
                              self.tr("storage.updated", time=self._refreshed, count=self._job_count) if self._refreshed else
                              self.tr("storage.hint"))
        visible = set()
        for name, keys in self.GROUPS:
            card = self._groups[name]
            items = [item for item in self._items if item.key not in self._cache_keys and
                     (item.key in keys or name == "other" and item.key.startswith("retired:"))]
            card.setVisible(bool(items))
            for item in items:
                visible.add(item.key)
                if item.key not in self._rows:
                    row = self._limits[item.key] if item.key in TEMP_KEYS else SettingRow(card)
                    button = AppleCompactButton(parent=row)
                    button.clicked.connect(lambda _checked=False, key=item.key: self.open_category(key))
                    row.add_control(button)
                    more = AppleIconButton(themed_icon("dots"), parent=row)
                    more.clicked.connect(lambda _checked=False, key=item.key, anchor=more: self.show_category_menu(key, anchor))
                    row.add_control(more)
                    card.add_widget(row)
                    self._rows[item.key] = row, button, more
                row, button, more = self._rows[item.key]
                action = self.primary_action(item.key)
                label = ("storage.view_locations" if item.key == "legacy_temp" else "storage.view_files") if action == "locations" else "storage." + action
                button.setText(self.tr(label) if action else "")
                button.setVisible(bool(action))
                button.setEnabled(self.runner is not None and not self.runner.busy)
                more.setVisible(bool(item.paths))
                more.setEnabled(self.runner is not None and not self.runner.busy)
                more.setToolTip(self.title(item.key) + " · " + self.tr("storage.more"))
                description = self.item_caption(item) if item.key != "settings" else self.tr("storage.system_path")
                if item.free is not None:
                    description += " · " + self.tr("storage.free", size=size_text(item.free))
                if item.key in TEMP_KEYS:
                    description += "\n" + self.tr("storage.limit_hint.temp")
                elif item.key in ("drafts", "mask_assets", "projects"):
                    description += "\n" + self.tr("storage.description." + item.key)
                locations = "\n".join(str(path) for path in item.paths)
                if item.free is not None:
                    locations += "\n" + self.tr("storage.free", size=size_text(item.free))
                row.setToolTip(locations)
                row.set_text(self.tr("storage.limit." + item.key) if item.key in TEMP_KEYS else self.title(item.key), description)
                row.show()
        for key, (row, button, more) in self._rows.items():
            row.setVisible(key in visible)
        self._buttons["refresh"].setEnabled(self.runner is not None and not self.runner.busy)
        self._buttons["refresh"].setVisible(not (self.runner is not None and self.runner.cancellable))
        self._buttons["cancel_scan"].setVisible(bool(self.runner is not None and self.runner.cancellable))
        self.more.setEnabled(self.runner is not None and not self.runner.busy)
        for key, button in self._cache_actions.items():
            button.setEnabled(self.runner is not None and not self.runner.busy and any(item.key == key for item in self._items))

    def can_clean(self, key):
        return key in self.CLEAN_KEYS or key.startswith("retired:")

    def primary_action(self, key):
        if key in (*self.OWNER_KEYS, "drafts", "projects"):
            return "manage"
        if self.can_clean(key):
            return "clean"
        if key == "settings":
            return ""
        if key in ("legacy_models", "configuration", "other", "thumbnails", "edit_cache"):
            return "open"
        return "locations"

    def open_category(self, key):
        if self.runner is None or self.runner.busy:
            return
        action = self.primary_action(key)
        if action == "manage":
            self.manage(key)
        elif action == "clean":
            self.clean(key)
        elif action == "locations":
            self.manage(key)
        elif action == "open":
            self.open_location(key)

    def show_category_menu(self, key, anchor):
        if self.runner is None or self.runner.busy:
            return
        item = next((item for item in self._items if item.key == key), None)
        if item is None:
            return
        menu = AppleMenu(self)
        if item.paths:
            action = menu.addAction(self.tr("storage.open"))
            action.triggered.connect(lambda: self.open_location(key))
            if len(item.paths) > 1 and self.primary_action(key) != "locations":
                action = menu.addAction(self.tr("storage.view_locations"))
                action.triggered.connect(lambda: self.runner.submit("locations:" + key, lambda: self.runner.service.locations(key)))
            menu.addSeparator()
            action = menu.addAction(self.tr("storage.backup"))
            action.triggered.connect(lambda: self.backup(key))
        if self.can_clean(key) and self.primary_action(key) != "clean":
            action = menu.addAction(self.tr("storage.clean"))
            action.triggered.connect(lambda: self.clean(key))
        if key in ("assistant", "library", "exports"):
            action = menu.addAction(self.tr("storage.compact"))
            action.triggered.connect(lambda: self.compact(key))
        if key in LOCATION_KEYS:
            action = menu.addAction(self.tr("storage.move"))
            action.triggered.connect(lambda: self.move(key))
        if not menu.isEmpty():
            menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        menu.deleteLater()

    def finished(self, operation, result, error):
        if error:
            self.notice.set_text(self.tr(error) if error.startswith("storage.") else error)
            self.notice.show()
            return
        self.notice.hide()
        if operation == "limit":
            self.project()
        elif operation == "scan":
            settings = read_settings()
            self.set_snapshot(result, refreshed=datetime.now().strftime("%H:%M:%S"), job_count=len(settings["jobs"]))
            if any(item.errors for item in result):
                self.notice.set_text(self.tr("storage.partial_hint"))
                self.notice.show()
            if settings["results"] and not settings["results"][-1]["success"]:
                last = settings["results"][-1]
                text = self.tr("storage.last_failure", kind=self.title(last["kind"]))
                text += " " + self.tr(last.get("error", "storage.error.operation"))
                if last.get("completed"):
                    text += " " + self.tr("storage.completed_items", count=last["completed"])
                self.notice.set_text(text)
                self.notice.show()
        elif operation.startswith("locations:"):
            self.show_locations(operation.split(":", 1)[1], result)
        elif operation.startswith(("objects:", "clean:")):
            self.choose_objects(operation.split(":", 1)[1], result, managing=operation.startswith("objects:"))
        elif operation in ("move", "backup_plan"):
            message = self.tr("storage.move_confirm" if operation == "move" else "storage.backup_plan",
                              source="\n".join(result.get("sources", [result["source"]])), target=result["target"], size=size_text(result["bytes"]))
            message += "\n" + self.tr("storage.free", size=size_text(result["free"]))
            if operation == "move" and result["kind"] in ("thumbnails", "edit_cache"):
                choice = ask_choice(self, self.tr("storage.move"), message,
                    (DialogButton("cancel", self.tr("common.cancel")),
                     DialogButton("rebuild", self.tr("storage.rebuild")),
                     DialogButton("copy", self.tr("storage.copy_cache"), "primary")), default="copy", escape="cancel")
                if choice != "cancel":
                    if choice == "copy" and result["free"] < result["bytes"] + 16 * 1024 ** 2:
                        self.notice.set_text(self.tr("storage.error.space"))
                        self.notice.show()
                        return
                    result["rebuild"] = choice == "rebuild"
                    self.schedule(result)
            elif show_confirm(self, self.tr("storage.move" if operation == "move" else "storage.backup"), message, self.tr("storage.schedule")):
                self.schedule(result)
        elif operation in ("register", "forget", "cancel_jobs"):
            self.refresh()
        else:
            if operation == "schedule":
                self._job_count = len(read_settings()["jobs"])
                self.project()
            self.summary.set_message(self.tr("storage.scheduled"))

    def set_snapshot(self, items, *, refreshed="", job_count=0):
        self._items, self._refreshed, self._job_count = list(items), refreshed, job_count
        self._scanned = True
        self._total = sum(item.unique_size for item in self._items)
        self.project()

    def schedule(self, job):
        self.runner.submit("schedule", lambda: self.runner.service.schedule(job))

    def open_location(self, key):
        item = next((item for item in self._items if item.key == key), None)
        if item and item.paths:
            open_path(item.paths[0])

    def manage(self, key):
        if key in ("drafts", "projects"):
            self.runner.submit("objects:" + key, lambda: self.runner.service.objects(key))
        elif key in self.OWNER_KEYS:
            self.manage_requested.emit(key)
        else:
            self.runner.submit("locations:" + key, lambda: self.runner.service.locations(key))

    def show_locations(self, key, entries):
        dialog = StorageListDialog(self.title(key), self.tr, self, empty_key="storage.empty.locations")
        dialog.set_entries((entry, Path(entry["name"]).name or entry["name"], entry["name"] + "\n" + size_text(entry["bytes"]) +
                            ("\n" + "\n".join(entry["errors"]) if entry["errors"] else ""), entry["name"])
                           for entry in entries)
        dialog.add_action(self.tr("common.close"), dialog.reject)
        dialog.exec()
        dialog.deleteLater()

    def clean(self, key):
        self.runner.submit("clean:" + key, lambda: self.runner.service.objects(key) if key == "drafts"
                           else self.runner.service.clean_candidates(key))

    def choose_objects(self, key, objects, *, managing=False):
        parents = {str(Path(entry["name"]).parent) for entry in objects}
        location = next(iter(parents)) if len(parents) == 1 else ""
        dialog = StorageListDialog(self.title(key), self.tr, self, selectable=True, location=location,
            note=self.tr("storage.project_hint" if key == "projects" and managing else
                         "storage.drafts_hint" if key == "drafts" else "storage.clean_hint"),
            empty_key="storage.empty.projects" if key == "projects" and managing else
                      "storage.empty.drafts" if key == "drafts" else "storage.empty.clean")
        entries = []
        for entry in objects:
            details = []
            if "bytes" in entry:
                details.append(size_text(entry["bytes"]))
            if "revisions" in entry:
                details.append(self.tr("storage.revisions", count=entry["revisions"]))
            if entry.get("unavailable"):
                details.append(self.tr("storage.partial"))
            details = " · ".join(details)
            entries.append((entry, Path(entry["name"]).name or entry["name"],
                            details if location else "\n".join(filter(None, (str(Path(entry["name"]).parent), details))),
                            "" if location else entry["id"]))
        dialog.set_entries(entries)
        if not objects and not (key == "projects" and managing):
            dialog.note.hide()
            dialog.add_action(self.tr("common.close"), dialog.reject)
            dialog.exec()
            dialog.deleteLater()
            return
        if key == "projects" and managing:
            if not objects:
                def register():
                    dialog.accept()
                    QTimer.singleShot(0, self, self.register)
                dialog.add_action(self.tr("common.close"), dialog.reject)
                dialog.add_action(self.tr("storage.register"), register, role="primary")
                dialog.exec()
                dialog.deleteLater()
                return
            backup = dialog.add_action(self.tr("storage.backup"), lambda: self.backup_project(dialog), role="primary")
            forget = dialog.add_action(self.tr("storage.forget"), lambda: self.forget_projects(dialog))
            def selected():
                records = dialog.selected_records()
                backup.setEnabled(len(records) == 1 and not records[0].get("unavailable"))
                forget.setEnabled(bool(records))
            dialog.selection_changed.connect(selected)
            selected()
            dialog.add_action(self.tr("common.close"), dialog.reject)
            dialog.exec()
            dialog.deleteLater()
            return
        dialog.add_action(self.tr("common.cancel"), dialog.reject)
        confirm = dialog.add_action(self.tr("storage.delete_selected" if key == "drafts" else "storage.clean_selected"), dialog.accept, role="danger")
        confirm.setEnabled(False)
        dialog.selection_changed.connect(lambda: confirm.setEnabled(bool(dialog.selected_records())))
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        selected = dialog.selected_records()
        dialog.deleteLater()
        if accepted and selected and confirm_destructive(self, self.tr("storage.clean"), self.tr("storage.clean_confirm", count=len(selected),
                size=size_text(sum(entry.get("bytes", 0) for entry in selected))), self.tr("storage.schedule"),
                detail="\n".join(entry["name"] for entry in selected)):
            self.schedule({"operation": "drafts" if key == "drafts" else "clean", "kind": key, "entries": selected})

    def backup_project(self, dialog):
        selected = dialog.selected_records()
        if len(selected) != 1 or selected[0].get("unavailable"):
            return
        target = QFileDialog.getExistingDirectory(self, self.tr("storage.backup"))
        if target:
            project = Path(selected[0]["id"])
            destination = Path(target) / (project.stem + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
            self.runner.submit("backup_plan", lambda: self.runner.service.backup_plan("projects", destination, project=str(project)))
            dialog.accept()

    def backup(self, key):
        if key == "projects":
            self.manage(key)
            return
        item = next((item for item in self._items if item.key == key), None)
        source = data_root() if key == "data" else item.paths[0] if item and item.paths else None
        if source is None:
            return
        target = QFileDialog.getExistingDirectory(self, self.tr("storage.backup"))
        if target:
            destination = Path(target) / ("OpenPictureStudio-" + key.replace(":", "-") + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
            self.runner.submit("backup_plan", lambda: self.runner.service.backup_plan(key, destination))

    def move(self, key):
        target = QFileDialog.getExistingDirectory(self, self.tr("storage.move"))
        if target:
            destination = Path(target) / ("OpenPictureStudio-" + key)
            self.runner.submit("move", lambda: self.runner.service.migration(key, destination))

    def register(self):
        paths, _ = QFileDialog.getOpenFileNames(self, self.tr("storage.register"), "", "OpenPictureStudio (*.opsproj)")
        if paths:
            self.runner.submit("register", lambda: [register_project(path) for path in paths])

    def cancel_jobs(self):
        self.runner.submit("cancel_jobs", self.runner.service.cancel_jobs)

    def pending(self):
        dialog = StorageListDialog(self.tr("storage.pending"), self.tr, self, selectable=True, empty_key="storage.empty.pending")
        entries = []
        for job in read_settings()["jobs"]:
            title = self.title(job["kind"]) + " · " + self.tr("storage.operation." + job["operation"])
            detail = (job.get("source", "") + "\n→ " + job["target"]) if job.get("target") else (
                "\n".join(entry["name"] for entry in job.get("entries", [])[:10]))
            entries.append((job, title, detail, ""))
        dialog.set_entries(entries)
        def cancel_selected():
            identities = {record["id"] for record in dialog.selected_records()}
            if identities:
                self.runner.submit("cancel_jobs", lambda: update_settings(lambda value: value.update(
                    jobs=[job for job in value["jobs"] if job["id"] not in identities])))
            dialog.accept()
        if entries:
            remove = dialog.add_action(self.tr("storage.cancel_selected"), cancel_selected)
            remove.setEnabled(False)
            dialog.selection_changed.connect(lambda: remove.setEnabled(bool(dialog.selected_records())))
        dialog.add_action(self.tr("common.close"), dialog.reject)
        dialog.exec()
        dialog.deleteLater()

    def history(self):
        dialog = StorageListDialog(self.tr("storage.history"), self.tr, self, empty_key="storage.empty.history")
        entries = []
        for result in reversed(read_settings()["results"]):
            title = result["time"] + " · " + self.tr("storage.last_success" if result["success"] else "storage.last_failure",
                                                    kind=self.title(result["kind"]))
            details = []
            if result.get("operation"):
                details.append(self.tr("storage.operation." + result["operation"]))
            if result.get("source"):
                details.append(result["source"])
            if result.get("target"):
                details.append("→ " + result["target"])
            if result.get("completed"):
                details.append(self.tr("storage.completed_items", count=result["completed"]))
            if not result["success"]:
                details.append(self.tr(result["error"]))
            entries.append((result, title, "\n".join(details), ""))
        dialog.set_entries(entries)
        dialog.add_action(self.tr("common.close"), dialog.reject)
        dialog.exec()
        dialog.deleteLater()

    def compact(self, key):
        if show_confirm(self, self.tr("storage.compact"), self.tr("storage.compact_note"), self.tr("storage.schedule")):
            self.schedule({"operation": "compact", "kind": key})

    def forget_projects(self, dialog):
        names = {entry["id"] for entry in dialog.selected_records()}
        if names:
            self.runner.submit("forget", lambda: update_settings(lambda value: value.update(projects=[path for path in value["projects"] if path not in names])))
            dialog.accept()

    def set_limit(self, key, value):
        if self.runner is not None and not self.runner.discarding_limits:
            self._pending_limits[key] = value
            self._limits_timer.start()
            self._update_limits_notice()

    @property
    def has_pending_limits(self):
        return bool(self._pending_limits) or (self.runner is not None and self.runner.has_pending_limits)

    def flush_limits(self):
        self._limits_timer.stop()
        if self._pending_limits and self.runner is not None:
            values, self._pending_limits = self._pending_limits, {}
            self.runner.persist_limits(values)

    def discard_limits(self):
        self._limits_timer.stop()
        self._pending_limits.clear()


class AssistantStorageDialog(StorageListDialog):
    def __init__(self, runner, translate, kind, parent=None):
        super().__init__(translate("storage.kind." + kind), translate, parent, selectable=True,
                         note=translate("storage.assistant_hint"),
                         empty_key="storage.empty.styles" if kind == "styles" else "storage.empty.conversations")
        self.runner, self.kind = runner, kind
        self.clear_button = None
        self.remove_button = self.add_action(translate("storage.delete_selected"), self.remove, role="danger")
        self.selection_changed.connect(lambda: self.remove_button.setEnabled(bool(self.selected_records())))
        if kind == "attachments":
            clear = AppleGhostButton(translate("assistant.clear_attachments"), self)
            clear.clicked.connect(self.clear_images)
            self.actions.insertWidget(0, clear)
            self.clear_button = clear
        self.add_action(translate("common.close"), self.reject)
        self.notice = NoticeBar("warning", parent=self)
        self.notice.hide()
        self.layout().insertWidget(1, self.notice)
        runner.changed.connect(self.refresh)
        runner.event_received.connect(self.receive)
        self.finished.connect(self.cleanup)
        self.refresh(runner.state)

    def refresh(self, state):
        records = state.get("styles" if self.kind == "styles" else "conversations", [])
        self.set_entries((record, record.get("name") or record.get("title") or record["id"], "", "")
                         for record in records)
        self.remove_button.setVisible(bool(records))
        if self.clear_button is not None:
            self.clear_button.setVisible(bool(records))

    def remove(self):
        records = self.selected_records()
        if records and confirm_destructive(self, self.tr("storage.delete_selected"), self.tr("storage.delete_note"),
                self.tr("storage.delete_selected"), detail="\n".join(
                    record.get("name") or record.get("title") or record["id"] for record in records)):
            for record in records:
                if self.kind == "styles":
                    self.runner.command("style_remove", style_id=record["id"], version=record["version"])
                else:
                    self.runner.command("delete", identity=record["id"])

    def clear_images(self):
        if confirm_destructive(self, self.tr("assistant.clear_attachments"), self.tr("assistant.clear_attachments_note"),
                               self.tr("assistant.clear_attachments")):
            self.runner.command("clear_attachments")

    def receive(self, kind, value):
        if kind == "error":
            self.notice.set_text(self.tr(value.get("key", "storage.error.operation") if isinstance(value, dict) else "storage.error.operation"))
            self.notice.show()

    def cleanup(self, _result):
        self.runner.changed.disconnect(self.refresh)
        self.runner.event_received.disconnect(self.receive)
