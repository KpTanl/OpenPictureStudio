from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

from ..core.contracts.workflow import WorkflowGraph
from ..core.tools.color import curve_channels, mixer_values
from .assistant_models import new_id
from .assistant_tools import TOOLS, changed_only, current_values
from .edit_changes import parameter_changes
from .photo_analysis import analysis_summary, reference_difference
from .assistant_images import jpeg_information


def source_matches(source, photo):
    return bool(photo and source["object_key"] == photo.object_key and source["source_hash"] == photo.source_hash
                and source["revision_id"] == photo.revision_id and source["workflow_hash"] == photo.workflow.workflow_hash())


def changes_between(before, after):
    return [{"tool": TOOLS[tool].tool_id, "key": change.key, "before": change.before, "after": change.after}
            for tool in ("color", "beauty")
            for change in parameter_changes(current_values(before, tool), current_values(after, tool), {})]


def variant_groups(record):
    from .assistant_conversations import message_path
    return [group for message in message_path(record) for group in message.get("variants", ())]


def variant_summary(groups):
    return [{"id": group["id"], "number": group["number"], "status": group["status"],
             "derived_from": group["derived_from"], "purpose": group.get("purpose"), "variants": {
                 label: {"description": item["description"], "values": {
                     tool: current_values(SimpleNamespace(color=item["color"], beauty=item["beauty"]), tool) for tool in TOOLS}}
                 for label, item in group["variants"].items()}} for group in groups]


