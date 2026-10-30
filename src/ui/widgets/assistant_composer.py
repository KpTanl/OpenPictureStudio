from __future__ import annotations

from urllib.parse import urlsplit

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy,
                            QStyle, QStyleOptionButton, QStylePainter, QVBoxLayout)

from resources.styles.theme_variables import (ASSISTANT_INPUT_HEIGHT, ASSISTANT_INPUT_MAX_HEIGHT,
                                              ASSISTANT_USAGE_DIAMETER, ASSISTANT_USAGE_STROKE,
                                              CONTROL_HEIGHT_COMPACT, SPACING_SM, SPACING_XS, px)
from ...application.assistant_models import IMAGE_SOURCES
from ..icons import themed_icon
from ..theme import get_theme_tokens, to_qcolor
from .apple_button import AppleGhostButton, AppleIconButton
from .apple_menu import AppleDropDown, AppleMenu
from .assistant_commands import AssistantCommandMenu
from .assistant_reference import AssistantReference
from .chip import Chip
from .tooltip import set_tooltip


class AssistantTextEdit(QPlainTextEdit):
    textEdited = Signal(str)
    submitted = Signal()
    focus_changed = Signal(bool)
    file_dropped = Signal(str)

    def __init__(self, parent=None, *, submit=False):
        super().__init__(parent)
        self._projecting = False
        self._composing = False
        self._placeholder = ""
        self._submit = submit
        self.textChanged.connect(self._edited)
        if submit:
            self._resize_timer = QTimer(self)
            self._resize_timer.setSingleShot(True)
            self._resize_timer.timeout.connect(self._resize_to_text)
            self.document().documentLayout().documentSizeChanged.connect(self._schedule_resize)
            self._schedule_resize()

    def setPlaceholderText(self, text):
        self._placeholder = text
        super().setPlaceholderText("" if self._composing else text)

    def _schedule_resize(self, *_args):
        self._resize_timer.start(0)

    def setPlainText(self, text):
        if text == self.toPlainText():
            return
        self._projecting = True
        try:
            super().setPlainText(text)
        finally:
            self._projecting = False

    def _edited(self):
        if self._submit:
            self._schedule_resize()
        if not self._projecting:
            self.textEdited.emit(self.toPlainText())

    def _resize_to_text(self, *_args):
        lines = max(1, self.document().lineCount())
        line_height = self.fontMetrics().lineSpacing()
        height = max(ASSISTANT_INPUT_HEIGHT, min(ASSISTANT_INPUT_MAX_HEIGHT,
            int(lines * line_height + self.document().documentMargin() * 2 + self.frameWidth() * 2)))
        if self.height() != height:
            self.setFixedHeight(height)
        self.viewport().update()
        QGuiApplication.inputMethod().update(Qt.InputMethodQuery.ImCursorRectangle)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._submit and event.size().width() != event.oldSize().width():
            self._schedule_resize()

    def keyPressEvent(self, event):
        if self._submit and not self._composing and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers():
            self.submitted.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

    def inputMethodEvent(self, event):
        self._composing = bool(event.preeditString())
        super().setPlaceholderText("" if self._composing else self._placeholder)
        super().inputMethodEvent(event)
        if self._submit:
            self._schedule_resize()
        self.viewport().update()

    @staticmethod
    def _file(event):
        urls = event.mimeData().urls()
        return urls[0].toLocalFile() if len(urls) == 1 and urls[0].isLocalFile() else ""

    def dragEnterEvent(self, event):
        if self._submit and event.mimeData().hasUrls():
            event.acceptProposedAction() if self._file(event) else event.ignore()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._submit and event.mimeData().hasUrls():
            event.acceptProposedAction() if self._file(event) else event.ignore()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if self._submit and event.mimeData().hasUrls():
            path = self._file(event)
            if path:
                self.file_dropped.emit(path)
                event.acceptProposedAction()
            else:
                event.ignore()
        else:
            super().dropEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.focus_changed.emit(True)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.focus_changed.emit(False)


