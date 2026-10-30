from __future__ import annotations

import logging
from copy import deepcopy
from threading import Event

from PySide6.QtCore import QObject, QPoint, QRunnable, Qt, QTimer, Signal, Slot
from shiboken6 import Shiboken
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel,
                            QStackedWidget, QVBoxLayout, QWidget)

from resources.styles.theme_variables import (ASSISTANT_CONTEXT_WIDTH, ASSISTANT_NAVIGATION_WIDTH,
                                              ASSISTANT_READING_WIDTH, ASSISTANT_VARIANT_HEIGHT, CONTROL_HEIGHT_COMPACT,
                                              SPACING_MD, SPACING_SM, SPACING_XS, px)
from ...application.assistant_actions import AssistantActionService
from ...application.assistant_context import AssistantContextBuilder
from ...application.assistant_models import (AssistantContext as AssistantContext, AssistantHost,
                                             AssistantModel)
from ...core.constants import SUPPORTED_FORMATS
from ...application.assistant_variants import variant_groups, variant_summary
from ..dialogs.assistant_dialogs import AssistantExportDialog, AssistantNameDialog
from ..icons import themed_icon
from ..services.ui_thread_relay import UiThreadRelay
from ..edit_descriptions import parameter_name, value_text
from ..themed_mixin import ThemedI18nMixin
from ..widgets.apple_button import AppleButton, AppleGhostButton, AppleIconButton
from ..widgets.apple_menu import AppleMenu
from ..widgets.assistant_composer import AssistantComposer, AssistantMenuButton
from ..widgets.assistant_context_inspector import ContextInspector, disclosure_text
from ..widgets.assistant_conversation_list import ConversationList
from ..widgets.assistant_transcript import AssistantTranscript, assistant_error_text
from ..widgets.assistant_variants import AssistantVariants
from ..widgets.assistant_styles import StyleConfirmation, StyleManager
from ..widgets.chip import Chip
from ..widgets.message_dialog import DialogButton, ask_choice, confirm_destructive, show_info
from ..widgets.notice_bar import NoticeBar
from ..widgets.tooltip import set_tooltip
from ..window_layout import assistant_layout

__all__ = ["AIAssistantPanel", "AssistantContext", "AssistantHost"]

logger = logging.getLogger(__name__)


class _Signals(QObject):
    finished = Signal(int, object)


class AssistantWorker(QRunnable):
    def __init__(self, generation, operation, cancelled):
        super().__init__()
        self.generation, self.operation, self.cancelled = generation, operation, cancelled
        self.signals = _Signals()

    def run(self):
        try:
            result = self.operation(self.cancelled.is_set)
        except Exception as exc:
            if not isinstance(exc, ValueError):
                logger.exception("助手的调整写入或历史操作失败")
            result = {"error": str(exc) if isinstance(exc, ValueError) else "AI_REQUEST_FAILED"}
        self.signals.finished.emit(self.generation, result)


