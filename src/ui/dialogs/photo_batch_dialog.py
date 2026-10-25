from __future__ import annotations

import json

from PyQt6.QtCore import QSettings, Qt, QTimer
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout

from resources.styles.theme_variables import SPACING_MD, SPACING_XL, px
from ...utils.app_info import APP_NAME
from ...core.tools.color import COLOR_SYNC_FIELDS, COLOR_STYLE_FIELDS
from ..base_window import BaseDialog
from ..widgets.apple_button import AppleButton, ApplePrimaryButton
from ..widgets.apple_menu import AppleDropDown
from ..widgets.color_sliders import WB_SLIDER_DEFS, TONE_SLIDER_DEFS, PRESENCE_SLIDER_DEFS

_FIELD_LABELS = {spec.key: spec.label_key for spec in WB_SLIDER_DEFS + TONE_SLIDER_DEFS + PRESENCE_SLIDER_DEFS}


class PhotoBatchDialog(BaseDialog):
    def __init__(self, runner, capture, navigate, parent=None, *, settings=None, is_current=lambda scope: True):
        super().__init__(title_key="batch.title", min_width=560, min_height=520,
                         default_width=900, default_height=760, modal=False, parent=parent)
        self._runner, self._capture, self._navigate = runner, capture, navigate
        self._is_current = is_current
        self._context, self._batch, self._prepared_signature = None, "", None
        self._settings = settings if settings is not None else QSettings(APP_NAME, "PhotoBatch")
        self._ranges = json.loads(self._settings.value("ranges", "{}"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*([px(SPACING_XL)] * 4))
        layout.setSpacing(px(SPACING_MD))
        self._scope_label = QLabel(self)
        self._scope_label.setTextFormat(Qt.TextFormat.PlainText)
        self._scope_label.setWordWrap(True)
        layout.addWidget(self._scope_label)
        choices = QHBoxLayout()
        self._range = AppleDropDown(self)
        self._range.addItem("", "selected")
        self._range.addItem("", "filtered")
        choices.addWidget(self._range, 1)
        self._recapture = AppleButton(parent=self)
        self._recapture.clicked.connect(self.capture_selection)
        choices.addWidget(self._recapture)
        layout.addLayout(choices)
        presets = QHBoxLayout()
        self._preset = AppleDropDown(self)
        self._preset.addItem("", "current")
        self._preset.addItem("", "style")
        self._preset.addItem("", "basic")
        self._preset.addItem("", "all")
        for name in self._ranges:
            self._preset.addItem(name, "custom:" + name)
        self._preset.activated.connect(self._select_preset)
        presets.addWidget(self._preset, 1)
        self._save_range = AppleButton(parent=self)
        self._save_range.clicked.connect(self._remember_range)
        presets.addWidget(self._save_range)
        layout.addLayout(presets)
        self._fields = QTreeWidget(self)
        self._fields.setHeaderHidden(True)
        self._field_items = {}
        for field in COLOR_SYNC_FIELDS:
            item = QTreeWidgetItem(self._fields)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            self._field_items[field] = item
        saved = self._settings.value("fields", list(COLOR_STYLE_FIELDS))
        self._set_fields(saved if isinstance(saved, list) else list(COLOR_STYLE_FIELDS))
        if set(self._selected_fields()) == set(COLOR_STYLE_FIELDS):
            self._preset.setCurrentIndex(1)
        layout.addWidget(self._fields, 2)
        operations = QGridLayout()
        self._prepare_sync = AppleButton(parent=self)
        self._prepare_sync.clicked.connect(lambda: self._prepare("sync"))
        operations.addWidget(self._prepare_sync, 0, 0)
        self._ev = QLineEdit("+0.30", self)
        self._ev.setAccessibleName(self._tr("batch.ev_label"))
        operations.addWidget(self._ev, 0, 1)
        self._prepare_ev = AppleButton(parent=self)
        self._prepare_ev.clicked.connect(lambda: self._prepare("exposure"))
        operations.addWidget(self._prepare_ev, 1, 0)
        self._execute = ApplePrimaryButton(parent=self)
        self._execute.clicked.connect(self._start)
        self._execute.setEnabled(False)
        operations.addWidget(self._execute, 1, 1)
        layout.addLayout(operations)
        self._batches = AppleDropDown(self)
        self._batches.activated.connect(self._choose_batch)
        layout.addWidget(self._batches)
        self._status = QLabel(self)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._results = QTreeWidget(self)
        self._results.setColumnCount(4)
        self._results.itemDoubleClicked.connect(lambda item, column: self._open_result())
        layout.addWidget(self._results, 3)
        row = QGridLayout()
        self._buttons = {}
        for key, action in (("previous", lambda: self._step(-1)), ("next", lambda: self._step(1)),
                            ("cancel", lambda: self._runner.cancel(self._batch)),
                            ("retry", lambda: self._run_action("apply")),
                            ("undo", lambda: self._run_action("undo")),
                            ("redo", lambda: self._run_action("redo"))):
            button = AppleButton(parent=self)
            button.clicked.connect(action)
            index = len(self._buttons)
            row.addWidget(button, index // 2, index % 2)
            self._buttons[key] = button
        layout.addLayout(row)
        self._notice = QLabel(self)
        self._notice.setWordWrap(True)
        layout.addWidget(self._notice)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self._refresh)
        self._runner.changed.connect(self._schedule_refresh)
        self.destroyed.connect(lambda: self._disconnect_runner())
        self._fields.itemChanged.connect(self._input_changed)
        self._range.activated.connect(self._input_changed)
        self._ev.textEdited.connect(self._input_changed)
        self.retranslate_ui()

    def _disconnect_runner(self):
        try:
            self._runner.changed.disconnect(self._schedule_refresh)
        except (RuntimeError, TypeError):
            pass

    def capture_selection(self):
        self._context, self._prepared_signature = None, None
        self._execute.setEnabled(False)
        self._scope_label.setText(self._tr("batch.no_targets"))
        try:
            self._context = self._capture()
            self._prepared_signature = None
            self._execute.setEnabled(False)
            self._refresh_scope()
        except ValueError as error:
            self._status.setText(self._tr(str(error)))

    def showEvent(self, event):
        if self.translation_service and self._observer_removed:
            self.translation_service.add_observer(self._on_language_changed)
            self._observer_removed = False
        if not self._theme_subscribed:
            self._subscribe_theme_manager()
        self.retranslate_ui()
        super().showEvent(event)

    def show_batch(self, batch_id):
        self._runner.service.summary(batch_id, limit=0)
        self._batch = batch_id
        self._prepared_signature = None
        self._execute.setEnabled(False)
        self._refresh()

    def _refresh_scope(self):
        if self._context:
            self._scope_label.setText(self._tr("batch.scope", sample=self._context["sample_name"],
                selected=self._context["selected_count"], filtered=self._context["filtered_count"]))

    def _selected_fields(self):
        return tuple(field for field, item in self._field_items.items() if item.checkState(0) == Qt.CheckState.Checked)

    def _set_fields(self, fields):
        for field, item in self._field_items.items():
            item.setCheckState(0, Qt.CheckState.Checked if field in fields else Qt.CheckState.Unchecked)

    def _signature(self):
        return self._selected_fields(), self._range.currentData(), self._ev.text()

    def _input_changed(self, *_):
        key = self._preset.currentData()
        preset = self._preset_fields(key)
        if preset is not None and set(preset) != set(self._selected_fields()):
            self._preset.setCurrentIndex(0)
        if self._prepared_signature != self._signature():
            self._prepared_signature = None
            self._execute.setEnabled(False)

    def _select_preset(self, index):
        key = self._preset.itemData(index)
        fields = self._preset_fields(key)
        if fields is not None:
            self._set_fields(fields)
            self._preset.setCurrentIndex(self._preset.findData(key))

    def _preset_fields(self, key):
        if key == "current":
            return None
        return (COLOR_STYLE_FIELDS if key == "style" else tuple(field for field in COLOR_SYNC_FIELDS
            if field not in COLOR_STYLE_FIELDS) if key == "basic" else COLOR_SYNC_FIELDS if key == "all"
            else self._ranges[key.removeprefix("custom:")])

    def _remember_range(self):
        name, accepted = QInputDialog.getText(self, self._tr("batch.save_range"), self._tr("batch.range_name"))
        if accepted and name.strip():
            name = name.strip()
            if name not in self._ranges:
                self._preset.addItem(name, "custom:" + name)
            self._ranges[name] = list(self._selected_fields())
            self._settings.setValue("ranges", json.dumps(self._ranges, ensure_ascii=False))

    def _prepare(self, operation):
        # 每次检查都重新捕获样板与范围，对话框开着期间的编辑和选择变化不会被旧快照覆盖
        self.capture_selection()
        if self._context is None:
            return
        try:
            self._batch = self._runner.service.create(self._context["scope"], operation,
                fields=self._selected_fields(), delta_ev=float(self._ev.text()) if operation == "exposure" else 0,
                range_kind=self._range.currentData(), sample_snapshot=self._context["sample_snapshot"])
            self._runner.service.preflight(self._batch)
            self._prepared_signature = self._signature()
            self._settings.setValue("fields", list(self._selected_fields()))
            self._refresh()
            self._execute.setEnabled(True)
        except ValueError as error:
            key = str(error) if str(error).startswith("batch.") else "batch.invalid_ev"
            self._status.setText(self._tr(key))

    def _start(self):
        if self._prepared_signature == self._signature():
            self._prepared_signature = None
            self._execute.setEnabled(False)
            if not self._is_current(self._context["scope"]):
                self._status.setText(self._tr("batch.capture_changed"))
                return
            self._run_action("apply")

    def _run_action(self, action):
        if self._batch:
            if action == "apply" and not self._runner.drafts.loaded:
                self._runner.drafts.retry()
            try:
                self._runner.start(self._batch, action=action)
            except ValueError as error:
                self._status.setText(self._tr(str(error)))

    def _choose_batch(self, index):
        self._batch = self._batches.itemData(index)
        self._prepared_signature = None
        self._execute.setEnabled(False)
        self._refresh()

    def _schedule_refresh(self, batch_id):
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _refresh(self):
        service = self._runner.service
        known = {self._batches.itemData(index) for index in range(self._batches.count())}
        for batch_id in service.batches():
            if batch_id not in known:
                info = service.summary(batch_id, limit=0)
                self._batches.addItem(self._tr("batch.batch_name", number=self._batches.count() + 1,
                    count=info["total"]), batch_id)
        for index in range(self._batches.count()):
            info = service.summary(self._batches.itemData(index), limit=0)
            self._batches.setItemText(index, self._tr("batch.batch_name", number=index + 1, count=info["total"]))
        if not self._batch:
            return
        self._batches.setCurrentIndex(self._batches.findData(self._batch))
        paths = service.paths(self._batch)
        result = service.summary(self._batch, limit=len(paths))
        skipped = self._tr("batch.skipped_count", count=result["counts"]["skipped"]) if result["counts"].get("skipped") else ""
        self._status.setText(self._tr("batch.summary", **result["counts"]) + (" · " + skipped if skipped else "") + " · " +
                             self._tr("batch.save." + result["save_state"]))
        selected_row = max(0, self._results.indexOfTopLevelItem(self._results.currentItem()))
        scroll_position = self._results.verticalScrollBar().value()
        self._results.clear()
        for index, item in enumerate(result["items"]):
            error = item["error"]
            row = QTreeWidgetItem(self._results, [f"{index + 1}. {item['name']}",
                self._tr("batch.status." + item["status"]), self._tr("batch.history." + item["history"]),
                self._tr(error["message_key"], **error.get("details", {})) if error else
                " · ".join(self._tr(key) for key in item.get("warnings", ()))])
            row.setToolTip(0, paths[index])
        if self._results.topLevelItemCount():
            self._results.setCurrentItem(self._results.topLevelItem(min(selected_row, len(paths) - 1)))
        for column in (0, 1, 2):
            self._results.resizeColumnToContents(column)
        self._results.verticalScrollBar().setValue(scroll_position)
        for key, button in self._buttons.items():
            button.setEnabled(result["active"] if key == "cancel" else not result["active"]
                              and result["started"] and (key == "retry" or result["counts"]["success"] > 0)
                              if key in ("retry", "undo", "redo") else bool(paths))

    def _open_result(self):
        index = self._results.indexOfTopLevelItem(self._results.currentItem())
        if self._batch and index >= 0:
            self._navigate(self._runner.service.paths(self._batch)[index])

    def _step(self, delta):
        index = self._results.indexOfTopLevelItem(self._results.currentItem()) + delta
        item = self._results.topLevelItem(index)
        if item is not None:
            self._results.setCurrentItem(item)
            self._open_result()

    def retranslate_ui(self):
        super().retranslate_ui()
        if not hasattr(self, "_context"):
            return
        for widget, key in ((self._recapture, "capture"), (self._save_range, "save_range"),
                            (self._prepare_sync, "prepare_sync"), (self._prepare_ev, "prepare_ev"),
                            (self._execute, "execute")):
            widget.setText(self._tr("batch." + key))
        for key, button in self._buttons.items():
            button.setText(self._tr("batch." + key))
        for index, key in enumerate(("selected", "filtered")):
            self._range.setItemText(index, self._tr("batch.range." + key))
        for index, key in enumerate(("current", "style", "basic", "all")):
            self._preset.setItemText(index, self._tr("batch.fields." + key))
        channels = {"rgb": "rgb", "r": "red", "g": "green", "b": "blue"}
        for field, item in self._field_items.items():
            parts = field.split(".")
            text = (self._tr("color_adjust.curves." + channels[parts[1]]) + " · " + self._tr("color_adjust.group.curves")
                    if parts[0] == "curves" else self._tr("color_adjust.mixer." + parts[1]) + " · " +
                    self._tr("color_adjust.mixer." + parts[2]) if parts[0] == "color_mixer"
                    else self._tr(_FIELD_LABELS[field]))
            item.setText(0, text)
        self._results.setHeaderLabels([self._tr("batch.photo"), self._tr("batch.result"),
                                       self._tr("batch.history"), self._tr("batch.reason")])
        self._notice.setText(self._tr("batch.recovery_notice"))
        self._refresh_scope()
        self._refresh()