class AssistantMenuButton(AppleGhostButton):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setFixedHeight(CONTROL_HEIGHT_COMPACT)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def set_label(self, text):
        self._full_text = text
        self.setText(text.replace("&", "&&"))
        self.setAccessibleName(text)
        set_tooltip(self, text)

    def _display_text(self, width):
        return self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideRight, width)

    def minimumSizeHint(self):
        return QSize(CONTROL_HEIGHT_COMPACT, CONTROL_HEIGHT_COMPACT)

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        width = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self).width()
        option.text = self._display_text(max(0, width)).replace("&", "&&")
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


class AssistantModelPicker(AssistantMenuButton):
    model_selected = Signal(str)
    effort_selected = Signal(str)
    settings_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("assistantModelPicker")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._state, self._tr = {}, None
        self._model_label, self._effort_label = "", ""
        self.clicked.connect(self._show_menu)

    def set_state(self, state, translate):
        self._state, self._tr = state, translate
        preferences = (state.get("conversation") or {}).get("preferences", {})
        model = next((model for model in state.get("models", ()) if model["id"] == preferences.get("model_id")), None)
        if model:
            effort = preferences.get("effort", "")
            label = model["name"] or model["model"] or translate("assistant.select_model")
            self._model_label = label
            self._effort_label = ""
            if model["reasoning"] != "none":
                self._effort_label = translate("assistant.thinking_label", effort=self.effort_text(effort, translate))
                label += " · " + self._effort_label
        else:
            label = translate("assistant.select_model")
            self._model_label, self._effort_label = label, ""
        self.set_label(label)
        connection = next((item for item in state.get("connections", ())
                           if model and item["id"] == model["connection_id"]), {})
        host = urlsplit(connection.get("endpoint", "")).hostname or translate("assistant.service_unset")
        capability = "" if model and model.get("tool_calling") else translate("assistant.agent_tools_disabled")
        set_tooltip(self, "\n".join(item for item in (label, host, capability) if item))
        self.setAccessibleDescription(capability)
        self.updateGeometry()

    def _display_text(self, width):
        if not getattr(self, "_effort_label", ""):
            return super()._display_text(width)
        suffix = " · " + self._effort_label
        name_width = width - self.fontMetrics().horizontalAdvance(suffix)
        name = self.fontMetrics().elidedText(self._model_label, Qt.TextElideMode.ElideRight, max(0, name_width))
        return name + suffix if name else self.fontMetrics().elidedText(
            self._effort_label, Qt.TextElideMode.ElideRight, width)

    @staticmethod
    def effort_text(effort, translate):
        return translate("assistant.effort." + (effort or "default"), effort or translate("assistant.effort.default"))

    def _show_menu(self, _checked=False, *, effort_only=False):
        if self._tr is None:
            return
        tr, state = self._tr, self._state
        preferences = (state.get("conversation") or {}).get("preferences", {})
        menu = AppleMenu(self)
        for connection in (() if effort_only else state.get("connections", ())):
            models = [model for model in state.get("models", ()) if model["connection_id"] == connection["id"]]
            if not models:
                continue
            host = urlsplit(connection.get("endpoint", "")).hostname
            menu.addSection(connection["name"] + (" · " + host if host else ""))
            for model in models:
                action = menu.addAction(model["name"] or model["model"] or tr("assistant.unnamed_model"))
                action.setCheckable(True)
                action.setChecked(model["id"] == preferences.get("model_id"))
                action.setEnabled(bool(model["model"]))
                action.triggered.connect(lambda _checked=False, identity=model["id"]: self.model_selected.emit(identity))
        model = next((item for item in state.get("models", ()) if item["id"] == preferences.get("model_id")), None)
        if model and model["reasoning"] != "none":
            menu.addSeparator()
            menu.addSection(tr("assistant.thinking"))
            for effort in ("", *model["efforts"]):
                action = menu.addAction(self.effort_text(effort, tr))
                action.setCheckable(True)
                action.setChecked(preferences.get("effort", "") == effort)
                action.triggered.connect(lambda _checked=False, value=effort: self.effort_selected.emit(value))
        menu.addSeparator()
        if model and not model.get("tool_calling"):
            note = menu.addAction(tr("assistant.agent_text_only"))
            note.setEnabled(False)
        menu.addAction(tr("assistant.manage_connections"), self.settings_requested.emit)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.mapToGlobal(QPoint(0, self.height())))


