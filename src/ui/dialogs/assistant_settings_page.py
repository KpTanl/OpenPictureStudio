from __future__ import annotations

import json

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QGridLayout, QLineEdit, QListWidget, QListWidgetItem, QStackedWidget, QVBoxLayout, QWidget

from resources.styles.theme_variables import ASSISTANT_CONTEXT_EDIT_HEIGHT, ASSISTANT_MODEL_LIST_HEIGHT, ASSISTANT_SETTINGS_FIELD_WIDTH, SPACING_MD, px
from ...application.assistant_models import AssistantConnection, AssistantModel, endpoint_url, new_id
from ...application.assistant_images import ImageObservationError, image_capability
from ..themed_mixin import ThemedI18nMixin
from ..widgets.apple_button import AppleButton, AppleGhostButton
from ..widgets.apple_choice import AppleCheckBox
from ..widgets.apple_menu import AppleDropDown, AppleMenu
from ..widgets.apple_spin_box import AppleSpinBox
from ..widgets.assistant_composer import AssistantTextEdit
from ..widgets.assistant_evaluations import EvaluationSettings
from ..widgets.assistant_transcript import assistant_error_text
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.management_page import ManagementListGroup, ManagementListItem, ManagementPage, management_label
from ..widgets.message_dialog import confirm_destructive
from ..widgets.segmented_control import AppleSegmentedControl
from ..widgets.setting_row import SettingRow, SettingSection


