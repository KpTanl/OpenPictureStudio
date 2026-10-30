from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea,
                             QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from resources.styles.theme_variables import SPACING_MD, SPACING_XL, px
from ..base_window import BaseDialog
from ..image_viewer import ImageViewer
from ..widgets.apple_button import AppleButton, ApplePrimaryButton
from ..widgets.apple_menu import AppleDropDown


class CalibrationPreview(QWidget):
    def __init__(self, runner, translate, parent=None):
        super().__init__(parent)
        self.runner, self.tr = runner, translate
        self.snapshots, self.shown, self.maximum = [None, None], [None, None], [1600, 1600]
        self.viewers, self.labels = [], []
        self.channels = [f"compare:calibration:{id(self)}:{side}" for side in range(2)]
        self._setting = False
        self._zoom_buttons = []
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        for side in range(2):
            pane = QWidget(self)
            layout = QVBoxLayout(pane)
            label = QLabel(self)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
            scroll = QScrollArea(pane)
            scroll.setWidgetResizable(False)
            scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            viewer = ImageViewer(scroll)
            scroll.setWidget(viewer)
            layout.addWidget(scroll, 1)
            buttons = QHBoxLayout()
            fit = AppleButton(self.tr("browser.fit"), pane)
            fit.clicked.connect(viewer.zoom_fit)
            buttons.addWidget(fit)
            actual = AppleButton(self.tr("browser.100"), pane)
            actual.clicked.connect(lambda _checked=False, target=viewer: target.set_zoom_percent(100))
            buttons.addWidget(actual)
            self._zoom_buttons.append((fit, actual))
            layout.addLayout(buttons)
            viewer.view_changed.connect(lambda index=side: self._view_changed(index))
            root.addWidget(pane, 1)
            self.labels.append(label)
            self.viewers.append(viewer)
        runner.ready.connect(self._ready)
        runner.clearing.connect(self._invalidated)
        self.setMinimumHeight(220)

    def set_snapshots(self, before, after):
        self.clear()
        self.snapshots = [before, after]
        self.maximum = [1600, 1600]
        for side, snapshot in enumerate(self.snapshots):
            self.labels[side].setText(self.tr("calibration.before" if side == 0 else "calibration.proposed"))
            if snapshot is not None:
                self.runner.request_fixed(self.channels[side], snapshot, self.maximum[side])
                self._ready(snapshot.path)

    def _ready(self, path):
        if not self.isVisible():
            return
        for side, snapshot in enumerate(self.snapshots):
            if snapshot is None or snapshot.path != path:
                continue
            image = self.runner.image(path, self.maximum[side], fixed=snapshot)
            if image is None or image is self.shown[side]:
                continue
            if image.error:
                self.labels[side].setText(self.tr(image.error))
                continue
            viewer = self.viewers[side]
            state = viewer.view_state()
            self._setting = True
            try:
                viewer.set_pixmap(image.pixmap, reference_size=QSize(*image.size))
                viewer.restore_view_state(state)
                self.shown[side] = image
                self.runner.retain(self.channels[side], image)
            finally:
                self._setting = False

    def _view_changed(self, side):
        if self._setting or self.shown[side] is None or self.viewers[side].is_fit_mode() or self.maximum[side] is None:
            return
        self.maximum[side] = None
        self.runner.request_fixed(self.channels[side], self.snapshots[side], None)

    def retranslate(self):
        for side, (fit, actual) in enumerate(self._zoom_buttons):
            fit.setText(self.tr("browser.fit"))
            actual.setText(self.tr("browser.100"))
            self.labels[side].setText(self.tr("calibration.before" if side == 0 else "calibration.proposed"))
            if self.snapshots[side] is not None:
                self._ready(self.snapshots[side].path)

    def _invalidated(self, path):
        if any(snapshot is not None and (not path or self.runner.service.key(path) == self.runner.service.key(snapshot.path))
               for snapshot in self.snapshots):
            self.clear()

    def clear(self):
        for side, viewer in enumerate(self.viewers):
            viewer.clear()
            self.runner.forget(self.channels[side])
        self.snapshots, self.shown = [None, None], [None, None]

    def hideEvent(self, event):
        self.clear()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        for snapshot in self.snapshots:
            if snapshot is not None:
                self._ready(snapshot.path)


