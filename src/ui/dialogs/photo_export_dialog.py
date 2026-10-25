from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import (QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog,
                             QLabel, QLineEdit, QScrollArea, QSpinBox, QTabWidget,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from resources.styles.theme_variables import SPACING_MD, SPACING_XL, px
from ...application.export_store import ExportPreset
from ...core.constants import IMAGE_QUALITY_RANGE
from ...core.image_resize import ResizeOptions, RESIZE_MODES
from ...core.output_formats import OUTPUT_FORMATS, output_format
from ..base_window import BaseDialog
from ..image_viewer import ImageViewer
from ..widgets.apple_button import AppleButton, ApplePrimaryButton
from ..widgets.apple_menu import AppleDropDown


class PhotoExportDialog(BaseDialog):
    def __init__(self, runner, capture, parent=None):
        super().__init__(title_key="export_queue.title", min_width=640, min_height=560,
                         default_width=1000, default_height=850, modal=False, parent=parent)
        self._runner, self._capture = runner, capture
        self._context, self._batch, self._page = None, "", 0
        self._known_presets = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*([px(SPACING_XL)] * 4))
        layout.setSpacing(px(SPACING_MD))
        self._scope = QLabel(self)
        self._scope.setWordWrap(True)
        self._scope.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._scope)
        row = QHBoxLayout()
        self._range = AppleDropDown(self)
        for value in ("current", "selected", "filtered"):
            self._range.addItem("", value)
        self._range.setCurrentIndex(1)
        row.addWidget(self._range, 1)
        self._recapture = AppleButton(parent=self)
        self._recapture.clicked.connect(self.capture_selection)
        row.addWidget(self._recapture)
        layout.addLayout(row)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        form_widget = QWidget(scroll)
        form = QFormLayout(form_widget)
        self._labels = {}
        def field(key, control):
            label = QLabel(self)
            self._labels[key] = label
            form.addRow(label, control)
        self._presets = AppleDropDown(self)
        self._presets.activated.connect(self._select_preset)
        field("preset", self._presets)
        directory = QWidget(self)
        directory_row = QHBoxLayout(directory)
        directory_row.setContentsMargins(0, 0, 0, 0)
        self._directory = QLineEdit(self)
        directory_row.addWidget(self._directory, 1)
        self._browse = AppleButton(parent=self)
        self._browse.clicked.connect(self._choose_directory)
        directory_row.addWidget(self._browse)
        field("directory", directory)
        self._format = AppleDropDown(self)
        for info in OUTPUT_FORMATS:
            self._format.addItem(info.label, info.extension)
        self._format.activated.connect(self._format_changed)
        field("format", self._format)
        self._quality = QSpinBox(self)
        self._quality.setRange(*IMAGE_QUALITY_RANGE)
        field("quality", self._quality)
        self._width, self._height = QLineEdit(self), QLineEdit(self)
        field("width", self._width)
        field("height", self._height)
        self._mode = AppleDropDown(self)
        for mode in RESIZE_MODES:
            self._mode.addItem("", mode)
        field("mode", self._mode)
        self._pattern = QLineEdit(self)
        field("pattern", self._pattern)
        self._pattern_hint = QLabel("{stem} · {folder} · {index}", self)
        form.addRow("", self._pattern_hint)
        scroll.setWidget(form_widget)
        layout.addWidget(scroll, 2)
        commands = QGridLayout()
        self._buttons = {}
        for index, (key, action) in enumerate((("use_settings", self._configure), ("save_preset", self._save_preset),
            ("enqueue", self._enqueue), ("preview", self._preview),
            ("pause", lambda: self._control("pause")), ("resume", lambda: self._control("resume")),
            ("cancel", lambda: self._control("cancel")), ("retry", lambda: self._control("retry")))):
            button = (ApplePrimaryButton if key == "enqueue" else AppleButton)(parent=self)
            button.clicked.connect(action)
            self._buttons[key] = button
            commands.addWidget(button, index // 2, index % 2)
        layout.addLayout(commands)
        self._batches = AppleDropDown(self)
        self._batches.activated.connect(self._select_batch)
        layout.addWidget(self._batches)
        self._status = QLabel(self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._tabs = QTabWidget(self)
        self._queue, self._history = QTreeWidget(self), QTreeWidget(self)
        for tree in (self._queue, self._history):
            tree.setColumnCount(5)
        self._tabs.addTab(self._queue, "")
        self._tabs.addTab(self._history, "")
        self._tabs.currentChanged.connect(self._change_tab)
        layout.addWidget(self._tabs, 3)
        footer = QGridLayout()
        for index, (key, action) in enumerate((("previous", lambda: self._turn_page(-1)),
            ("next", lambda: self._turn_page(1)), ("verify", self._verify), ("close", self.close))):
            button = AppleButton(parent=self)
            self._buttons[key] = button
            button.clicked.connect(action)
            footer.addWidget(button, index // 2, index % 2)
        layout.addLayout(footer)
        self._notice = QLabel(self)
        self._notice.setWordWrap(True)
        self._notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._notice)
        self._viewer = ImageViewer(self)
        self._viewer.setMinimumHeight(220)
        self._viewer.hide()
        layout.addWidget(self._viewer, 2)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self._refresh)
        self._delivery_timer = QTimer(self)
        self._delivery_timer.setInterval(1500)
        self._delivery_timer.timeout.connect(lambda: self._schedule_refresh() if self.isVisible() else None)
        self._delivery_timer.start()
        runner.changed.connect(self._schedule_refresh)
        runner.preview_ready.connect(self._show_preview)
        runner.preview_cleared.connect(self._viewer.clear)
        self._verifications = {}
        self._set_preset(runner.service.configuration or ExportPreset())
        self.retranslate_ui()

    def capture_selection(self):
        self._context = None
        try:
            self._context = self._capture()
        except ValueError as error:
            self._notice.setText(self._tr(str(error)))
        self._refresh_scope()

    def _refresh_scope(self):
        self._scope.setText(self._tr("export_queue.scope", selected=self._context["selected_count"],
            filtered=self._context["filtered_count"]) if self._context else self._tr("batch.no_targets"))
        self._buttons["enqueue"].setEnabled(self._context is not None)

    def _choose_directory(self):
        directory = QFileDialog.getExistingDirectory(self, self._tr("export_queue.directory"), self._directory.text())
        if directory:
            self._directory.setText(directory)

    def _preset(self):
        resize = ResizeOptions.from_text(self._width.text(), self._height.text(), self._mode.currentData())
        preset = ExportPreset(self._directory.text().strip(), self._format.currentData(), self._quality.value(),
                              resize.width, resize.height, resize.mode, self._pattern.text())
        preset.validate()
        return preset

    def _configure(self):
        try:
            self._runner.service.configure(self._preset())
            self._notice.setText(self._tr("export_queue.configured"))
            return True
        except ValueError as error:
            self._notice.setText(self._tr(str(error)))
            return False

    def _set_preset(self, preset):
        self._directory.setText(preset.directory)
        self._format.setCurrentIndex(self._format.findData(preset.format))
        self._quality.setValue(preset.quality)
        self._width.setText(str(preset.width) if preset.width is not None else "")
        self._height.setText(str(preset.height) if preset.height is not None else "")
        self._mode.setCurrentIndex(self._mode.findData(preset.mode))
        self._pattern.setText(preset.pattern)
        self._format_changed()

    def _select_preset(self):
        name = self._presets.currentData()
        if name:
            try:
                self._set_preset(ExportPreset(**self._runner.service.presets[name]))
            except (TypeError, ValueError) as error:
                self._notice.setText(self._tr("export_queue.store_failed") + " · " + str(error))

    def _save_preset(self):
        if not self._configure():
            return
        name, accepted = QInputDialog.getText(self, self._tr("export_queue.save_preset"), self._tr("export_queue.preset_name"))
        if accepted and name.strip():
            self._runner.save_preset(name, self._runner.service.configuration,
                lambda result: self._notice.setText(self._tr("export_queue.preset_saved" if result.success else result.error.message_key)))

    def _format_changed(self):
        self._quality.setEnabled(output_format(self._format.currentData()).supports_quality)

    def _enqueue(self):
        if self._context is None or not self._configure():
            return
        try:
            summary = self._runner.enqueue(self._context["scope"], self._range.currentData())
            self._batch, self._page = summary["export"], 0
            self._notice.setText(self._tr("export_queue.notice"))
            self._refresh()
        except ValueError as error:
            self._notice.setText(self._tr(str(error)))

    def _control(self, action):
        if self._batch:
            try:
                self._runner.control(self._batch, action)
            except ValueError as error:
                self._notice.setText(self._tr(str(error)))

    def _select_batch(self):
        self._runner.cancel_preview()
        self._batch, self._page = self._batches.currentData() or "", 0
        self._refresh()

    def _schedule_refresh(self, identity=""):
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _change_tab(self):
        self._page = 0
        self._refresh()

    def _turn_page(self, step):
        self._page = max(0, self._page + step)
        self._refresh()

    def _preview(self):
        item = self._queue.currentItem()
        if item is not None and self._batch:
            try:
                self._runner.preview(self._batch, item.data(0, Qt.ItemDataRole.UserRole))
            except ValueError as error:
                self._notice.setText(self._tr(str(error)))

    def _show_preview(self, result):
        if not result.success:
            self._notice.setText(self._tr(result.error.message_key))
            return
        frame, full_size = result.data
        width, height = frame.size
        image = QImage(frame.rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        self._viewer.set_qimage(image, reference_size=QSize(*full_size))
        self._viewer.show()
        self._notice.setText(self._tr("export_queue.preview_size", width=full_size[0], height=full_size[1],
                                     preview_width=width, preview_height=height))

    def _verify(self):
        tree = self._history if self._tabs.currentIndex() else self._queue
        row = tree.currentItem()
        if row is None:
            return
        batch_item = None
        if tree is self._queue:
            batch_item = self._runner.service.get(self._batch).items[row.data(0, Qt.ItemDataRole.UserRole)]
            record = batch_item.record
        else:
            record = row.data(0, Qt.ItemDataRole.UserRole)
        if record is None:
            return
        batch_item = self._runner.service.record_item(record["id"])
        def completed(result):
            if result.success:
                value = result.data
                self._verifications[record["id"]] = value
                self._notice.setText(self._tr("export_queue." + value["verification"]))
            else:
                self._notice.setText(self._tr(result.error.message_key))
            self._refresh()
        self._runner.verify(record, completed, repair=batch_item is not None and batch_item.record_state == "failed")

    def _refresh(self):
        service = self._runner.service
        names = tuple(service.presets)
        if names != self._known_presets:
            previous = self._presets.currentData()
            self._presets.clear()
            self._presets.addItem(self._tr("export_queue.custom"), "")
            for name in names:
                self._presets.addItem(name, name)
            self._presets.setCurrentIndex(max(0, self._presets.findData(previous)))
            self._known_presets = names
        identities = list(service.batches)
        known = [self._batches.itemData(index) for index in range(self._batches.count())]
        if known != identities:
            self._batches.clear()
            for index, identity in enumerate(identities, 1):
                self._batches.addItem(self._tr("export_queue.batch_name", index=index), identity)
        if self._batch not in identities:
            self._runner.cancel_preview()
            self._batch = identities[-1] if identities else ""
            self._page = 0
        self._batches.setCurrentIndex(self._batches.findData(self._batch))
        total = len(service.records) if self._tabs.currentIndex() else 0
        for tree in (self._queue, self._history):
            selected = tree.currentItem()
            selected_value = selected.data(0, Qt.ItemDataRole.UserRole) if selected is not None else None
            selected_identity = selected_value.get("id") if isinstance(selected_value, dict) else selected_value
            scroll = tree.verticalScrollBar().value()
            tree.clear()
            if tree is self._queue and self._batch:
                summary = service.summary(self._batch, offset=self._page * 50, limit=50)
                if not self._tabs.currentIndex():
                    total = summary["total"]
                counts = summary["counts"]
                self._status.setText(self._tr("export_queue.summary", total=summary["total"],
                    done=counts["success"], failed=counts["failed"], cancelled=counts["cancelled"]) +
                    (" · " + self._tr("export_queue.paused") if summary["paused"] else ""))
                if summary.get("cancelled") and counts.get("exporting"):
                    self._status.setText(self._status.text() + " · " + self._tr("export.cancelling"))
                for value in summary["items"]:
                    state = self._tr("export_queue.status." + value["status"])
                    error = value.get("error")
                    if error:
                        state += " · " + self._tr(error["message_key"])
                    row = QTreeWidgetItem(tree, [value["name"], state, self._tr("export_queue." + value["delivery"]),
                        self._tr("export_queue.record." + value["record_state"]), value["target"]])
                    row.setData(0, Qt.ItemDataRole.UserRole, value["index"])
                    row.setToolTip(4, value["target"])
            elif tree is self._history:
                for record in service.records[self._page * 50:self._page * 50 + 50]:
                    verified = self._verifications.get(record["id"], {})
                    state = verified.get("verification", "unchecked")
                    delivery = service.delivery_state(record["source_path"], record["workflow_hash"], record["source_hash"])
                    if verified.get("contract") in ("render_changed", "render_unverified"):
                        delivery = verified["contract"]
                    record_label = (self._tr("export_queue.record.failed") if service.record_state(record) == "failed"
                                    else record["completed_at"])
                    row = QTreeWidgetItem(tree, [Path(record["source_path"]).name, self._tr("export_queue." + state),
                        self._tr("export_queue." + delivery), record_label, record["target_path"]])
                    row.setData(0, Qt.ItemDataRole.UserRole, record)
                    row.setToolTip(4, record["target_path"])
            for index in range(tree.topLevelItemCount()):
                row = tree.topLevelItem(index)
                value = row.data(0, Qt.ItemDataRole.UserRole)
                if (value.get("id") if isinstance(value, dict) else value) == selected_identity:
                    tree.setCurrentItem(row)
                    break
            tree.verticalScrollBar().setValue(scroll)
        for key in ("pause", "resume", "cancel", "retry", "preview"):
            self._buttons[key].setEnabled(bool(self._batch))
        self._buttons["previous"].setEnabled(self._page > 0)
        self._buttons["next"].setEnabled((self._page + 1) * 50 < total)
        if service.store_error:
            self._notice.setText(self._tr(service.store_error["message_key"]))

    def retranslate_ui(self):
        super().retranslate_ui()
        if not hasattr(self, "_buttons"):
            return
        for key, label in self._labels.items():
            label.setText(self._tr("export_queue." + key))
        for key, button in self._buttons.items():
            button.setText(self._tr("common.close" if key == "close" else "export_queue." + key))
        self._recapture.setText(self._tr("batch.capture"))
        self._browse.setText(self._tr("export_queue.browse"))
        for index, kind in enumerate(("current", "selected", "filtered")):
            self._range.setItemText(index, self._tr("export_queue.current_photo" if kind == "current" else "batch.range." + kind))
        for index, mode in enumerate(RESIZE_MODES):
            self._mode.setItemText(index, self._tr("compress.resize.mode." + mode))
        self._width.setPlaceholderText(self._tr("export_queue.original"))
        self._height.setPlaceholderText(self._tr("export_queue.original"))
        self._tabs.setTabText(0, self._tr("export_queue.queue"))
        self._tabs.setTabText(1, self._tr("export_queue.history"))
        headers = [self._tr("export_queue." + key) for key in ("photo", "status", "delivery", "record", "target")]
        self._queue.setHeaderLabels(headers)
        self._history.setHeaderLabels(headers)
        self._known_presets = None
        self._refresh_scope()
        self._refresh()

    def closeEvent(self, event):
        self._runner.cancel_preview()
        self._viewer.clear()
        super().closeEvent(event)

    def done(self, result):
        self._runner.cancel_preview()
        super().done(result)

    def showEvent(self, event):
        if self.translation_service and self._observer_removed:
            self.translation_service.add_observer(self._on_language_changed)
            self._observer_removed = False
        if not self._theme_subscribed:
            self._subscribe_theme_manager()
        self.retranslate_ui()
        super().showEvent(event)
