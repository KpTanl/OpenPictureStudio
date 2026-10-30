from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QDesktopServices, QTextDocument
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                            QTextBrowser, QVBoxLayout, QWidget)

from resources.styles.theme_variables import CONTROL_HEIGHT_COMPACT, SPACING_LG, SPACING_MD, SPACING_SM, px
from ...application.assistant_tools import TOOLS, grouped
from ..edit_descriptions import parameter_name, value_text
from ..icons import themed_icon
from .apple_button import AppleCompactButton, AppleGhostButton, AppleIconButton
from .change_card import ChangeCard, ChangeRow
from .collapsible_card import CollapsibleCard
from .empty_state import EmptyState
from .status_badge import StatusBadge
from .assistant_reference import AssistantReference


def assistant_error_text(error: dict, tr) -> str:
    code = error.get("code", "AI_REQUEST_FAILED")
    if code.startswith("AI_HTTP_") and code not in ("AI_HTTP_401", "AI_HTTP_403", "AI_HTTP_429"):
        text = tr("assistant.error.AI_HTTP_FAILED") + " (" + str(error.get("status") or code.removeprefix("AI_HTTP_")) + ")"
    else:
        text = tr("assistant.error." + code, tr("assistant.error.generic"))
    if code == "AI_ATTACHMENT_LIMIT" and "bytes" in error and "max_bytes" in error:
        text += "\n" + tr("assistant.attachment_usage", used=f'{error["bytes"] / (1024 * 1024):.1f}',
                          limit=f'{error["max_bytes"] / (1024 * 1024):.0f}')
    if error.get("parameter"):
        text += " · " + tr("assistant.unsupported_parameter", parameter=error["parameter"])
    if error.get("request_id"):
        text += "\n" + tr("assistant.request_reference", id=error["request_id"])
    return text


def fit_result_rows(result, tr):
    rows = [tr("assistant.fit.status." + result["status"])]
    for step in result.get("steps", ()):
        dimension = tr("assistant.fit.dimension." + step["dimension"])
        outcome = tr("assistant.fit.reason." + step["code"])
        if "before_error" in step and step["dimension"] in step["before_error"] and step["dimension"] in step["after_error"]:
            rows.append(tr("assistant.fit.step", dimension=dimension, result=outcome,
                           before=f'{step["before_error"][step["dimension"]]:.2f}',
                           after=f'{step["after_error"][step["dimension"]]:.2f}'))
        else:
            rows.append(dimension + " · " + outcome)
    for item in result.get("skipped", ()):
        field = item["field"]
        label = (tr("assistant.fit.dimension." + field) if field in ("tone", "split_tone", "hue", "chroma")
                 else tr("assistant.fit." + field.removeprefix("split_tone.")) if field.startswith("split_tone.")
                 else tr("assistant.fit.strength") if field == "strength"
                 else parameter_name("beauty", field.removeprefix("beauty."), tr) if field.startswith("beauty.")
                 else parameter_name("color_adjust", field, tr))
        text = label + " · " + tr("assistant.fit.reason." + item["code"])
        if text not in rows:
            rows.append(text)
    return rows


def evaluation_result_rows(result, tr):
    rows = [tr("assistant.evaluation.result", backend=tr("assistant.evaluation.backend." + result["backend"]),
               model=result.get("model", "—"), version=result["criteria_version"])]
    rows.append(tr("assistant.evaluation.status." + result["status"]))
    for label, source in result.get("versions", {}).items():
        rows.append(tr("assistant.evaluation.source." + source["kind"], label=label,
                       revision=source.get("revision", ""), group=source.get("group", ""), variant=source.get("variant", "")))
    if result.get("reason"):
        rows.append(tr("assistant.evaluation." + result["reason"]))
    for name, item in result.get("dimensions", {}).items():
        dimension = (tr("assistant.evaluation.color", color=tr("color_adjust.mixer." + name.removeprefix("color:")))
                     if name.startswith("color:") else tr("assistant.evaluation.dimension." + name))
        rows.append(dimension + " · " + tr("assistant.evaluation.choice." + item["choice"]))
        if item.get("confidence") is not None:
            rows.append(tr("assistant.evaluation.confidence", value=f'{item["confidence"]:.0%}'))
        if "position_consistent" in item and item["position_consistent"] is not None:
            rows.append(tr("assistant.evaluation.consistent" if item["position_consistent"] else "assistant.evaluation.inconsistent"))
        if item.get("error"):
            rows.append(assistant_error_text(item["error"], tr))
    for request in result.get("requests", ()):
        if request.get("error"):
            rows.append(assistant_error_text(request["error"], tr))
        usage = request.get("usage", {})
        rows.append(tr("assistant.evaluation.usage", input=usage.get("input_tokens", usage.get("prompt_tokens", "—")),
                       output=usage.get("output_tokens", usage.get("completion_tokens", "—"))))
        if request.get("estimated_cost_usd") is not None:
            rows.append(tr("assistant.evaluation.cost", cost=f'{request["estimated_cost_usd"]:.6f}'))
    return rows