class PhotoCalibrationDialog(BaseDialog):
    def __init__(self, runner, batches, browser, capture, navigate, parent=None):
        super().__init__(title_key="calibration.title", min_width=720, min_height=650,
                         default_width=1100, default_height=900, modal=False, parent=parent)
        self.runner, self.batches, self.browser = runner, batches, browser
        self.capture, self.navigate = capture, navigate
        self._plan = ""
        self._suggestions = None
        self._suggestion_scope = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(*([px(SPACING_XL)] * 4))
        root.setSpacing(px(SPACING_MD))
        group_row = QGridLayout()
        self.groups = AppleDropDown(self)
        self.groups.activated.connect(lambda _index: self._choose_group(clear_plan=True))
        group_row.addWidget(self.groups, 0, 0, 1, 2)
        self.name = QLineEdit(self)
        group_row.addWidget(self.name, 0, 2, 1, 2)
        self.sample = AppleDropDown(self)
        group_row.addWidget(self.sample, 1, 0)
        self._buttons = {}
        for name, action in (("capture", self.capture_selection), ("create_group", lambda: self._set_group(False)),
                             ("update_group", lambda: self._set_group(True))):
            button = AppleButton(parent=self)
            button.clicked.connect(action)
            self._buttons[name] = button
            group_row.addWidget(button, 1, len(self._buttons))
        root.addLayout(group_row)
        self.members = QLabel(self)
        self.members.setWordWrap(True)
        self.members.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.members)
        time_row = QGridLayout()
        self.minutes_label = QLabel(self)
        time_row.addWidget(self.minutes_label, 0, 0)
        self.minutes = QSpinBox(self)
        self.minutes.setRange(1, 1440)
        self.minutes.setValue(30)
        time_row.addWidget(self.minutes, 0, 1)
        self.local_time = QCheckBox(self)
        time_row.addWidget(self.local_time, 1, 0, 1, 4)
        for name, action in (("suggest_groups", self._suggest), ("use_suggestion", self._use_suggestion)):
            button = AppleButton(parent=self)
            button.clicked.connect(action)
            self._buttons[name] = button
            time_row.addWidget(button, 0, 2 if name == "suggest_groups" else 3)
        root.addLayout(time_row)
        self.suggestions = QTreeWidget(self)
        self.suggestions.setColumnCount(2)
        self.suggestions.setMaximumHeight(120)
        root.addWidget(self.suggestions)
        operations = QGridLayout()
        self.exposure, self.white_balance = QCheckBox(self), QCheckBox(self)
        self.exposure.setChecked(True)
        self.white_balance.setChecked(True)
        operations.addWidget(self.exposure, 0, 0, 1, 2)
        operations.addWidget(self.white_balance, 0, 2, 1, 3)
        for name, action in (("prepare", self._prepare), ("apply", self._apply), ("cancel", self._cancel),
                             ("retry", self._retry), ("history", self._history)):
            button = ApplePrimaryButton(parent=self) if name == "apply" else AppleButton(parent=self)
            button.clicked.connect(action)
            self._buttons[name] = button
            operations.addWidget(button, 1, ("prepare", "apply", "cancel", "retry", "history").index(name))
        root.addLayout(operations)
        self.plans = AppleDropDown(self)
        self.plans.activated.connect(self._choose_plan)
        root.addWidget(self.plans)
        self.status = QLabel(self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        split = QSplitter(Qt.Orientation.Vertical, self)
        self.results = QTreeWidget(split)
        self.results.setColumnCount(4)
        self.results.currentItemChanged.connect(self._preview)
        self.preview = CalibrationPreview(browser.runner, self._tr, split)
        split.addWidget(self.results)
        split.addWidget(self.preview)
        root.addWidget(split, 1)
        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        root.addWidget(self.notice)
        runner.changed.connect(self.refresh)
        batches.changed.connect(self.refresh)
        self.retranslate_ui()

    def capture_selection(self):
        try:
            selection = self.capture()
            self._paths = selection.selected
            self.sample.clear()
            for path in self._paths:
                self.sample.addItem(os.path.basename(path), path)
            index = self.sample.findData(selection.active)
            if index >= 0:
                self.sample.setCurrentIndex(index)
            self.members.setText(self._tr("calibration.members", count=len(self._paths)))
        except ValueError as error:
            self.status.setText(self._tr(str(error)))

    def _set_group(self, update):
        try:
            self.runner.check_capture()
            result = self.runner.service.set_group(self.name.text(), getattr(self, "_paths", ()),
                self.sample.currentData(), identity=self.groups.currentData() if update else None)
            self._plan = ""
            self.refresh()
            self.groups.setCurrentIndex(self.groups.findData(result["group"]))
        except ValueError as error:
            self.status.setText(self._tr(str(error)))

    def _choose_group(self, *, clear_plan=False):
        if clear_plan:
            self._plan = ""
            self.preview.clear()
        group = self.runner.service.groups.get(self.groups.currentData())
        if group is not None:
            self._paths = group.paths
            self.name.setText(group.name)
            self.sample.clear()
            for path in group.paths:
                self.sample.addItem(os.path.basename(path), path)
            sample_key = self.runner.service.sessions.key(group.sample_path)
            self.sample.setCurrentIndex(next((index for index, path in enumerate(group.paths)
                                             if self.runner.service.sessions.key(path) == sample_key), 0))
            self.members.setText(self._tr("calibration.members", count=len(group.paths)))
        if clear_plan:
            self.refresh()

    def _suggest(self):
        try:
            selection = self.capture()
            paths = selection.selected or selection.filtered
            service = self.browser.service
            self._suggestion_scope = service.scope
            references = {path: service.reference(path) for path in paths}
            def finished(result):
                if not result.success:
                    self.status.setText(self._tr(result.error.message_key))
                    return
                self._suggestions = result.data
                self.suggestions.clear()
                for index, group in enumerate(result.data["groups"]):
                    row = QTreeWidgetItem(self.suggestions, [self._tr("calibration.group_number", number=index + 1),
                        self._tr("calibration.group_count", count=len(group["photos"]))])
                    row.setData(0, Qt.ItemDataRole.UserRole, index)
                    row.setToolTip(0, "\n".join([group["camera"], group["lens"],
                        group["captured_from"] + " — " + group["captured_to"],
                        self._tr("calibration.body_verified" if group["same_body_verified"] else "calibration.body_unknown"),
                        *group["names"]]))
                self.status.setText(self._tr("calibration.group_results", groups=len(result.data["groups"]),
                                             skipped=len(result.data["skipped"])))
                self.status.setToolTip("\n".join(self._tr(item["reason"]) for item in result.data["skipped"]))
            self.runner.suggest(paths, references, finished, minutes=self.minutes.value(), confirm_local=self.local_time.isChecked())
        except ValueError as error:
            self.status.setText(self._tr(str(error)))

    def _use_suggestion(self):
        row = self.suggestions.currentItem()
        if row is None or self._suggestions is None:
            return
        try:
            index = row.data(0, Qt.ItemDataRole.UserRole)
            self.runner.check_capture()
            group = self._suggestions["groups"][index]
            paths = self.browser.service.resolve(self._suggestion_scope, group["photos"])
            self.runner.service.set_group(self._tr("calibration.group_number", number=index + 1), paths, paths[0])
            self.refresh()
            self.groups.setCurrentIndex(self.groups.count() - 1)
            self._choose_group(clear_plan=True)
        except ValueError as error:
            self.status.setText(self._tr(str(error)))

    def _prepare(self):
        try:
            self.preview.clear()
            operations = [key for key, button in (("exposure", self.exposure), ("white_balance", self.white_balance)) if button.isChecked()]
            self._plan = self.runner.propose(self.groups.currentData(), operations, self._prepared)
            self.refresh()
        except ValueError as error:
            self.status.setText(self._tr(str(error)))

    def _prepared(self, result):
        self.refresh()
        if not result.success:
            self.status.setText(self._tr(result.error.message_key))

    def _choose_plan(self):
        self.preview.clear()
        self._plan = self.plans.currentData() or ""
        plan = self.runner.service.plans.get(self._plan)
        if plan is not None:
            self.groups.setCurrentIndex(self.groups.findData(plan["group"]))
            self._choose_group()
        self.refresh()

    def _apply(self):
        try:
            self.runner.check_capture()
            identity = self.runner.service.apply(self._plan)
            self.batches.start(identity)
            self.refresh()
        except ValueError as error:
            self.status.setText(self._tr(str(error)))

    def _retry(self):
        plan = self.runner.service.plans.get(self._plan)
        if plan is not None and plan["batch"]:
            try:
                self.batches.start(plan["batch"])
            except ValueError as error:
                self.status.setText(self._tr(str(error)))
        else:
            self._prepare()

    def _cancel(self):
        self.runner.cancel()
        plan = self.runner.service.plans.get(self._plan)
        if plan is not None and plan["batch"]:
            self.batches.cancel(plan["batch"])

    def _history(self):
        plan = self.runner.service.plans.get(self._plan)
        if plan is not None and plan["batch"]:
            self.navigate(plan["batch"])

    def _preview(self, *_):
        item = self.results.currentItem()
        plan = self.runner.service.plans.get(self._plan)
        index = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if plan is not None and plan["state"] == "ready" and index is not None:
            row = plan["rows"][index]
            self.preview.set_snapshots(row["base"], row["preview"])
        else:
            self.preview.clear()

    def refresh(self, *_):
        if not hasattr(self, "plans"):
            return
        service = self.runner.service
        current_group = self.groups.currentData()
        self.groups.clear()
        for group in service.groups.values():
            name = group.name + (" · " + self._tr("calibration.state.expired") if group.expired else "")
            self.groups.addItem(name, group.id)
        if current_group:
            self.groups.setCurrentIndex(max(0, self.groups.findData(current_group)))
        self.plans.clear()
        for index, plan in enumerate(service.plans.values()):
            self.plans.addItem(self._tr("calibration.plan_number", number=index + 1), plan["id"])
        self.plans.setCurrentIndex(self.plans.findData(self._plan))
        plan = service.plans.get(self._plan)
        batch = self.batches.service.get(plan["batch"]) if plan is not None and plan["batch"] else None
        active = self.runner.busy or batch is not None and batch.active
        self._buttons["apply"].setEnabled(bool(plan and plan["state"] == "ready" and not plan["batch"] and not active
                                              and any(row["status"] == "ready" for row in plan["rows"])))
        self._buttons["prepare"].setEnabled(bool(service.groups) and not active)
        self._buttons["cancel"].setEnabled(bool(active))
        self._buttons["retry"].setEnabled(bool(plan) and not active)
        self._buttons["history"].setEnabled(batch is not None)
        selected = self.results.indexOfTopLevelItem(self.results.currentItem())
        blocked = self.results.blockSignals(True)
        try:
            self.results.clear()
            if plan is not None:
                for index, row in enumerate(plan["rows"]):
                    updates, before = row["updates"], row.get("before", {})
                    exposure = (f"{before['exposure_amt'] / 100:+.2f} → {updates['exposure_amt'] / 100:+.2f} EV"
                                if "exposure_amt" in updates else "–")
                    wb = (f"{before['temperature']} / {before['tint']} → {updates['temperature']} / {updates['tint']}"
                          if "temperature" in updates else "–")
                    reasons = [self._tr(detail["reason"]) for detail in row["details"].values() if detail["reason"]]
                    status = (self._tr("batch.status." + batch.items[index].status) if batch
                              else self._tr("batch.status.unchanged" if row["status"] == "unchanged"
                                            else "calibration.state." + row["status"]))
                    if row["error"]:
                        reasons.append(self._tr(row["error"]))
                    if batch and batch.items[index].error:
                        error = batch.items[index].error
                        reasons.append(self._tr(error["message_key"], **error.get("details", {})))
                    item = QTreeWidgetItem(self.results, [os.path.basename(row["path"]), exposure, wb, " · ".join([status, *reasons])])
                    item.setData(0, Qt.ItemDataRole.UserRole, index)
                    details = []
                    for operation, detail in row["details"].items():
                        if "metadata_resolution_ev" in detail:
                            details.append(self._tr("calibration.exposure_errors", resolution=f"{detail['metadata_resolution_ev']:.5f}",
                                discrepancy=f"{detail['metadata_discrepancy_ev']:.5f}", quantization=f"{detail['quantization_ev']:.5f}"))
                        if "anchor_ev" in detail:
                            details.append(self._tr("calibration.wb_errors", anchor=f"{detail['anchor_ev']:.5f}",
                                chroma=f"{detail['chroma_ev']:.5f}", scale=f"{detail['scale_ev']:.5f}"))
                    item.setToolTip(3, "\n".join(details))
                if self.results.topLevelItemCount():
                    self.results.setCurrentItem(self.results.topLevelItem(max(0, min(selected, len(plan["rows"]) - 1))))
                self.status.setText(self._tr("calibration.state." + plan["state"]) + (" · " + self._tr("batch.save." + batch.save_state) if batch else ""))
            for column in range(3):
                self.results.resizeColumnToContents(column)
        finally:
            self.results.blockSignals(blocked)
        if plan is None:
            self.preview.clear()
        elif self.isVisible() and self.results.currentItem() is not None:
            index = self.results.currentItem().data(0, Qt.ItemDataRole.UserRole)
            expected = plan["rows"][index]
            if self.preview.snapshots != [expected["base"], expected["preview"]]:
                self._preview()

    def showEvent(self, event):
        if self.translation_service and self._observer_removed:
            self.translation_service.add_observer(self._on_language_changed)
            self._observer_removed = False
        if not self._theme_subscribed:
            self._subscribe_theme_manager()
        self.retranslate_ui()
        super().showEvent(event)
        self.refresh()

    def retranslate_ui(self):
        super().retranslate_ui()
        if not hasattr(self, "_buttons"):
            return
        for key, button in self._buttons.items():
            button.setText(self._tr("calibration." + key))
        self.name.setPlaceholderText(self._tr("calibration.group_name"))
        self.sample.setAccessibleName(self._tr("calibration.sample"))
        self.minutes_label.setText(self._tr("calibration.minutes"))
        self.local_time.setText(self._tr("calibration.confirm_local"))
        self.exposure.setText(self._tr("calibration.exposure"))
        self.white_balance.setText(self._tr("calibration.white_balance"))
        self.notice.setText(self._tr("calibration.notice"))
        self.results.setHeaderLabels([self._tr(key) for key in ("batch.photo", "calibration.exposure", "calibration.white_balance", "batch.result")])
        self.suggestions.setHeaderLabels([self._tr("calibration.group_name"), self._tr("calibration.members_label")])
        self.preview.retranslate()
        self.refresh()
