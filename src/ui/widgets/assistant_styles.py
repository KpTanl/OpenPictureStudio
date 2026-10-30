from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_MD, px
from .apple_button import AppleButton, AppleGhostButton
from .apple_menu import AppleDropDown
from .assistant_reference import AssistantReference
from .message_dialog import confirm_destructive
from .setting_row import SettingRow
from ..edit_descriptions import parameter_name, value_text


def _label(parent):
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _row(control, parent):
    row = SettingRow(parent)
    row.add_control(control)
    return row


class StyleConfirmation(QWidget):
    confirmed = Signal(str, object)
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ticket = ""
        layout = QVBoxLayout(self)
        self.title = _label(self)
        layout.addWidget(self.title)
        self.scroll = QScrollArea(self)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        content = QWidget(self.scroll)
        form = QVBoxLayout(content)
        self.fields, self.rows = {}, {}
        for key in ("existing", "name", "scene", "before", "result", "text"):
            control = AppleDropDown(content) if key in ("existing", "before", "result") else QLineEdit(content)
            if isinstance(control, QLineEdit):
                control.setMaxLength(2000 if key == "text" else 240 if key == "scene" else 80)
            row = _row(control, content)
            form.addWidget(row)
            self.fields[key], self.rows[key] = control, row
        self.fields["before"].activated.connect(self._warning)
        self.fields["result"].activated.connect(self._warning)
        self.fields["existing"].activated.connect(self._existing)
        self.fields["name"].textEdited.connect(self._warning)
        self.fields["text"].textEdited.connect(self._warning)
        self.warning = _label(content)
        self.warning.setObjectName("mutedText")
        form.addWidget(self.warning)
        form.addStretch()
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        footer = QHBoxLayout()
        self.cancel = AppleGhostButton(parent=self)
        self.cancel.clicked.connect(self.cancelled)
        self.save = AppleButton(parent=self)
        self.save.clicked.connect(self._confirm)
        footer.addWidget(self.cancel)
        footer.addWidget(self.save)
        layout.addLayout(footer)

    def set_state(self, pending, styles, tr):
        self._pending, self._styles, self._tr = pending, styles, tr
        preference = pending["kind"] == "preference"
        self.title.setText(tr("assistant.style.confirm_preference" if preference else "assistant.style.confirm"))
        self.cancel.setText(tr("common.cancel"))
        self.save.setText(tr("assistant.style.saving" if pending["status"] == "saving" else "assistant.style.confirm_save"))
        self.save.setEnabled(pending["status"] == "ready")
        for key, row in self.rows.items():
            row.set_text(tr("assistant.style.field." + key))
            row.setVisible((key == "text") == preference)
            row.setEnabled(pending["status"] == "ready")
        if pending["ticket"] != self._ticket or (pending["status"] == "ready" and not self.fields["before"].count() and not preference):
            self._ticket = pending["ticket"]
            for key in ("name", "scene", "text"):
                self.fields[key].setText(pending.get(key, ""))
            existing = self.fields["existing"]
            existing.clear()
            existing.addItem(tr("assistant.style.new"), "")
            for style in styles:
                existing.addItem(style["name"], style["id"])
            existing.setCurrentIndex(max(0, existing.findData(pending.get("style_id", ""))))
            for key, selected in (("before", "before_revision"), ("result", "result_revision")):
                self.fields[key].clear()
                for row in pending.get("history", ()):
                    title = tr("assistant.style.revision", number=row["number"]) + (" · " + row["title"] if row.get("title") else "")
                    self.fields[key].addItem(title, row["id"])
                self.fields[key].setCurrentIndex(max(0, self.fields[key].findData(pending.get(selected, ""))))
        self._existing()
        self._warning()
        self.fields["existing"].setItemText(0, tr("assistant.style.new"))
        for row in pending.get("history", ()):
            title = tr("assistant.style.revision", number=row["number"]) + (" · " + row["title"] if row.get("title") else "")
            for key in ("before", "result"):
                self.fields[key].setItemText(self.fields[key].findData(row["id"]), title)

    def _existing(self, *_args):
        style_id = self.fields["existing"].currentData()
        self.fields["name"].setEnabled(not style_id and self._pending["status"] == "ready")
        self._warning()

    def _warning(self, *_args):
        if self._pending["kind"] == "preference":
            self.warning.setText(self._tr("assistant.style.preference_scope"))
            self.save.setEnabled(self._pending["status"] == "ready" and bool(self.fields["text"].text().strip()))
            return
        ids = [row["id"] for row in self._pending.get("history", ())]
        a, b = self.fields["before"].currentData(), self.fields["result"].currentData()
        valid = a in ids and b in ids and ids.index(a) < ids.index(b)
        mixed = valid and any(row.get("purpose") != "style" for row in self._pending["history"][ids.index(a) + 1:ids.index(b) + 1])
        self.warning.setText(self._tr("assistant.style.mixed" if mixed else "assistant.style.pair_help" if valid else "assistant.style.invalid_pair"))
        self.save.setEnabled(valid and self._pending["status"] == "ready"
                             and bool(self.fields["existing"].currentData() or self.fields["name"].text().strip()))

    def _confirm(self):
        fields = {key: self.fields[key].text().strip() for key in ("name", "scene", "text")}
        fields.update(before_revision=self.fields["before"].currentData(), result_revision=self.fields["result"].currentData(),
                      style_id=self.fields["existing"].currentData())
        selected = next((style for style in self._styles if style["id"] == fields["style_id"]), None)
        if selected:
            fields.update(name=selected["name"], version=selected["version"])
        self.confirmed.emit(self._ticket, fields)