class AssistantContextUsage(AppleIconButton):
    def __init__(self, parent=None):
        super().__init__(size=CONTROL_HEIGHT_COMPACT, parent=parent)
        self._ratio = None

    def set_usage(self, estimate):
        self._ratio = (min(1.0, max(0.0, estimate["estimated_tokens"] / estimate["window"]))
                       if estimate and estimate.get("window") else None)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        tokens = get_theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        diameter = ASSISTANT_USAGE_DIAMETER
        ring = QRectF((self.width() - diameter) / 2, (self.height() - diameter) / 2, diameter, diameter)
        painter.setPen(QPen(to_qcolor(tokens["track" if self.isEnabled() else "control_disabled"]),
                            ASSISTANT_USAGE_STROKE))
        painter.drawEllipse(ring)
        if self._ratio:
            painter.setPen(QPen(to_qcolor(tokens["accent" if self.isEnabled() else "text_disabled"]),
                                ASSISTANT_USAGE_STROKE))
            painter.drawArc(ring, 90 * 16, -round(self._ratio * 360 * 16))
        painter.end()


class AssistantComposer(QFrame):
    """投影会话偏好、输入和本地命令，状态由会话服务保存。"""
    submitted = Signal()
    stopped = Signal()
    context_requested = Signal()
    mode_selected = Signal(str)
    image_selected = Signal(str)      # "" 表示不发送照片
    analysis_toggled = Signal(bool)
    settings_requested = Signal()

    MODES = ("agent", "plan")
    reference_requested = Signal()
    reference_dropped = Signal(str)
    reference_removed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("aiComposer")
        self._mode, self._tr, self._busy, self._can_send = "agent", None, False, False
        self._image, self._image_supported = "", False
        self._photo_edge = 0
        self.scope_kind = "current"
        self.scope_revision = 0
        self._scope_conversation = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_SM), px(SPACING_SM), px(SPACING_SM), px(SPACING_SM))
        layout.setSpacing(px(SPACING_SM))
        self.mode = AppleDropDown(self, flat=True)
        self.mode.setObjectName("assistantMode")
        self.mode.setFixedHeight(CONTROL_HEIGHT_COMPACT)
        for mode in self.MODES:
            self.mode.addItem(mode, mode)
        self.mode.activated.connect(lambda index: self.mode_selected.emit(self.MODES[index]))
        chips = QHBoxLayout()
        chips.setSpacing(px(SPACING_XS))
        self.photo = Chip(parent=self)
        self.photo.clicked.connect(self._photo_menu)
        self.analysis = Chip(parent=self, checkable=True)
        self.analysis.clicked[bool].connect(lambda checked: self.analysis_toggled.emit(checked))
        self.scope = Chip(parent=self)
        self.scope.clicked.connect(self._scope_menu)
        self.context = AssistantContextUsage(self)
        self.context.clicked.connect(self.context_requested)
        scope_row = QHBoxLayout()
        scope_row.addWidget(self.scope)
        scope_row.addStretch()
        layout.addLayout(scope_row)
        for chip in (self.photo, self.analysis):
            chips.addWidget(chip)
        chips.addStretch()
        self.add_reference = AppleIconButton(themed_icon("plus"), CONTROL_HEIGHT_COMPACT, self)
        self.add_reference.clicked.connect(self.reference_requested)
        chips.addWidget(self.add_reference)
        layout.addLayout(chips)
        self.reference = AssistantReference(self, removable=True)
        self.reference.removed.connect(self.reference_removed)
        layout.addWidget(self.reference)
        self.reference_loading = QLabel(self)
        self.reference_loading.setObjectName("mutedText")
        self.reference_loading.setWordWrap(True)
        self.reference_loading.hide()
        layout.addWidget(self.reference_loading)
        self.input = AssistantTextEdit(self, submit=True)
        self.input.file_dropped.connect(self.reference_dropped)
        self.input.setObjectName("aiInput")
        self.input.submitted.connect(self.submitted)
        self.input.focus_changed.connect(self._focus)
        self.input.textChanged.connect(self._update_send)
        self.commands = AssistantCommandMenu(self.input)
        self.commands.selected.connect(self._command)
        layout.addWidget(self.input)
        controls = QHBoxLayout()
        controls.setSpacing(px(SPACING_XS))
        self.model = AssistantModelPicker(self)
        self.send = AppleIconButton(themed_icon("arrow-up", role="on_accent"), CONTROL_HEIGHT_COMPACT, self)
        self.send.setObjectName("assistantSend")
        self.send.clicked.connect(lambda: self.stopped.emit() if self._busy and not self.input.toPlainText().strip()
                                  else self.submitted.emit())
        controls.addWidget(self.mode)
        controls.addStretch()
        controls.addWidget(self.model)
        controls.addWidget(self.context)
        controls.addWidget(self.send)
        layout.addLayout(controls)

    def _focus(self, focused):
        self.setProperty("focused", focused)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()
        self.input.viewport().update()

    def set_state(self, state, translate, *, can_send=True, context_count=0, estimate=None, photo_edge=0):
        """photo_edge：所绑定照片源图的长边，没有照片时为 0。"""
        self._tr = translate
        self._photo_edge = photo_edge
        record = state.get("conversation") or {}
        if record.get("id") != self._scope_conversation:
            self.scope_kind = "current"
            self.scope_revision = 0
            self._scope_conversation = record.get("id")
        self.scope.setText(translate("batch.scope_kind." + self.scope_kind))
        set_tooltip(self.scope, translate("batch.scope_tip"))
        preferences = record.get("preferences", {})
        context = preferences.get("context", {})
        self._mode = "plan" if preferences.get("mode") == "plan" else "agent"
        self._busy = record.get("id") in state.get("busy", ())
        loading = record.get("id") in state.get("reference_loading", ())
        self._can_send = can_send and not loading
        model = next((item for item in state.get("models", ()) if item["id"] == preferences.get("model_id")), {})
        self._image_supported = bool(model.get("image_input"))
        self._image = context.get("image_source", "current") if context.get("image") else ""
        sharing = ("image_analysis" if self._image and self._image_supported else "analysis_only") if context.get("analysis", True) else (
            "image_only" if self._image and self._image_supported else "local_only")
        self.reference.set_reference(state.get("reference"), translate,
                                     status=translate("assistant.reference.loading" if loading else "assistant.reference." + sharing))
        tip = translate("assistant.reference.loading" if loading else "assistant.reference.add")
        self.reference_loading.setText(translate("assistant.reference.loading"))
        self.reference_loading.setVisible(loading and not state.get("reference"))
        set_tooltip(self.add_reference, tip)
        self.add_reference.setAccessibleName(tip)
        for index, mode in enumerate(self.MODES):
            self.mode.setItemText(index, translate("assistant.mode." + mode))
        self.mode.setCurrentIndex(self.MODES.index(self._mode))
        self.mode.setAccessibleName(translate("assistant.mode_picker"))
        self.photo.setVisible(True)
        self.photo.setIcon(themed_icon("photo"))
        self.photo.setText(translate("assistant.photo." + (self._image or "off")) if self._image_supported
                           else translate("assistant.photo.unsupported"))
        set_tooltip(self.photo, translate("assistant.photo.tip" if self._image_supported
                                          else "assistant.photo.unsupported_tip"))
        self.analysis.setVisible(True)
        self.analysis.setText(translate("assistant.analysis"))
        self.analysis.setChecked(context.get("analysis", True))
        set_tooltip(self.analysis, translate("assistant.analysis_tip"))
        tip = translate("assistant.context_count", count=context_count)
        budget = (translate("assistant.budget", tokens=estimate["estimated_tokens"],
                            window=estimate["window"] or translate("assistant.window_unset"),
                            reserved=estimate["reserved_tokens"], omitted=estimate["omitted_messages"])
                  if estimate else translate("assistant.estimate_unavailable"))
        set_tooltip(self.context, tip + "\n" + budget)
        self.context.setAccessibleName(tip)
        self.context.setAccessibleDescription(budget)
        self.context.set_usage(estimate)
        self.input.setPlaceholderText(translate("assistant.placeholder." + self._mode))
        set_tooltip(self.input, translate("assistant.input_hint"))
        self._update_send()
        self.model.set_state(state, translate)
        running = any(item.get("run") and not item["run"].get("reason") for item in state.get("messages", ()))
        self.commands.set_state(record.get("id"), translate, effort=model.get("reasoning", "none") != "none", running=running)

    def _update_send(self):
        if self._tr is None or not hasattr(self, "send"):
            return
        stop = self._busy and not self.input.toPlainText().strip()
        text = self._tr("assistant.stop" if stop else "assistant.agent_steer" if self._busy else "assistant.send")
        set_tooltip(self.send, text)
        self.send.setAccessibleName(text)
        self.send.setIcon(themed_icon("player-stop" if stop else "arrow-up", role="on_accent"))
        self.send.setEnabled(stop or self._can_send)

    def _command(self, command):
        if command in self.MODES:
            self.mode_selected.emit(command)
        elif command in ("model", "effort"):
            self.model._show_menu(effort_only=command == "effort")
        elif command == "context":
            self.context_requested.emit()
        elif command == "stop":
            self.stopped.emit()

    def hideEvent(self, event):
        self.commands.hide()
        super().hideEvent(event)

    def _scope_menu(self):
        if self._tr is None:
            return
        menu = AppleMenu(self)
        for key in ("current", "selected", "filtered"):
            action = menu.addAction(self._tr("batch.scope_kind." + key))
            action.setCheckable(True)
            action.setChecked(key == self.scope_kind)
            action.triggered.connect(lambda checked=False, key=key: self._select_scope(key))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.scope.mapToGlobal(QPoint(0, self.scope.height())))

    def _select_scope(self, key):
        self.scope_kind = key
        self.scope_revision += 1
        self.scope.setText(self._tr("batch.scope_kind." + key))

    def _photo_menu(self):
        if self._tr is None:
            return
        menu = AppleMenu(self)
        for source in ("", "current", "original"):
            action = menu.addAction(self._photo_label(source))
            action.setCheckable(True)
            action.setChecked(self._image == source)
            action.setEnabled(not source or self._image_supported)
            action.triggered.connect(lambda _checked=False, value=source: self.image_selected.emit(value))
        if not self._image_supported:
            menu.addSeparator()
            note = menu.addAction(self._tr("assistant.photo.unsupported_tip"))
            note.setEnabled(False)
            menu.addAction(self._tr("assistant.manage_connections"), self.settings_requested.emit)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.photo.mapToGlobal(QPoint(0, self.photo.height())))

    def _photo_label(self, source):
        """发出的预览只缩小不放大。当前画面会随裁切、放大改变尺寸，只写上限；原始画面按照片的实际长边写。"""
        if not source:
            return self._tr("assistant.photo_option.off")
        edge = IMAGE_SOURCES[source][0]
        if source == "original" and self._photo_edge:
            return self._tr("assistant.photo_option.original_size", edge=min(edge, self._photo_edge))
        return self._tr("assistant.photo_option." + source, edge=edge)