class _MessageBody(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("assistantMessageBody")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenLinks(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._content = None
        self.document().documentLayout().documentSizeChanged.connect(self._height)
        self.anchorClicked.connect(lambda url: QDesktopServices.openUrl(url) if url.scheme() in ("https", "http") else None)

    def loadResource(self, resource_type, url):
        return None

    def set_content(self, text, *, markdown=False):
        if (text, markdown) == self._content:
            return
        self._content = (text, markdown)
        if markdown:
            self.document().setMarkdown(text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub
                                        | QTextDocument.MarkdownFeature.MarkdownNoHTML)
        else:
            self.setPlainText(text)
        self.setVisible(bool(text))
        self._height()

    def _height(self, *_args):
        self.setFixedHeight(max(self.fontMetrics().height(), int(self.document().size().height()) + px(SPACING_SM)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.document().setTextWidth(self.viewport().width())
        self._height()


def change_rows(changes, tr) -> list:
    """按工具分段的改动行；只有一个工具时不加分段标题。"""
    tools = list(dict.fromkeys(change.get("tool", "color_adjust") for change in changes))
    rows = []
    for tool in tools:
        items = [change for change in changes if change.get("tool", "color_adjust") == tool]
        if len(tools) > 1:
            rows.append(ChangeRow(tr("assistant.tool." + tool, count=len(items)), section=True))
        rows.extend(ChangeRow(parameter_name(tool, change["key"], tr) or change["key"],
                              value_text(tool, change["key"], change["before"]),
                              value_text(tool, change["key"], change["after"])) for change in items)
    return rows


def changes_title(action, tr) -> str:
    """改动卡的标题：“调色 · 5 项 · 美颜 · 3 项”，候选还没算出改动时按建议的取值计数。"""
    changes = action.get("changes", ())
    counts = {}
    if changes:
        for change in changes:
            tool = change.get("tool", "color_adjust")
            counts[tool] = counts.get(tool, 0) + 1
    else:
        counts = {TOOLS[tool].tool_id: len(values) for tool, values in grouped(action.get("adjustments", {})).items()}
    if len(counts) == 1:
        return tr("assistant.adjustment_count", count=next(iter(counts.values())))
    return " · ".join(tr("assistant.tool." + tool, count=count) for tool, count in counts.items())


class AssistantMessage(QWidget):
    action_requested = Signal(str, str)

    def __init__(self, message, tr, parent=None, *, changes=True, progress=True):
        """changes、progress：放在任务卡里时，改动与进行中的状态由任务卡统一显示，消息自己不再显示。"""
        super().__init__(parent)
        self._id = message["id"]
        self._message = message
        self._hovered = False
        self._show_changes, self._show_progress = changes, progress
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        if message["role"] == "user" and not message.get("loop"):
            outer.addSpacing(px(SPACING_MD) * 2)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(px(SPACING_SM))
        bubble = QFrame(self)
        bubble.setObjectName("aiLoopMessage" if message["role"] == "user" and message.get("loop")
                             else "aiUserMessage" if message["role"] == "user" else "aiMessage")
        body_layout = QVBoxLayout(bubble)
        margin = px(SPACING_MD) if message["role"] == "user" and not message.get("loop") else 0
        body_layout.setContentsMargins(margin, margin, margin, margin)
        self.reference = AssistantReference(bubble)
        body_layout.addWidget(self.reference)
        self.body = _MessageBody(bubble)
        body_layout.addWidget(self.body)
        column.addWidget(bubble)
        self.status = QLabel(self)
        self.status.setObjectName("assistantMessageStatus")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        column.addWidget(self.status)
        self.changes = CollapsibleCard("", self, is_expanded=False)
        self.changes.setObjectName("assistantChanges")
        self.card = ChangeCard(self.changes)
        self.changes.add_widget(self.card)
        column.addWidget(self.changes)
        self.reasoning = CollapsibleCard("", self, is_expanded=False)
        self.reasoning_text = _MessageBody(self.reasoning)
        self.reasoning.add_widget(self.reasoning_text)
        column.addWidget(self.reasoning)
        # 回复详情（模型、思考档位、用量）不占对话的位置，经消息的“更多”菜单查看
        self.details = CollapsibleCard("", self, is_expanded=False)
        self.details.hide()
        self.details_text = QLabel(self.details)
        self.details_text.setObjectName("mutedText")
        self.details_text.setTextFormat(Qt.TextFormat.PlainText)
        self.details_text.setWordWrap(True)
        self.details.add_widget(self.details_text)
        column.addWidget(self.details)
        self.actions = QWidget(self)
        retain = self.actions.sizePolicy()
        retain.setRetainSizeWhenHidden(not message.get("loop") or message["role"] != "user")
        self.actions.setSizePolicy(retain)
        actions = QHBoxLayout(self.actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(px(SPACING_SM))
        self.primary_action = AppleGhostButton(parent=self.actions)
        self.primary_action.setFixedHeight(CONTROL_HEIGHT_COMPACT)
        self.primary_action.clicked.connect(lambda: self.action_requested.emit(
            self._id, "edit" if self._message["role"] == "user" else "copy"))
        self.retry = AppleGhostButton(parent=self.actions)
        self.retry.setFixedHeight(CONTROL_HEIGHT_COMPACT)
        self.retry.clicked.connect(lambda: self.action_requested.emit(self._id, "retry"))
        self.plan = AppleGhostButton(parent=self)
        self.plan.setFixedHeight(CONTROL_HEIGHT_COMPACT)
        self.plan.clicked.connect(lambda: self.action_requested.emit(self._id, "apply_plan"))
        self.settings = AppleGhostButton(parent=self)
        self.settings.setFixedHeight(CONTROL_HEIGHT_COMPACT)
        self.settings.clicked.connect(lambda: self.action_requested.emit(self._id, "settings"))
        self.more = AppleIconButton(themed_icon("dots"), CONTROL_HEIGHT_COMPACT, self.actions)
        self.more.clicked.connect(lambda: self.action_requested.emit(self._id, "more"))
        for button in (self.plan, self.settings):
            column.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        if message["role"] == "user":
            actions.addStretch()
        for button in (self.primary_action, self.retry):
            actions.addWidget(button)
        if message["role"] != "user":
            actions.addStretch()
        actions.addWidget(self.more)
        column.addWidget(self.actions)
        outer.addLayout(column, 1)
        self.set_message(message, tr)

    def detail_text(self) -> str:
        return self.details_text.text() if self._has_details else ""

    def enterEvent(self, event):
        super().enterEvent(event)
        self._hovered = True
        self._project_actions()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._hovered = False
        self._project_actions()

    def _project_actions(self):
        """消息操作在鼠标停在消息上、键盘焦点在操作上或回复失败时才出现，平时只留位置。"""
        focused = self.actions.isAncestorOf(self.focusWidget()) if self.focusWidget() else False
        loop = bool(self._message.get("loop")) and self._message["role"] == "user"
        shown = self._hovered or focused or self._message.get("status") == "failed"
        self.actions.setVisible(not loop and shown)


    def set_message(self, message, tr):
        self._message = message
        self.reference.set_reference(message.get("reference"), tr)
        user = message["role"] == "user"
        loop = message.get("loop") or {}
        if user and loop:
            # 循环调整的自查消息由程序发出，只显示一行说明，不显示发给模型的原文。
            self.body.set_content(tr("assistant.loop_review_sent" if message.get("attachments")
                                     else "assistant.loop_review_stats", round=loop["round"]))
        else:
            self.body.set_content(message["text"], markdown=not user)
        status = message["status"]
        error = message.get("error")
        progress = status in ("queued", "preparing", "waiting", "streaming")
        self.status.setText("" if progress and not self._show_progress
                            else assistant_error_text(error, tr) if error and status != "stopped"
                            else tr("assistant.status." + status) if status != "completed"
                            else tr("assistant.loop_end.satisfied") if loop.get("done") and not user
                            else tr("assistant.no_proposal") if message.get("notice") == "no_proposal"
                            else tr("assistant.no_changes") if message.get("mode") == "edit" and not message.get("action")
                            and not user else "")
        references = [tr("assistant.style.deleted") if item.get("deleted") else tr("assistant.style.saved_name", name=item["name"])
                      for item in message.get("saved_styles", ())]
        if references:
            self.status.setText("\n".join(([self.status.text()] if self.status.text() else []) + references))
        if message.get("notice") == "style_replayed":
            self.status.setText(tr("assistant.style.deleted") if message["style"].get("deleted") else
                                tr("assistant.style.replayed_name", name=message["style"]["name"]))
        self.status.setVisible(bool(self.status.text()))
        self.status.setProperty("kind", "error" if status == "failed" else "info")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        action = message.get("action") or {}
        changes = action.get("changes", ())
        self.changes.setVisible(self._show_changes and bool(action))
        self.changes.set_title(changes_title(action, tr))
        self.card.set_title("")
        kind = ("success" if action.get("outcome") == "applied" else "warning" if action.get("outcome") in ("stale", "failed")
                else "pending" if action.get("outcome") == "replaced"
                else "active" if action.get("outcome") in ("pending", "previewing", "ready", "applying") else "pending")
        self.card.set_status(kind,
                             tr("assistant.outcome." + action.get("outcome", "pending")))
        self.card.set_rows(change_rows(changes, tr))
        self.reasoning.set_title(tr("assistant.reasoning_summary"))
        self.reasoning.setVisible(bool(message.get("reasoning_summary")))
        self.reasoning_text.set_content(message.get("reasoning_summary", ""), markdown=True)
        self.details.set_title(tr("assistant.reply_details"))
        self._has_details = not user and bool(message.get("configuration"))
        self.details.hide()
        configuration = message.get("configuration", {})
        model = configuration.get("model", {})
        detail = (model.get("name") or model.get("model", "")) + " · " + configuration.get("connection", {}).get("name", "")
        effort = configuration.get("effort", "")
        if model.get("reasoning", "none") != "none":
            detail += "\n" + tr("assistant.thinking") + " · " + tr("assistant.effort." + (effort or "default"), effort)
        usage = message.get("usage") or {}
        if usage:
            incoming = usage.get("input_tokens", usage.get("prompt_tokens", "—"))
            outgoing = usage.get("output_tokens", usage.get("completion_tokens", "—"))
            reasoning = (usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}).get("reasoning_tokens", "—")
            detail += "\n" + tr("assistant.actual_usage", input=incoming, output=outgoing, reasoning=reasoning)
        context = message.get("context", {})
        if context:
            detail += "\n" + tr("assistant.estimated_usage", tokens=context["estimated_tokens"])
        self.details_text.setText(detail.strip(" ·"))
        self.primary_action.setText(tr("assistant.edit_message" if user else "common.copy"))
        self.retry.setText(tr("assistant.retry"))
        self.retry.setText(tr("assistant.ask_for_adjustments") if message.get("notice") == "no_proposal"
                           else tr("assistant.retry"))
        self.retry.setVisible(not user and not loop)
        self.retry.setEnabled(status not in ("queued", "preparing", "waiting", "streaming"))
        self.primary_action.setVisible(not (user and loop))
        self.plan.setText(tr("assistant.apply_plan"))
        self.plan.setVisible(not user and message.get("mode") == "plan" and status == "completed")
        self.settings.setText(tr("assistant.settings"))
        self.settings.setVisible(bool(error) and error["code"] in ("AI_KEY_MISSING", "AI_HTTP_401", "AI_HTTP_403", "AI_MODEL_MISSING"))
        self.more.setAccessibleName(tr("assistant.message_actions"))
        self._project_actions()


class AssistantRunCard(QWidget):
    action_requested = Signal(str, str)

    def __init__(self, messages, tr, parent=None):
        super().__init__(parent)
        self._messages = []
        self._round_widgets = {}
        self._run, self._tr = {}, tr
        self._history, self._busy, self._comparing, self._loading = "detached", False, False, False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        header = QHBoxLayout()
        self.title = QLabel(self)
        self.title.setObjectName("changeCardTitle")
        self.badge = StatusBadge(parent=self)
        header.addWidget(self.title, 1)
        header.addWidget(self.badge)
        layout.addLayout(header)
        self.reason = QLabel(self)
        self.reason.setObjectName("assistantMessageStatus")
        self.reason.setTextFormat(Qt.TextFormat.PlainText)
        self.reason.setWordWrap(True)
        layout.addWidget(self.reason)
        self._summary = QVBoxLayout()
        self._summary.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._summary)
        self.plan = CollapsibleCard("", self, is_expanded=True)
        self.plan_text = QLabel(self.plan)
        self.plan_text.setTextFormat(Qt.TextFormat.PlainText)
        self.plan_text.setWordWrap(True)
        self.plan.add_widget(self.plan_text)
        layout.addWidget(self.plan)
        self.changes = CollapsibleCard("", self, is_expanded=False)
        self.card = ChangeCard(self.changes)
        self.changes.add_widget(self.card)
        layout.addWidget(self.changes)
        self.rounds = CollapsibleCard("", self, is_expanded=False)
        self.tool_log = QLabel(self.rounds)
        self.tool_log.setTextFormat(Qt.TextFormat.PlainText)
        self.tool_log.setWordWrap(True)
        self.rounds.add_widget(self.tool_log)
        self._record_messages = QWidget(self.rounds)
        self._record_layout = QVBoxLayout(self._record_messages)
        self._record_layout.setContentsMargins(0, 0, 0, 0)
        self._record_layout.setSpacing(px(SPACING_SM))
        self.rounds.add_widget(self._record_messages)
        layout.addWidget(self.rounds)
        self.usage = QLabel(self)
        self.usage.setObjectName("mutedText")
        self.usage.setWordWrap(True)
        layout.addWidget(self.usage)
        self.compare = AppleCompactButton(parent=self)
        self.compare.setCheckable(True)
        self.compare.clicked.connect(lambda: self._request("compare"))
        layout.addWidget(self.compare)
        self.save_style = AppleCompactButton(parent=self)
        self.save_style.clicked.connect(lambda: self._request("save_style"))
        layout.addWidget(self.save_style)
        actions = QHBoxLayout()
        actions.setSpacing(px(SPACING_SM))
        self.undo = AppleCompactButton(parent=self)
        self.undo.clicked.connect(lambda: self._request("undo_adjustment"))
        self.redo = AppleCompactButton(parent=self)
        self.redo.clicked.connect(lambda: self._request("redo_adjustment"))
        self.stop = AppleCompactButton(parent=self)
        self.stop.clicked.connect(lambda: self._request("stop"))
        self.retry = AppleCompactButton(parent=self)
        self.retry.clicked.connect(lambda: self.action_requested.emit(self._messages[0]["id"],
            "continue" if self._run.get("reason") in ("max_calls", "time_limit", "interrupted") else "retry"))
        for button in (self.undo, self.redo, self.stop, self.retry):
            actions.addWidget(button)
        self.batch_undo = AppleCompactButton(parent=self)
        self.batch_redo = AppleCompactButton(parent=self)
        self.batch_undo.clicked.connect(lambda: self._request("batch_undo"))
        self.batch_redo.clicked.connect(lambda: self._request("batch_redo"))
        actions.addWidget(self.batch_undo)
        actions.addWidget(self.batch_redo)
        actions.addStretch()
        layout.addLayout(actions)
        self.set_messages(messages, tr)

    def _request(self, operation):
        target = self._run.get("latest_message") or self._messages[-1]["id"]
        self.action_requested.emit(target, operation)

    def set_messages(self, messages, tr):
        self._messages, self._tr = list(messages), tr
        self._run = next((message["run"] for message in messages if message.get("run")), {})
        agent = self._run.get("runtime") == "agent"
        self.title.setText(tr("assistant.agent_plan_title" if agent and messages[0].get("mode") == "plan"
                              else "assistant.agent_task_title" if agent else "assistant.task.title"))
        reason = self._run.get("reason", "")
        success = reason in ("completed", "satisfied")
        waiting = not reason and self._run.get("phase") == "waiting_user"
        state = ("completed" if success else "failed" if reason in ("failed", "invalid_response") else "stopped" if reason
                 else "waiting" if waiting else "active")
        self.badge.set_status("success" if success else "warning" if reason else "pending" if waiting else "active",
                              tr("assistant.task.state." + state))
        progress = (tr("assistant.agent_progress", count=len(self._run.get("tool_calls", ())), total=self._run.get("max_calls", 32))
                    if agent else tr("assistant.task.progress", round=self._run.get("round", 1), total=self._run.get("max_rounds", 1)))
        phase = tr("assistant.task.phase." + self._run.get("phase", "preparing"))
        self.reason.setText(tr("assistant.task.reason." + reason, rounds=self._run.get("max_rounds", 1),
                               seconds=self._run.get("max_seconds", 0), calls=self._run.get("max_calls", 32)) if reason
                            else progress + " · " + phase if self._run.get("loop") else phase)
        self.plan.set_title(tr("assistant.agent_plan"))
        self.plan.setVisible(bool(self._run.get("plan")))
        self.plan_text.setText("\n".join(tr("assistant.agent_plan_step", title=step["title"],
            status=tr("assistant.agent_status." + step["status"])) for step in self._run.get("plan", ())))
        receipts = self._run.get("batch_receipts", ())
        exports = self._run.get("export_receipts", ())
        batch_context = self._run.get("batch_context") or {}
        if receipts or self._run.get("batch_mode") and not exports:
            self.reason.setText(self.reason.text() + "\n" + tr("batch.scope",
                sample=batch_context.get("sample_name", ""), selected=batch_context.get("selected_count", 0),
                filtered=batch_context.get("filtered_count", 0)))
            if receipts:
                result = receipts[-1]
                self.reason.setText(self.reason.text() + "\n" + tr("batch.summary", **result["counts"]) +
                                    (" · " + tr("batch.skipped_count", count=result["counts"]["skipped"]) if result["counts"].get("skipped") else "") +
                                    " · " + tr("batch.save." + result["save_state"]))
            self.reason.setText(self.reason.text() + "\n" + tr("batch.recovery_notice"))
        receipt = receipts[-1] if receipts else {}
        self.batch_undo.setText(tr("batch.undo"))
        self.batch_redo.setText(tr("batch.redo"))
        self.batch_undo.setVisible(bool(receipts))
        self.batch_redo.setVisible(bool(receipts))
        self.batch_undo.setEnabled(bool(receipt.get("available")) and not receipt.get("active") and bool(reason))
        self.batch_redo.setEnabled(self.batch_undo.isEnabled())
        if exports:
            result = exports[-1]
            counts = result.get("counts", {})
            queue_state = ("expired" if not result.get("available") else "paused" if result.get("paused")
                           else "status.exporting" if counts.get("pending") or counts.get("exporting") else "notice")
            queue_label = (tr("export.cancelling") if result.get("cancelled") and counts.get("exporting")
                           else tr("export_queue." + queue_state))
            self.reason.setText(self.reason.text() + "\n" + tr("export_queue.summary", total=result.get("total", 0),
                done=counts.get("success", 0), failed=counts.get("failed", 0), cancelled=counts.get("cancelled", 0)) +
                " · " + queue_label)
            if result.get("record_failures"):
                self.reason.setText(self.reason.text() + "\n" + tr("export_queue.record.failed"))
        changes = self._run.get("changes", ())
        self.changes.set_title(tr("assistant.agent_changes" if agent else "assistant.task.changes",
                                  rounds=self._run.get("applied_rounds", 0), count=len(changes)))
        self.changes.setVisible(bool(self._run.get("applied_rounds")))
        self.card.set_title("")
        self.card.set_rows(change_rows(changes, tr))
        self.rounds.set_title(tr("assistant.agent_records" if agent else "assistant.task.rounds",
                                 count=len(self._run.get("tool_calls", ())) if agent
                                 else sum(message["role"] == "assistant" for message in messages)))
        calls = self._run.get("tool_calls", ())
        rows = []
        for call in calls:
            label = tr("assistant.agent_tool." + call["name"], call["name"])
            status = tr("assistant.agent_status." + call["status"])
            result = call.get("result") or {}
            error = result.get("error") or result.get("image_error")
            rows.append(label + " · " + status + (" · " + assistant_error_text(error, tr) if error else ""))
            if call["name"] == "fit_reference" and (call.get("result") or {}).get("ok"):
                rows.extend(fit_result_rows(call["result"], tr))
            if call["name"] == "view_photo" and (call.get("result") or {}).get("ok"):
                for info in call["result"].get("images", ()):
                    width, height = info["size"]
                    rows.append(tr("assistant.observation.image", source=tr("assistant.observation.source." + info.get("source", "result")),
                        quality=tr("assistant.observation.quality." + info.get("quality", "preview")),
                        width=width, height=height, tokens=info["estimated_tokens"]))
            if call["name"] == "evaluate_versions" and (call.get("result") or {}).get("ok"):
                rows.extend(evaluation_result_rows(call["result"], tr))
            if call["name"] == "apply_style" and (call.get("result") or {}).get("ok"):
                result = call["result"]
                rows.append(tr("assistant.style.deleted") if result["style"].get("deleted") else
                            tr("assistant.style.used", name=result["style"]["name"]))
                rows.append(tr("assistant.style.selection." + result["selection"]["code"]))
                rows.extend(fit_result_rows(result, tr))
            if call["name"] == "remember_style" and (call.get("result") or {}).get("ok"):
                result = call["result"]
                rows.append(tr("assistant.style.deleted") if (result.get("style") or {}).get("deleted") else
                            tr("assistant.style.saved" if result.get("status") == "saved" else
                               "assistant.style.cancelled" if result.get("status") == "cancelled" else "assistant.style.confirmation_required"))
        self.tool_log.setText("\n".join(rows))
        self.tool_log.setVisible(bool(calls))
        ids = {message["id"] for message in messages}
        for message_id in tuple(self._round_widgets):
            if message_id not in ids:
                widget = self._round_widgets.pop(message_id)
                widget.hide()
                widget.deleteLater()
        for message in messages:
            widget = self._round_widgets.get(message["id"])
            if widget is None:
                widget = AssistantMessage(message, tr, self, changes=not agent, progress=False)
                widget.action_requested.connect(self.action_requested)
                self._round_widgets[message["id"]] = widget
            else:
                widget.set_message(message, tr)
        self._place_messages()
        tokens = self._run.get("tokens", 0)
        self.usage.setText(tr("assistant.task.usage_estimated" if self._run.get("estimated_usage") else "assistant.task.usage",
                              tokens=tokens, seconds=self._run.get("elapsed", 0)))
        evaluations = self._run.get("evaluation_requests", ())
        if evaluations:
            counts = [item.get("usage", {}).get("input_tokens", item.get("usage", {}).get("prompt_tokens", 0)) for item in evaluations]
            incoming = sum(count for count in counts if type(count) is int and count >= 0)
            self.usage.setText(self.usage.text() + "\n" + tr("assistant.evaluation.total", count=len(evaluations), input=incoming))
        self.usage.setVisible(bool(reason and tokens))
        self.undo.setText(tr("assistant.task.undo"))
        self.redo.setText(tr("assistant.task.redo"))
        self.stop.setText(tr("assistant.stop"))
        self.retry.setText(tr("assistant.agent_continue" if reason in ("max_calls", "time_limit", "interrupted") else "assistant.retry"))
        self.stop.setVisible(not reason)
        self.retry.setVisible(reason in ("failed", "no_proposal", "invalid_response", "interrupted", "time_limit", "max_calls"))
        self.set_action_state(self._history, busy=self._busy, comparing=self._comparing, loading=self._loading)

    def update_message(self, message, tr):
        self._messages = [message if item["id"] == message["id"] else item for item in self._messages]
        widget = self._round_widgets.get(message["id"])
        if widget:
            widget.set_message(message, tr)
        self._place_messages()
        if message.get("status") == "streaming" and not self._run.get("reason"):
            phase = tr("assistant.task.phase.responding")
            if self._run.get("loop") and self._run.get("runtime") != "agent":
                phase = tr("assistant.task.progress", round=self._run.get("round", 1),
                           total=self._run.get("max_rounds", 1)) + " · " + phase
            self.reason.setText(phase)

    def _place_messages(self):
        """最新一条有文字的回复作为正文，其余消息按先后放进执行记录，同一条消息只出现一次。"""
        completed_only = self._run.get("reason") in ("failed", "invalid_response")
        summary = next((item["id"] for item in reversed(self._messages)
                        if item["role"] == "assistant" and item.get("text")
                        and (not completed_only or (item.get("action") or {}).get("after_revision"))), "")
        placed = ([self._round_widgets[summary]] if summary else [],
                  [self._round_widgets[item["id"]] for item in self._messages if item["id"] != summary])
        layouts = (self._summary, self._record_layout)
        if any([layout.itemAt(index).widget() for index in range(layout.count())] != widgets
               for layout, widgets in zip(layouts, placed)):
            # 先全部取出再按顺序放回：消息在正文与执行记录之间移动时，布局里不会同时留着同一个控件
            for layout in layouts:
                while layout.count():
                    layout.takeAt(0)
            for layout, widgets in zip(layouts, placed):
                for widget in widgets:
                    layout.addWidget(widget)
                    widget.show()
        self._record_messages.setVisible(bool(placed[1]))
        self.rounds.setVisible(bool(self._run.get("tool_calls")) or bool(placed[1]))

    def detail_text(self, message_id):
        widget = self._round_widgets.get(message_id)
        return widget.detail_text() if widget else ""

    def set_action_state(self, history, *, busy, comparing=False, loading=False):
        self._history, self._busy, self._comparing, self._loading = history, busy, comparing, loading
        finished = bool(self._run.get("reason"))
        self.undo.setVisible(finished and history in ("applied", "later", "partial"))
        self.redo.setVisible(finished and history in ("undone", "partial"))
        self.compare.setVisible(finished and history != "detached")
        self.compare.setChecked(comparing)
        self.compare.setText(self._tr("assistant.task.comparing_loading" if loading else
                                     "assistant.task.compare_current" if comparing else "assistant.task.compare_before"))
        for button in (self.undo, self.redo, self.compare, self.retry):
            button.setEnabled(not busy and not loading)
        self.save_style.setText(self._tr("assistant.style.save_current"))
        self.save_style.setVisible(finished and bool(self._run.get("applied_rounds")))
        self.save_style.setEnabled(not busy and not loading)
        if history in ("undone", "partial"):
            self.badge.set_status("pending", self._tr("assistant.task.state." + history))

    def action_message_id(self):
        return self._run.get("latest_message", "")


class AssistantTranscript(QScrollArea):
    action_requested = Signal(str, str)
    scroll_edited = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("aiTranscript")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._conversation = ""
        self._widgets = {}
        self._message_groups = {}
        self._following = True
        self._limit = 80
        self._restore_ratio = 1.0
        self._messages, self._tr = [], None
        content = QWidget(self)
        content.setObjectName("aiMessages")
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(0, px(SPACING_SM), 0, px(SPACING_SM))
        self._layout.setSpacing(px(SPACING_LG))
        self.older = AppleGhostButton(parent=content)
        self.older.clicked.connect(self._older)
        self._layout.addWidget(self.older)
        self.welcome = EmptyState("sparkles", content)
        self._layout.addWidget(self.welcome, 1)
        self._layout.addStretch()
        self.setWidget(content)
        self.new_reply = AppleCompactButton(parent=self.viewport())
        self.new_reply.clicked.connect(self.follow_end)
        self.new_reply.hide()
        self.verticalScrollBar().rangeChanged.connect(self._range_changed)
        self.verticalScrollBar().actionTriggered.connect(self._user_scroll)
        self.verticalScrollBar().sliderReleased.connect(self._save_scroll)

    def set_messages(self, identity, messages, tr, *, scroll=1.0):
        changed = identity != self._conversation
        if changed:
            self._limit = 80
            self._following = scroll >= 0.98
        self._conversation, self._messages, self._tr = identity, messages, tr
        groups = []
        self._message_groups = {}
        for message in messages:
            key = "run:" + message["run_id"] if message.get("run_id") else message["id"]
            if groups and groups[-1][0] == key:
                groups[-1][1].append(message)
            else:
                groups.append((key, [message]))
            self._message_groups[message["id"]] = key
        visible = groups[-self._limit:]
        identities = {key for key, _messages in visible}
        for key in tuple(self._widgets):
            if key not in identities:
                widget = self._widgets.pop(key)
                self._layout.removeWidget(widget)
                widget.deleteLater()
        for index, (key, entries) in enumerate(visible):
            widget = self._widgets.get(key)
            if widget is None:
                widget = (AssistantRunCard(entries, tr, self.widget()) if entries[0].get("run_id")
                          else AssistantMessage(entries[0], tr, self.widget()))
                widget.action_requested.connect(self.action_requested)
                self._widgets[key] = widget
            else:
                if isinstance(widget, AssistantRunCard):
                    widget.set_messages(entries, tr)
                else:
                    widget.set_message(entries[0], tr)
            self._layout.insertWidget(index + 2, widget)
        self.welcome.set_text(tr("assistant.welcome_title"), tr("assistant.welcome_modern"))
        self.welcome.setVisible(not messages)
        self.older.setText(tr("assistant.load_older"))
        self.older.setVisible(len(groups) > self._limit)
        self.new_reply.setText(tr("assistant.new_reply"))
        if changed:
            self._restore_ratio = scroll
            QTimer.singleShot(0, self, self._restore_scroll)

    def _restore_scroll(self):
        self.verticalScrollBar().setValue(int(self.verticalScrollBar().maximum() * self._restore_ratio))

    def update_message(self, message, tr):
        widget = self._widgets.get(self._message_groups.get(message["id"], message["id"]))
        if widget:
            if isinstance(widget, AssistantRunCard):
                widget.update_message(message, tr)
            else:
                widget.set_message(message, tr)

    def set_action_states(self, states, *, busy, comparing="", loading=False):
        for widget in self._widgets.values():
            if isinstance(widget, AssistantRunCard):
                message_id = widget.action_message_id()
                widget.set_action_state(states.get(message_id, "detached"), busy=busy,
                                        comparing=comparing == message_id, loading=loading and comparing == message_id)

    def detail_text(self, message_id):
        widget = self._widgets.get(self._message_groups.get(message_id, message_id))
        return (widget.detail_text(message_id) if isinstance(widget, AssistantRunCard)
                else widget.detail_text() if widget else "")

    def change_cards(self):
        return tuple(widget.card for widget in self._widgets.values()
                     if isinstance(widget, AssistantRunCard) or widget.card.rows())

    def _older(self):
        self._limit += 80
        self.set_messages(self._conversation, self._messages, self._tr)

    def _range_changed(self, _minimum, maximum):
        if self._following:
            self.verticalScrollBar().setValue(maximum)
        else:
            self.new_reply.show()
            self.new_reply.adjustSize()
            self.new_reply.move(max(0, (self.viewport().width() - self.new_reply.width()) // 2),
                                max(0, self.viewport().height() - self.new_reply.height() - px(SPACING_SM)))

    def _user_scroll(self, _action):
        self._following = False
        QTimer.singleShot(0, self, self._save_scroll)

    def _save_scroll(self):
        scroll = self.verticalScrollBar()
        self._following = scroll.maximum() - scroll.value() <= px(SPACING_LG)
        if self._following:
            self.new_reply.hide()
        self.scroll_edited.emit(scroll.value() / max(1, scroll.maximum()))

    def follow_end(self):
        self._following = True
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        self.new_reply.hide()