class AssistantVariants:
    def __init__(self, service):
        self.service = service
        self.adoptions = {}
        self.images = {}

    def find(self, identity, group_id):
        group = next((group for group in variant_groups(self.service.records[identity]) if group["id"] == group_id), None)
        if group is None:
            raise ValueError("AI_VARIANT_MISSING")
        return group

    def _stored(self, identity, group):
        message = self.service.records[identity]["messages"][group["message_id"]]
        return next(item for item in message["variants"] if item["id"] == group["id"])

    def end(self, identity, state="ended", *, run_id=None):
        record = self.service.records.get(identity)
        if not record:
            return
        changed = False
        for message in record["messages"].values():
            for group in message.get("variants", ()):
                if group["status"] in ("pending", "restored") and (run_id is None or group["run_id"] == run_id):
                    group["status"] = state
                    changed = True
        pending = self.adoptions.get(identity)
        if pending:
            pending[1].set()
        if changed:
            self.service._save(record, touch=False)

    def feedback(self, run, text, message_id):
        groups = variant_groups(self.service.records[run.conversation_id])
        if groups and groups[-1]["status"] in ("pending", "restored"):
            groups[-1]["feedback"].append({"text": text, "message_id": message_id, "selection": "unselected",
                                           "dimensions": [], "origin": "user_message"})

    async def propose(self, job, values):
        service = self.service
        run = service.runs[job["run_id"]]
        attempts = run.runtime_state.setdefault("variant_attempts", {})
        attempts[run.goal_version] = attempts.get(run.goal_version, 0) + 1
        if attempts[run.goal_version] > 2:
            raise ValueError("AI_VARIANT_RETRY_LIMIT")
        source, base = run.photo, run.photo
        if "base" in values:
            origin = self.find(run.conversation_id, values["base"]["group"])
            target = origin["variants"][values["base"]["variant"]]
            if not source_matches(origin["source"], source) and target["workflow"] != source.workflow.to_dict():
                raise ValueError("AI_VARIANT_STALE")
            base = replace(source, workflow=WorkflowGraph.from_dict(target["workflow"]),
                           color=copy.deepcopy(target["color"]), beauty=copy.deepcopy(target["beauty"]))
        adjustments = []
        for label in ("A", "B"):
            supplied = {tool: TOOLS[tool].normalize(values[label][tool]) for tool in TOOLS if tool in values[label]}
            if supplied.get("beauty") and (source.tools.get("beauty", "disabled") or run.faces is False):
                raise ValueError("AI_TOOL_UNAVAILABLE")
            changed = changed_only(base, supplied)
            if not changed:
                raise ValueError("AI_VARIANT_EMPTY")
            adjustments.append(changed)
        states = [json.dumps({tool: current_values(base, tool, item.get(tool)) for tool in TOOLS}, sort_keys=True)
                  for item in adjustments]
        if states[0] == states[1]:
            raise ValueError("AI_VARIANTS_IDENTICAL")
        feedback = values.get("feedback")
        if feedback:
            group = self.find(run.conversation_id, feedback["group"])
            raw = service.records[run.conversation_id]["messages"][job["user"]]["text"]
            row = {**copy.deepcopy(feedback), "text": raw, "message_id": job["user"], "origin": "model_annotation"}
            previous = next((item for item in group["feedback"] if item.get("message_id") == job["user"]
                             and item.get("origin") == "model_annotation"), None)
            if previous is not None:
                previous.update(row)
            else:
                group["feedback"].append(row)
        future = asyncio.get_running_loop().create_future()
        proposal = {**job, "kind": "variants", "photo": source, "base": base, "adjustments": adjustments,
                    "variant_values": copy.deepcopy(values), "write_future": future}
        service.proposals[job["message"]] = proposal
        service.emit("variants_proposal", {"conversation_id": job["conversation"], "message_id": job["message"],
            "photo": source, "base": base, "adjustments": adjustments, "goal_version": run.goal_version})
        return await future

    async def generate(self, job, workflows):
        service, result = self.service, None
        run = service.runs[job["run_id"]]
        future = job["write_future"]
        try:
            result = await service.variant_provider("generate", job["photo"], base=job["base"],
                adjustments=job["adjustments"], workflows=workflows, model=run.model.model, regions=run.regions,
                is_cancelled=lambda: not run.active or run.goal_version != job["goal_version"])
            if not run.active or run.goal_version != job["goal_version"]:
                raise ValueError("AI_AGENT_REDIRECT")
            ids = service.store.save_attachments([result["before"], *(result["variants"][key]["preview"] for key in ("A", "B"))])
            groups = variant_groups(service.records[run.conversation_id])
            group = {"id": new_id(), "number": len(groups) + 1, "status": "pending", "run_id": run.id,
                     "message_id": job["message"], "source": job["photo"].identity(),
                     "goal_version": job["goal_version"],
                     "purpose": job["variant_values"].get("purpose"),
                     "baseline": job["base"].workflow.to_dict(), "derived_from": job["variant_values"].get("base"),
                     "before_attachment": ids[0], "reference_attachment": run.reference["attachment"] if run.reference else "",
                     "variants": result["variants"], "feedback": [], "receipt": None, "model": run.model.model}
            output = {"group": group["id"], "number": group["number"], "status": "waiting_for_user", "variants": {},
                      "image_order": ["A", "B"] if run.sends_image else [], "images": []}
            for label, attachment in zip(("A", "B"), ids[1:]):
                item = group["variants"][label]
                item["description"] = job["variant_values"][label]["description"]
                item["attachment"] = attachment
                if run.sends_image:
                    output["images"].append({**jpeg_information(item["preview"]), "source": "result", "quality": "preview",
                        "variant_group": group["id"], "variant": label, "role": "pair " + str(group["number"]) + " " + label,
                        "group": "variant:" + group["id"]})
                item.pop("preview")
                output["variants"][label] = {"description": item["description"], "changes": item["changes"],
                    "editor_changes": item["editor_changes"], "values": {
                        tool: current_values(replace(job["base"], color=item["color"], beauty=item["beauty"]), tool) for tool in TOOLS}}
                if run.preferences["context"].get("analysis", True):
                    output["variants"][label]["analysis"] = analysis_summary(item["analysis"])
                    if run.reference:
                        output["variants"][label]["reference_difference"] = reference_difference(item["analysis"], run.reference["analysis"])
            record = service.records[run.conversation_id]
            previous = [(old, old["status"]) for old in groups if old["status"] in ("pending", "restored")]
            for old, _status in previous:
                old["status"] = "ended"
            message = record["messages"][job["message"]]
            previous_attachments = list(message["attachments"])
            message.setdefault("variants", []).append(group)
            message["attachments"] = list(dict.fromkeys(message["attachments"] + ids))
            try:
                service._save(record)
            except Exception:
                message["variants"].remove(group)
                message["attachments"] = previous_attachments
                for old, status in previous:
                    old["status"] = status
                raise
            service.emit("variants_ready", {"conversation_id": run.conversation_id, "group": group["id"]})
            images = [service.store.attachment(identity) for identity in ids[1:]] if run.sends_image else []
            if not future.done():
                future.set_result((output, images))
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
            if not future.done():
                future.set_exception(exc)
        finally:
            service.operations.pop(run.id, None)
            service._release_run(run.id)
            service._publish()

    def adopt(self, identity, group_id, label, photo):
        service = self.service
        group = self.find(identity, group_id)
        if label not in ("A", "B") or identity in self.adoptions or group["status"] not in ("pending", "restored"):
            raise ValueError("AI_VARIANT_STALE")
        if not source_matches(group["source"], photo):
            group["status"] = "invalid"
            service._save(service.records[identity])
            service._publish()
            raise ValueError("AI_VARIANT_STALE")
        previous_status = group["status"]
        group["status"] = "adopting"
        try:
            service._save(service.records[identity], touch=False)
            service._cancel_conversation(identity)
        except (OSError, sqlite3.Error):
            group["status"] = previous_status
            raise
        cancelled = Event()
        task = asyncio.create_task(self._adopt(identity, group, label, photo, cancelled))
        self.adoptions[identity] = (task, cancelled)
        service._publish()

    async def _adopt(self, identity, group, label, photo, cancelled):
        service = self.service
        try:
            pending = [task for key, task in service.operations.items()
                       if service.runs.get(key) and service.runs[key].conversation_id == identity]
            pending += [task for task, job in service.tasks.values() if job["conversation"] == identity]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            item = group["variants"][label]
            target = replace(photo, color=item["color"], beauty=item["beauty"])
            values = {tool: current_values(target, tool) for tool in TOOLS}
            values["color"].update(curves=curve_channels(target.color.get("curves")),
                                   color_mixer=mixer_values(target.color.get("color_mixer")))
            adjustments = changed_only(photo, values)
            result = await service.variant_provider("adopt", photo, workflow=WorkflowGraph.from_dict(item["workflow"]),
                adjustments=adjustments, label="ai.variant." + label, conversation_id=identity,
                message_id=group["message_id"], request_id=new_id(), model=group["model"], is_cancelled=cancelled.is_set,
                purpose=group.get("purpose"))
            group = self._stored(identity, group)
            group["status"] = "adopted_" + label
            group["feedback"].append({"selection": label, "text": "", "origin": "adopt_button", "dimensions": []})
            action = {"outcome": "applied", "photo": photo.identity(), "before_revision": photo.revision_id,
                      "before_workflow_hash": photo.workflow.workflow_hash(), "after_revision": result["revision_id"],
                      "after_workflow_hash": result["after_workflow_hash"], "changes": result["changes"],
                      "adjustments": adjustments, "purpose": group.get("purpose")}
            group["receipt"] = copy.deepcopy(action)
            message = service.records[identity]["messages"][group["message_id"]]
            message["action"] = action
            message.setdefault("writes", []).append({key: copy.deepcopy(action[key]) for key in (
                "purpose", "before_revision", "before_workflow_hash", "after_revision", "after_workflow_hash")})
            if message.get("run"):
                message["run"].update(reason="completed", receipt=action, latest_message=message["id"],
                                      changes=result["changes"], applied_rounds=1)
            error = result.get("error")
            try:
                service._save(service.records[identity])
            except (OSError, sqlite3.Error):
                error = "AI_STORE_FAILED"
            service.emit("variant_applied", {"conversation_id": identity, "message_id": message["id"],
                "photo": result["context"], "image": result["image"], "error": {"code": error} if error else None})
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
            group = self._stored(identity, group)
            group["status"] = "invalid"
            service.emit("variant_apply_failed", {"conversation_id": identity, "error": {"code": str(exc) if isinstance(exc, ValueError) else "AI_REQUEST_FAILED"}})
        finally:
            self.adoptions.pop(identity, None)
            try:
                service._save(service.records[identity], touch=False)
            except (OSError, sqlite3.Error):
                service.emit("error", {"code": "AI_STORE_FAILED", "operation": "adopt_variant"})
            service._publish()

    def load_images(self, identity, group_id, ticket):
        previous = self.images.pop(identity, None)
        if previous:
            previous.cancel()
        self.images[identity] = asyncio.create_task(self._images(identity, group_id, ticket))

    async def _images(self, identity, group_id, ticket):
        try:
            group = self.find(identity, group_id)
            ids = {label: item["attachment"] for label, item in group["variants"].items()}
            ids.update(before=group["before_attachment"], reference=group["reference_attachment"])
            raw = {label: self.service.store.attachment(key) for label, key in ids.items()
                   if key and self.service.store.attachment_present(key)}
            images = await self.service.variant_provider("images", None, images=raw)
            self.service.emit("variant_images", {"conversation_id": identity, "group": group_id, "ticket": ticket, "images": images})
        except asyncio.CancelledError:
            pass
        except (ValueError, OSError, sqlite3.Error):
            self.service.emit("variant_images", {"conversation_id": identity, "group": group_id, "ticket": ticket,
                                                  "images": {}, "error": {"code": "AI_IMAGE_UNAVAILABLE"}})
        finally:
            if self.images.get(identity) is asyncio.current_task():
                self.images.pop(identity, None)

    async def close(self):
        images = tuple(self.images.values())
        for task in images:
            task.cancel()
        if images:
            await asyncio.gather(*images, return_exceptions=True)
        for _task, cancelled in self.adoptions.values():
            cancelled.set()
        if self.adoptions:
            await asyncio.gather(*(task for task, _cancelled in self.adoptions.values()), return_exceptions=True)