class AIAssistantPanel(QWidget, ThemedI18nMixin):
    """AI 助手。调整模式下 AI 给出的调整立即写成历史里的一步，与手动编辑落在同一组工具参数上。"""
    render_requested = Signal()
    committed = Signal(object)   # AI 写入了一步（带它渲染好的画面）或撤销、重做了 AI 的调整（None）
    commit_busy = Signal(bool)
    settings_requested = Signal()
    expanded_requested = Signal(bool)
    comparison_requested = Signal(object)

    def __init__(self, host: AssistantHost, core, agent, thread_pool, parent=None, *, runner):
        super().__init__(parent)
        self._host, self._core, self._thread_pool = host, core, thread_pool
        self._runner, self._state = runner, runner.state
        self._actions = AssistantActionService(core, agent)
        self._context_builder = AssistantContextBuilder()
        self._identity, self._binding = "", None
        self._history_identity = None
        self._requested_identity, self._requested_from = None, None
        self._expanded, self._context_open = False, False
        self._generation, self._cancelled = 0, Event()
        # 正在后台写入或撤销、重做 AI 调整的那一件事；同一时间只有一件
        self._action_job, self._committing = None, False
        self._summary_busy = set()
        self._comparison_generation = 0
        self._comparison_cancelled = Event()
        self._comparing_message = ""
        self._comparison_loading = False
        self._submitted_text, self._draft_pending = None, None
        self._init_themed_i18n()
        self.destroyed.connect(self._cleanup_themed_i18n)
        self.setObjectName("aiAssistantPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(350)
        self._draft_timer.timeout.connect(self._flush_draft)
        self._scroll_pending = None
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(600)
        self._scroll_timer.timeout.connect(self._flush_scroll)
        self._build()
        runner.changed.connect(self._on_state)
        runner.event_received.connect(self._on_event)
        self.retranslate_ui()
        self.refresh()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._panes = QHBoxLayout()
        self._panes.setSpacing(0)
        outer.addLayout(self._panes, 1)
        self.navigation = self._conversation_list()
        self.navigation.setFixedWidth(ASSISTANT_NAVIGATION_WIDTH)
        self._panes.addWidget(self.navigation)
        self.pages = QStackedWidget(self)
        self._panes.addWidget(self.pages, 1)
        self.chat_page = QWidget(self.pages)
        wrapper = QHBoxLayout(self.chat_page)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.setSpacing(0)
        wrapper.addStretch()
        self.chat = QWidget(self.chat_page)
        self.chat.setMaximumWidth(ASSISTANT_READING_WIDTH)
        wrapper.addWidget(self.chat, 1)
        wrapper.addStretch()
        layout = QVBoxLayout(self.chat)
        self._chat_layout = layout
        layout.setContentsMargins(px(SPACING_MD), px(SPACING_XS), px(SPACING_MD), px(SPACING_XS))
        layout.setSpacing(px(SPACING_SM))
        header = QHBoxLayout()
        header.setSpacing(px(SPACING_XS))
        self.title = AssistantMenuButton(self.chat)
        self.title.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.title.clicked.connect(self._open_conversations)
        self.new_chat = AppleIconButton(themed_icon("plus"), CONTROL_HEIGHT_COMPACT, self.chat)
        self.new_chat.clicked.connect(lambda: self._new_chat(False))
        self.expand = AppleIconButton(themed_icon("arrows-maximize"), CONTROL_HEIGHT_COMPACT, self.chat)
        self.expand.clicked.connect(lambda: self.expanded_requested.emit(not self._expanded))
        header.addWidget(self.title, 1)
        header.addWidget(self.new_chat)
        header.addWidget(self.expand)
        layout.addLayout(header)
        self.photo = QLabel(self.chat)
        self.photo.setObjectName("mutedText")
        self.photo.setTextFormat(Qt.TextFormat.PlainText)
        self.photo.setWordWrap(True)
        layout.addWidget(self.photo)
        self.transcript = AssistantTranscript(self.chat)
        self.transcript.action_requested.connect(self._message_action)
        self.transcript.scroll_edited.connect(self._scroll_edited)
        layout.addWidget(self.transcript, 1)
        self.variants = AssistantVariants(self.chat)
        self.variants.images_requested.connect(lambda group_id, ticket: self._runner.command(
            "variant_images", identity=self._identity, group_id=group_id, ticket=ticket))
        self.variants.adopt_requested.connect(self._adopt_variant)
        self.variants.end_requested.connect(lambda: self._runner.command("stop", identity=self._identity))
        self.variants.expand_requested.connect(lambda: self.expanded_requested.emit(True))
        layout.addWidget(self.variants)
        self.notice = NoticeBar("warning", parent=self.chat)
        self.notice.action_triggered.connect(self.settings_requested)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.candidate_box = QFrame(self.chat)
        self.candidate_box.setObjectName("aiCandidate")
        actions = QHBoxLayout(self.candidate_box)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(px(SPACING_SM))
        self.candidate_status = QLabel(self.candidate_box)
        self.candidate_status.setObjectName("mutedText")
        self.candidate_status.setWordWrap(True)
        actions.addWidget(self.candidate_status, 1)
        self.undo_adjustment = AppleButton(parent=self.candidate_box)
        self.redo_adjustment = AppleButton(parent=self.candidate_box)
        self.regenerate = AppleGhostButton(parent=self.candidate_box)
        self.undo_adjustment.clicked.connect(lambda: self._move_adjustment(False))
        self.redo_adjustment.clicked.connect(lambda: self._move_adjustment(True))
        self.regenerate.clicked.connect(self._regenerate)
        for button in (self.undo_adjustment, self.redo_adjustment, self.regenerate):
            actions.addWidget(button)
        layout.addWidget(self.candidate_box)
        self.prompt_box = QWidget(self.chat)
        prompts = QHBoxLayout(self.prompt_box)
        prompts.setContentsMargins(0, 0, 0, 0)
        prompts.setSpacing(px(SPACING_SM))
        self.suggestions = []
        for key in ("exposure", "portrait", "film"):
            chip = Chip(parent=self.prompt_box)
            chip.clicked.connect(lambda _checked=False, value=key: self._use_suggestion(value))
            prompts.addWidget(chip, 1)
            self.suggestions.append((key, chip))
        layout.addWidget(self.prompt_box)
        self.composer = AssistantComposer(self.chat)
        self.input, self.send = self.composer.input, self.composer.send
        self.input.textEdited.connect(self._input_edited)
        self.composer.submitted.connect(self.request_suggestion)
        self.composer.stopped.connect(self.cancel_request)
        self.composer.context_requested.connect(self._open_context)
        self.composer.mode_selected.connect(lambda mode: self._preferences({"mode": mode}))
        self.composer.model.model_selected.connect(lambda identity: self._preferences({"model_id": identity}))
        self.composer.model.effort_selected.connect(lambda effort: self._preferences({"effort": effort}))
        self.composer.model.settings_requested.connect(self.settings_requested)
        self.composer.settings_requested.connect(self.settings_requested)
        self.composer.image_selected.connect(lambda source: self._preferences(
            {"context": {"image": bool(source), "image_source": source or "current"}}))
        self.composer.analysis_toggled.connect(lambda checked: self._preferences({"context": {"analysis": checked}}))
        self.composer.reference_requested.connect(self._choose_reference)
        self.composer.reference_dropped.connect(self._set_reference)
        self.composer.reference_removed.connect(lambda: self._set_reference(""))
        layout.addWidget(self.composer)
        self.editing_box = QWidget(self.chat)
        editing_layout = QHBoxLayout(self.editing_box)
        editing_layout.setContentsMargins(0, 0, 0, 0)
        self.editing_note = QLabel(self.editing_box)
        self.editing_note.setObjectName("mutedText")
        self.editing_note.setWordWrap(True)
        self.cancel_edit = AppleGhostButton(parent=self.editing_box)
        self.cancel_edit.clicked.connect(lambda: self._runner.command("cancel_edit", identity=self._identity))
        editing_layout.addWidget(self.editing_note, 1)
        editing_layout.addWidget(self.cancel_edit)
        layout.addWidget(self.editing_box)
        self.pages.addWidget(self.chat_page)
        self.conversations = self._conversation_list()
        self.pages.addWidget(self.conversations)
        self.context_page = self._context_inspector()
        self.pages.addWidget(self.context_page)
        self.style_confirmation = StyleConfirmation(self.pages)
        self.style_confirmation.confirmed.connect(self._confirm_style)
        self.style_confirmation.cancelled.connect(lambda: self._runner.command("style_cancel", identity=self._identity))
        self.pages.addWidget(self.style_confirmation)
        self.style_manager = StyleManager(self.pages)
        self.style_manager.back_requested.connect(lambda: self.pages.setCurrentWidget(self.chat_page))
        self.style_manager.command_requested.connect(self._style_command)
        self.style_manager.apply_requested.connect(self._select_style)
        self.style_manager.replay_requested.connect(self._replay_style)
        self.style_manager.preference_requested.connect(lambda text: self._runner.command("style_begin", identity=self._identity,
            fields={"kind": "preference", "text": text}))
        self.pages.addWidget(self.style_manager)
        self.context_side = self._context_inspector()
        self.context_side.setFixedWidth(ASSISTANT_CONTEXT_WIDTH)
        self._panes.addWidget(self.context_side)
        self.pages.setCurrentWidget(self.chat_page)
        self._layout_views()

    def _conversation_list(self):
        widget = ConversationList(self)
        widget.selected.connect(self._select_conversation)
        widget.new_requested.connect(self._new_chat)
        widget.manage_requested.connect(self._conversation_menu)
        widget.back_requested.connect(lambda: self.pages.setCurrentWidget(self.chat_page))
        return widget

    def _context_inspector(self):
        widget = ContextInspector(self)
        widget.edited.connect(lambda values: self._preferences({"context": values}))
        widget.summary_edited.connect(lambda text: self._runner.command("summary_edit", identity=self._identity, text=text))
        widget.summarize_requested.connect(self._summarize)
        widget.close_requested.connect(self._close_context)
        return widget

    def _preferences(self, fields):
        if self._identity:
            self._runner.command("preferences", identity=self._identity, fields=fields)

    def _input_edited(self, text):
        if self._identity:
            self._draft_pending = (self._identity, text)
            self._draft_timer.start()
        self.refresh()

    def _flush_draft(self):
        self._draft_timer.stop()
        if self._draft_pending:
            identity, text = self._draft_pending
            self._draft_pending = None
            self._runner.command("update", identity=identity, fields={"draft": text})
        self._flush_scroll()

    def _scroll_edited(self, ratio):
        if self._identity:
            self._scroll_pending = (self._identity, ratio)
            self._scroll_timer.start()

    def _flush_scroll(self):
        self._scroll_timer.stop()
        if self._scroll_pending:
            identity, ratio = self._scroll_pending
            self._scroll_pending = None
            self._runner.command("update", identity=identity, fields={"scroll": ratio})

    def _select_conversation(self, identity):
        if self._committing:
            return
        if identity != self._identity:
            self._requested_identity = identity
        self._flush_draft()
        self._runner.command("select", identity=identity)
        self.pages.setCurrentWidget(self.chat_page)
        self.refresh()

    def _new_chat(self, general=False):
        if self._committing:
            return
        self._flush_draft()
        self._requested_identity, self._requested_from = "new", self._identity
        self._runner.command("new",**({"object_key": "", "photo_name": ""} if general else {}))
        self.pages.setCurrentWidget(self.chat_page)
        self.input.setFocus()
        self.refresh()

    def _open_conversations(self):
        if self.navigation.isVisible():
            self._conversation_menu()
        else:
            self.pages.setCurrentWidget(self.conversations)
            self.conversations.search.setFocus()

    def _open_context(self):
        self._context_open = True
        self._layout_views()
        self.refresh()

    def _close_context(self):
        self._context_open = False
        self.context_side.hide()
        self.pages.setCurrentWidget(self.chat_page)

    def set_expanded(self, expanded):
        self._expanded = expanded
        self._layout_views()
        self.refresh()

    def _layout_views(self):
        navigation, context = assistant_layout(self.width(), self._expanded)
        self.navigation.setVisible(navigation)
        self.context_side.setVisible(context and self._context_open)
        if self._context_open:
            self.pages.setCurrentWidget(self.chat_page if context else self.context_page)
        elif self.pages.currentWidget() is self.context_page:
            self.pages.setCurrentWidget(self.chat_page)

    def _on_state(self, state):
        identity = state.get("current", "")
        if self._requested_identity:
            if self._requested_identity == "new" and identity == self._requested_from:
                return
            if self._requested_identity != "new" and identity != self._requested_identity:
                return
            self._requested_identity = None
        changed = identity != self._identity
        if changed:
            self.notice.hide()
        self._state, self._identity = state, identity
        record = state.get("conversation") or {}
        if changed or not self.input.hasFocus() or (self._submitted_text is not None and self.input.toPlainText() == self._submitted_text
                                                  and not record.get("draft")):
            self.input.setPlainText(record.get("draft", ""))
            self._submitted_text = None
        self.transcript.set_messages(identity, state.get("messages", ()), self._tr, scroll=record.get("scroll", 1.0))
        if changed:
            self._end_comparison()
        self.navigation.set_state(state, self._tr, show_back=False)
        self.conversations.set_state(state, self._tr)
        self.style_manager.set_state(state, self._tr)
        pending = state.get("style_pending")
        if pending:
            self._context_open = False
            self.context_side.hide()
            label_key = (pending["ticket"], self._language())
            if pending["kind"] == "style" and pending["status"] == "ready" and getattr(self, "_style_label_key", None) != label_key:
                self._style_label_key = label_key
                self._style_labels = self._host.assistant_revision_labels()
            for row in pending.get("history", ()):
                row["title"] = getattr(self, "_style_labels", {}).get(row["id"], "")
            self.style_confirmation.set_state(pending, state.get("styles", ()), self._tr)
            self.pages.setCurrentWidget(self.style_confirmation)
        elif self.pages.currentWidget() == self.style_confirmation:
            self.pages.setCurrentWidget(self.chat_page)
        self.refresh()

    def _on_event(self, kind, value):
        if kind == "style_saved" and value["conversation_id"] == self._identity:
            self.notice.set_text(self._tr("assistant.style.preference_saved" if value["kind"] == "preference" else "assistant.style.saved"))
            self.notice.show()
        elif kind == "fit_proposal":
            self._prepare_fit(value)
        elif kind == "variants_proposal":
            self._prepare_variants(value)
        elif kind == "variants_ready" and value["conversation_id"] == self._identity:
            self.expanded_requested.emit(True)
        elif kind == "variant_images" and value["conversation_id"] == self._identity:
            self.variants.set_images(value["group"], value["ticket"], value["images"])
        elif kind in ("variant_applied", "variant_apply_failed", "style_replayed", "style_replay_failed"):
            if kind in ("variant_applied", "style_replayed"):
                if value["conversation_id"] == self._identity and value["photo"].object_key == self._host.assistant_binding()[0]:
                    self.committed.emit(value["image"])
                elif value["image"] is not None:
                    value["image"].close()
            if value["conversation_id"] == self._identity:
                self._committing = False
                self.commit_busy.emit(False)
                if value.get("error"):
                    self._show_error(value["error"])
                self.refresh()
        elif kind == "proposal":
            if value["conversation_id"] == self._identity:
                self._apply_proposal(value)
            else:
                self._runner.command("reject_proposal", identity=value["conversation_id"],
                                     message_id=value["message_id"], reason="stopped")
        elif kind in ("applied", "apply_failed"):
            job = self._action_job or {}
            if value["conversation_id"] == job.get("conversation_id") and value["message_id"] == job.get("message_id"):
                self._action_job = None
                if kind == "applied":
                    # 这一步刚写入 Core，界面会话还没跟上，assistant_context() 此时取不到；只核对仍是这张照片，
                    # 同步由宿主收到 committed 后完成
                    if (value["conversation_id"] == self._identity
                            and value["photo"].object_key == self._host.assistant_binding()[0]):
                        self.committed.emit(value["image"])
                    elif value["image"] is not None:
                        value["image"].close()
                else:
                    self.render_requested.emit()
                self._committing = False
                self.commit_busy.emit(False)
                error = value.get("error")
                if (value["conversation_id"] == self._identity and error
                        and error.get("code") not in ("AI_CANCELLED", "AI_LOOP_STOPPED", "AI_AGENT_REDIRECT")):
                    self._show_error(error)
                self.refresh()
        elif kind == "delta" and value["conversation_id"] == self._identity:
            message = ((self._state.get("conversation") or {}).get("messages", {})).get(value["message_id"])
            if message and message.get("request_id") == value["request_id"]:
                message.update(text=value["text"], status=value["status"], reasoning_summary=value["summary"])
                self.transcript.update_message(message, self._tr)
        elif kind == "summary_state":
            if value["busy"]:
                self._summary_busy.add(value["conversation_id"])
            else:
                self._summary_busy.discard(value["conversation_id"])
            self.refresh()
        elif kind == "error" and value.get("operation") not in ("configuration", "diagnose"):
            if value.get("operation") in ("adopt_variant", "style_replay"):
                self._committing = False
                self.commit_busy.emit(False)
            if value.get("operation") == "apply_proposal" and self._committing:
                self._committing = False
                self._action_job = None
                self.commit_busy.emit(False)
            if value.get("operation") in ("new", "select"):
                self._requested_identity = None
                self.refresh()
            self._show_error(value)
        elif kind == "notice":
            self.notice.set_text(self._tr(value["key"]))
            self.notice.set_action_text("")
            self.notice.show()

    def refresh(self):
        if not hasattr(self, "composer"):
            return
        binding = self._host.assistant_binding()
        if binding != self._binding:
            self._binding = binding
            self._flush_draft()
            self.invalidate()
            self._runner.command("photo", object_key=binding[0], name=binding[1])
        record = self._state.get("conversation") or {}
        preferences = record.get("preferences", {})
        photo = self._host.assistant_context()
        if photo is not None and (photo.object_key, photo.revision_id) != self._history_identity:
            self._history_identity = (photo.object_key, photo.revision_id)
            timeline = self._core.history_timeline(photo.session_id)
            if timeline.success:
                self._runner.command("history_position", object_key=photo.object_key,
                                     revisions=[revision.revision_id for revision in timeline.data["revisions"]],
                                     current=timeline.data["current"])
        if photo is not None and not self._committing:
            self._runner.command("observe", photo=photo)
        if record.get("object_key") and photo and photo.object_key != record["object_key"]:
            photo = None
        self.title.set_label(record.get("title") or self._tr("assistant.new_chat"))
        selected_model = next((item for item in self._state.get("models", ())
                               if item["id"] == preferences.get("model_id")), {})
        self.photo.setText((record.get("photo_name") or self._tr("assistant.general_conversation")) + "\n"
                           + disclosure_text(preferences.get("context", {}), selected_model, self._tr))
        can_send = (self._state.get("ready", False) and bool(self.input.toPlainText().strip())
                    and (not self._committing or self._agent_active())
                    and self._identity not in self._state.get("reference_loading", ()))
        estimate = self._estimate(photo)
        self.composer.set_state(self._state, self._tr, can_send=can_send,
                                context_count=self._context_count(preferences, photo), estimate=estimate,
                                photo_edge=max(photo.width, photo.height) if photo else 0)
        self.composer.setEnabled(not record.get("archived", False) and self._requested_identity is None)
        self.prompt_box.setVisible(not self._state.get("messages") and not self.input.toPlainText().strip())
        self.editing_box.setVisible(bool(preferences.get("editing")))
        for inspector in (self.context_page, self.context_side):
            if not inspector.isHidden():
                inspector.set_state(self._state, self._tr, photo=photo, estimate=estimate,
                                    summary_busy=self._identity in self._summary_busy)
        self._project_candidate(photo)
        self.variants.set_state(self._state, self._tr, photo=photo, expanded=self._expanded)
        # 展开视图里在对比候选时，对话只留几行高度，其余给对比画面；侧栏里对比区只是一行摘要
        comparing = self._expanded and not self.variants.isHidden()
        self._chat_layout.setStretchFactor(self.variants, 1 if comparing else 0)
        self.transcript.setMaximumHeight(ASSISTANT_VARIANT_HEIGHT // 2 if comparing else 16777215)
        for widget in (self.navigation, self.conversations, self.new_chat, self.title):
            widget.setEnabled(not self._committing)
        self.new_chat.setEnabled(not self._committing and self._requested_identity is None and self._state.get("ready", False))
        set_tooltip(self.new_chat, self._tr("assistant.new_chat"))
        set_tooltip(self.expand, self._tr("assistant.collapse" if self._expanded else "assistant.expand"))
        self.new_chat.setAccessibleName(self._tr("assistant.new_chat"))
        self.expand.setAccessibleName(self._tr("assistant.collapse" if self._expanded else "assistant.expand"))
        self.expand.setIcon(themed_icon("layout-sidebar-right" if self._expanded else "arrows-maximize"))

    def _context_count(self, preferences, photo):
        context = preferences.get("context", {})
        record = self._state.get("conversation") or {}
        model = next((item for item in self._state.get("models", ()) if item["id"] == preferences.get("model_id")), {})
        return sum((bool(context.get("history") and self._state.get("messages")),
                    bool(photo and (context.get("color") or preferences.get("mode") != "plan")),
                    bool(photo and context.get("workflow")), bool(photo and context.get("image") and model.get("image_input")),
                    bool(photo and context.get("analysis", True)),
                    bool(self._state.get("reference")),
                    bool(context.get("requirements_enabled") and context.get("requirements")),
                    bool(context.get("summary_enabled") and record.get("summary"))))

    def _estimate(self, photo):
        record = self._state.get("conversation")
        if not record:
            return None
        model = next((item for item in self._state.get("models", ()) if item["id"] == record["preferences"]["model_id"]), None)
        if not model:
            return None
        preferences = deepcopy(record["preferences"])
        preferences["mode"] = "plan" if preferences.get("mode") == "plan" else "agent"
        reference = self._state.get("reference")
        preferences["context"]["image"] = bool(preferences["context"]["image"] and (photo or reference) and model.get("image_input"))
        messages = list(self._state.get("messages", ())) + [{"id": "draft", "role": "user", "text": self.input.toPlainText(), "status": "completed"}]
        try:
            return self._context_builder.build(messages, preferences, AssistantModel.from_dict(model), photo,
                                                language=self._language(), mode=preferences["mode"],
                                                summary=record.get("summary"),
                                                image_attached=bool(photo and preferences["context"]["image"]), reference=reference,
                                                variants=variant_summary(variant_groups(record)[-3:])
                                                if preferences["context"].get("history") else (),
                                                long_term_preferences=self._state.get("style_preferences", ()))
        except ValueError:
            return None

    def _language(self):
        return self.translation_service.get_language() if self.translation_service else "zh-CN"

    def _choose_reference(self):
        path, _filter = QFileDialog.getOpenFileName(self, self._tr("assistant.reference.add"), "",
                                                    self._tr("assistant.reference.filter") + " (" + " ".join(SUPPORTED_FORMATS) + ")")
        if path:
            self._set_reference(path)

    def _set_reference(self, path):
        if self._identity and self._requested_identity is None:
            self._runner.command("reference", identity=self._identity, path=path, photo=self._host.assistant_context())

    def _use_suggestion(self, key):
        self.input.setPlainText(self._tr("assistant.prompt." + key))
        self._input_edited(self.input.toPlainText())
        self.input.setFocus()

    def _agent_active(self):
        return any(item.get("run", {}).get("runtime") == "agent" and not item["run"].get("reason")
                   for item in self._state.get("messages", ()))

    def request_suggestion(self, retry="", *, continuation=False):
        if self._identity in self._state.get("reference_loading", ()):
            return
        if ((self._committing and (retry or not self._agent_active()))
                or not self._identity or self._requested_identity is not None):
            return
        if (self._state.get("conversation") or {}).get("archived"):
            self._show_error({"code": "AI_ARCHIVED"})
            return
        text = self.input.toPlainText().strip()
        if not text and not retry:
            return
        identity = self._identity
        if identity != self._identity or self._requested_identity is not None:
            return
        self._flush_draft()
        self.notice.hide()
        self._submitted_text = self.input.toPlainText() if not retry else None
        photo = self._host.assistant_context()
        self._end_comparison()
        self.transcript.follow_end()
        self._runner.command("send", identity=self._identity, text=text, photo=photo,
                             language=self._language(), retry=retry, stop_first=bool(retry),
                             scope_kind=self.composer.scope_kind, scope_revision=self.composer.scope_revision,
                             continuation=continuation)

    def cancel_request(self):
        """任务服务负责取消检查与提交边界，已提交的结果仍会同步到宿主。"""
        if self._identity:
            self._runner.command("stop", identity=self._identity)

    def invalidate(self, clear=False):
        """宿主报告照片变了（换图、撤销、保存、导出等）：循环调整不再继续。AI 自己写入时的刷新不算。"""
        if not self._committing:
            if self._identity:
                self._runner.command("invalidate", identity=self._identity)
        self._end_comparison()

    def _valid_context(self, context):
        current = self._host.assistant_context()
        return (current is not None and current.session_id == context.session_id
                and current.state_version == context.state_version and current.object_key == context.object_key
                and current.revision_id == context.revision_id
                and current.workflow.workflow_hash() == context.workflow.workflow_hash())

    def _prepare_fit(self, job):
        if job["conversation_id"] != self._identity or self._committing or not self._valid_context(job["photo"]):
            self._runner.command("reject_proposal", identity=job["conversation_id"], message_id=job["message_id"])
            return
        try:
            builder = self._host.assistant_candidate_builder(job["photo"])
        except ValueError:
            self._runner.command("reject_proposal", identity=job["conversation_id"], message_id=job["message_id"])
            return
        self._runner.command("apply_proposal", identity=job["conversation_id"], message_id=job["message_id"], workflow=builder)

    def _confirm_style(self, ticket, fields):
        pending = self._state.get("style_pending") or {}
        photo = self._host.assistant_context() if pending.get("kind") == "style" else None
        try:
            builder = self._host.assistant_candidate_builder(photo) if photo else None
        except ValueError:
            self._show_error({"code": "AI_PHOTO_CHANGED"})
            return
        self._runner.command("style_confirm", identity=self._identity, ticket=ticket, fields=fields, photo=photo, builder=builder)

    def _begin_style(self, message_id=""):
        if self._identity and not self.is_busy():
            self._runner.command("style_begin", identity=self._identity, message_id=message_id,
                                 photo=self._host.assistant_context(), fields={"kind": "style"})

    def _style_command(self, command, values):
        """风格管理页的编辑、删除与忘记长期偏好。命令与参数逐项写出，测试据此核对它们与服务的对应。"""
        if command == "style_edit":
            self._runner.command("style_edit", style_id=values["style_id"], version=values["version"],
                                 fields=values["fields"])
        elif command == "style_remove":
            self._runner.command("style_remove", style_id=values["style_id"], version=values["version"],
                                 sample_id=values["sample_id"])
        elif command == "preference_remove":
            self._runner.command("preference_remove", preference_id=values["preference_id"])

    def _select_style(self, style_id, sample_id, name):
        self._runner.command("style_select", identity=self._identity, style_id=style_id, sample_id=sample_id)
        self.input.setPlainText(self._tr("assistant.style.apply_request", name=name))
        self._input_edited(self.input.toPlainText())
        self.pages.setCurrentWidget(self.chat_page)
        self.input.setFocus()

    def _open_styles(self):
        self._context_open = False
        self.pages.setCurrentWidget(self.style_manager)
        self._layout_views()

    def _replay_style(self, style_id, sample_id):
        photo = self._host.assistant_context()
        if photo is None or self.is_busy():
            return
        self._host.stop_preview()
        self._committing = True
        self.commit_busy.emit(True)
        self._runner.command("style_replay", identity=self._identity, style_id=style_id, sample_id=sample_id, photo=photo)
        self.pages.setCurrentWidget(self.chat_page)
        self.refresh()

    def _prepare_variants(self, job):
        if job["conversation_id"] != self._identity or self._committing or not self._valid_context(job["photo"]):
            self._runner.command("reject_proposal", identity=job["conversation_id"], message_id=job["message_id"])
            return
        try:
            workflows = [self._host.assistant_candidate(job["base"], item) for item in job["adjustments"]]
        except ValueError:
            self._runner.command("reject_proposal", identity=job["conversation_id"], message_id=job["message_id"], reason="variant_failed")
            return
        self._runner.command("apply_proposal", identity=job["conversation_id"], message_id=job["message_id"], workflow=workflows)

    def _adopt_variant(self, group_id, label):
        photo = self._host.assistant_context()
        if self._committing or photo is None:
            return
        self._host.stop_preview()
        self._committing = True
        self.commit_busy.emit(True)
        self._runner.command("adopt_variant", identity=self._identity, group_id=group_id, label=label, photo=photo)
        self.refresh()

    def _apply_proposal(self, job):
        """从宿主取得候选工作流，交给任务服务校验、写入与调度。"""
        if self._committing or self._requested_identity is not None:
            self._runner.command("reject_proposal", identity=job["conversation_id"], message_id=job["message_id"], reason="stopped")
            return
        if (self._state.get("conversation") or {}).get("archived"):
            self._runner.command("reject_proposal", identity=job["conversation_id"], message_id=job["message_id"], reason="stopped")
            return
        if not self._valid_context(job["photo"]):
            self._runner.command("reject_proposal", identity=job["conversation_id"], message_id=job["message_id"])
            return
        try:
            workflow = self._host.assistant_candidate(job["photo"], job["adjustments"])
        except ValueError:
            self._runner.command("reject_proposal", identity=job["conversation_id"], message_id=job["message_id"])
            return
        self._end_comparison()
        self._host.stop_preview()
        self._action_job = job
        self._committing = True
        self.commit_busy.emit(True)
        self._runner.command("apply_proposal", identity=job["conversation_id"], message_id=job["message_id"], workflow=workflow)
        self.refresh()

    def _run_worker(self, worker, callback):
        relay = UiThreadRelay.shared()
        def deliver(generation, result):
            if Shiboken.isValid(self):
                callback(generation, result)
            elif isinstance(result, dict) and result.get("image") is not None:
                result["image"].close()

        worker.signals.finished.connect(lambda generation, result: relay.post(lambda: deliver(generation, result)),
                                        Qt.ConnectionType.DirectConnection)
        self._thread_pool.start(worker)

    def _start(self, operation):
        self._committing = True
        self.commit_busy.emit(True)
        self._cancelled = Event()
        worker = AssistantWorker(self._generation, operation, self._cancelled)
        self._run_worker(worker, self._finished)
        self.refresh()

    @Slot(int, object)
    def _finished(self, generation, result):
        if generation != self._generation:
            return
        job, self._action_job = self._action_job, None
        self._committing = False
        if result.get("outcome"):
            self._runner.command("action", identity=job["conversation_id"], message_id=job["message_id"],
                                 fields={"outcome": result["outcome"]})
            self.committed.emit(None)
            if job.get("withdraw_message"):
                self._runner.command("withdraw", identity=job["conversation_id"], message_id=job["withdraw_message"])
        self.commit_busy.emit(False)
        if result.get("error"):
            self._show_error({"code": result["error"]})
        self.refresh()

    def _latest_action(self):
        """最近一次 AI 调整；放弃的（含旧版里被下一轮替代的候选）不算。"""
        return next((message for message in reversed(self._state.get("messages", ()))
                     if message.get("action") and message["action"]["outcome"] not in ("discarded", "replaced")), None)

    def _project_candidate(self, photo):
        states = {}
        for item in self._state.get("messages", ()):
            run = item.get("run") or {}
            receipt = run.get("receipt") or {}
            if receipt and photo and receipt.get("photo", {}).get("object_key") == photo.object_key:
                try:
                    states[run["latest_message"]] = self._actions.history_status(photo.session_id, receipt)["state"]
                except ValueError:
                    pass
        self.transcript.set_action_states(states, busy=self.is_busy(), comparing=self._comparing_message,
                                          loading=self._comparison_loading)
        message = self._latest_action()
        action = message["action"] if message else {}
        outcome = action.get("outcome", "")
        self.candidate_box.setVisible(bool(action) and not message.get("run_id"))
        history = "detached"
        if action.get("after_revision") and photo and action["photo"]["object_key"] == photo.object_key:
            try:
                history = self._actions.history_status(photo.session_id, action)["state"]
            except ValueError:
                pass
        self.undo_adjustment.setVisible(bool(action.get("after_revision")) and history in ("applied", "later"))
        self.redo_adjustment.setVisible(history == "undone")
        self.undo_adjustment.setEnabled(not self._committing)
        self.redo_adjustment.setEnabled(not self._committing)
        # ready、previewing 是旧版留下的、没有应用的候选
        self.regenerate.setVisible(outcome in ("stale", "pending", "failed", "ready", "previewing") and not self._committing)
        self.regenerate.setEnabled(photo is not None)
        status = "applying" if self._committing else "undone" if history == "undone" else ""
        text = self._tr("assistant.outcome." + status) if status else ""
        self.candidate_status.setText(text)
        self.candidate_status.setVisible(bool(text))

    def _end_comparison(self, *, notify=True):
        self._comparison_cancelled.set()
        self._comparison_generation += 1
        if self._comparing_message:
            self._comparing_message = ""
            self._comparison_loading = False
            if notify:
                self.comparison_requested.emit(None)

    def reset_comparison(self):
        if self._comparing_message:
            self._end_comparison(notify=False)
            self.refresh()

    def _toggle_comparison(self, message):
        if self._comparing_message == message["id"]:
            self._end_comparison()
            self.refresh()
            return
        context = self._host.assistant_context()
        if context is None or self.is_busy():
            return
        self._end_comparison()
        self._comparing_message = message["id"]
        self._comparison_loading = True
        self._comparison_cancelled = Event()
        generation = self._comparison_generation
        worker = AssistantWorker(generation, lambda cancelled: self._actions.before_image(
            context, message["action"], is_cancelled=cancelled), self._comparison_cancelled)
        self._run_worker(worker, lambda ticket, result: self._comparison_ready(ticket, context, result))
        self.refresh()

    def _comparison_ready(self, generation, context, result):
        if generation != self._comparison_generation:
            if result.get("image") is not None:
                result["image"].close()
            return
        self._comparison_loading = False
        if result.get("image") is not None and self._valid_context(context):
            self.comparison_requested.emit(result["image"])
        else:
            self._end_comparison()
            if result.get("error"):
                self._show_error({"code": result["error"]})
        if result.get("image") is not None:
            result["image"].close()
        self.refresh()

    def _move_adjustment(self, redo, *, message=None, withdraw_message=""):
        message, photo = message or self._latest_action(), self._host.assistant_context()
        if not message or photo is None or self._committing or message["action"]["photo"]["object_key"] != photo.object_key:
            return
        self._end_comparison()
        self._runner.command("invalidate", identity=self._identity)
        action = message["action"]
        try:
            status = self._actions.history_status(photo.session_id, action)
        except ValueError as exc:
            self._show_error({"code": str(exc)})
            return
        confirmed = False
        if status["state"] == "later":
            choice = ask_choice(self, self._tr("assistant.revert_title"), self._tr("assistant.revert_note"),
                               (DialogButton("cancel", self._tr("common.cancel")),
                                DialogButton("revert", self._tr("assistant.revert_to_before"), "primary")),
                               detail=self._host.assistant_history_impact(status["affected"]), default="cancel", escape="cancel")
            if choice != "revert":
                return
            confirmed = True
        self._action_job = {"conversation_id": self._identity, "message_id": message["id"], "withdraw_message": withdraw_message}
        self._start(lambda _cancelled: self._actions.move(photo, action, redo=redo, confirmed=confirmed,
                                                        expected_version=status["version"]))

    def _regenerate(self):
        message = self._latest_action()
        if message:
            self.request_suggestion(retry=message["id"])

    def _message_action(self, message_id, operation):
        if operation == "stop":
            self.cancel_request()
            return
        record = self._state.get("conversation") or {}
        message = record.get("messages", {}).get(message_id)
        if not message or self._committing:
            return
        if operation == "compare":
            self._toggle_comparison(message)
        elif operation in ("batch_undo", "batch_redo"):
            run = message.get("run") or next((item.get("run", {}) for item in record.get("messages", {}).values()
                if item.get("run_id") == message.get("run_id") and item.get("run")), {})
            receipts = run.get("batch_receipts", ())
            if receipts:
                self._runner.batch_history(self._identity, message_id, receipts[-1]["batch"], operation == "batch_redo")
        elif operation == "save_style":
            self._begin_style(message_id)
        elif operation in ("undo_adjustment", "redo_adjustment"):
            self._move_adjustment(operation == "redo_adjustment", message=message)
        elif operation == "copy":
            QApplication.clipboard().setText(message["text"])
        elif operation == "retry":
            self.request_suggestion(retry=message_id)
        elif operation == "continue":
            self.input.setPlainText(self._tr("assistant.agent_continue_request"))
            self._input_edited(self.input.toPlainText())
            self.request_suggestion(continuation=True)
        elif operation == "edit":
            self._flush_draft()
            self._runner.command("edit", identity=self._identity, message_id=message_id)
            self.input.setPlainText(message["text"])
            self.input.setFocus()
        elif operation == "settings":
            self.settings_requested.emit()
        elif operation == "apply_plan":
            self._preferences({"mode": "agent"})
            self.input.setPlainText(self._tr("assistant.plan_request", plan=message["text"]))
            self._input_edited(self.input.toPlainText())
            self.request_suggestion()
        elif operation == "more":
            self._message_menu(record, message)

    def _message_menu(self, record, message):
        menu, message_id = AppleMenu(self), message["id"]
        conversation_id = record["id"]
        if message["role"] == "assistant" and message["status"] in ("queued", "preparing", "waiting", "streaming"):
            menu.addAction(self._tr("assistant.stop"), lambda: self._runner.command(
                "cancel_request", request_id=message["request_id"]))
        if message["role"] == "user":
            menu.addAction(self._tr("assistant.withdraw_message"), lambda: self._runner.command(
                "withdraw", identity=conversation_id, message_id=message_id))
            path = self._state.get("messages", ())
            start = next((index for index, item in enumerate(path) if item["id"] == message_id), len(path))
            applied = next((item for item in path[start:] if item.get("action") and item["action"].get("after_revision")
                            and item["action"]["outcome"] == "applied"), None)
            if applied:
                menu.addAction(self._tr("assistant.withdraw_and_revert"),
                               lambda: self._move_adjustment(False, message=applied, withdraw_message=message_id))
            menu.addAction(self._tr("assistant.context_start_here"), lambda: self._preferences({"context_start": message_id}))
        menu.addAction(self._tr("assistant.branch_here"), lambda: self._runner.command(
            "branch", identity=conversation_id, message_id=message_id))
        detail = self.transcript.detail_text(message_id)
        if detail:
            menu.addAction(self._tr("assistant.reply_details"),
                           lambda: show_info(self, self._tr("assistant.reply_details"), detail))
        versions = sorted((item for item in record["messages"].values() if item["parent"] == message["parent"]
                           and item["role"] == message["role"]), key=lambda item: item["created"])
        if len(versions) > 1:
            menu.addSeparator()
            menu.addSection(self._tr("assistant.message_versions"))
            for index, version in enumerate(versions):
                action = menu.addAction(self._tr("assistant.version_number", number=index + 1))
                action.setCheckable(True)
                action.setChecked(version["id"] == message_id)
                action.triggered.connect(lambda _checked=False, target=version["id"]: self._runner.command(
                    "branch", identity=conversation_id, message_id=target, tail=True))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.mapToGlobal(QPoint(self.width() // 2, self.height() // 2)))

    def _summarize(self):
        if self._identity:
            self._runner.command("summarize", identity=self._identity, language=self._language())

    def _conversation_menu(self):
        record = self._state.get("conversation")
        if not record or self._committing:
            return
        menu = AppleMenu(self)
        menu.addAction(self._tr("assistant.style.manage"), self._open_styles)
        menu.addAction(self._tr("assistant.style.save_current"), lambda: self._begin_style(
            (self._latest_action() or {}).get("id", "")))
        menu.addAction(self._tr("assistant.rename_conversation"), self._rename)
        menu.addAction(self._tr("assistant.unpin" if record["pinned"] else "assistant.pin"),
                       lambda: self._runner.command("update", identity=self._identity, fields={"pinned": not record["pinned"]}))
        menu.addAction(self._tr("assistant.restore_conversation" if record["archived"] else "assistant.archive_conversation"),
                       lambda: self._runner.command("update", identity=self._identity, fields={"archived": not record["archived"]}))
        menu.addAction(self._tr("assistant.export_conversation"), self._export)
        menu.addAction(self._tr("assistant.context_reset_start"), lambda: self._preferences({"context_start": ""}))
        parents = {message["parent"] for message in record["messages"].values()}
        leaves = [message for message in record["messages"].values() if message["id"] not in parents]
        if len(leaves) > 1:
            menu.addSeparator()
            menu.addSection(self._tr("assistant.saved_branches"))
            for index, leaf in enumerate(sorted(leaves, key=lambda item: item["created"])):
                action = menu.addAction(self._tr("assistant.branch_number", number=index + 1))
                action.setCheckable(True)
                action.setChecked(leaf["id"] == record["head"])
                action.triggered.connect(lambda _checked=False, identity=leaf["id"]: self._runner.command(
                    "branch", identity=self._identity, message_id=identity))
        menu.addSeparator()
        menu.addAction(self._tr("assistant.clear_attachments"), self._clear_attachments)
        menu.addAction(self._tr("assistant.delete_conversation"), self._delete_conversation)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.title.mapToGlobal(QPoint(0, self.title.height())))

    def _rename(self):
        record = self._state.get("conversation") or {}
        dialog = AssistantNameDialog(record.get("title", ""), self)
        if dialog.exec() and dialog.name.text().strip():
            self._runner.command("update", identity=self._identity, fields={"title": dialog.name.text().strip()})
        dialog.deleteLater()

    def _export(self):
        dialog = AssistantExportDialog(self)
        if dialog.exec():
            path, _filter = QFileDialog.getSaveFileName(self, self._tr("assistant.export_conversation"), "", "Markdown (*.md)")
            if path:
                changes, variant_texts, image_names = {}, {}, {}
                for message in self._state.get("messages", ()):
                    action = message.get("action")
                    if action:
                        rows = [self._tr("assistant.outcome." + action["outcome"])]
                        rows.extend((parameter_name(row.get("tool", "color_adjust"), row["key"], self._tr) or row["key"])
                                    + ": " + value_text(row.get("tool", "color_adjust"), row["key"], row["before"])
                                    + " → " + value_text(row.get("tool", "color_adjust"), row["key"], row["after"])
                                    for row in action.get("changes", ()))
                        changes[message["id"]] = "\n".join(rows)
                    descriptions, names = [], {}
                    for group in message.get("variants", ()):
                        title = self._tr("assistant.variant.group", number=group["number"])
                        descriptions.append(title + " · " + self._tr("assistant.variant.status." + group["status"]))
                        names[group["before_attachment"]] = self._tr("assistant.variant.image.before")
                        for label, item in group["variants"].items():
                            descriptions.append(label + " · " + item["description"])
                            names[item["attachment"]] = title + " " + label
                    variant_texts[message["id"]] = "\n".join(descriptions)
                    image_names[message["id"]] = names
                self._runner.command("export", identity=self._identity, path=path,
                                     include_changes=dialog.changes.isChecked(), include_images=dialog.images.isChecked(),
                                     labels=(self._tr("assistant.export_role.user"), self._tr("assistant.export_role.assistant")),
                                     change_texts=changes, variant_texts=variant_texts, image_names=image_names)
        dialog.deleteLater()

    def _delete_conversation(self):
        if confirm_destructive(self, self._tr("assistant.delete_conversation"), self._tr("assistant.delete_conversation_note"),
                               self._tr("assistant.delete_conversation")):
            self.cancel_request()
            self._runner.command("delete", identity=self._identity)

    def _clear_attachments(self):
        if confirm_destructive(self, self._tr("assistant.clear_attachments"), self._tr("assistant.clear_attachments_note"),
                               self._tr("assistant.clear_attachments")):
            self._runner.command("clear_attachments")

    def _show_error(self, value):
        self.notice.set_text(assistant_error_text(value, self._tr))
        self.notice.set_action_text(self._tr("assistant.manage_connections") if value.get("code") in (
            "AI_MODEL_MISSING", "AI_KEY_MISSING", "AI_CONNECTION_MISSING", "AI_HTTP_401", "AI_HTTP_403") else "")
        self.notice.show()

    def messages(self):
        return tuple((message["role"], message["text"]) for message in self._state.get("messages", ()))

    def candidate_cards(self):
        return self.transcript.change_cards()

    def candidate_outcomes(self):
        return tuple(message["action"]["outcome"] for message in self._state.get("messages", ()) if message.get("action"))

    def is_busy(self):
        return self._committing or self._identity in self._state.get("busy", ())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "pages"):
            self._layout_views()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "composer"):
            self._on_state(self._runner.state)
            if self.focusWidget() is None or not self.isAncestorOf(self.focusWidget()):
                self.input.setFocus()

    def retranslate_ui(self):
        if not hasattr(self, "composer"):
            return
        self.undo_adjustment.setText(self._tr("assistant.undo_adjustment"))
        self.redo_adjustment.setText(self._tr("assistant.redo_adjustment"))
        self.regenerate.setText(self._tr("assistant.retry"))
        self.editing_note.setText(self._tr("assistant.editing_message"))
        self.cancel_edit.setText(self._tr("common.cancel"))
        for key, chip in self.suggestions:
            chip.setText(self._tr("assistant.chip." + key))
            chip.setToolTip(self._tr("assistant.prompt." + key))
        self._on_state(self._state)

    def cleanup(self):
        self._flush_draft()
        self.cancel_request()
        self._end_comparison()
        # 后台的写入在提交之前停下；已经在提交的照常写完，结果不再回到这个面板
        self._cancelled.set()
        self._generation += 1
        self._cleanup_themed_i18n()