class AssistantSettingsPage(ManagementPage, ThemedI18nMixin):
    def __init__(self, runner, parent=None):
        super().__init__(parent)
        self._runner, self._state = runner, runner.state
        self._connection_id, self._model_id = "", ""
        self._signature, self._fields, self._rows, self._sections = None, {}, {}, []
        self._field_notes = {}
        self._diagnostic_ticket, self._feedback_key = "", ""
        self._diagnostic_target = None
        self._dirty, self._edit_revision, self._saving, self._after_save = False, 0, None, None
        self._discoveries, self._discovery_signature = {}, None
        self._addition, self._focus_model = None, False
        self._init_themed_i18n()
        self.destroyed.connect(self._cleanup_themed_i18n)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save)
        self.add_connection = AppleGhostButton(parent=self)
        self.add_connection.clicked.connect(self._add_connection)
        self.navigation_layout.insertWidget(0, self.add_connection)
        self.remove_connection = AppleGhostButton(parent=self)
        self.remove_connection.clicked.connect(self._remove_connection)
        self.header_actions.addWidget(self.remove_connection)
        self.navigation.selection_changed.connect(self._select_connection)
        self.model_picker = AppleDropDown(self)
        self.model_picker.activated.connect(self._select_model)
        self.add_model = AppleGhostButton(parent=self)
        self.add_model.clicked.connect(self._add_model)
        self.remove_model = AppleGhostButton(parent=self)
        self.remove_model.clicked.connect(self._remove_model)
        self.tabs = AppleSegmentedControl(self)
        self.tabs.add_segments(["", "", ""])
        self.pages = QStackedWidget(self)
        self.tabs.bind_stacked_widget(self.pages)
        self.body_layout.addWidget(self.tabs)
        self.body_layout.addWidget(self.pages)
        self._build_connection()
        self._build_model()
        self._build_advanced()
        self.evaluation = EvaluationSettings(self.pages)
        self.evaluation.settings_edited.connect(lambda values: self._runner.command(
            "evaluation_settings", backend=values["backend"], decisions_connection=values["decisions_connection"]))
        self.pages.addWidget(self.evaluation)
        self.discard = AppleGhostButton(parent=self)
        self.discard.clicked.connect(self._discard_changes)
        self.footer_layout.addWidget(self.discard)
        self.footer_layout.parentWidget().hide()
        self.badge.hide()
        runner.changed.connect(self._set_state)
        runner.event_received.connect(self._event)
        self.retranslate_ui()
        self._set_state(runner.state)
        if runner.last_error:
            self._event("error", runner.last_error)

    def _page(self):
        page = QWidget(self.pages)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_MD))
        self.pages.addWidget(page)
        return page, layout

    def _section(self, layout, key):
        section = SettingSection(self)
        self._sections.append((key, section))
        layout.addWidget(section)
        return section

    def _row(self, section, name, kind="text", *, options=(), low=0, high=1000000, note=False):
        row = SettingRow(section)
        if kind == "choice":
            control = AppleDropDown(row)
            for value in options:
                control.addItem(value, value)
            control.activated.connect(lambda _index: self._edited(name))
        elif kind == "bool":
            control = AppleCheckBox("", row)
            control.clicked.connect(lambda _checked: self._edited(name))
        elif kind == "int":
            control = AppleSpinBox(row)
            control.setRange(low, high)
            control.valueEdited.connect(lambda _value: self._edited(name))
        elif kind == "json":
            control = AssistantTextEdit(row)
            control.setFixedHeight(ASSISTANT_CONTEXT_EDIT_HEIGHT)
            control.textEdited.connect(lambda _text: self._edited(name))
        else:
            control = QLineEdit(row)
            if name == "connection.api_key":
                control.setEchoMode(QLineEdit.EchoMode.Password)
            control.textEdited.connect(lambda _text: self._edited(name))
            control.editingFinished.connect(self._save)
        control.setMinimumWidth(0)
        if kind in ("text", "json"):
            control.setMinimumWidth(ASSISTANT_SETTINGS_FIELD_WIDTH)
        else:
            control.setFixedWidth(ASSISTANT_SETTINGS_FIELD_WIDTH)
        row.add_control(control)
        section.add_row(row)
        self._fields[name], self._rows[name] = (control, kind, options), row
        if note:
            self._field_notes[name] = "assistant.field_note." + name
        return control

    def _build_connection(self):
        page, layout = self._page()
        section = self._section(layout, "connection")
        self._row(section, "connection.name")
        self._row(section, "connection.endpoint", note=True)
        self._row(section, "connection.api_key", note=True)
        self.show_key = AppleCheckBox("", page)
        self.show_key.clicked.connect(lambda visible: self._fields["connection.api_key"][0].setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password))
        self._rows["connection.api_key"].add_control(self.show_key)
        self.connection_options = CollapsibleCard("", page, is_expanded=False)
        layout.addWidget(self.connection_options)
        options = SettingSection(self.connection_options)
        self.connection_options.add_widget(options)
        self._row(options, "connection.provider", "choice", options=("generic", "openai", "deepseek"))
        self._row(options, "connection.protocol", "choice", options=("chat_completions", "responses"))
        self._row(options, "connection.authentication", "choice", options=("bearer", "header", "none"))
        self._row(options, "connection.key_header")
        self.tests = {"models": AppleButton(parent=page), "model": AppleButton(parent=page)}
        for key, button in self.tests.items():
            button.clicked.connect(lambda _checked=False, operation=key: self._diagnose(operation))
        layout.addWidget(self.tests["models"])
        self.cancel_test = AppleGhostButton(parent=page)
        self.cancel_test.clicked.connect(self._cancel_diagnostic)
        layout.addWidget(self.cancel_test)
        self.discovery_section = self._section(layout, "available_models")
        self.discovery_status = management_label(page, "managementCaption")
        self.discovery_section.layout().addWidget(self.discovery_status)
        self.model_search = QLineEdit(page)
        self.model_search.textEdited.connect(self._render_discovered)
        self.discovery_section.layout().addWidget(self.model_search)
        self.discovered_models = QListWidget(page)
        self.discovered_models.setMinimumWidth(0)
        self.discovered_models.setFixedHeight(ASSISTANT_MODEL_LIST_HEIGHT)
        self.discovered_models.currentItemChanged.connect(self._discovery_selection)
        self.discovered_models.itemActivated.connect(lambda _item: self._import_model())
        self.discovery_section.layout().addWidget(self.discovered_models)
        self.import_model = AppleButton(parent=page)
        self.import_model.clicked.connect(self._import_model)
        self.discovery_section.layout().addWidget(self.import_model)
        configured = self._section(layout, "configured_models")
        configured.layout().addWidget(self.model_picker)
        self.model_empty = management_label(page, "managementCaption")
        configured.layout().addWidget(self.model_empty)
        configured.layout().addWidget(self.add_model)
        actions = QGridLayout()
        self.default_model = AppleButton(parent=page)
        self.default_model.clicked.connect(self._set_default_model)
        actions.addWidget(self.default_model, 0, 0)
        actions.addWidget(self.tests["model"], 0, 1)
        actions.addWidget(self.remove_model, 1, 1)
        configured.layout().addLayout(actions)
        self.model_settings = CollapsibleCard("", page, is_expanded=False)
        layout.addWidget(self.model_settings)
        layout.addStretch()

    def _build_model(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.model_settings.add_layout(layout)
        section = self._section(layout, "model")
        self._row(section, "model.model")
        self._row(section, "model.name")
        self._row(section, "model.image_input", "bool")
        self._row(section, "model.image_effective_long_edge", "int", high=65535, note=True)
        self._row(section, "model.image_detail", "choice", options=("service_default", "auto", "low", "high", "original"), note=True)
        self._row(section, "model.tool_calling", "bool", note=True)
        self._row(section, "model.structured_output", "choice", options=("none", "json", "json_schema"))
        self._row(section, "model.streaming", "bool")
        self._row(section, "model.stream_usage", "bool", note=True)
        self._row(section, "model.reasoning", "choice", options=("none", "effort", "deepseek", "custom"))
        self._row(section, "model.efforts", note=True)
        self._row(section, "model.default_effort", note=True)
        self._row(section, "model.reasoning_summary", "bool", note=True)
        self._row(section, "model.reasoning_field", note=True)
        self._row(section, "model.context_window", "int", high=10000000, note=True)
        self._row(section, "model.max_output_tokens", "int", low=16)
        self._row(section, "model.reasoning_reserve", "int", note=True)
        sampling = self._section(layout, "generation")
        self._row(sampling, "model.output_field", "choice", options=("max_tokens", "max_completion_tokens"))
        self._row(sampling, "model.sampling_supported", "bool")
        self._row(sampling, "model.temperature", note=True)
        self._row(sampling, "model.top_p", note=True)
        self._row(sampling, "model.extra_fields", "json", note=True)
        layout.addStretch()

    def _build_advanced(self):
        _page, layout = self._page()
        timeout = self._section(layout, "timeouts")
        for key in ("connect_timeout", "first_event_timeout", "idle_timeout", "total_timeout"):
            self._row(timeout, "connection." + key, "int", low=1, high=3600)
        network = self._section(layout, "network")
        self._row(network, "connection.proxy")
        self._row(network, "connection.headers", "json", note=True)
        layout.addStretch()

    def _configuration(self):
        connection = next((item for item in self._state.get("connections", ()) if item["id"] == self._connection_id), None)
        models = [dict(item) for item in self._state.get("models", ()) if item["connection_id"] == self._connection_id]
        if connection is None:
            raise ValueError("AI_CONNECTION_MISSING")
        connection = dict(connection)
        model = next((item for item in models if item["id"] == self._model_id), None)
        previous_model = next((item for item in self._state.get("models", ()) if item["id"] == self._model_id), {})
        for name, (control, kind, _options) in self._fields.items():
            owner, field = name.split(".")
            target = connection if owner == "connection" else model
            if target is None:
                continue
            if kind == "choice":
                value = control.currentData()
                if field == "image_detail" and value == "service_default":
                    value = ""
            elif kind == "bool":
                value = control.isChecked()
            elif kind == "int":
                value = control.value()
            elif kind == "json":
                try:
                    value = json.loads(control.toPlainText().strip() or "{}")
                except ValueError:
                    raise ValueError("AI_HEADERS_INVALID" if field == "headers" else "AI_FIELDS_INVALID") from None
            else:
                value = control.text().strip()
                if field == "efforts":
                    value = tuple(item.strip() for item in value.split(",") if item.strip())
                elif field in ("temperature", "top_p"):
                    try:
                        value = float(value.replace(",", ".")) if value else None
                    except ValueError:
                        raise ValueError("AI_CONFIGURATION_INVALID") from None
            target[field] = value
        if model:
            if not model["name"] or model["name"] == previous_model.get("model") and model["model"] != previous_model.get("model"):
                model["name"] = model["model"]
            capability_fields = ("image_input", "image_effective_long_edge", "image_detail", "tool_calling", "structured_output", "streaming", "stream_usage", "reasoning", "efforts",
                                 "context_window", "reasoning_field", "reasoning_summary")
            if any(model[key] != previous_model.get(key) for key in capability_fields):
                model["capability_source"] = "manual"
            if model["reasoning"] == "none":
                model.update(efforts=(), default_effort="", reasoning_summary=False)
        return connection, models

    def _edited(self, name):
        if name == "connection.api_key" and self._fields[name][0].text().strip():
            authentication = self._fields["connection.authentication"][0]
            if authentication.currentData() == "none":
                authentication.setCurrentIndex(authentication.findData("bearer"))
        self._dirty = True
        self._edit_revision += 1
        self._after_save = None
        self._cancel_diagnostic()
        if name.startswith("connection."):
            self._discoveries.pop(self._connection_id, None)
            self._render_discovered()
        self._feedback_key = "assistant.saving"
        self.set_notice(self._tr(self._feedback_key))
        self._save_timer.start()
        self._save_controls()
        self._constraints()

    def _save(self):
        self._save_timer.stop()
        if self._saving or not self._dirty:
            return True
        if not self._connection_id:
            return False
        try:
            connection, models = self._configuration()
            AssistantConnection(**connection).validate()
            for model in models:
                if model["model"] or model["name"]:
                    AssistantModel.from_dict(model).validate()
        except ValueError as exc:
            self._after_save = None
            self._feedback_key = ""
            self.set_notice(assistant_error_text({"code": str(exc)}, self._tr), "warning")
            self.scroll.ensureWidgetVisible(self._notices["warning"])
            return False
        previous = next((item for item in self._state.get("connections", ()) if item["id"] == self._connection_id), None)
        previous_models = [item for item in self._state.get("models", ()) if item["connection_id"] == self._connection_id]
        if connection == previous and models == previous_models:
            self._dirty = False
            self._feedback_key = "assistant.settings_saved"
            self.set_notice(self._tr(self._feedback_key))
            self._save_controls()
            self._continue_after_save()
            return True
        self._saving = {"ticket": new_id(), "target": (self._connection_id, self._model_id),
                        "revision": self._edit_revision, "connection": connection, "models": models}
        self._runner.command("configuration", connection=connection, models=models, ticket=self._saving["ticket"])
        self._save_controls()
        return True

    def has_unsaved_changes(self):
        return self._dirty or self._saving is not None

    def save_before_close(self, action):
        self._cancel_diagnostic()
        self._when_saved(action)

    def _save_controls(self):
        self.footer_layout.parentWidget().setVisible(self.has_unsaved_changes())
        self.discard.setEnabled(self._dirty and self._saving is None)

    def _discard_changes(self):
        if self._saving or not confirm_destructive(self, self._tr("assistant.discard_settings"),
                self._tr("assistant.discard_settings_note"), self._tr("assistant.discard_settings")):
            return
        self._save_timer.stop()
        self._after_save = None
        self._dirty = False
        self._edit_revision += 1
        self._signature = None
        self.set_notice()
        self._set_state(self._state)

    def _when_saved(self, action):
        self._after_save = ((self._connection_id, self._model_id), self._edit_revision, action)
        if self._dirty or self._saving:
            self._save()
        else:
            self._continue_after_save()

    def _continue_after_save(self):
        pending, self._after_save = self._after_save, None
        if pending and pending[:2] == ((self._connection_id, self._model_id), self._edit_revision):
            pending[2]()

    def _set_state(self, state):
        self._state = state
        self.evaluation.set_state(state, self._tr)
        connections = state.get("connections", ())
        if self._connection_id not in {item["id"] for item in connections}:
            self._cancel_diagnostic()
            self._after_save = None
            self._dirty = False
            self._connection_id = next(iter(connections), {}).get("id", "")
            self._signature = None
        self.navigation.set_groups((ManagementListGroup("connections", self._tr("assistant.connections"), tuple(
            ManagementListItem(item["id"], item["name"], item["endpoint"], "cpu") for item in connections)),),
            preferred_key=self._connection_id)
        connection = next((item for item in connections if item["id"] == self._connection_id), {})
        models = [item for item in state.get("models", ()) if item["connection_id"] == self._connection_id]
        if self._model_id not in {item["id"] for item in models} and (self._model_id or models):
            self._model_id = next(iter(models), {}).get("id", "")
        self.model_picker.clear()
        for item in models:
            label = item["name"] or item["model"] or self._tr("assistant.unnamed_model")
            if item["id"] == state.get("default_model"):
                label = self._tr("assistant.model_with_default", model=label)
            self.model_picker.addItem(label, item["id"])
        index = next((index for index, item in enumerate(models) if item["id"] == self._model_id), -1)
        self.model_picker.setCurrentIndex(index)
        self.title.setText(connection.get("name") or self._tr("assistant.services_title"))
        try:
            self.subtitle.setText(endpoint_url(connection.get("endpoint", ""), connection.get("protocol", "chat_completions")))
        except ValueError:
            self.subtitle.setText(self._tr("assistant.services_empty"))
        signature = (self._connection_id, self._model_id)
        selected_model = next((item for item in models if item["id"] == self._model_id), {})
        if signature != self._signature:
            previous = self._signature
            self._signature = signature
            model = next((item for item in models if item["id"] == self._model_id), {})
            self._project_values(connection, model, connection_changed=previous is None or previous[0] != self._connection_id)
        self.remove_connection.setEnabled(bool(connection))
        self.add_model.setEnabled(bool(connection))
        self.remove_model.setEnabled(bool(self._model_id))
        self.remove_model.setVisible(bool(self._model_id))
        self.model_picker.setVisible(bool(models))
        self.model_empty.setVisible(not models)
        self.model_settings.setVisible(bool(self._model_id))
        self.tests["model"].setVisible(bool(self._model_id))
        self.default_model.setVisible(bool(self._model_id))
        self.default_model.setEnabled(bool(selected_model.get("model")) and self._model_id != state.get("default_model"))
        for key, button in self.tests.items():
            button.setEnabled(bool(connection) and (key != "model" or bool(selected_model.get("model"))) and not self._diagnostic_ticket)
        self.cancel_test.setVisible(bool(self._diagnostic_ticket))
        self._render_discovered()
        self._save_controls()
        self._constraints()
        if self._focus_model and selected_model and not selected_model.get("model"):
            self._focus_model = False
            self.model_settings.set_expanded(True)
            self._fields["model.model"][0].setFocus()
            self.scroll.ensureWidgetVisible(self._fields["model.model"][0])

    def _project_values(self, connection, model, *, connection_changed=True):
        for name, (control, kind, options) in self._fields.items():
            owner, field = name.split(".")
            if owner == "connection" and not connection_changed:
                continue
            values = connection if owner == "connection" else model
            value = values.get(field)
            control.setEnabled(bool(values))
            if kind == "choice":
                if field == "image_detail" and not value:
                    value = "service_default"
                control.setCurrentIndex(options.index(value) if value in options else 0)
            elif kind == "bool":
                control.setChecked(bool(value))
            elif kind == "int":
                control.setValue(value or control.minimum())
            elif kind == "json":
                control.setPlainText(json.dumps(value or {}, ensure_ascii=False, indent=2))
            else:
                text = ", ".join(value) if field == "efforts" and value else "" if value is None else str(value)
                control.setText(text)
        self.show_key.setChecked(False)
        self._fields["connection.api_key"][0].setEchoMode(QLineEdit.EchoMode.Password)

    def _constraints(self):
        image_enabled = bool(self._model_id) and self._fields["model.image_input"][0].isChecked()
        for name in ("model.image_effective_long_edge", "model.image_detail"):
            self._rows[name].setEnabled(image_enabled)
        self._image_constraints()
        authentication = self._fields["connection.authentication"][0].currentData()
        self._rows["connection.key_header"].setVisible(authentication == "header")
        self._fields["connection.api_key"][0].setEnabled(bool(self._connection_id))
        self.show_key.setEnabled(bool(self._connection_id))
        self._rows["connection.api_key"].set_message(
            self._tr("assistant.authentication.key_enables_bearer") if authentication == "none" else "")
        reasoning = self._fields["model.reasoning"][0].currentData()
        effort = self._fields["model.default_effort"][0].text().strip()
        preferences = (self._state.get("conversation") or {}).get("preferences", {})
        if preferences.get("model_id") == self._model_id:
            effort = preferences.get("effort", "")
        thinking = reasoning == "deepseek" and effort != "none"
        sampling = self._fields["model.sampling_supported"][0].isChecked()
        self._rows["model.temperature"].setEnabled(bool(self._model_id) and sampling and not thinking)
        self._rows["model.temperature"].set_message(self._tr("assistant.temperature_thinking") if thinking else "")
        self._rows["model.top_p"].setEnabled(bool(self._model_id) and sampling)
        self._rows["model.top_p"].set_message(self._tr("assistant.top_p_thinking") if thinking else "")
        self._rows["model.reasoning_field"].setVisible(reasoning == "custom")
        for name in ("model.efforts", "model.default_effort"):
            self._rows[name].setEnabled(bool(self._model_id) and reasoning != "none")
        self._rows["model.reasoning_summary"].setEnabled(bool(self._model_id) and reasoning == "effort"
            and self._fields["connection.protocol"][0].currentData() == "responses")

    def _image_constraints(self):
        model = AssistantModel(model=self._fields["model.model"][0].text(),
            image_effective_long_edge=self._fields["model.image_effective_long_edge"][0].value(),
            image_detail=self._fields["model.image_detail"][0].currentData().replace("service_default", ""))
        row = self._rows["model.image_effective_long_edge"]
        detail_row = self._rows["model.image_detail"]
        try:
            capability = image_capability(model)
        except ImageObservationError as exc:
            row.set_message()
            detail_row.set_message(assistant_error_text({"code": str(exc)}, self._tr), "warning")
            return
        detail_row.set_message()
        limit = capability["rule_long_edge"]
        if not self._model_id:
            row.set_message()
        elif limit:
            exceeded = capability["configured_long_edge"] > limit
            row.set_message(self._tr("assistant.image_rule_exceeded" if exceeded else "assistant.image_rule_known",
                                    limit=limit, effective=capability["effective_long_edge"]),
                            "warning" if exceeded else "info")
        else:
            row.set_message(self._tr("assistant.image_rule_unknown"))

    def _select_connection(self, identity):
        self._when_saved(lambda: self._switch_connection(identity))
        self.navigation.set_groups((ManagementListGroup("connections", self._tr("assistant.connections"), tuple(
            ManagementListItem(item["id"], item["name"], item["endpoint"], "cpu")
            for item in self._state.get("connections", ()))),), preferred_key=self._connection_id)

    def _switch_connection(self, identity):
        self._cancel_diagnostic()
        self._connection_id, self._model_id, self._signature = identity, "", None
        self._focus_model = False
        self.model_search.clear()
        self.set_notice()
        self._set_state(self._state)
        self.tabs.setCurrentIndex(0)
        self.scroll.verticalScrollBar().setValue(0)

    def _select_model(self, index):
        identity = self.model_picker.itemData(index)
        self._when_saved(lambda: self._switch_model(identity))
        self.model_picker.setCurrentIndex(self.model_picker.findData(self._model_id))

    def _switch_model(self, identity):
        self._cancel_diagnostic()
        self._model_id = identity
        self._set_state(self._state)

    def _add_model(self):
        self._when_saved(lambda: self._create_model())

    def _create_model(self, model_id=""):
        if self._addition or model_id and any(model["connection_id"] == self._connection_id and model["model"] == model_id
                                              for model in self._state.get("models", ())):
            return
        self._addition = ("add_model", (self._connection_id, self._model_id), self._edit_revision)
        self._focus_model = not model_id
        self._runner.command("add_model", connection_id=self._connection_id, model_id=model_id)

    def _create_connection(self, provider):
        if self._addition:
            return
        self._addition = ("add_connection", (self._connection_id, self._model_id), self._edit_revision)
        self._runner.command("add_connection", provider=provider)

    def _set_default_model(self):
        self._when_saved(lambda: self._runner.command("default_model", identity=self._model_id))

    def _add_connection(self):
        from PyQt6.QtCore import QPoint
        menu = AppleMenu(self)
        for provider in ("openai", "deepseek", "generic"):
            menu.addAction(self._tr("assistant.option." + provider),
                           lambda _checked=False, value=provider: self._when_saved(
                               lambda: self._create_connection(value)))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.add_connection.mapToGlobal(QPoint(0, self.add_connection.height())))

    def _remove_connection(self):
        if self._connection_id and confirm_destructive(self, self._tr("assistant.delete_connection"),
                self._tr("assistant.delete_connection_note"), self._tr("assistant.delete_connection")):
            self._when_saved(lambda: self._runner.command("delete_connection", identity=self._connection_id))

    def _remove_model(self):
        if self._model_id and confirm_destructive(self, self._tr("assistant.delete_model"),
                self._tr("assistant.delete_model_note"), self._tr("assistant.delete_model")):
            self._when_saved(lambda: self._runner.command("delete_model", identity=self._model_id))

    def _diagnose(self, operation):
        self._when_saved(lambda: self._start_diagnostic(operation))

    def _start_diagnostic(self, operation):
        self._cancel_diagnostic()
        self._diagnostic_ticket = new_id()
        self._diagnostic_target = (self._connection_id, self._model_id, self._edit_revision)
        self._runner.command("diagnose", connection_id=self._connection_id, model_id=self._model_id,
                             operation=operation, ticket=self._diagnostic_ticket)
        self.set_notice(self._tr("assistant.diagnostic_running"))
        self._set_state(self._state)
        self.scroll.ensureWidgetVisible(self.cancel_test)

    def _cancel_diagnostic(self):
        if self._diagnostic_ticket:
            self._runner.command("cancel_diagnostic", ticket=self._diagnostic_ticket)
            self.set_notice(self._tr("assistant.error.AI_CANCELLED"))
        self._diagnostic_ticket, self._diagnostic_target = "", None
        if hasattr(self, "cancel_test"):
            self.cancel_test.hide()
            self.tests["models"].setEnabled(bool(self._connection_id))
            self.tests["model"].setEnabled(bool(self._model_id and self._fields["model.model"][0].text().strip()))

    def _event(self, kind, value):
        current_target = (self._connection_id, self._model_id)
        if kind == "configured" and self._saving and value["ticket"] == self._saving["ticket"]:
            saved, self._saving = self._saving, None
            connection = dict(saved["connection"])
            connection["revision"] += 1
            models = [dict(model, tool_calling=value.get("tool_calling", {}).get(model["id"], model["tool_calling"]))
                      for model in saved["models"]]
            self._state = dict(self._state, connections=[connection if item["id"] == connection["id"] else item
                                for item in self._state.get("connections", ())],
                               models=[item for item in self._state.get("models", ())
                                       if item["connection_id"] != connection["id"]] + models)
            if current_target == saved["target"] and self._edit_revision == saved["revision"]:
                self._dirty = False
                model = next((item for item in models if item["id"] == self._model_id), None)
                if model:
                    self._fields["model.name"][0].setText(model["name"])
                    self._fields["model.tool_calling"][0].setChecked(model["tool_calling"])
                self._feedback_key = "assistant.settings_saved"
                self.set_notice(self._tr(self._feedback_key))
            if self._dirty:
                self._save()
            else:
                self._save_controls()
                self._continue_after_save()
        elif (kind == "error" and value.get("operation") == "configuration" and self._saving
              and value.get("ticket") == self._saving["ticket"]):
            self._saving, self._after_save = None, None
            self._save_timer.stop()
            self._feedback_key = ""
            self.set_notice(assistant_error_text(value, self._tr), "warning")
            self.scroll.ensureWidgetVisible(self._notices["warning"])
            self._save_controls()
        elif kind == "error" and value.get("operation") in ("load", "add_connection", "add_model", "delete_connection", "delete_model", "default_model", "evaluation_settings"):
            self._addition = None
            self._focus_model = False
            self.set_notice(assistant_error_text(value, self._tr), "warning")
        elif kind in ("configuration_added", "model_added"):
            addition, self._addition = self._addition, None
            if addition and addition[1:] == (current_target, self._edit_revision):
                self._cancel_diagnostic()
                self._connection_id = value["connection_id"]
                self._model_id = value.get("model_id", "")
                self.tabs.setCurrentIndex(0)
            else:
                self._focus_model = False
        elif kind == "diagnostic" and value["ticket"] == self._diagnostic_ticket:
            target = self._diagnostic_target
            self._diagnostic_ticket, self._diagnostic_target = "", None
            if target != (self._connection_id, self._model_id, self._edit_revision):
                return
            if value["success"]:
                self.set_notice(self._tr("assistant.diagnostic_success." + value["operation"]))
                if value["operation"] == "models":
                    models = {model["id"]: model for model in value.get("models", ()) if model["id"].strip()}
                    self._discoveries[self._connection_id] = tuple(models[key] for key in sorted(models, key=str.casefold))
            else:
                self.set_notice(assistant_error_text(value["error"], self._tr), "warning")
            self._set_state(self._state)
            self.scroll.ensureWidgetVisible(self.discovery_status if value["success"] and value["operation"] == "models"
                                            else self._notices["info" if value["success"] else "warning"])

    def _render_discovered(self, *_args):
        models = self._discoveries.get(self._connection_id)
        self.model_search.setVisible(bool(models))
        self.discovered_models.setVisible(bool(models))
        self.import_model.setVisible(bool(models))
        self.discovery_status.setText(self._tr("assistant.models_not_loaded" if models is None else
                                               "assistant.models_empty" if not models else "assistant.models_available",
                                               count=len(models or ())))
        query = self.model_search.text().strip().casefold()
        existing = {model["model"] for model in self._state.get("models", ()) if model["connection_id"] == self._connection_id}
        entries = tuple((model["id"], model["id"] in existing) for model in models or () if query in model["id"].casefold())
        signature = (self._connection_id, entries, self._tr("assistant.model_already_added", model=""))
        if signature != self._discovery_signature:
            selected = self.discovered_models.currentItem()
            identity = selected.data(Qt.ItemDataRole.UserRole) if selected else ""
            self.discovered_models.clear()
            self._discovery_signature = signature
            for model_id, added in entries:
                item = QListWidgetItem(self._tr("assistant.model_already_added", model=model_id) if added else model_id)
                item.setData(Qt.ItemDataRole.UserRole, model_id)
                item.setToolTip(model_id)
                self.discovered_models.addItem(item)
                if added:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                elif model_id == identity:
                    self.discovered_models.setCurrentItem(item)
            if not self.discovered_models.currentItem():
                first = next((self.discovered_models.item(index) for index in range(self.discovered_models.count())
                              if self.discovered_models.item(index).flags() & Qt.ItemFlag.ItemIsEnabled), None)
                if first:
                    self.discovered_models.setCurrentItem(first)
        if models and not entries:
            self.discovery_status.setText(self._tr("assistant.models_no_match"))
        self._discovery_selection()

    def _discovery_selection(self, *_args):
        item = self.discovered_models.currentItem()
        self.import_model.setEnabled(bool(item and item.flags() & Qt.ItemFlag.ItemIsEnabled))

    def _import_model(self):
        item = self.discovered_models.currentItem()
        if item and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            identity = item.data(Qt.ItemDataRole.UserRole)
            self._when_saved(lambda: self._create_model(identity))

    def retranslate_ui(self):
        if not hasattr(self, "tests"):
            return
        self.add_connection.setText(self._tr("assistant.add_connection"))
        self.remove_connection.setText(self._tr("assistant.delete_connection"))
        self.add_model.setText(self._tr("assistant.add_model_manually"))
        self.remove_model.setText(self._tr("assistant.delete_model"))
        self.default_model.setText(self._tr("assistant.set_default_model"))
        self.show_key.setText(self._tr("assistant.show_key"))
        self.cancel_test.setText(self._tr("common.cancel"))
        self.discard.setText(self._tr("assistant.discard_settings"))
        self.connection_options.set_title(self._tr("assistant.connection_options"))
        self.model_settings.set_title(self._tr("assistant.model_settings"))
        self.model_empty.setText(self._tr("assistant.configured_models_empty"))
        self.model_search.setPlaceholderText(self._tr("assistant.search_models"))
        self.model_search.setAccessibleName(self._tr("assistant.search_models"))
        self.discovered_models.setAccessibleName(self._tr("assistant.settings_section.available_models"))
        self.import_model.setText(self._tr("assistant.add_selected_model"))
        for index, key in enumerate(("connection_models", "advanced", "evaluation")):
            self.tabs.set_segment_text(index, self._tr("assistant.settings_tab." + key))
        for key, section in self._sections:
            section.set_title(self._tr("assistant.settings_section." + key))
        for name, row in self._rows.items():
            note_key = self._field_notes.get(name)
            row.set_text(self._tr("assistant.field." + name), self._tr(note_key) if note_key else "")
        for name, (control, kind, options) in self._fields.items():
            if kind == "choice":
                for index, value in enumerate(options):
                    key = "assistant.authentication.none" if name == "connection.authentication" and value == "none" else "assistant.option." + value
                    control.setItemText(index, self._tr(key, value))
        for key, button in self.tests.items():
            button.setText(self._tr("assistant.verify_models" if key == "models" else "assistant.diagnostic." + key))
        self.tests["model"].setToolTip(self._tr("assistant.model_test_note"))
        self._set_state(self._state)

    def hideEvent(self, event):
        self._after_save = None
        self._addition = None
        self._cancel_diagnostic()
        self._save()
        self.show_key.setChecked(False)
        self._fields["connection.api_key"][0].setEchoMode(QLineEdit.EchoMode.Password)
        super().hideEvent(event)

    def cleanup(self):
        self._after_save = None
        self._cancel_diagnostic()
        self._save()
        self._cleanup_themed_i18n()
