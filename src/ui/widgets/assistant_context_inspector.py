from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from resources.styles.theme_variables import (ASSISTANT_CONTEXT_EDIT_HEIGHT, CONTROL_HEIGHT_COMPACT,
                                              SPACING_MD, SPACING_SM, px)
from .apple_button import AppleGhostButton
from .apple_choice import AppleCheckBox
from .apple_menu import AppleDropDown
from .assistant_composer import AssistantTextEdit
from .assistant_reference import AssistantReference, measurement_text
from .collapsible_card import CollapsibleCard
from ...application.assistant_tools import TOOLS, flat_values, grouped
from ..edit_descriptions import parameter_name, stage_title, value_text


def disclosure_text(context, model, tr):
    image = ("off" if not context.get("image") else "unavailable" if not model.get("image_input")
             else "native" if model.get("image_effective_long_edge", 0) else "preview")
    return (tr("assistant.disclosure." + image) + " · "
            + tr("assistant.disclosure.analysis_on" if context.get("analysis", True) else "assistant.disclosure.analysis_off"))


class ContextInspector(QScrollArea):
    edited = pyqtSignal(dict)
    summary_edited = pyqtSignal(str)
    summarize_requested = pyqtSignal()
    close_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("assistantContextInspector")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._state = {}
        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(px(SPACING_MD), px(SPACING_MD), px(SPACING_MD), px(SPACING_MD))
        layout.setSpacing(px(SPACING_SM))
        header = QHBoxLayout()
        self.title = QLabel(content)
        self.title.setObjectName("toolSidebarTitle")
        self.close = AppleGhostButton(parent=content)
        self.close.setFixedHeight(CONTROL_HEIGHT_COMPACT)
        self.close.clicked.connect(self.close_requested)
        header.addWidget(self.title, 1)
        header.addWidget(self.close)
        layout.addLayout(header)
        self.photo = QLabel(content)
        self.photo.setObjectName("mutedText")
        self.photo.setWordWrap(True)
        self.photo.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.photo)
        self.choices = {}
        for key in ("history", "color", "workflow", "image", "analysis", "requirements_enabled", "summary_enabled"):
            control = AppleCheckBox("", content)
            control.clicked.connect(lambda checked, name=key: self.edited.emit({name: checked}))
            layout.addWidget(control)
            self.choices[key] = control
        self.image_source = AppleDropDown(content)
        self.image_source.activated.connect(lambda index: self.edited.emit({"image_source": self.image_source.itemData(index)}))
        layout.addWidget(self.image_source)
        self.image_note = QLabel(content)
        self.image_note.setObjectName("mutedText")
        self.image_note.setWordWrap(True)
        layout.addWidget(self.image_note)
        self.reference = AssistantReference(content)
        layout.addWidget(self.reference)
        self.reference_measurements = QLabel(content)
        self.reference_measurements.setObjectName("mutedText")
        self.reference_measurements.setTextFormat(Qt.TextFormat.PlainText)
        self.reference_measurements.setWordWrap(True)
        self.reference_measurements.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.reference_measurements)
        self.requirements = AssistantTextEdit(content)
        self.requirements.setFixedHeight(ASSISTANT_CONTEXT_EDIT_HEIGHT)
        self.requirements.textEdited.connect(lambda text: self.edited.emit({"requirements": text}))
        layout.addWidget(self.requirements)
        self.summary = CollapsibleCard("", content, is_expanded=False)
        self.summary_text = AssistantTextEdit(self.summary)
        self.summary_text.setFixedHeight(ASSISTANT_CONTEXT_EDIT_HEIGHT)
        self.summary_text.textEdited.connect(self.summary_edited)
        self.summary.add_widget(self.summary_text)
        self.summary_range = QLabel(self.summary)
        self.summary_range.setObjectName("mutedText")
        self.summary_range.setWordWrap(True)
        self.summary.add_widget(self.summary_range)
        layout.addWidget(self.summary)
        self.compress = AppleGhostButton(parent=content)
        self.compress.clicked.connect(self.summarize_requested)
        layout.addWidget(self.compress)
        self.budget = QLabel(content)
        self.budget.setObjectName("mutedText")
        self.budget.setWordWrap(True)
        layout.addWidget(self.budget)
        self.manifest = CollapsibleCard("", content, is_expanded=False)
        self.manifest_text = QLabel(self.manifest)
        self.manifest_text.setObjectName("mutedText")
        self.manifest_text.setWordWrap(True)
        self.manifest.add_widget(self.manifest_text)
        layout.addWidget(self.manifest)
        layout.addStretch()
        self.setWidget(content)

    def set_state(self, state, tr, *, photo=None, estimate=None, summary_busy=False):
        self._state = state
        record = state.get("conversation") or {}
        preferences = record.get("preferences", {})
        context = preferences.get("context", {})
        self.title.setText(tr("assistant.context_title"))
        self.close.setText(tr("common.close"))
        self.photo.setText(tr("assistant.context_photo", name=photo.photo_name, version=photo.state_version)
                           if photo else tr("assistant.no_photo"))
        model = next((model for model in state.get("models", ()) if model["id"] == preferences.get("model_id")), {})
        reference = state.get("reference")
        self.reference.set_reference(reference, tr)
        self.reference_measurements.setText(measurement_text((reference or {}).get("analysis"), tr))
        self.reference_measurements.setVisible(bool(reference))
        for key, control in self.choices.items():
            control.setText(tr("assistant.context." + key))
            checked = context.get(key, key == "analysis")
            if key == "image":
                checked = checked and (photo is not None or reference is not None) and model.get("image_input", False)
            if key == "color" and preferences.get("mode") in ("edit", "agent"):
                checked = True
            control.setChecked(checked)
            control.setEnabled((key not in ("color", "workflow", "image", "analysis") or photo is not None
                                or key in ("image", "analysis") and reference is not None)
                               and (key != "image" or model.get("image_input", False))
                               and (key != "color" or preferences.get("mode") not in ("edit", "agent")))
        self.image_source.clear()
        for source in ("current", "original"):
            self.image_source.addItem(tr("assistant.image_source." + source), source)
        self.image_source.setCurrentIndex(1 if context.get("image_source") == "original" else 0)
        self.image_source.setEnabled(bool(context.get("image") and photo and model.get("image_input")))
        self.image_note.setText(tr("assistant.image_note" if model.get("image_input") else "assistant.image_unavailable_note"))
        if not self.requirements.hasFocus():
            self.requirements.setPlainText(context.get("requirements", ""))
        self.requirements.setPlaceholderText(tr("assistant.requirements_placeholder"))
        self.requirements.setEnabled(context.get("requirements_enabled", False))
        summary = record.get("summary") or {}
        self.summary.set_title(tr("assistant.summary"))
        self.summary.setVisible(bool(summary))
        if not self.summary_text.hasFocus():
            self.summary_text.setPlainText(summary.get("text", ""))
        self.summary_range.setText(tr("assistant.summary_range", count=len(summary.get("covered", ()))))
        self.compress.setText(tr("assistant.summarizing" if summary_busy else "assistant.compress"))
        self.compress.setEnabled(not summary_busy and record.get("id") not in state.get("busy", ()) and bool(state.get("messages")))
        if estimate and "estimated_tokens" in estimate:
            window = str(estimate["window"]) if estimate["window"] else tr("assistant.window_unset")
            self.budget.setText(tr("assistant.budget", tokens=estimate["estimated_tokens"], window=window,
                                  reserved=estimate["reserved_tokens"], omitted=estimate["omitted_messages"]))
        else:
            self.budget.setText(tr("assistant.estimate_unavailable"))
        previous = next((message.get("context") for message in reversed(state.get("messages", ())) if message.get("context")), None)
        self.manifest.set_title(tr("assistant.last_sent"))
        self.manifest.setVisible(bool(previous))
        if previous:
            details = []
            for item in previous["manifest"]:
                detail = tr("assistant.context." + ("requirements_enabled" if item["kind"] == "requirements"
                            else "summary_enabled" if item["kind"] == "summary" else item["kind"]))
                if "version" in item:
                    detail += " · " + tr("assistant.photo_version", version=item["version"])
                if "count" in item:
                    detail += " · " + str(item["count"])
                if item["kind"] == "reference":
                    detail += " · " + tr("assistant.reference." + (
                        "image_analysis" if item.get("image") and item.get("analysis") else
                        "image_only" if item.get("image") else "analysis_only" if item.get("analysis") else "local_only"))
                details.append(detail)
            data = previous.get("data", {})
            if data.get("photo_analysis"):
                details.append(tr("assistant.photo.current") + "\n" + measurement_text(data["photo_analysis"], tr))
            if (previous.get("reference_context") or {}).get("analysis"):
                details.append(tr("assistant.reference.label") + "\n"
                               + measurement_text(previous["reference_context"]["analysis"], tr))
            for tool, values in grouped(data.get("current_adjustments", {})).items():
                tool_id = TOOLS[tool].tool_id
                for key, value in flat_values(values).items():
                    reading = value_text(tool_id, key, value)
                    details.append((parameter_name(tool_id, key, tr) or key) + (" · " + reading if reading else ""))
            for node in data.get("workflow", ()):
                details.append(stage_title(node["tool"], tr) + " · " + str(node["version"]))
            capability = data.get("image_observation")
            if capability:
                source = tr("assistant.image_rule_source.openai" if capability.get("rule_long_edge")
                            else "assistant.capability_source.manual")
                details.append(tr("assistant.observation.capability", edge=capability["effective_long_edge"],
                                  detail=tr("assistant.option." + capability["detail"]), source=source))
            for info in previous.get("sent_images", ()):
                width, height = info["size"]
                details.append(tr("assistant.observation.image", source=tr("assistant.observation.source." + info.get("source", "result")),
                    quality=tr("assistant.observation.quality." + info.get("quality", "preview")),
                    width=width, height=height, tokens=info["estimated_tokens"]))
            if previous.get("image_budget"):
                usage = previous["image_budget"]
                details.append(tr("assistant.observation.budget", count=usage["count"], max_count=usage["max_count"],
                                  size=f'{usage["bytes"] / (1024 * 1024):.1f}',
                                  max_size=usage["max_bytes"] // (1024 * 1024)))
            if data.get("requirements"):
                details.append(data["requirements"])
            self.manifest_text.setText("\n".join(details) + "\n" + tr("assistant.estimated_usage", tokens=previous["estimated_tokens"]))
