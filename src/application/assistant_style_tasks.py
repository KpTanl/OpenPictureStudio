from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import time
from threading import Event

from .assistant_models import new_id
from .assistant_references import selected_reference
from ..core.contracts.workflow import WorkflowGraph
from ..core.tools.color import COLOR_UI_PARAMETERS
from ..core.tools.beauty import BEAUTY_PARAMETERS


class AssistantStyleTasks:
    def __init__(self, service):
        self.service, self.pending, self.tasks = service, {}, {}
        self.all_tasks = {}

    def _track(self, task, cancelled):
        self.all_tasks[task] = cancelled
        task.add_done_callback(lambda finished: self.all_tasks.pop(finished, None))

    def cancel(self, identity, *, finish=False):
        """撤下确认卡。finish 为真表示用户在卡上点了取消：等这张卡的任务随之结束；
        发消息、切会话等顺带撤卡时不结束，任务由后续流程接着处理。"""
        pending = self.pending.pop(identity, None)
        if pending:
            pending["outcome"] = "cancelled"
            self._resolved(identity, pending["ticket"], "cancelled")
            if finish:
                self._finish_waiting(identity, pending["ticket"], "stopped")
        running = self.tasks.get(identity)
        if running:
            running[1].set()

    def _finish_waiting(self, identity, ticket, reason):
        """确认卡有了结果，等它的任务没有别的事可做：结束这个暂停的任务，不再停在“等待回复”。"""
        for run in tuple(self.service.runs.values()):
            if (run.conversation_id == identity and run.active and run.paused
                    and any(call["name"] == "remember_style" and (call.get("result") or {}).get("ticket") == ticket
                            for call in run.calls)):
                self.service._finish_run(run, reason)

    def _resolved(self, identity, ticket, status, style=None):
        record = self.service.records.get(identity)
        if not record:
            return
        collections = [run.calls for run in self.service.runs.values() if run.conversation_id == identity]
        collections += [message[key].get("tool_calls", ()) for message in record["messages"].values()
                        for key in ("run", "run_step") if message.get(key)]
        for calls in collections:
            for call in calls:
                result = call.get("result") or {}
                if call["name"] == "remember_style" and result.get("ticket") == ticket:
                    result["status"] = status
                    if style:
                        result["style"] = copy.deepcopy(style)
        for run in self.service.runs.values():
            context = run.runtime_state.get("context")
            if run.conversation_id == identity and run.active and context:
                context["agent_items"].append({"role": "user", "content":
                    "Memory confirmation result (data): " + json.dumps({"ticket": ticket, "status": status, "style": style})})

    def view(self, identity):
        pending = self.pending.get(identity)
        return {key: copy.deepcopy(value) for key, value in pending.items()
                if key not in ("photo", "writes", "reference", "feedback", "user_text")} if pending else None

    async def begin(self, identity, message_id, photo, values):
        self.cancel(identity)
        record = self.service.records[identity]
        from .assistant_conversations import message_path
        message = record["messages"].get(message_id, {})
        run_id = message.get("run_id")
        messages = ([item for item in message_path(record) if item.get("run_id") == run_id]
                    if run_id else [message] if message else [])
        if run_id:
            first_reply = next((item for item in messages if item["role"] == "assistant"), None)
            initial = record["messages"].get(first_reply["parent"]) if first_reply else None
            if initial and initial["role"] == "user" and initial not in messages:
                messages.insert(0, initial)
        writes = [write for item in messages for write in item.get("writes", ())]
        reference = selected_reference(record, draft=False, head=message_id or record["head"])
        pending = {"ticket": new_id(), "identity": identity, "message_id": message_id,
                   "photo": photo if values.get("kind", "style") == "style" else None,
                   "kind": values.get("kind", "style"), "name": values.get("name", ""),
                   "scene": values.get("scene", ""), "text": values.get("text", ""),
                   "style_id": values.get("style_id", ""), "status": "loading", "history": [], "writes": writes,
                   "reference": copy.deepcopy(reference),
                   "user_text": "\n".join(item["text"] for item in messages if item["role"] == "user"),
                   "feedback": [{key: copy.deepcopy(group[key]) for key in ("id", "number", "status", "feedback", "derived_from")}
                                for item in messages for group in item.get("variants", ())]}
        if pending["kind"] == "style" and photo is None:
            raise ValueError("AI_PHOTO_REQUIRED")
        self.pending[identity] = pending
        cancelled = Event()
        task = asyncio.create_task(self._prepare(pending, cancelled))
        self.tasks[identity] = (task, cancelled)
        self._track(task, cancelled)
        self.service._publish()
        await asyncio.shield(task)
        return {"status": pending.get("outcome", "confirmation_required"), "ticket": pending["ticket"], "kind": pending["kind"]}

    async def _prepare(self, pending, cancelled):
        identity = pending["identity"]
        try:
            if pending["kind"] == "style":
                if self.service.style_provider is None:
                    raise ValueError("AI_IMAGE_UNAVAILABLE")
                history = await self.service.style_provider("history", pending["photo"], is_cancelled=cancelled.is_set)
                if len(history) < 2:
                    raise ValueError("AI_STYLE_HISTORY")
                pending["history"] = history
                candidates = {item["id"] for item in history}
                first = next((write for write in pending["writes"] if write.get("purpose") == "style"
                              and write["before_revision"] in candidates), None)
                pending["before_revision"] = first["before_revision"] if first else history[0]["id"]
                pending["result_revision"] = pending["photo"].revision_id
                for row in history:
                    row["purpose"] = next((write.get("purpose") for write in pending["writes"]
                                            if write["after_revision"] == row["id"]), None)
            if cancelled.is_set() or self.pending.get(identity) is not pending:
                raise ValueError("AI_CANCELLED")
            pending["status"] = "ready"
        except Exception:
            if self.pending.get(identity) is pending:
                self.pending.pop(identity)
            if cancelled.is_set():
                raise ValueError("AI_CANCELLED") from None
            raise
        finally:
            if self.tasks.get(identity, (None,))[0] is asyncio.current_task():
                self.tasks.pop(identity, None)
            self.service._publish()

    def confirm(self, identity, ticket, fields, builder=None):
        pending = self.pending.get(identity)
        if not pending or pending["ticket"] != ticket or pending["status"] != "ready":
            raise ValueError("AI_STYLE_CHANGED")
        if pending["kind"] == "preference":
            self.service.styles.remember_preference(fields.get("text", pending["text"]))
            pending["outcome"] = "saved"
            self._resolved(identity, ticket, "saved")
            self.pending.pop(identity)
            self._finish_waiting(identity, ticket, "completed")
            self.service.preferences_updated()
            self.service.emit("style_saved", {"conversation_id": identity, "kind": "preference"})
            self.service._publish()
            return
        history = pending["history"]
        ids = [row["id"] for row in history]
        before, after = fields["before_revision"], fields["result_revision"]
        if before not in ids or after not in ids or ids.index(before) >= ids.index(after):
            raise ValueError("AI_STYLE_HISTORY")
        name, scene = fields.get("name", "").strip(), fields.get("scene", "").strip()
        if not 1 <= len(name) <= 80 or len(scene) > 240 or builder is None:
            raise ValueError("AI_STYLE_ARGUMENTS")
        pending["status"] = "saving"
        cancelled = Event()
        task = asyncio.create_task(self._capture(pending, fields, builder, cancelled))
        self.tasks[identity] = (task, cancelled)
        self._track(task, cancelled)
        self.service._publish()

    async def _capture(self, pending, fields, builder, cancelled):
        service, identity = self.service, pending["identity"]
        try:
            result = await service.style_provider("capture", pending["photo"], builder=builder,
                before_revision=fields["before_revision"], result_revision=fields["result_revision"], is_cancelled=cancelled.is_set)
            if cancelled.is_set() or self.pending.get(identity) is not pending:
                return
            assets, sample = result["assets"], result["sample"]
            reference = copy.deepcopy(pending["reference"])
            if reference and service.store.attachment_present(reference["attachment"]):
                assets["reference"] = service.store.attachment(reference["attachment"])
                reference["missing"] = False
            elif reference:
                reference.update(missing=True, thumbnail="")
            sample.update(scene=fields.get("scene", "").strip(), source=pending["photo"].identity(),
                          reference=copy.deepcopy(reference), user_text=pending["user_text"], feedback=pending["feedback"])
            record, sample_id = service.styles.save_sample(sample, assets, name=fields["name"],
                style_id=fields.get("style_id", ""), version=fields.get("version"))
            pending["outcome"] = "saved"
            self._resolved(identity, pending["ticket"], "saved", {key: record[key] for key in ("id", "name", "version")})
            self.pending.pop(identity)
            self._finish_waiting(identity, pending["ticket"], "completed")
            message = service.records[identity]["messages"].get(pending["message_id"])
            if message is not None:
                message.setdefault("saved_styles", []).append({"id": record["id"], "sample": sample_id, "name": record["name"]})
                try:
                    service._save(service.records[identity], touch=False)
                except (OSError, sqlite3.Error):
                    service.emit("error", {"code": "AI_STORE_FAILED", "operation": "style_reference"})
            service.emit("style_saved", {"conversation_id": identity, "kind": "style"})
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
            if self.pending.get(identity) is pending:
                pending["status"] = "ready"
            if not cancelled.is_set():
                service.emit("error", {"code": str(exc) if isinstance(exc, ValueError) else "AI_STORE_FAILED", "operation": "save_style"})
        finally:
            if self.tasks.get(identity, (None,))[0] is asyncio.current_task():
                self.tasks.pop(identity, None)
            service._publish()

    async def close(self):
        for identity in tuple(self.pending):
            self.cancel(identity)
        tasks = tuple(self.all_tasks.items())
        for _task, cancelled in tasks:
            cancelled.set()
        if tasks:
            await asyncio.gather(*(task for task, _cancelled in tasks), return_exceptions=True)
        self.pending.clear()

    def replay(self, identity, style_id, sample_id, photo):
        if identity in self.tasks or identity in self.service.variants.adoptions or photo is None:
            raise ValueError("AI_CONVERSATION_BUSY")
        style = self.service.styles.get(style_id, active=True)
        sample = next((item for item in style["samples"] if item["id"] == sample_id), None)
        if sample is None:
            raise ValueError("AI_STYLE_MISSING")
        source = sample["source"]
        if not photo.source_hash or source["source_hash"] != photo.source_hash or source["object_key"] != photo.object_key:
            raise ValueError("AI_PHOTO_CHANGED")
        if WorkflowGraph.from_dict(sample["result_workflow"]).workflow_hash() == photo.workflow.workflow_hash():
            self.service.emit("style_replayed", {"conversation_id": identity, "photo": photo, "image": None, "error": None})
            return
        self.service._cancel_conversation(identity)
        cancelled = Event()
        task = asyncio.create_task(self._replay(identity, style, sample, photo, cancelled))
        self.tasks[identity] = (task, cancelled)
        self._track(task, cancelled)
        self.service._publish()

    async def _replay(self, identity, style, sample, photo, cancelled):
        service = self.service
        try:
            pending = [task for key, task in service.operations.items()
                       if service.runs.get(key) and service.runs[key].conversation_id == identity]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            message_id, request_id = new_id(), new_id()
            result = await service.variant_provider("adopt", photo, workflow=WorkflowGraph.from_dict(sample["result_workflow"]),
                adjustments={"color": {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version), **sample["full_color"]},
                             "beauty": {**BEAUTY_PARAMETERS.defaults(BEAUTY_PARAMETERS.version), **sample["full_beauty"]}}, label="ai.style.replay",
                model="saved-style", conversation_id=identity, message_id=message_id, request_id=request_id,
                is_cancelled=cancelled.is_set, purpose="style")
            action = {"outcome": "applied", "photo": photo.identity(), "before_revision": photo.revision_id,
                      "before_workflow_hash": photo.workflow.workflow_hash(), "after_revision": result["revision_id"],
                      "after_workflow_hash": result["after_workflow_hash"], "changes": result["changes"], "purpose": "style"}
            record = service.records[identity]
            record["messages"][message_id] = {"id": message_id, "parent": record["head"], "role": "assistant", "text": "",
                "raw": "", "status": "completed", "mode": "agent", "created": time.time(), "attachments": [],
                "notice": "style_replayed", "style": {key: style[key] for key in ("id", "name", "version")},
                "action": action, "usage": {}, "configuration": {}, "error": None, "reasoning_summary": "",
                "writes": [{key: action[key] for key in ("purpose", "before_revision", "before_workflow_hash", "after_revision", "after_workflow_hash")}]}
            record["head"] = message_id
            error = result.get("error")
            try:
                service._save(record)
            except (OSError, sqlite3.Error):
                error = "AI_STORE_FAILED"
            service.emit("style_replayed", {"conversation_id": identity, "photo": result["context"], "image": result["image"],
                                            "error": {"code": error} if error else None})
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            service.emit("style_replay_failed", {"conversation_id": identity,
                "error": {"code": str(exc) if isinstance(exc, ValueError) else "AI_REQUEST_FAILED"}})
        finally:
            if self.tasks.get(identity, (None,))[0] is asyncio.current_task():
                self.tasks.pop(identity, None)
            service._publish()
