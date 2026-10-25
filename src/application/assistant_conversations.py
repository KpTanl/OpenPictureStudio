from __future__ import annotations

import asyncio
import base64
import copy
import logging
import os
import sqlite3
import time
from dataclasses import asdict, replace
from pathlib import Path

from .assistant_context import AssistantContextBuilder
from .assistant_actions import AI_TOOL_LABEL
from .assistant_models import (AssistantConnection, AssistantModel, MAX_QUEUED_REQUESTS, MODES,
                               builtin_reasoning, context_defaults, new_id)
from .assistant_protocols import build_payload
from .assistant_request import AssistantRequestError, AssistantRequestService
from .assistant_store import AssistantStore
from .assistant_runs import AssistantRun, RUN_MAX_SECONDS
from .assistant_references import selected_reference
from .assistant_images import image_sizing_rule

logger = logging.getLogger(__name__)


def _error_code(exc: BaseException, fallback: str, operation: str) -> str:
    """业务错误（AI_ 开头的 ValueError）原样给出；其余是程序或环境故障，转成错误码前记下完整异常。"""
    if isinstance(exc, ValueError) and str(exc).startswith("AI_"):
        return str(exc)
    logger.error("助手操作 %s 失败，按 %s 报告", operation, fallback, exc_info=exc)
    return fallback


def message_path(record: dict, head: str | None = None) -> list[dict]:
    path, seen = [], set()
    cursor = record["head"] if head is None else head
    while cursor and cursor not in seen:
        seen.add(cursor)
        message = record["messages"][cursor]
        path.append(message)
        cursor = message["parent"]
    return list(reversed(path))