class StyleManager(QWidget):
    command_requested = Signal(str, object)
    apply_requested = Signal(str, str, str)
    replay_requested = Signal(str, str)
    preference_requested = Signal(str)
    back_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._signature = None
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        self.back = AppleGhostButton(parent=self)
        self.back.clicked.connect(self.back_requested)
        self.title = _label(self)
        header.addWidget(self.back)
        header.addWidget(self.title, 1)
        root.addLayout(header)
        self.picker = AppleDropDown(self)
        self.picker.activated.connect(self._selected)
        root.addWidget(self.picker)
        scroll = QScrollArea(self)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        body = QWidget(scroll)
        self.layout = QVBoxLayout(body)
        self.layout.setSpacing(px(SPACING_MD))
        self.name, self.description = QLineEdit(body), QLineEdit(body)
        self.name.setMaxLength(80)
        self.description.setMaxLength(2000)
        self.name_row, self.description_row = _row(self.name, body), _row(self.description, body)
        self.layout.addWidget(self.name_row)
        self.layout.addWidget(self.description_row)
        self.sample_picker = AppleDropDown(body)
        self.sample_picker.activated.connect(self._sample)
        self.layout.addWidget(self.sample_picker)
        self.scene = QLineEdit(body)
        self.scene.setMaxLength(240)
        self.scene_row = _row(self.scene, body)
        self.layout.addWidget(self.scene_row)
        self.previews = {}
        for key in ("before", "result"):
            preview = AssistantReference(body)
            self.layout.addWidget(preview)
            self.previews[key] = preview
        self.details = _label(body)
        self.layout.addWidget(self.details)
        self.remove_sample = AppleGhostButton(parent=body)
        self.remove_sample.clicked.connect(lambda: self._remove(True))
        self.layout.addWidget(self.remove_sample)
        self.preferences_title = _label(body)
        self.layout.addWidget(self.preferences_title)
        self.preferences = AppleDropDown(body)
        self.layout.addWidget(self.preferences)
        self.preference_text = QLineEdit(body)
        self.preference_text.setMaxLength(2000)
        self.layout.addWidget(self.preference_text)
        self.remember = AppleGhostButton(parent=body)
        self.remember.clicked.connect(lambda: self.preference_requested.emit(self.preference_text.text()))
        self.forget = AppleGhostButton(parent=body)
        self.forget.clicked.connect(self._forget)
        self.layout.addWidget(self.remember)
        self.layout.addWidget(self.forget)
        self.layout.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        self.buttons = {}
        for key, action in (("apply", self._apply), ("replay", self._replay), ("save", self._save),
                            ("toggle", self._toggle), ("remove", lambda: self._remove(False))):
            button = AppleButton(parent=self) if key == "apply" else AppleGhostButton(parent=self)
            button.clicked.connect(action)
            self.buttons[key] = button
            root.addWidget(button)

    def set_state(self, state, tr):
        self._state, self._styles, self._tr = state, state.get("styles", ()), tr
        self.title.setText(tr("assistant.style.manage"))
        self.back.setText(tr("assistant.back_to_chat"))
        self.name_row.set_text(tr("assistant.style.field.name"))
        self.description_row.set_text(tr("assistant.style.description"))
        self.scene_row.set_text(tr("assistant.style.field.scene"))
        self.remove_sample.setText(tr("assistant.style.remove_sample"))
        self.preferences_title.setText(tr("assistant.style.preferences"))
        self.remember.setText(tr("assistant.style.add_preference"))
        self.forget.setText(tr("assistant.style.forget"))
        self.preference_text.setPlaceholderText(tr("assistant.style.preference_placeholder"))
        language_changed = getattr(self, "_language_title", "") != tr("assistant.style.manage")
        self._language_title = tr("assistant.style.manage")
        signature = ([(style["id"], style["version"]) for style in self._styles], state.get("style_preferences", ()))
        if signature != self._signature:
            old = self.picker.currentData()
            self._signature = signature
            self.picker.clear()
            for style in self._styles:
                self.picker.addItem(style["name"] + (" · " + tr("assistant.style.disabled") if style["status"] == "disabled" else ""), style["id"])
            self.picker.setCurrentIndex(max(0, self.picker.findData(old)))
            self.preferences.clear()
            for item in state.get("style_preferences", ()):
                self.preferences.addItem(item["text"], item["id"])
            self._selected()
        else:
            for style in self._styles:
                self.picker.setItemText(self.picker.findData(style["id"]), style["name"] +
                    (" · " + tr("assistant.style.disabled") if style["status"] == "disabled" else ""))
            self.sample_picker.setItemText(0, tr("assistant.style.auto_sample"))
            if language_changed:
                scene = self.scene.text()
                self._sample()
                self.scene.setText(scene)
            self._labels()

    def _record(self):
        return next((style for style in self._styles if style["id"] == self.picker.currentData()), None)

    def _selected(self, *_args):
        record = self._record()
        self.name.setText(record["name"] if record else "")
        self.description.setText(record["description"] if record else "")
        self.sample_picker.clear()
        self.sample_picker.addItem(self._tr("assistant.style.auto_sample"), "")
        for sample in record["samples"] if record else ():
            self.sample_picker.addItem(sample["scene"] or self._tr("assistant.style.unnamed_scene"), sample["id"])
        self._sample()
        self._labels()

    def _labels(self):
        record = self._record()
        busy = bool(self._state.get("busy") or self._state.get("style_busy"))
        for key, button in self.buttons.items():
            button.setText(self._tr("assistant.style." + ("restore" if key == "toggle" and record and record["status"] == "disabled" else key)))
            button.setEnabled(bool(record) and not busy and (key != "apply" or record["status"] == "active" and bool(record["samples"])))
        sample = next((item for item in record["samples"] if item["id"] == self.sample_picker.currentData()), None) if record else None
        self.buttons["replay"].setEnabled(bool(sample and record["status"] == "active"
            and sample["source"]["object_key"] == self._state.get("photo") and not busy))

    def _sample(self, *_args):
        record = self._record()
        sample = next((item for item in record["samples"] if item["id"] == self.sample_picker.currentData()), None) if record else None
        self.scene.setText(sample["scene"] if sample else "")
        self.scene_row.setVisible(bool(sample))
        self.remove_sample.setVisible(bool(sample))
        for key, preview in self.previews.items():
            preview.set_reference({"name": self._tr("assistant.style.preview." + key), "thumbnail": sample["thumbnails"][key]} if sample else None,
                                  self._tr, title=self._tr("assistant.style.preview." + key))
        lines = [self._tr("assistant.style.version", version=record["version"])] if record else [self._tr("assistant.style.empty")]
        if sample:
            lines.append(self._tr("assistant.style.replay_help"))
            for tool in ("color", "beauty"):
                for key, value in sample["delta"][tool].items():
                    tool_id = "color_adjust" if tool == "color" else "beauty"
                    lines.append((parameter_name(tool_id, key, self._tr) or key) + " " + (value_text(tool_id, key, value) or f"{value:+g}"))
            for color, parts in sample["delta"]["mixer"].items():
                for part, value in parts.items():
                    lines.append(parameter_name("color_adjust", "color_mixer." + color + "." + part, self._tr) + f" {value:+g}")
            for group in sample.get("feedback", ()):
                lines.extend(item["text"] for item in group["feedback"] if item.get("text") and item.get("origin") == "user_message")
        self.details.setText("\n".join(lines))
        self._labels()

    def _save(self):
        record = self._record()
        if record:
            scenes = {self.sample_picker.currentData(): self.scene.text()}
            self.command_requested.emit("style_edit", {"style_id": record["id"], "version": record["version"], "fields": {
                "name": self.name.text(), "description": self.description.text(), "status": record["status"], "scenes": scenes}})

    def _toggle(self):
        record = self._record()
        if record:
            self.command_requested.emit("style_edit", {"style_id": record["id"], "version": record["version"], "fields": {
                "name": record["name"], "description": record["description"],
                "status": "disabled" if record["status"] == "active" else "active", "scenes": {}}})

    def _remove(self, sample):
        record = self._record()
        if record and confirm_destructive(self, self._tr("assistant.style.remove_sample" if sample else "assistant.style.remove"),
                                          self._tr("assistant.style.remove_confirm"),
                                          self._tr("assistant.style.remove_sample" if sample else "assistant.style.remove")):
            self.command_requested.emit("style_remove", {"style_id": record["id"], "version": record["version"],
                                        "sample_id": self.sample_picker.currentData() if sample else ""})

    def _forget(self):
        identity = self.preferences.currentData()
        if identity and confirm_destructive(self, self._tr("assistant.style.forget"), self._tr("assistant.style.forget_confirm"),
                                            self._tr("assistant.style.forget")):
            self.command_requested.emit("preference_remove", {"preference_id": identity})

    def _apply(self):
        record = self._record()
        if record:
            self.apply_requested.emit(record["id"], self.sample_picker.currentData() or "", record["name"])

    def _replay(self):
        record = self._record()
        if record:
            self.replay_requested.emit(record["id"], self.sample_picker.currentData())

    def select_sample(self, identity):
        self.sample_picker.setCurrentIndex(max(0, self.sample_picker.findData(identity)))
        self._sample()