class AssistantConversationService:
    """状态和持久化由一个后台循环持有；网络任务在该循环内异步执行。"""

    def __init__(self, directory: str, emit, *, material_provider=None, action_provider=None, reference_provider=None,
                 variant_provider=None, fit_provider=None, style_provider=None, evaluation_provider=None,
                 observation_provider=None, image_memory_provider=None, batch_provider=None, audit=None):
        """material_provider(photo, request, cancel_event)：按 request（image_source、analysis、faces）
        在后台取这一轮的照片素材，返回 {"image": JPEG 字节或 None, "analysis": 统计或 None, "faces": 是否有人脸或 None}。"""
        self.store = AssistantStore(directory)
        self.emit = emit
        self.material_provider = material_provider
        self.observation_provider = observation_provider
        self.image_memory_provider = image_memory_provider
        self.batch_provider = batch_provider
        self.action_provider = action_provider
        self.reference_provider = reference_provider
        self.fit_provider = fit_provider
        from .assistant_styles import AssistantStyles
        from .assistant_style_tasks import AssistantStyleTasks
        self.styles = AssistantStyles(self.store.database, self.store.directory)
        self.style_tasks = AssistantStyleTasks(self)
        self.style_provider = style_provider
        self.reference_tasks = {}
        from .assistant_variants import AssistantVariants
        self.variant_provider = variant_provider
        self.variants = AssistantVariants(self)
        self.audit = audit
        migration_error = self.store.migrate()
        self.connections, self.models, errors = self.store.configurations()
        self.records = self.store.conversations()
        self.request = AssistantRequestService()
        from .assistant_evaluations import AssistantEvaluations
        self.evaluation_provider = evaluation_provider
        self.evaluations = AssistantEvaluations(self)
        self.context_builder = AssistantContextBuilder()
        self.tasks = {}
        self.runs = {}
        self.operations = {}
        self.proposals = {}
        self.run_timers = {}
        self.conversation_locks = {}
        self.unsaved = set()
        self.current_photo = ""
        self.current_name = ""
        self.current = self.store.setting("last_conversation", "")
        self.default_model = self.store.setting("default_model", "")
        self.closing = False
        self._publish()
        for error in ([migration_error] if migration_error else []) + errors:
            self.emit("error", {"code": error, "operation": "load"})

    def _save(self, record: dict, *, touch=True) -> None:
        """新会话在有了消息或草稿之后才写入数据库；此前只在内存里，切走即丢弃。"""
        if touch:
            record["updated"] = time.time()
        identity = record["id"]
        for run in self.runs.values():
            if run.conversation_id == identity and run.message_id in record["messages"]:
                record["messages"][run.message_id]["run"] = run.snapshot()
        pending = identity in self.unsaved
        if pending and not (record["messages"] or record["draft"].strip() or record.get("draft_reference")):
            self.records[identity] = record
            return
        self.store.save_conversation(record)
        self.records[identity] = record
        if pending:
            self.unsaved.discard(identity)
            if identity == self.current:
                self._remember(record)

    def _remember(self, record: dict) -> None:
        self.store.set_setting("last_conversation", record["id"])
        if not record["archived"]:
            self.store.set_setting("photo:" + record["object_key"], record["id"])

    def _modify(self, identity: str, mutate, *, touch=True) -> dict:
        record = copy.deepcopy(self.records[identity])
        mutate(record)
        self._save(record, touch=touch)
        return record

    def _publish(self) -> None:
        current = self.records.get(self.current)
        path = copy.deepcopy(message_path(current)) if current else []
        from .assistant_styles import mark_style_references
        styles = self.styles.view_records()
        identities = {style["id"] for style in styles}
        projected = copy.deepcopy(current)
        mark_style_references(path, identities)
        mark_style_references(projected, identities)
        path_ids = {message["id"] for message in path}
        for message in path:
            run = message.get("run")
            if run and run.get("last_response") and run["last_response"] not in path_ids:
                step = next((item["run_step"] for item in reversed(path)
                             if item.get("run_id") == run["id"] and item.get("run_step")), None)
                if step:
                    message["run"] = {**step, "reason": "branch"}
        infos = [{key: record[key] for key in ("id", "title", "object_key", "photo_name", "updated", "pinned", "archived", "unread")}
                 | {"search_text": "\n".join(message["text"] for message in record["messages"].values())}
                 for record in self.records.values()]
        infos.sort(key=lambda item: (not item["pinned"], -item["updated"]))
        self.emit("state", {"ready": True, "current": self.current, "photo": self.current_photo,
                            "conversations": infos, "conversation": projected,
                            "messages": copy.deepcopy(path),
                            "connections": [asdict(item) for item in self.connections.values()],
                            "models": [asdict(item) for item in self.models.values()],
                            "default_model": self.default_model,
                            "evaluations": self.evaluations.configuration(),
                            "reference": copy.deepcopy(selected_reference(current)) if current else None,
                            "reference_loading": list(self.reference_tasks),
                            "styles": styles, "style_preferences": self.styles.preferences(),
                            "style_pending": self.style_tasks.view(self.current),
                            "style_busy": list(self.style_tasks.tasks),
                            "variant_busy": list(self.variants.adoptions),
                            "busy": list(set(self.variants.adoptions) | {job["conversation"] for _, job in self.tasks.values()}
                                         | {run.conversation_id for run in self.runs.values() if run.active and not run.paused}
                                         | {self.runs[key].conversation_id for key in self.operations})})

    def _finish_run(self, run, reason, *, cancel_requests=False):
        run.finish(reason)
        if run.reason not in ("completed", "interrupted"):
            self.variants.end(run.conversation_id, "invalid" if run.reason == "photo_changed" else "ended", run_id=run.id)
        timer = self.run_timers.pop(run.id, None)
        if timer:
            timer.cancel()
        for message_id, job in tuple(self.proposals.items()):
            if job.get("run_id") == run.id:
                message = self.records[run.conversation_id]["messages"][message_id]
                if message.get("action") and message["action"]["outcome"] == "pending":
                    message["action"]["outcome"] = "applied" if message["action"].get("after_revision") else "discarded"
                future = job.get("write_future")
                if future is not None and not future.done():
                    future.set_exception(ValueError("AI_CANCELLED"))
                self.proposals.pop(message_id)
        if cancel_requests:
            for task, job in tuple(self.tasks.values()):
                if job.get("run_id") == run.id:
                    task.cancel()
        record = self.records.get(run.conversation_id)
        if record:
            try:
                self._save(record, touch=False)
            except (OSError, sqlite3.Error) as exc:
                self.emit("error", {"code": _error_code(exc, "AI_STORE_FAILED", "finish"), "operation": "finish"})
        self._release_run(run.id)

    def _release_run(self, run_id):
        run = self.runs.get(run_id)
        if (run and not run.active and run_id not in self.operations
                and not any(job.get("run_id") == run_id for _task, job in self.tasks.values())):
            if run.image_memory is not None:
                run.image_memory.close()
                run.image_memory = None
            self.runs.pop(run_id)

    def _expire_run(self, run_id):
        run = self.runs.get(run_id)
        if run and run.active:
            self._finish_run(run, "time_limit", cancel_requests=True)
            self._publish()

    def _command_invalidate(self, identity: str):
        self.variants.end(identity, "invalid")
        for run in tuple(self.runs.values()):
            if run.conversation_id == identity and run.active and not run.batch_mode:
                self._finish_run(run, "photo_changed", cancel_requests=True)
        self._publish()

    def _command_batch_receipt(self, identity, message_id, summary):
        self._task_receipt(identity, message_id, summary, "batch_receipts", "batch")

    def _task_receipt(self, identity, message_id, summary, collection, key):
        record = self.records[identity]
        message = record["messages"][message_id]
        run_id = message.get("run_id")
        target = next((item for item in record["messages"].values()
                       if item.get("run_id") == run_id and item.get("run")), message)
        run = target.get("run") or {}
        receipts = run.setdefault(collection, [])
        index = next((index for index, item in enumerate(receipts) if item[key] == summary[key]), None)
        if index is None:
            receipts.append(summary)
        else:
            receipts[index] = summary
        target["run"] = run
        live = self.runs.get(run_id)
        if live is not None:
            setattr(live, collection, copy.deepcopy(receipts))
        self._save(record, touch=False)
        self._publish()

    def _command_exports_changed(self, summaries):
        by_id = {summary["export"]: summary for summary in summaries}
        for identity, record in tuple(self.records.items()):
            for message_id, message in tuple(record["messages"].items()):
                for receipt in tuple((message.get("run") or {}).get("export_receipts", ())):
                    summary = by_id.get(receipt["export"])
                    if summary is not None and summary != receipt:
                        self._task_receipt(identity, message_id, summary, "export_receipts", "export")

    def _command_observe(self, photo):
        changed = False
        for identity, pending in tuple(self.style_tasks.pending.items()):
            source = pending.get("photo")
            if source and source.object_key == photo.object_key and (source.state_version != photo.state_version
                    or source.revision_id != photo.revision_id or source.workflow.workflow_hash() != photo.workflow.workflow_hash()):
                self.style_tasks.cancel(identity)
                changed = True
        from .assistant_variants import source_matches, variant_groups
        for record in self.records.values():
            expired = False
            for group in variant_groups(record):
                if (group["status"] in ("pending", "restored") and group["source"]["object_key"] == photo.object_key
                        and not source_matches(group["source"], photo)):
                    group["status"] = "invalid"
                    expired = True
            if expired:
                self._save(record, touch=False)
                changed = True
        for run in tuple(self.runs.values()):
            if (run.active and not run.batch_mode and run.start is not None and run.photo is not None
                    and run.start.object_key == photo.object_key and run.photo.session_id == photo.session_id
                    and photo.state_version > run.photo.state_version):
                self._finish_run(run, "photo_changed", cancel_requests=True)
                changed = True
        if changed:
            self._publish()

    def command(self, name: str, values: dict) -> None:
        if self.closing:
            return
        try:
            getattr(self, "_command_" + name)(**values)
        except AssistantRequestError as exc:
            self.emit("error", {**exc.public(), "operation": name, "ticket": values.get("ticket", "")})
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            code = _error_code(exc, "AI_STORE_FAILED", name)
            self.emit("error", {"code": code, **getattr(exc, "details", {}),
                                "operation": name, "ticket": values.get("ticket", "")})

    def _command_photo(self, object_key: str, name: str = "") -> None:
        if any(run.active and run.batch_mode and run.conversation_id == self.current for run in self.runs.values()):
            self.current_photo, self.current_name = object_key, name
            self._publish()
            return
        if object_key == self.current_photo and self.current in self.records:
            self.current_name = name
            return
        if self.current_photo or self.records.get(self.current, {}).get("object_key") not in (None, "", object_key):
            self.style_tasks.cancel(self.current)
            self.variants.end(self.current)
        self.current_photo, self.current_name = object_key, name
        for run in tuple(self.runs.values()):
            if run.active and (run.start is None or run.start.object_key != object_key):
                self._finish_run(run, "photo_changed", cancel_requests=True)
        if self.current in self.records and not self.records[self.current].get("follow_photo", True):
            self._publish()
            return
        preferred = self.store.setting("photo:" + object_key, "")
        matching = sorted((record for record in self.records.values()
                           if record["object_key"] == object_key and not record["archived"]),
                          key=lambda record: record["updated"], reverse=True)
        if preferred in self.records and self.records[preferred]["object_key"] == object_key and not self.records[preferred]["archived"]:
            self._command_select(preferred)
        elif matching:
            self._command_select(matching[0]["id"])
        else:
            self._command_new()

    def _command_new(self, object_key: str | None = None, photo_name: str = "") -> None:
        identity = new_id()
        key = self.current_photo if object_key is None else object_key
        context = context_defaults()
        context.update(self.store.setting("photo_context", {}))
        record = {"id": identity, "title": "", "object_key": key,
                  "follow_photo": bool(key) or object_key is None,
                  "photo_name": photo_name or (self.current_name if key == self.current_photo else ""),
                  "updated": time.time(), "pinned": False, "archived": False, "unread": False,
                  "draft": "", "head": "", "messages": {}, "summary": None,
                  "preferences": {"mode": "agent", "model_id": self.default_model,
                                  "effort": self.models[self.default_model].default_effort if self.default_model in self.models else "",
                                  "context": context,
                                  "context_start": "", "editing": ""}, "scroll": 1.0}
        self.unsaved.add(identity)
        self._save(record)
        self._command_select(identity)

    def _command_select(self, identity: str) -> None:
        record = self.records[identity]
        previous = self.current
        if previous != identity:
            self.style_tasks.cancel(previous)
            self.variants.end(previous)
        if previous != identity:
            for run in tuple(self.runs.values()):
                if run.conversation_id == previous and run.active:
                    self._finish_run(run, "stopped", cancel_requests=True)
        if previous != identity and previous in self.unsaved:
            self.records.pop(previous, None)
            self.unsaved.discard(previous)
        if record["unread"]:
            self._modify(identity, lambda item: item.update(unread=False), touch=False)
        self.current = identity
        if identity not in self.unsaved:
            self._remember(record)
        self.emit("selection", {"conversation_id": identity})
        self._publish()

    def _command_update(self, identity: str, fields: dict) -> None:
        if fields.get("archived"):
            self._cancel_conversation(identity)
        allowed = {"title", "pinned", "archived", "draft", "scroll"}
        if set(fields) - allowed:
            raise ValueError("AI_CONFIGURATION_INVALID")
        self._modify(identity, lambda item: item.update(fields), touch=set(fields) - {"scroll"} != set())
        if set(fields) != {"scroll"}:
            # 滚动位置只在切回这个会话时使用，不必让界面重画。
            self._publish()

    def _command_preferences(self, identity: str, fields: dict) -> None:
        if set(fields) & {"mode", "model_id", "effort", "loop", "context"}:
            self._cancel_conversation(identity)
        def change(item):
            preferences = item["preferences"]
            if set(fields) - {"mode", "model_id", "effort", "loop", "context", "context_start"}:
                raise ValueError("AI_CONFIGURATION_INVALID")
            if "mode" in fields and fields["mode"] not in MODES:
                raise ValueError("AI_CONFIGURATION_INVALID")
            if "loop" in fields and not isinstance(fields["loop"], bool):
                raise ValueError("AI_CONFIGURATION_INVALID")
            if "context" in fields:
                preferences["context"].update(fields["context"])
                if preferences["context"]["image_source"] not in ("current", "original"):
                    raise ValueError("AI_CONFIGURATION_INVALID")
            preferences.update({key: value for key, value in fields.items() if key != "context"})
            if "model_id" in fields:
                model = self.models.get(fields["model_id"])
                preferences["effort"] = model.default_effort if model else ""
        record = self._modify(identity, change, touch=False)
        if set(fields.get("context", {})) & {"image", "image_source"}:
            context = record["preferences"]["context"]
            self.store.set_setting("photo_context", {key: context[key] for key in ("image", "image_source")})
        self._publish()

    def _command_edit(self, identity: str, message_id: str) -> None:
        self._cancel_conversation(identity)
        def change(item):
            message = item["messages"][message_id]
            if message["role"] != "user":
                raise ValueError("AI_CONFIGURATION_INVALID")
            item["draft"] = message["text"]
            item.update(draft_reference=copy.deepcopy(selected_reference(item, draft=False, head=message_id)),
                        reference_pending=True)
            item["preferences"]["editing"] = message_id
        self._modify(identity, change)
        self._publish()

    def _command_cancel_edit(self, identity: str) -> None:
        self._modify(identity, lambda item: item["preferences"].update(editing=""))
        self._publish()

    def _invalidate_branch(self, record: dict, head: str) -> None:
        record["head"] = head
        valid = {message["id"] for message in message_path(record)}
        summary = record.get("summary")
        if summary and not set(summary["covered"]).issubset(valid):
            record["summary"] = None
        for message in record["messages"].values():
            action = message.get("action")
            if action and action["outcome"] in ("pending", "ready") and message["id"] not in valid:
                action["outcome"] = "stale"
        self.emit("branch", {"conversation_id": record["id"]})

    def _command_withdraw(self, identity: str, message_id: str) -> None:
        self._cancel_conversation(identity)
        self._modify(identity, lambda item: self._invalidate_branch(item, item["messages"][message_id]["parent"]))
        self._publish()

    def _command_branch(self, identity: str, message_id: str, tail: bool = False) -> None:
        self._cancel_conversation(identity)
        if tail:
            messages = self.records[identity]["messages"]
            while True:
                children = [message for message in messages.values() if message["parent"] == message_id]
                if not children:
                    break
                message_id = max(children, key=lambda message: message["created"])["id"]
        self._modify(identity, lambda item: self._invalidate_branch(item, message_id))
        self._publish()

    def _command_delete(self, identity: str) -> None:
        if identity in self.variants.adoptions:
            raise ValueError("AI_CONVERSATION_BUSY")
        self._cancel_conversation(identity)
        pending = self.reference_tasks.pop(identity, None)
        if pending:
            pending[1].cancel()
        if identity in self.unsaved:
            self.unsaved.discard(identity)
        else:
            self.store.delete_conversation(identity)
        self.records.pop(identity)
        if self.current == identity:
            self.current = ""
            self._command_photo(self.current_photo, self.current_name)
        else:
            self._publish()

    def _command_clear_attachments(self) -> None:
        for task in self.variants.images.values():
            task.cancel()
        self.variants.images.clear()
        for _ticket, task in self.reference_tasks.values():
            task.cancel()
        self.reference_tasks.clear()
        for run in tuple(self.runs.values()):
            if run.active:
                self._finish_run(run, "stopped", cancel_requests=True)
        self.store.prune_attachments(clear=True)
        for record in tuple(self.records.values()):
            self.variants.end(record["id"])
            if record.get("draft_reference"):
                record["draft_reference"]["missing"] = True
                record["draft_reference"]["thumbnail"] = ""
            for message in record["messages"].values():
                for group in message.get("variants", ()):
                    group["images_missing"] = True
                if message.get("reference"):
                    message["reference"]["missing"] = True
                    message["reference"]["thumbnail"] = ""
            self._save(record, touch=False)
        self.emit("notice", {"key": "assistant.attachments_cleared"})
        self._publish()

    def _command_reference(self, identity: str, path: str = "", photo=None) -> None:
        if self.records[identity]["archived"]:
            raise ValueError("AI_ARCHIVED")
        pending = self.reference_tasks.pop(identity, None)
        if pending:
            pending[1].cancel()
        if not path:
            self._modify(identity, lambda item: item.update(draft_reference=None, reference_pending=True))
        else:
            ticket = new_id()
            task = asyncio.create_task(self._load_reference(identity, path, photo, ticket))
            self.reference_tasks[identity] = (ticket, task)
        self._publish()

    async def _load_reference(self, identity, path, photo, ticket):
        try:
            if self.reference_provider is None:
                raise ValueError("AI_IMAGE_UNAVAILABLE")
            result = await self.reference_provider(path, photo)
            if identity not in self.records or self.reference_tasks.get(identity, (None,))[0] != ticket:
                return
            attachment = self.store.save_attachment(result["image"])
            analysis = result["analysis"]
            analysis["identity"] = {"reference_attachment": attachment}
            if analysis.get("face_rectangle"):
                analysis["face_rectangle"]["identity"] = analysis["identity"]
            reference = {"role": "reference", "attachment": attachment, "name": Path(path).name,
                         "analysis": analysis, "thumbnail": result["thumbnail"]}
            from .assistant_images import jpeg_information
            reference["image_info"] = jpeg_information(result["image"])
            for record in tuple(self.records.values()):
                references = [record.get("draft_reference")] + [message.get("reference") for message in record["messages"].values()]
                changed = False
                for existing in references:
                    if existing and existing["attachment"] == attachment and existing.get("missing"):
                        existing.update(missing=False, thumbnail=reference["thumbnail"])
                        changed = True
                if changed:
                    self._save(record, touch=False)
            self._modify(identity, lambda item: item.update(draft_reference=reference, reference_pending=True))
        except asyncio.CancelledError:
            pass
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.emit("error", {"code": _error_code(exc, "AI_REFERENCE_FAILED", "reference"),
                                **getattr(exc, "details", {}), "operation": "reference"})
        finally:
            if self.reference_tasks.get(identity, (None,))[0] == ticket:
                self.reference_tasks.pop(identity, None)
            if not self.closing:
                self._publish()

    def _command_configuration(self, connection: dict, models: list[dict], ticket: str = "") -> None:
        values = AssistantConnection(**connection)
        for run in tuple(self.runs.values()):
            if run.active and (run.connection.id == values.id or values.id == self.evaluations.configuration()["decisions_connection"]):
                self._finish_run(run, "stopped", cancel_requests=True)
        previous = self.connections.get(values.id)
        values = replace(values, revision=(previous.revision + 1) if previous else 1)
        configured, overrides = [], {}
        saved_overrides = self.store.setting("tool_calling_overrides", {})
        for data in models:
            model = AssistantModel.from_dict(data)
            previous_model = self.models.get(model.id)
            if previous_model and model.tool_calling != previous_model.tool_calling:
                overrides[model.id] = model.tool_calling
            elif model.id not in saved_overrides and builtin_reasoning(values.provider, model.model).get("tool_calling"):
                model = replace(model, tool_calling=True)
            configured.append(model)
        configured = tuple(configured)
        self.store.save_configuration(values, configured, tool_calling_overrides=overrides)
        self.connections[values.id] = values
        if values.id == self.store.setting("legacy_connection", ""):
            self.store.set_setting("legacy_migrated", True)
        self.models.update({model.id: model for model in configured})
        for record in tuple(self.records.values()):
            model = self.models.get(record["preferences"]["model_id"])
            if model and model.id in {item.id for item in configured}:
                def refresh(item):
                    preferences = item["preferences"]
                    if preferences["effort"] and preferences["effort"] not in model.efforts:
                        preferences["effort"] = model.default_effort
                self._modify(record["id"], refresh, touch=False)
        self.emit("configured", {"ticket": ticket, "tool_calling": {model.id: model.tool_calling for model in configured}})
        self._publish()

    def _command_add_connection(self, provider: str = "generic") -> None:
        defaults = {"openai": ("OpenAI", "https://api.openai.com/v1", "responses"),
                    "deepseek": ("DeepSeek", "https://api.deepseek.com", "chat_completions"),
                    "generic": ("API", "http://localhost:8080/v1", "chat_completions")}
        name, endpoint, protocol = defaults[provider]
        connection = AssistantConnection(name=name, endpoint=endpoint, provider=provider,
                                         protocol=protocol)
        self.store.save_configuration(connection, ())
        self.connections[connection.id] = connection
        self.emit("configuration_added", {"connection_id": connection.id})
        self._publish()

    def _command_add_model(self, connection_id: str, model_id: str = "") -> None:
        connection = self.connections[connection_id]
        model = AssistantModel(connection_id=connection_id, model=model_id, name=model_id,
                               **builtin_reasoning(connection.provider, model_id))
        rule = image_sizing_rule(model)
        if rule:
            model = replace(model, image_effective_long_edge=rule.native_long_edge)
        self.store.save_configuration(connection, (model,), validate=bool(model_id))
        self.models[model.id] = model
        if not self.default_model:
            self._command_default_model(model.id)
        self.emit("model_added", {"connection_id": connection_id, "model_id": model.id})
        self._publish()

    def _command_delete_connection(self, identity: str) -> None:
        for run in tuple(self.runs.values()):
            if run.active and (run.connection.id == identity or identity == self.evaluations.configuration()["decisions_connection"]):
                self._finish_run(run, "stopped", cancel_requests=True)
        for task, job in tuple(self.tasks.values()):
            if job.get("connection") and job["connection"].id == identity:
                task.cancel()
        self.store.delete_connection(identity)
        self.connections.pop(identity)
        self.models = {key: model for key, model in self.models.items() if model.connection_id != identity}
        self._publish()

    def _command_delete_model(self, identity: str) -> None:
        for run in tuple(self.runs.values()):
            if run.active and run.model.id == identity:
                self._finish_run(run, "stopped", cancel_requests=True)
        self.store.delete_model(identity)
        self.models.pop(identity)
        self._publish()

    def _command_default_model(self, identity: str) -> None:
        if identity not in self.models:
            raise ValueError("AI_MODEL_MISSING")
        self.store.set_setting("default_model", identity)
        self.default_model = identity
        for record in tuple(self.records.values()):
            if not record["preferences"]["model_id"]:
                self._modify(record["id"], lambda item: item["preferences"].update(
                    model_id=identity, effort=self.models[identity].default_effort), touch=False)
        self._publish()

    def _cancel_conversation(self, identity: str) -> None:
        self.style_tasks.cancel(identity)
        self.variants.end(identity)
        for run in tuple(self.runs.values()):
            if run.conversation_id == identity and run.active:
                self._finish_run(run, "stopped")
        for task, job in tuple(self.tasks.values()):
            if job["conversation"] == identity:
                task.cancel()

    def _command_stop(self, identity: str) -> None:
        self._cancel_conversation(identity)
        self._publish()

    def _command_send(self, identity: str, text: str, photo=None, language: str = "zh-CN", *,
                      retry: str = "", stop_first: bool = False, batch_context=None, continuation=False) -> None:
        self.style_tasks.cancel(identity)
        if identity in self.variants.adoptions:
            raise ValueError("AI_CONVERSATION_BUSY")
        if identity in self.reference_tasks:
            raise ValueError("AI_REFERENCE_LOADING")
        if len(self.tasks) >= MAX_QUEUED_REQUESTS + 2:
            raise ValueError("AI_QUEUE_FULL")
        record = copy.deepcopy(self.records[identity])
        if record["archived"]:
            raise ValueError("AI_ARCHIVED")
        preferences = copy.deepcopy(record["preferences"])
        preferences["mode"] = "plan" if preferences.get("mode") == "plan" else "agent"
        model = self.models.get(preferences["model_id"])
        if model is None:
            raise ValueError("AI_MODEL_MISSING")
        model.validate()
        connection = self.connections.get(model.connection_id)
        if connection is None:
            raise ValueError("AI_CONNECTION_MISSING")
        connection.validate()
        connection.request_headers()
        if photo is not None and record["object_key"] and photo.object_key != record["object_key"]:
            photo = None
        reference = selected_reference(record)
        if len(text) > 32000:
            raise ValueError("AI_PROMPT_TOO_LONG")
        if stop_first:
            self._cancel_conversation(identity)
            record = copy.deepcopy(self.records[identity])
        elif not retry:
            active = next((run for run in self.runs.values() if run.active and run.conversation_id == identity), None)
            if active:
                if batch_context and (not active.batch_mode or batch_context.get("scope_revision")
                                      != active.batch_context.get("scope_revision")):
                    active.batch_context = copy.deepcopy(batch_context)
                    active.batch_mode = active.batch_mode or bool(batch_context.get("explicit"))
                self._steer_run(active, text)
                return
        previous_batches = {}
        if continuation:
            previous_batches = next((item["run"] for item in reversed(message_path(record))
                                     if item.get("run", {}).get("batch_receipts") or item.get("run", {}).get("export_receipts")), {})
        if retry:
            target = record["messages"][retry]
            user = target if target["role"] == "user" else record["messages"][target["parent"]]
            previous_batches = target.get("run") or next((item["run"] for item in record["messages"].values()
                if (item.get("run", {}).get("batch_receipts") or item.get("run", {}).get("export_receipts")) and (item.get("parent") == user["id"]
                    or item.get("run_id") == target.get("run_id"))), {})
            self._invalidate_branch(record, user["id"])
            text = user["text"]
        else:
            if not text.strip():
                return
            editing = preferences.get("editing", "")
            if editing:
                self._cancel_conversation(identity)
                record = copy.deepcopy(self.records[identity])
                self._invalidate_branch(record, record["messages"][editing]["parent"])
            user = {"id": new_id(), "parent": record["head"], "role": "user", "text": text.strip(),
                    "status": "completed", "created": time.time(),
                    "reference": copy.deepcopy(reference),
                    "attachments": [reference["attachment"]] if reference else []}
            record["messages"][user["id"]] = user
            record["head"] = user["id"]
            record["preferences"]["editing"] = ""
        reference = selected_reference(record, draft=False, head=user["id"])
        preferences["context"]["image"] = bool(record["preferences"]["context"]["image"] and model.image_input
                                               and (photo is not None or reference and not reference.get("missing")))
        if not record["title"]:
            record["title"] = text.replace("\n", " ")[:36]
        response_id, request_id = new_id(), new_id()
        message = {"id": response_id, "parent": user["id"], "role": "assistant", "text": "",
                   "raw": "", "status": "queued", "mode": preferences["mode"], "created": time.time(),
                   "request_id": request_id, "configuration": {"connection": connection.public(), "model": model.public(),
                                                              "effort": preferences["effort"]},
                   "attachments": [], "error": None, "action": None, "usage": {}, "reasoning_summary": ""}
        record["messages"][response_id] = message
        record["head"] = response_id
        record["preferences"]["mode"] = preferences["mode"]
        run = AssistantRun(request_id, identity, response_id, text, photo, preferences, language, model, connection,
                           runtime="agent", reference=copy.deepcopy(reference),
                           batch_context=copy.deepcopy(batch_context or {}),
                           batch_mode=bool(batch_context and batch_context.get("explicit")))
        if previous_batches.get("batch_receipts") or previous_batches.get("export_receipts"):
            run.batch_context = copy.deepcopy(previous_batches.get("batch_context") or {})
            run.batch_receipts = copy.deepcopy(previous_batches.get("batch_receipts", []))
            run.export_receipts = copy.deepcopy(previous_batches.get("export_receipts", []))
            run.batch_mode = True
            run.runtime_state["batch_retry_only"] = True
            run.runtime_state["export_retry_only"] = True
        self.runs[run.id] = run
        message["run_id"] = run.id
        self.run_timers[run.id] = asyncio.get_running_loop().call_later(RUN_MAX_SECONDS, self._expire_run, run.id)
        if not retry:
            record["draft"] = ""
            record.update(draft_reference=None, reference_pending=False)
        try:
            self._save(record)
        except (OSError, sqlite3.Error):
            if run:
                self._finish_run(run, "failed")
            raise
        job = {"conversation": identity, "message": response_id, "user": user["id"], "request": request_id,
               "preferences": preferences, "photo": photo, "language": language,
               "connection": connection, "model": model, "retry": bool(retry)}
        if run:
            job["run_id"] = run.id
        task = asyncio.create_task(self._run(job))
        self.tasks[request_id] = (task, job)
        task.add_done_callback(lambda completed: self._job_done(completed, job))
        self._publish()

    def _steer_run(self, run, text):
        if not text.strip():
            return
        user_id, message_id = new_id(), new_id()
        self.variants.feedback(run, text.strip(), user_id)
        record = copy.deepcopy(self.records[run.conversation_id])
        for message in record["messages"].values():
            if message.get("run_id") == run.id and message["role"] == "assistant" and message["status"] in (
                    "queued", "preparing", "waiting", "streaming"):
                message["status"] = "stopped"
        reference = copy.deepcopy(selected_reference(record))
        record["messages"][user_id] = {"id": user_id, "parent": record["head"], "role": "user", "text": text.strip(),
                                       "status": "completed", "created": time.time(), "reference": reference,
                                       "attachments": [reference["attachment"]] if reference else [], "run_id": run.id}
        record["messages"][message_id] = {
            "id": message_id, "parent": user_id, "role": "assistant", "text": "", "raw": "", "status": "queued",
            "created": time.time(), "attachments": [], "run_id": run.id, "request_id": run.id,
            "mode": run.preferences["mode"], "usage": {}, "reasoning_summary": "", "action": None, "error": None,
            "configuration": {"connection": run.connection.public(), "model": run.model.public(),
                              "effort": run.preferences["effort"]}}
        record.update(head=message_id, draft="", draft_reference=None, reference_pending=False)
        run.goal_version += 1
        run.runtime_state.pop("batch_retry_only", None)
        run.runtime_state.pop("export_retry_only", None)
        run.reference = reference
        run.preferences["selected_style"] = copy.deepcopy(record["preferences"].get("selected_style"))
        run.preferences["context"]["image"] = bool(record["preferences"]["context"]["image"] and run.model.image_input
                                                   and (run.photo is not None or reference and not reference.get("missing")))
        run.updates.append({"user": user_id, "message": message_id, "text": text.strip(),
                            "goal_version": run.goal_version,
                            "reference": reference["attachment"] if reference else None,
                            "selected_style": run.preferences.get("selected_style")})
        self._save(record)
        if run.paused:
            run.resume()
            remaining = max(0, RUN_MAX_SECONDS - (time.monotonic() - run.started))
            self.run_timers[run.id] = asyncio.get_running_loop().call_later(remaining, self._expire_run, run.id)
            job = {"conversation": run.conversation_id, "message": message_id, "user": user_id, "request": run.id,
                   "preferences": run.preferences, "photo": run.photo, "language": run.language,
                   "connection": run.connection, "model": run.model, "retry": False, "run_id": run.id}
            task = asyncio.create_task(self._run(job))
            self.tasks[run.id] = (task, job)
            task.add_done_callback(lambda completed: self._job_done(completed, job))
        elif run.request_task is not None:
            run.request_task.cancel()
        for pending_id, proposal in tuple(self.proposals.items()):
            if proposal.get("run_id") == run.id:
                action = record["messages"][pending_id].get("action")
                if action and action["outcome"] == "pending":
                    action["outcome"] = "applied" if action.get("after_revision") else "discarded"
                future = proposal.get("write_future")
                if future is not None and not future.done():
                    future.set_exception(ValueError("AI_AGENT_REDIRECT"))
                self.proposals.pop(pending_id)
        self._publish()


    def _command_reject_proposal(self, identity: str, message_id: str, reason="photo_changed"):
        job = self.proposals.pop(message_id, None)
        if job and job["conversation"] == identity:
            action = self.records[identity]["messages"][message_id].get("action")
            if action:
                action["outcome"] = "applied" if action.get("after_revision") else "stale"
            future = job.get("write_future")
            if future is not None and not future.done():
                future.set_exception(ValueError("AI_VARIANT_FAILED" if reason == "variant_failed" else "AI_PHOTO_CHANGED"))
            if reason != "variant_failed":
                self._finish_run(self.runs[job["run_id"]], reason)
            self._publish()

    def _command_apply_proposal(self, identity: str, message_id: str, workflow):
        job = self.proposals.get(message_id)
        if job is None or job["conversation"] != identity:
            self.emit("apply_failed", {"conversation_id": identity, "message_id": message_id,
                                       "error": {"code": "AI_LOOP_STOPPED"}})
            return
        run = self.runs[job["run_id"]]
        if not run.active or run.id in self.operations:
            self.emit("apply_failed", {"conversation_id": identity, "message_id": message_id,
                                       "error": {"code": "AI_LOOP_STOPPED"}})
            return
        record = self.records[identity]
        if record["archived"] or record["head"] != message_id:
            self._command_reject_proposal(identity, message_id, "stopped")
            self.emit("apply_failed", {"conversation_id": identity, "message_id": message_id,
                                       "error": {"code": "AI_LOOP_STOPPED"}})
            return
        self.proposals.pop(message_id)
        if job.get("kind") == "variants":
            self.operations[run.id] = asyncio.create_task(self.variants.generate(job, workflow))
            self._publish()
            return
        if job.get("kind") in ("fit", "style_apply"):
            self.operations[run.id] = asyncio.create_task(self._fit(run, job, workflow))
            self._publish()
            return
        run.phase = "applying"
        record["messages"][message_id]["action"]["outcome"] = "applying"
        try:
            self._save(record)
        except (OSError, sqlite3.Error) as exc:
            record["messages"][message_id]["action"]["outcome"] = "failed"
            self._finish_run(run, "failed")
            future = job.get("write_future")
            if future is not None and not future.done():
                future.set_exception(ValueError("AI_STORE_FAILED"))
            self.emit("apply_failed", {"conversation_id": identity, "message_id": message_id,
                                       "error": {"code": _error_code(exc, "AI_STORE_FAILED", "apply")}})
            self._publish()
            return
        self.operations[run.id] = asyncio.create_task(self._apply(run, job, workflow))
        self._publish()

    def _command_adopt_variant(self, identity: str, group_id: str, label: str, photo):
        if identity != self.current or self.records[identity]["archived"]:
            raise ValueError("AI_VARIANT_STALE")
        self.variants.adopt(identity, group_id, label, photo)

    def _command_variant_images(self, identity: str, group_id: str, ticket: str):
        if identity == self.current:
            self.variants.load_images(identity, group_id, ticket)

    async def fit_reference(self, job, values):
        run = self.runs[job["run_id"]]
        if not run.preferences["context"].get("analysis", True):
            raise ValueError("AI_ANALYSIS_DISABLED")
        if not run.reference:
            raise ValueError("AI_REFERENCE_REQUIRED")
        future = asyncio.get_running_loop().create_future()
        self.proposals[job["message"]] = {**job, "kind": "fit", "photo": run.photo,
                                         "fit_values": copy.deepcopy(values), "write_future": future}
        self.emit("fit_proposal", {"conversation_id": job["conversation"], "message_id": job["message"], "photo": run.photo})
        return await future

    async def apply_style(self, job, values):
        run = self.runs[job["run_id"]]
        if not run.preferences["context"].get("analysis", True):
            raise ValueError("AI_ANALYSIS_DISABLED")
        style = self.styles.get(values["style_id"], active=True)
        future = asyncio.get_running_loop().create_future()
        options = {key: value for key, value in values.items() if key != "style_id"}
        selected = run.preferences.get("selected_style") or {}
        if selected.get("id") == style["id"] and selected.get("sample_id"):
            options.setdefault("sample_id", selected["sample_id"])
        self.proposals[job["message"]] = {**job, "kind": "style_apply", "photo": run.photo,
                                         "fit_values": options, "style": style, "write_future": future}
        self.emit("fit_proposal", {"conversation_id": job["conversation"], "message_id": job["message"], "photo": run.photo})
        return await future

    def _command_style_begin(self, identity: str, message_id: str = "", photo=None, fields=None):
        if identity != self.current:
            raise ValueError("AI_STYLE_CHANGED")
        async def prepare():
            try:
                await self.style_tasks.begin(identity, message_id, photo, fields or {})
            except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
                if isinstance(exc, ValueError) and str(exc) == "AI_CANCELLED":
                    return
                self.emit("error", {"code": _error_code(exc, "AI_STORE_FAILED", "style_begin"), "operation": "style_begin"})
        asyncio.create_task(prepare())

    def _command_style_cancel(self, identity: str):
        self.style_tasks.cancel(identity, finish=True)
        self._save(self.records[identity], touch=False)
        self._publish()

    def _command_style_confirm(self, identity: str, ticket: str, fields, photo=None, builder=None):
        pending = self.style_tasks.pending.get(identity)
        if identity != self.current or not pending or pending["ticket"] != ticket:
            raise ValueError("AI_STYLE_CHANGED")
        source = pending["photo"]
        if source and (photo is None or source.identity() != photo.identity()):
            raise ValueError("AI_PHOTO_CHANGED")
        self.style_tasks.confirm(identity, ticket, fields, builder)

    def _command_style_edit(self, style_id: str, version: int, fields):
        self.styles.edit(style_id, version, **fields)
        self._publish()

    def _command_style_remove(self, style_id: str, version: int, sample_id=""):
        self.styles.remove(style_id, version, sample_id)
        self._publish()

    def _command_style_replay(self, identity: str, style_id: str, sample_id: str, photo):
        if identity != self.current:
            raise ValueError("AI_STYLE_CHANGED")
        self.style_tasks.replay(identity, style_id, sample_id, photo)

    def _command_style_select(self, identity: str, style_id: str, sample_id=""):
        style = self.styles.get(style_id, active=True)
        if sample_id and not any(sample["id"] == sample_id for sample in style["samples"]):
            raise ValueError("AI_STYLE_MISSING")
        self._modify(identity, lambda record: record["preferences"].update(
            selected_style={"id": style_id, "sample_id": sample_id, "name": style["name"], "version": style["version"]}), touch=False)
        self._publish()

    def preferences_updated(self):
        for run in tuple(self.runs.values()):
            if run.active:
                self._finish_run(run, "stopped", cancel_requests=True)

    def _command_evaluation_settings(self, backend: str, decisions_connection: str):
        from .assistant_evaluations import decisions_connection as configured_connection
        settings = {"backend": backend, "decisions_connection": decisions_connection}
        if backend not in ("main", "decisions") or decisions_connection and decisions_connection not in self.connections:
            raise ValueError("AI_CONFIGURATION_INVALID")
        if backend == "decisions" and configured_connection(settings, self.connections) is None:
            raise ValueError("AI_EVALUATION_UNCONFIGURED")
        if settings != {key: self.evaluations.configuration()[key] for key in settings}:
            self.store.set_setting("evaluations", settings)
            self.preferences_updated()
        self._publish()

    def _command_preference_remove(self, preference_id: str):
        self.styles.forget_preference(preference_id)
        self.preferences_updated()
        self._publish()

    async def _fit(self, run, job, builder):
        future = job["write_future"]
        try:
            cancelled = lambda: not run.active or run.goal_version != job["goal_version"]
            if job.get("kind") == "style_apply":
                if self.style_provider is None:
                    raise ValueError("AI_IMAGE_UNAVAILABLE")
                result = await self.style_provider("apply", job["photo"], builder=builder, style=job["style"],
                    is_cancelled=cancelled, beauty=not run.photo.tools.get("beauty", "disabled") and run.faces is not False,
                    **job["fit_values"])
                current = self.styles.get(job["style"]["id"], active=True)
                if current["version"] != job["style"]["version"]:
                    raise ValueError("AI_STYLE_CHANGED")
            else:
                if self.fit_provider is None:
                    raise ValueError("AI_IMAGE_UNAVAILABLE")
                result = await self.fit_provider(job["photo"], builder, copy.deepcopy(run.reference["analysis"]),
                    job["fit_values"], is_cancelled=cancelled)
            if not run.active or run.goal_version != job["goal_version"]:
                raise ValueError("AI_AGENT_REDIRECT")
            result["goal_version"] = job["goal_version"]
            if job.get("kind") == "fit":
                result["reference"] = run.reference["attachment"]
            if not future.done():
                future.set_result(result)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            if not future.done():
                future.set_exception(ValueError("AI_AGENT_REDIRECT")
                                     if run.active and run.goal_version != job["goal_version"] else exc)
        finally:
            self.operations.pop(run.id, None)
            self._release_run(run.id)
            self._publish()

    async def _apply(self, run, job, workflow):
        result = {}
        try:
            if self.action_provider is None:
                raise ValueError("AI_IMAGE_UNAVAILABLE")
            label = AI_TOOL_LABEL + str(run.applied_rounds + 1)
            result = await self.action_provider(job["photo"], workflow, job["adjustments"], label=label,
                conversation_id=run.conversation_id, message_id=job["message"], request_id=job["request"],
                model=run.model.model, purpose=job.get("purpose"),
                is_cancelled=lambda: run.cancelled.is_set() or job.get("goal_version") != run.goal_version,
                review=run.material_request())
            record = self.records[run.conversation_id]
            action = record["messages"][job["message"]]["action"]
            run.written(job["message"], result)
            record["messages"][job["message"]]["analysis"] = (result.get("review") or {}).get("analysis")
            action.update(run.receipt, outcome="applied", changes=result["changes"])
            action["purpose"] = job.get("purpose")
            record["messages"][job["message"]].setdefault("writes", []).append({
                "purpose": job.get("purpose"), "before_revision": job["photo"].revision_id,
                "before_workflow_hash": job["photo"].workflow.workflow_hash(),
                "after_revision": result["revision_id"], "after_workflow_hash": result["after_workflow_hash"]})
            if result.get("error"):
                self._finish_run(run, "failed")
            elif run.active:
                reason = run.limit_reason()
                if reason:
                    self._finish_run(run, reason)
            record["messages"][job["message"]]["run_step"] = run.snapshot()
            try:
                self._save(record)
            except (OSError, sqlite3.Error) as exc:
                result["error"] = _error_code(exc, "AI_STORE_FAILED", "apply")
                self._finish_run(run, "failed")
            self.emit("applied", {"conversation_id": run.conversation_id, "message_id": job["message"],
                                  "photo": run.photo, "image": result["image"],
                                  "error": {"code": result["error"]} if result.get("error") else None})
            future = job.get("write_future")
            if future is not None and not future.done():
                future.set_result(result)
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            code = ("AI_PHOTO_CHANGED" if isinstance(exc, ValueError) and str(exc) == "STALE_CANDIDATE"
                    else _error_code(exc, "AI_REQUEST_FAILED", "apply"))
            if run.active and job.get("goal_version") != run.goal_version:
                code = "AI_AGENT_REDIRECT"
            else:
                self._finish_run(run, "photo_changed" if code in ("AI_PHOTO_CHANGED", "STALE_CANDIDATE")
                                 else "stopped" if code == "AI_CANCELLED" else "failed")
            record = self.records.get(run.conversation_id)
            if record and not result.get("revision_id"):
                action = record["messages"][job["message"]]["action"]
                action["outcome"] = "applied" if action.get("after_revision") else "failed"
                try:
                    self._save(record)
                except (OSError, sqlite3.Error) as store_error:
                    self.emit("error", {"code": _error_code(store_error, "AI_STORE_FAILED", "apply"), "operation": "apply"})
            self.emit("apply_failed", {"conversation_id": run.conversation_id, "message_id": job["message"],
                                       "error": {"code": code}})
            future = job.get("write_future")
            if future is not None and not future.done():
                future.set_exception(ValueError(code))
        finally:
            self.operations.pop(run.id, None)
            self._release_run(run.id)
            self._publish()

    async def _materials(self, job: dict, cancel_event) -> dict:
        """自查轮复用已提交结果的素材；首轮按固定偏好在后台生成。"""
        if "materials" in job:
            return job["materials"]
        preferences, photo = job["preferences"], job["photo"]
        context = preferences["context"]
        mode = preferences["mode"]
        request = {"image_source": context["image_source"] if context["image"] else None,
                   "analysis": photo is not None and mode in ("plan", "edit", "agent") and context.get("analysis", True),
                   "faces": photo is not None and mode in ("edit", "agent", "plan") and not photo.tools.get("beauty", "disabled")}
        run = self.runs.get(job.get("run_id"))
        request["baseline"] = bool(run and run.looping and run.sends_image)
        request.update(run_id=run.id if run else "", goal_version=run.goal_version if run else 0)
        if photo is None or not any(request.values()):
            return {"image": None, "analysis": None, "faces": None}
        if self.material_provider is None:
            raise ValueError("AI_IMAGE_UNAVAILABLE")
        return await self.material_provider(photo, request, cancel_event)

    def _job_done(self, task, job):
        if not job.get("started"):
            self._finish_error(job, {"code": "AI_CANCELLED"}, "stopped")
            self.tasks.pop(job["request"], None)
            self._release_run(job.get("run_id"))
            self._publish()
        elif not task.cancelled() and task.exception() is not None:
            self._finish_error(job, {"code": _error_code(task.exception(), "AI_REQUEST_FAILED", "send")})
            self._publish()

    def _record_request(self, event, job, **fields):
        if self.audit:
            result = self.audit(event, request_id=job["request"], conversation_id=job["conversation"],
                                message_id=job["message"], model=job["model"].model,
                                endpoint_host=job["connection"].public()["host"],
                                mode=job["preferences"]["mode"], **fields)
            if result is not None and not result.success:
                raise ValueError("AUDIT_WRITE_FAILED")

    def _stream_event(self, job: dict, kind: str, delta: str) -> None:
        run = self.runs.get(job.get("run_id"))
        if run and (not run.active or job.get("goal_version") != run.goal_version):
            return
        record = self.records.get(job["conversation"])
        if record is None:
            return
        message = record["messages"][job["message"]]
        if kind == "text":
            message["raw"] += delta
            message["text"] = message["raw"]
            message["status"] = "streaming"
        else:
            message["reasoning_summary"] += delta
        now = time.monotonic()
        if now - job.get("last_emitted", 0) >= 0.05:
            self.emit("delta", {"conversation_id": record["id"], "message_id": message["id"],
                                "request_id": job["request"], "text": message["text"],
                                "summary": message["reasoning_summary"], "status": message["status"]})
            job["last_emitted"] = now
        if now - job.get("last_saved", 0) >= 1:
            self._save(record, touch=False)
            job["last_saved"] = now

    async def _run(self, job: dict) -> None:
        from .assistant_agent import AssistantAgentRuntime
        await AssistantAgentRuntime(self, job).run_task()

    def _finish_error(self, job: dict, error: dict, status="failed") -> None:
        run = self.runs.get(job.get("run_id"))
        if run:
            self._finish_run(run, "stopped" if status == "stopped" else "failed")
        record = self.records.get(job["conversation"])
        if record is None:
            return
        message = record["messages"][job["message"]]
        message.update(status=status, error=error)
        if not (message.get("action") or {}).get("after_revision"):
            message["action"] = None
        if not record["draft"] and status == "failed" and not job.get("review"):
            record["draft"] = record["messages"][job["user"]]["text"]
        try:
            self._record_request("agent.result", job, success=False, error=error["code"])
        except ValueError:
            self.emit("error", {"code": "AUDIT_WRITE_FAILED", "operation": "response"})
        try:
            self._save(record)
        except (OSError, sqlite3.Error) as exc:
            self.emit("error", {"code": _error_code(exc, "AI_STORE_FAILED", "response"), "operation": "response"})

    def _command_cancel_request(self, request_id: str) -> None:
        entry = self.tasks.get(request_id)
        if entry:
            run = self.runs.get(entry[1].get("run_id"))
            if run:
                self._finish_run(run, "stopped")
            entry[0].cancel()

    def _command_history_position(self, object_key: str, revisions: list[str], current: int) -> None:
        indices = {identity: index for index, identity in enumerate(revisions)}
        for record in tuple(self.records.values()):
            replacement = copy.deepcopy(record)
            changed = False
            for message in replacement["messages"].values():
                action = message.get("action")
                if not action or not action.get("after_revision"):
                    continue
                if action.get("photo", {}).get("object_key") != object_key:
                    continue
                after = indices.get(action["after_revision"])
                if after is None:
                    continue
                outcome = "applied" if after <= current else "undone"
                if action["outcome"] != outcome:
                    action["outcome"] = outcome
                    changed = True
            if changed:
                self._save(replacement, touch=False)
        self._publish()

    def _command_action(self, identity: str, message_id: str, fields: dict) -> None:
        def change(item):
            action = item["messages"][message_id].get("action")
            if action:
                action.update(fields)
        self._modify(identity, change)
        self._publish()

    def _command_summary_edit(self, identity: str, text: str) -> None:
        def change(item):
            if item.get("summary"):
                item["summary"]["text"] = text[:32000]
        self._modify(identity, change, touch=False)
        self._publish()

    def _command_summarize(self, identity: str, language: str = "zh-CN") -> None:
        record = self.records[identity]
        path = message_path(record)
        complete = []
        for index in range(0, len(path) - 1, 2):
            if path[index]["role"] == "user" and path[index + 1]["status"] == "completed":
                complete.extend(path[index:index + 2])
        covered = complete[:-4] if len(complete) > 6 else complete
        if not covered:
            raise ValueError("AI_SUMMARY_EMPTY")
        model = self.models.get(record["preferences"]["model_id"])
        if model is None:
            raise ValueError("AI_MODEL_MISSING")
        if any(job["conversation"] == identity for _, job in self.tasks.values()):
            raise ValueError("AI_CONVERSATION_BUSY")
        connection = self.connections[model.connection_id]
        request_id = new_id()
        job = {"conversation": identity, "request": request_id, "message": ""}
        task = asyncio.create_task(self._summarize(identity, covered, model, connection, language, request_id))
        self.tasks[request_id] = (task, job)
        task.add_done_callback(lambda completed: self._aux_finished(request_id, completed))
        self.emit("summary_state", {"conversation_id": identity, "busy": True})
        self._publish()

    async def _summarize(self, identity, covered, model, connection, language, request_id):
        try:
            context = {"instructions": "Summarize this photography conversation in " + language
                       + ". Preserve user preferences, decisions and actual applied adjustments. Return concise text only.",
                       "messages": [{"id": "summary", "role": "user", "content": "\n\n".join(
                           message["role"] + ": " + message["text"] for message in covered)}]}
            from .assistant_context import estimate_tokens
            if model.context_window and estimate_tokens(context) + model.max_output_tokens + model.reasoning_reserve >= model.context_window:
                raise ValueError("AI_CONTEXT_BUDGET")
            async with self.request.slots:
                result = await self.request.request(connection, build_payload(connection, model, context, mode="chat"),
                                                    lambda _kind, _delta: None)
            valid = {message["id"] for message in message_path(self.records[identity])}
            if not {message["id"] for message in covered}.issubset(valid):
                raise ValueError("AI_SUMMARY_STALE")
            self._modify(identity, lambda item: item.update(summary={"text": result["text"],
                         "covered": [message["id"] for message in covered], "created": time.time(),
                         "usage": result["usage"]}))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            code = exc.code if isinstance(exc, AssistantRequestError) else _error_code(exc, "AI_REQUEST_FAILED", "summarize")
            self.emit("error", {"code": code, "operation": "summarize"})
        finally:
            self.tasks.pop(request_id, None)
            self.emit("summary_state", {"conversation_id": identity, "busy": False})
            self._publish()

    def _aux_finished(self, key, task):
        entry = self.tasks.pop(key, None)
        if entry is None:
            return
        job = entry[1]
        code = "AI_CANCELLED" if task.cancelled() else "AI_REQUEST_FAILED"
        if not task.cancelled() and task.exception() is not None:
            code = _error_code(task.exception(), code, "diagnose" if key.startswith("diagnostic:") else "summarize")
        if key.startswith("diagnostic:"):
            self.emit("diagnostic", {"ticket": job["request"], "operation": job["operation"],
                                     "success": False, "error": {"code": code}})
        else:
            self.emit("summary_state", {"conversation_id": job["conversation"], "busy": False})
            if not task.cancelled():
                self.emit("error", {"code": code, "operation": "summarize"})
        self._publish()

    def _command_diagnose(self, connection_id: str, operation: str, model_id: str = "", ticket: str = "") -> None:
        connection = self.connections[connection_id]
        model = self.models.get(model_id)
        task = asyncio.create_task(self._diagnose(connection, model, operation, ticket))
        self.tasks["diagnostic:" + ticket] = (task, {"conversation": "", "request": ticket, "operation": operation})
        task.add_done_callback(lambda completed: self._aux_finished("diagnostic:" + ticket, completed))

    async def _diagnose(self, connection, model, operation, ticket):
        try:
            async with self.request.slots:
                if operation in ("models", "authentication"):
                    models = await self.request.models(connection)
                    result = {"models": models} if operation == "models" else {}
                else:
                    if model is None:
                        raise ValueError("AI_MODEL_MISSING")
                    context = {"instructions": "Reply briefly.", "messages": [
                        {"id": "diagnostic", "role": "user", "content": "Reply OK."}]}
                    result = await self.request.request(connection, build_payload(connection, model, context, mode="chat"),
                                                        lambda _kind, _delta: None)
                self.emit("diagnostic", {"ticket": ticket, "operation": operation, "connection_id": connection.id,
                                         "success": True, **result})
        except asyncio.CancelledError:
            self.emit("diagnostic", {"ticket": ticket, "operation": operation, "success": False,
                                     "error": {"code": "AI_CANCELLED"}})
        except Exception as exc:
            error = exc.public() if isinstance(exc, AssistantRequestError) else {"code": _error_code(exc, "AI_REQUEST_FAILED", "diagnose")}
            self.emit("diagnostic", {"ticket": ticket, "operation": operation, "connection_id": connection.id,
                                     "success": False, "error": error})
        finally:
            self.tasks.pop("diagnostic:" + ticket, None)

    def _command_cancel_diagnostic(self, ticket: str) -> None:
        entry = self.tasks.get("diagnostic:" + ticket)
        if entry:
            entry[0].cancel()

    def _command_export(self, identity: str, path: str, include_changes: bool = True, include_images: bool = False,
                        labels: tuple = ("用户", "助手"), change_texts: dict | None = None,
                        variant_texts: dict | None = None, image_names: dict | None = None) -> None:
        record = self.records[identity]
        lines = ["# " + (record["title"] or record["photo_name"]), ""]
        for message in message_path(record):
            lines.extend(["**" + (labels[0] if message["role"] == "user" else labels[1]) + "**", "", message["text"], ""])
            if (variant_texts or {}).get(message["id"]):
                lines.extend([variant_texts[message["id"]], ""])
            if include_changes and message.get("action"):
                lines.extend([(change_texts or {}).get(message["id"], ""), ""])
            if include_images:
                for identity in message.get("attachments", ()):
                    reference = message.get("reference") or {}
                    if not self.store.attachment_present(identity):
                        continue
                    data = self.store.attachment(identity)
                    label = reference.get("name", "照片预览") if identity == reference.get("attachment") else "照片预览"
                    label = (image_names or {}).get(message["id"], {}).get(identity, label)
                    label = label.replace("[", "").replace("]", "").replace("\n", " ")
                    lines.extend(["![" + label + "](data:image/jpeg;base64," + base64.b64encode(data).decode("ascii") + ")", ""])
        target = Path(path)
        temporary = target.with_name(target.name + "." + new_id() + ".tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8")
        os.replace(temporary, target)
        self.emit("notice", {"key": "assistant.exported"})

    async def close(self) -> None:
        self.closing = True
        await self.style_tasks.close()
        await self.variants.close()
        references = [task for _ticket, task in self.reference_tasks.values()]
        for task in references:
            task.cancel()
        if references:
            await asyncio.gather(*references, return_exceptions=True)
        for run in tuple(self.runs.values()):
            if run.active:
                self._finish_run(run, "interrupted")
        tasks = [task for task, _ in self.tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.operations:
            await asyncio.gather(*tuple(self.operations.values()), return_exceptions=True)
        await self.request.close()
        self.store.close()
