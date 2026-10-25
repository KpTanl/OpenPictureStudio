from __future__ import annotations

import asyncio
import copy
import json
import sqlite3

from .assistant_conversations import _error_code, message_path
from .assistant_context import available_tools, estimate_tokens, safe_parameters
from .assistant_protocols import build_payload, request_token_estimate, tool_result_items
from .assistant_request import AssistantRequestError
from .assistant_runs import AGENT_MAX_CALLS
from .assistant_tools import AGENT_TOOLS, agent_arguments, agent_functions, changed_only, current_values, ToolArgumentError
from .assistant_references import reference_context
from .photo_analysis import analysis_summary, reference_difference
from .assistant_variants import variant_groups, variant_summary
from .assistant_images import ImageObservationError, constrain_images, described_image, image_capability
from .assistant_observation import ObservationRequest, ObservationSource, ObservationRegion, frame_descriptor
from ..core.contracts.workflow import WorkflowGraph


class AssistantAgentRuntime:
    def __init__(self, service, job):
        self.service, self.job = service, job
        self.run = service.runs[job["run_id"]]
        self.state = self.run.runtime_state

    def _remember_frames(self, frames):
        expected = {"current": self.run.photo.workflow.workflow_hash() if self.run.photo else "",
                    "start": self.run.start.workflow.workflow_hash() if self.run.start else "",
                    "original": WorkflowGraph().workflow_hash()}
        self.run.observation_frames = {key: frame for key, frame in self.run.observation_frames.items()
                                       if frame["goal_version"] == self.run.goal_version
                                       and frame["workflow_hash"] == expected[frame["source"]]}
        for frame in frames:
            if frame["goal_version"] != self.run.goal_version:
                continue
            previous = self.run.observation_frames.get(frame["frame_id"])
            if previous and not frame["face_regions"]:
                frame = {**frame, "face_regions": previous["face_regions"]}
            self.run.observation_frames[frame["frame_id"]] = frame

    def _context_projection(self):
        return {key: value for key, value in self.state["context"].items()
                if key not in ("instructions", "messages", "agent_items", "functions")}

    async def _image_memory(self, size=None):
        if self.service.image_memory_provider is not None:
            version = self.run.goal_version
            try:
                self.run.image_memory = await self.service.image_memory_provider(self.run.image_memory,
                    (self.state["context"]["image_budget"]["bytes"] if size is None else size) * 6,
                    is_cancelled=lambda: self.run.cancelled.is_set() or self.run.goal_version != version)
            except ValueError:
                if self.run.cancelled.is_set():
                    raise asyncio.CancelledError() from None
                if self.run.goal_version != version:
                    return False
                raise
        return True

    def _message(self):
        return self.service.records[self.job["conversation"]]["messages"][self.job["message"]]

    def _publish(self):
        self.service._save(self.service.records[self.job["conversation"]], touch=False)
        self.service._publish()

    def _updates(self):
        if not self.run.updates:
            return
        for update in self.run.updates:
            self.state["context"]["agent_items"].append({"role": "user", "content": update["text"]
                + "\nGoal version " + str(update["goal_version"]) + ". Current reference attachment: "
                + (update["reference"] or "none") + ". Earlier reference targets are superseded."})
            self.job.update(user=update["user"], message=update["message"])
            if self.run.batch_context:
                self.state["context"]["agent_items"].append({"role": "user", "content":
                    "Updated fixed batch scope (host data): " + json.dumps(self.run.batch_context)})
            if update.get("selected_style"):
                self.state["context"]["agent_items"].append({"role": "user", "content":
                    "User-selected style and sample (data): " + json.dumps(update["selected_style"])})
        self.run.updates.clear()
        self.job["goal_version"] = self.run.goal_version
        context = self.state["context"]
        data = reference_context(self.run.reference, analysis=self.run.preferences["context"].get("analysis", True),
                                 image=self.run.sends_image and not (self.run.reference or {}).get("missing"),
                                 goal_version=self.run.goal_version)
        context.update(reference_context=data)
        context["manifest"] = [item for item in context["manifest"] if item["kind"] != "reference"]
        if data:
            context["manifest"].append({"kind": "reference", "attachment": data["attachment"],
                                        "image": data["image_attached"], "analysis": "analysis" in data,
                                        "missing": data["missing"], "goal_version": self.run.goal_version})
        self._reference_images()
        refreshed = []
        for frame in self.run.observation_frames.values():
            source = self.run.start if frame["source"] == "start" else self.run.photo
            workflow = WorkflowGraph() if frame["source"] == "original" else source.workflow
            if frame["workflow_hash"] == workflow.workflow_hash():
                refreshed.append(frame_descriptor(source, workflow, frame["source"], frame["full_size"], frame["geometry"],
                    run_id=self.run.id, goal_version=self.run.goal_version, regions=frame["face_regions"]))
        self.run.observation_frames.clear()
        self._remember_frames(refreshed)
        context["agent_items"].append({"role": "user", "content": "Observed coordinate frames for the updated goal (data): "
                                      + json.dumps(refreshed)})
        self._message()["context"] = self._context_projection()
        self.run.resume()
        self._publish()

    def _reference_images(self):
        data = self.state["context"].get("reference_context")
        self.state["images"].pop("reference", None)
        self.state["context"].setdefault("image_info", {}).pop("reference", None)
        self.state["context"].setdefault("pinned_images", {}).pop("reference", None)
        if data and data["image_attached"]:
            image = self.service.store.attachment(data["attachment"])
            self.state["images"]["reference"] = [image]
            self.state["context"]["image_info"]["reference"] = [described_image(image, self.run.model, source="reference")]
            self.state["context"].setdefault("pinned_images", {})["reference"] = [0]

    async def _prepare(self):
        job, run, service = self.job, self.run, self.service
        materials = await service._materials(job, job["cancel_event"])
        attached = materials.get("images") or ([materials["image"]] if materials.get("image") else [])
        baseline = list(materials.get("baseline", attached))
        saved = service.store.save_attachments([*attached, *baseline])
        attachments = saved[:len(attached)]
        record = service.records[job["conversation"]]
        if attached:
            previous = record["messages"][job["user"]].get("reference")
            record["messages"][job["user"]]["attachments"] = list(dict.fromkeys(
                ([previous["attachment"]] if previous else []) + attachments))
        record["messages"][job["user"]]["analysis"] = materials.get("analysis")
        run.faces = materials.get("faces")
        run.regions = tuple(materials.get("regions", ()))
        run.baseline_analysis = materials.get("analysis")
        run.baseline = saved[len(attached):]
        record["messages"][job["user"]]["baseline_attachments"] = list(run.baseline)
        frame_ids = {}
        frames = []
        for frame in materials.get("frames", ()):
            workflow = WorkflowGraph() if frame["source"] == "original" else run.start.workflow
            refreshed = frame_descriptor(run.start, workflow, frame["source"], frame["full_size"], frame["geometry"],
                run_id=run.id, goal_version=run.goal_version, regions=frame["face_regions"])
            frame_ids[frame["frame_id"]] = refreshed["frame_id"]
            frames.append(refreshed)
        initial = next((frame for frame in frames if frame["source"] == "current"), None)
        if initial:
            frames = [*frames, frame_descriptor(run.start, run.start.workflow, "start", initial["full_size"], initial["geometry"],
                      run_id=run.id, goal_version=run.goal_version, regions=initial["face_regions"])]
        self._remember_frames(frames)
        attached_info = [described_image(image, run.model, **(materials.get("image_info", [{}] * len(attached))[index]))
                         for index, image in enumerate(attached)]
        for info in attached_info:
            if info.get("frame_id") in frame_ids:
                info["frame_id"] = frame_ids[info["frame_id"]]
        user = record["messages"][job["user"]]
        user["image_info"] = dict(zip(attachments, attached_info))
        user["image_evidence"] = [{"attachment": key, **info} for key, info in zip(attachments, attached_info)]
        user["observation_frames"] = frames
        if saved:
            service._save(record, touch=False)
        context = service.context_builder.build(
            message_path(record, job["user"]), job["preferences"], job["model"], job["photo"],
            language=job["language"], mode=job["preferences"]["mode"], summary=record.get("summary"),
            image_attached=bool(attached), image_count=len(attached), analysis=materials.get("analysis"), faces=run.faces,
            reference=run.reference, variants=variant_summary(variant_groups(record)[-3:])
            if job["preferences"]["context"]["history"] else (), long_term_preferences=service.styles.preferences(),
            image_info=attached_info, observations=frames)
        context["agent_items"] = []
        if run.batch_context:
            text = ("Fixed batch scope (host data): " + json.dumps(run.batch_context) +
                    ". Use get_batch_context for actual capabilities. Batch mode: " + str(run.batch_mode) +
                    ". In batch mode use shared batch tools for writes. Preserve this scope while browsing. "
                    "Only explicit new user messages authorize a new relative exposure operation. "
                    "The sample_photo snapshot and the style reference attachment are separate. "
                    "Observe only requested samples; do not send the whole batch. "
                    "Batch history is available only during this app run; report draft save failures separately.")
            context["agent_items"].append({"role": "user", "content": text})
            context["estimated_tokens"] += estimate_tokens(text)
        if run.runtime_state.get("batch_retry_only"):
            text = ("This is a retry/continuation of existing batch operations. Do not start a new batch or relative exposure. "
                    "Use retry_batch with these exact existing batch references, or report expiration: " +
                    json.dumps([item["batch"] for item in run.batch_receipts]))
            context["agent_items"].append({"role": "user", "content": text})
            context["estimated_tokens"] += estimate_tokens(text)
        images = {job["user"]: attached} if attached else {}
        context["image_info"] = {job["user"]: attached_info} if attached else {}
        context["pinned_images"] = {}
        for item in context["messages"][:-1]:
            if item["attachments"] and run.sends_image:
                saved = item["image_info"]
                missing_groups = {entry["group"] for entry in saved
                                  if entry.get("group") and not service.store.attachment_present(entry["attachment"])}
                saved = [entry for entry in saved if entry.get("group") not in missing_groups
                         and service.store.attachment_present(entry["attachment"])]
                item["attachments"] = [entry["attachment"] for entry in saved]
                item["image_roles"] = [entry["role"] for entry in saved]
                original = record["messages"][item["id"]]
                information = [{**entry, **service.store.attachment_information(entry["attachment"])} for entry in saved]
                item["image_info"] = information
                images[item["id"]] = [None] * len(saved)
                context["image_info"][item["id"]] = information
                original.setdefault("image_info", {}).update({info["attachment"]: info for info in information})
        if run.sends_image and baseline:
            if attached and baseline[0] == attached[0] and job["preferences"]["context"]["image_source"] == "current":
                context["pinned_images"][job["user"]] = [0]
            else:
                start_frame = next((frame for frame in frames if frame["source"] == "start"), {})
                images["start"] = [baseline[0]]
                context["image_info"]["start"] = [described_image(baseline[0], run.model, source="start",
                    quality="preview", frame_id=start_frame.get("frame_id", ""))]
                context["pinned_images"]["start"] = [0]
                context["messages"].append({"role": "user", "id": "start", "content": "Fixed task-start image evidence (data)."})
        evaluation_context = "\nIndependent evaluation configuration (data): " + json.dumps(service.evaluations.configuration())
        context["instructions"] += evaluation_context
        context["estimated_tokens"] += estimate_tokens(evaluation_context)
        self.state.update(context=context, images=images, pending_evidence=[])
        self._reference_images()
        self._budget()
        while not await self._image_memory():
            self._updates()
            self._budget()
        for owner, attached in images.items():
            for index, image in enumerate(attached):
                if image is None:
                    attached[index] = service.store.attachment(context["image_info"][owner][index]["attachment"])
        self._message()["context"] = self._context_projection()
        self._publish()

    def _prepare_budget(self, output=()):
        context = copy.deepcopy(self.state["context"])
        images = {owner: list(attached) for owner, attached in self.state["images"].items()}
        evidence = copy.deepcopy(list(self.state.get("pending_evidence", ())))
        if output:
            context["agent_items"].append(output[0])
            evidence.extend(output[1:])
        context["protected_tool_groups"] = list(dict.fromkeys([
            *context.get("protected_tool_groups", ()), *(item["_ops_group"] for item in evidence)]))
        context["estimated_tokens"] = constrain_images(context, images, self.run.model,
            lambda: request_token_estimate(self.job["connection"], self.run.model, context, images, evidence=evidence),
            evidence=evidence)
        return context, images, evidence

    def _accept_budget(self, prepared):
        context, images, evidence = prepared
        self.state["context"].clear()
        self.state["context"].update(context)
        self.state["images"].clear()
        self.state["images"].update(images)
        self.state["pending_evidence"][:] = evidence

    async def _append_output(self, call, result, images, *, attachment_ids=()):
        images = list(images) if not attachment_ids else [None] * len(attachment_ids)
        information = []
        for index, image in enumerate(images):
            previous = result.get("images", ())
            info = {"source": "result", **(previous[index] if previous else {})}
            if info.get("call_id") != call["id"]:
                info.update(call_id=call["id"], group=call["id"] + ":" + str(info.get("group", index)))
            if image is None:
                info.update(self.service.store.attachment_information(attachment_ids[index]))
                information.append(info)
            else:
                information.append(described_image(image, self.run.model, **info))
        result = {**result, "images": information} if images else result
        options = {"information": information, "detail": self.run.model.image_detail,
                   "evidence_id": call["id"] + ":" + str(len(self.state["context"]["agent_items"]))}
        output = tool_result_items(self.job["connection"].protocol, call["id"], result, images, **options, encode=False)
        prepared = self._prepare_budget(output)
        if images:
            required = max(prepared[0]["image_budget"]["bytes"], self.state["context"]["image_budget"]["bytes"])
            if not await self._image_memory(required):
                raise ImageObservationError("AI_AGENT_REDIRECT")
            if not self.run.active:
                raise ImageObservationError("AI_CANCELLED")
            if self.job["goal_version"] != self.run.goal_version:
                raise ImageObservationError("AI_AGENT_REDIRECT")
            if attachment_ids:
                images = [self.service.store.attachment(identity) for identity in attachment_ids]
            attachments = list(attachment_ids) if attachment_ids else self.service.store.save_attachments(images)
            encoded = tool_result_items(self.job["connection"].protocol, call["id"], result, images, **options)
            encoded_groups = {item["_ops_group"]: item for item in encoded[1:]}
            prepared[2][:] = [encoded_groups.get(item.get("_ops_group"), item) for item in prepared[2]]
        else:
            attachments = []
        self._accept_budget(prepared)
        if images:
            message = self._message()
            message["attachments"] = list(dict.fromkeys(message["attachments"] + attachments))
            message.setdefault("image_info", {}).update(dict(zip(attachments, information)))
            message.setdefault("image_evidence", []).extend(
                {"attachment": identity, **info} for identity, info in zip(attachments, information))
        return result, attachments

    async def _deliver_output(self, call, result, images=(), *, attachment_ids=()):
        try:
            return await self._append_output(call, result, images, attachment_ids=attachment_ids)
        except ValueError as exc:
            error = {"code": str(exc), **getattr(exc, "details", {})}
            fallback = ({**result, "images": [], "image_error": error} if result.get("changed")
                        else {"ok": False, "error": error})
            return await self._append_output(call, fallback, ())

    def _append_evidence(self, evidence):
        self.state["context"]["agent_items"].extend(evidence)
        evidence.clear()

    def _budget(self):
        prepared = self._prepare_budget()
        self._accept_budget(prepared)
        return prepared[0]["estimated_tokens"]

    async def run_task(self):
        job, run, service = self.job, self.run, self.service
        job["started"] = True
        job["cancel_event"] = asyncio.Event()
        job["goal_version"] = run.goal_version
        try:
            lock = service.conversation_locks.setdefault(job["conversation"], asyncio.Lock())
            async with lock:
                if not self.state:
                    await self._prepare()
                while run.active:
                    self._updates()
                    if run.limit_reason():
                        service._finish_run(run, "time_limit")
                        break
                    available, _reasons = available_tools(run.photo, run.faces)
                    context = self.state["context"]
                    functions = agent_functions(job["preferences"]["mode"], run.photo, available,
                                                            enabled=run.model.tool_calling,
                                                            reference=bool(run.reference and run.preferences["context"].get("analysis", True)))
                    context["estimated_tokens"] += estimate_tokens(functions) - estimate_tokens(context["functions"])
                    context["functions"] = functions
                    estimated = self._budget()
                    if not await self._image_memory():
                        continue
                    message = self._message()
                    message["context"] = self._context_projection()
                    message.update(raw="", text="", reasoning_summary="", status="waiting")
                    run.phase = "waiting"
                    self._publish()
                    payload = build_payload(job["connection"], run.model, context, mode=job["preferences"]["mode"],
                                            effort=job["preferences"]["effort"], images=self.state["images"])
                    service._record_request("agent.request", job, send_image=run.sends_image)
                    try:
                        async def send():
                            async with service.request.slots:
                                if not run.active or job["goal_version"] != run.goal_version:
                                    raise asyncio.CancelledError()
                                return await service.request.request(
                                    job["connection"], payload, lambda kind, delta: service._stream_event(job, kind, delta))
                        run.request_task = asyncio.create_task(send())
                        result = await run.request_task
                    except asyncio.CancelledError:
                        if run.active and job["goal_version"] != run.goal_version:
                            continue
                        raise
                    finally:
                        run.request_task = None
                        payload = None
                    if not run.active:
                        break
                    context["protected_tool_groups"] = []
                    run.add_usage(result["usage"], estimated,
                                  estimate_tokens(result["text"] + result["summary"] + json.dumps(result["tool_calls"])))
                    if job["goal_version"] != run.goal_version:
                        continue
                    if result["tool_calls"] and not run.model.tool_calling:
                        raise ValueError("AI_TOOL_UNAVAILABLE")
                    message = self._message()
                    message.update(raw=result["text"], text=result["text"], usage=result["usage"],
                                   reasoning_summary=result["summary"], status="completed",
                                   protocol_items=result["protocol_items"])
                    run.last_response = job["message"]
                    if not result["tool_calls"]:
                        service._record_request("agent.result", job, success=True, has_adjustments=bool(run.applied_rounds))
                        service._finish_run(run, "completed")
                        break
                    context["agent_items"].extend(result["protocol_items"])
                    evidence = []
                    self.state["pending_evidence"] = evidence
                    waiting = False
                    for call in result["tool_calls"]:
                        if not run.active:
                            break
                        previous = next((item for item in run.calls if item["id"] == call["id"]), None)
                        if previous:
                            if previous["name"] != call["name"] or previous["arguments"] != call["arguments"]:
                                raise ValueError("AI_TOOL_ARGUMENTS")
                            if previous["status"] == "running":
                                raise ValueError("AI_TOOL_INTERRUPTED")
                            if waiting or job["goal_version"] != run.goal_version:
                                await self._deliver_output(call, {"ok": False, "error": {"code": "AI_AGENT_REDIRECT"}})
                            else:
                                await self._deliver_output(call, previous["result"],
                                                           attachment_ids=previous.get("attachments", ()))
                            self._publish()
                            continue
                        if len(run.calls) >= AGENT_MAX_CALLS:
                            service._finish_run(run, "max_calls")
                            break
                        entry = {**call, "status": "running", "result": None, "attachments": [],
                                 "goal_version": run.goal_version, "message_id": job["message"]}
                        run.calls.append(entry)
                        run.phase = "tool_running"
                        self._publish()
                        images = []
                        try:
                            if waiting or job["goal_version"] != run.goal_version:
                                raise ValueError("AI_AGENT_REDIRECT")
                            allowed = {item["name"] for item in context["functions"]}
                            arguments = agent_arguments(call["name"], call["arguments"], allowed)
                            data, images = await self._execute(call["name"], arguments, call_id=call["id"])
                            output = {"ok": True, **data}
                        except asyncio.CancelledError:
                            entry.update(status="interrupted", result={"ok": False, "error": {"code": "AI_CANCELLED"}})
                            raise
                        except ValueError as exc:
                            output = {"ok": False, "error": {"code": str(exc), **getattr(exc, "details", {})}}
                            images = []
                        try:
                            output, attachments = await self._deliver_output(call, output, images)
                        except asyncio.CancelledError:
                            entry.update(status="interrupted", result={"ok": False, "error": {"code": "AI_CANCELLED"}})
                            raise
                        entry["attachments"] = attachments
                        entry["result"] = output
                        code = output.get("error", {}).get("code")
                        entry["status"] = ("completed" if output.get("ok") else "superseded" if code == "AI_AGENT_REDIRECT"
                                           else "interrupted" if code == "AI_CANCELLED" else "failed")
                        waiting = waiting or bool(output.get("ok") and (call["name"] in ("request_input", "propose_variants")
                            or call["name"] == "remember_style" and output["status"] == "confirmation_required"))
                        self._publish()
                    self._append_evidence(evidence)
                    if waiting and run.active and job["goal_version"] == run.goal_version:
                        run.pause()
                        timer = service.run_timers.pop(run.id, None)
                        if timer:
                            timer.cancel()
                        self._message()["status"] = "completed"
                        self._publish()
                        return
                    run.round += 1
        except asyncio.CancelledError:
            job["cancel_event"].set()
            service._finish_error(job, {"code": "AI_CANCELLED"}, "stopped")
        except AssistantRequestError as exc:
            service._finish_error(job, exc.public())
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            service._finish_error(job, {"code": _error_code(exc, "AI_REQUEST_FAILED", "agent"),
                                        **getattr(exc, "details", {})})
        finally:
            service.tasks.pop(job["request"], None)
            service._release_run(run.id)
            service._publish()

    async def _batch_command(self, name, values):
        run, job, service = self.run, self.job, self.service
        if service.batch_provider is None:
            raise ValueError("AI_TOOL_UNAVAILABLE")
        version = run.goal_version
        writing = AGENT_TOOLS[name].writes
        exposure = run.runtime_state.setdefault("batch_exposure", {})
        if name == "adjust_batch_exposure" and version in exposure:
            return {"already_started": True, "batch": exposure[version], "goal_version": version}
        if name in ("sync_batch_adjustments", "adjust_batch_exposure"):
            if run.runtime_state.get("batch_retry_only"):
                return {"retry_existing_batches": [item["batch"] for item in run.batch_receipts], "new_operation": False}
            if not run.batch_context or values["scope"] != run.batch_context.get("scope"):
                raise ToolArgumentError("scope", "use get_batch_context to obtain the host-captured scope")
            run.batch_mode = True
        if name == "adjust_batch_exposure":
            exposure[version] = ""
        if name == "apply_photo_calibration":
            run.batch_mode = True

        def progress(summary):
            if name == "adjust_batch_exposure":
                exposure[version] = summary["batch"]
            record = service.records.get(job["conversation"])
            if record is None:
                return
            receipts = run.batch_receipts
            index = next((index for index, item in enumerate(receipts) if item["batch"] == summary["batch"]), None)
            if index is None:
                receipts.append(summary)
            else:
                receipts[index] = summary
            target = record["messages"].get(run.message_id)
            if target is not None:
                target["run"] = run.snapshot()
            service._save(record, touch=False)
            service._publish()

        try:
            if writing:
                run.phase = "applying"
                self._publish()
            result = await service.batch_provider(name, values, run.batch_context,
                is_cancelled=lambda: run.cancelled.is_set() or run.goal_version != version,
                progress=progress)
            photo = result.pop("_photo", None)
            if photo is not None and run.photo is not None and photo.session_id == run.photo.session_id:
                run.photo = photo
                run.observation_frames = {key: frame for key, frame in run.observation_frames.items()
                    if frame["source"] != "current" or frame["workflow_hash"] == photo.workflow.workflow_hash()}
                result["current_sample"] = {"photo": safe_parameters(photo.identity()),
                    "adjustments": current_values(photo, "color")}
            if result.get("batch"):
                progress(result)
            return result
        except ValueError as error:
            if name == "adjust_batch_exposure" and not exposure.get(version):
                exposure.pop(version, None)
            if str(error) == "AI_CANCELLED":
                raise
            raise ToolArgumentError("batch", str(error)) from None
        finally:
            if writing and run.active:
                run.phase = "preparing"

    async def _export_command(self, name, values):
        run, service = self.run, self.service
        if service.batch_provider is None:
            raise ValueError("AI_TOOL_UNAVAILABLE")
        version = run.goal_version
        queues = run.runtime_state.setdefault("export_queues", {})
        if name == "enqueue_exports":
            if run.runtime_state.get("export_retry_only"):
                return {"retry_existing_exports": [item["export"] for item in run.export_receipts], "new_queue": False}
            if version in queues:
                return {"already_queued": True, "export": queues[version], "new_queue": False}
            if not run.batch_context or values["scope"] != run.batch_context.get("scope"):
                raise ToolArgumentError("scope", "use get_batch_context to obtain the captured scope")
            run.batch_mode = True
        def progress(result):
            if not result.get("export"):
                return
            if name == "enqueue_exports":
                queues[version] = result["export"]
            receipts = run.export_receipts
            index = next((index for index, receipt in enumerate(receipts) if receipt["export"] == result["export"]), None)
            if index is None:
                receipts.append(result)
            else:
                receipts[index] = result
            record = service.records.get(run.conversation_id)
            if record is not None:
                message = record["messages"].get(run.message_id)
                if message is not None:
                    message["run"] = run.snapshot()
                service._save(record, touch=False)
                service._publish()
        try:
            result = await service.batch_provider(name, values, run.batch_context,
                is_cancelled=lambda: run.cancelled.is_set() or run.goal_version != version, progress=progress)
        except ValueError as error:
            if str(error) == "AI_CANCELLED":
                raise
            raise ToolArgumentError("export", str(error)) from None
        if result.get("export") and not any(receipt["export"] == result["export"] for receipt in run.export_receipts):
            progress(result)
        return result

    async def _execute(self, name, values, *, call_id=None):
        run, job, service = self.run, self.job, self.service
        tool = AGENT_TOOLS[name]
        if tool.writes and job["preferences"]["mode"] != "agent":
            raise ValueError("AI_TOOL_UNAVAILABLE")
        from .photo_browser import BROWSER_TOOLS
        from .photo_calibration import CALIBRATION_TOOLS
        from .photo_library import LIBRARY_TOOLS
        if name in LIBRARY_TOOLS:
            version = run.goal_version
            if service.batch_provider is None:
                raise ValueError("AI_TOOL_UNAVAILABLE")
            result = await service.batch_provider(name, {**values, "request": run.id + ":" + (call_id or name)}, run.batch_context,
                is_cancelled=lambda: run.cancelled.is_set() or run.goal_version != version, progress=lambda _result: None)
            return result, []
        if name in CALIBRATION_TOOLS:
            if name == "apply_photo_calibration":
                return await self._batch_command(name, values), []
            version = run.goal_version
            if service.batch_provider is None:
                raise ValueError("AI_TOOL_UNAVAILABLE")
            result = await service.batch_provider(name, values, run.batch_context,
                is_cancelled=lambda: run.cancelled.is_set() or run.goal_version != version, progress=lambda _result: None)
            return result, []
        if name in BROWSER_TOOLS:
            version = run.goal_version
            if service.batch_provider is None:
                raise ValueError("AI_TOOL_UNAVAILABLE")
            result = await service.batch_provider(name, values, run.batch_context,
                is_cancelled=lambda: run.cancelled.is_set() or run.goal_version != version, progress=lambda _result: None)
            return result, []
        if name in ("get_export_context", "enqueue_exports", "get_export_status", "control_exports", "get_export_history", "verify_export", "repair_export_record"):
            return await self._export_command(name, values), []
        if name == "view_batch_photo":
            return await self._view_batch_photo(values)
        if name in ("get_batch_context", "sync_batch_adjustments", "adjust_batch_exposure", "get_batch_status",
                    "retry_batch", "undo_batch", "redo_batch"):
            return await self._batch_command(name, values), []
        if run.batch_mode and name in ("adjust_color", "adjust_beauty", "propose_variants"):
            raise ValueError("AI_TOOL_UNAVAILABLE")
        if tool.photo and run.photo is None:
            raise ValueError("AI_PHOTO_REQUIRED")
        pending = service.operations.get(run.id)
        if tool.photo and pending is not None:
            await asyncio.shield(pending)
            if not run.active:
                raise ValueError("AI_CANCELLED")
            if job["goal_version"] != run.goal_version:
                raise ValueError("AI_AGENT_REDIRECT")
        if name == "inspect_photo":
            return {"photo": safe_parameters(run.photo.identity()), "adjustments": {
                key: current_values(run.photo, key) for key in ("color", "beauty")
                if job["preferences"]["context"].get("color") or job["preferences"]["mode"] == "agent"},
                "capabilities": {"unavailable": dict(run.photo.tools),
                                 "image_effective_long_edge": run.model.effective_image_long_edge,
                                 "image_detail": run.model.image_detail or "service_default",
                                 "image_rule": image_capability(run.model)},
                "observation_frames": list(run.observation_frames.values())}, []
        if name == "inspect_history":
            return {"revision": run.photo.revision_id, "start_revision": run.start.revision_id, "applied_steps": run.applied_rounds,
                    "changes": run.snapshot()["changes"], "receipt": run.receipt,
                    "writes": [write for message in message_path(service.records[job["conversation"]])
                               if message.get("run_id") == run.id for write in message.get("writes", ())],
                    "variant_groups": variant_summary([pair for pair in variant_groups(service.records[job["conversation"]])
                        if job["preferences"]["context"]["history"] or pair["run_id"] == run.id][-32:]),
                    "calls": [{key: item[key] for key in ("id", "name", "status")} for item in run.calls]}, []
        if name == "update_plan":
            run.plan = values["steps"]
            return {"steps": run.plan}, []
        if name == "request_input":
            self._message()["text"] = values["question"]
            return {"question": values["question"], "status": "waiting_for_user"}, []
        if name == "propose_variants":
            return await service.variants.propose(job, values)
        if name == "evaluate_versions":
            return await service.evaluations.compare(job, values), []
        if name == "fit_reference":
            return await service.fit_reference(job, values), []
        if name == "list_styles":
            from .assistant_styles import style_catalog
            return {"styles": style_catalog(service.styles.view_records())}, []
        if name == "remember_style":
            return await service.style_tasks.begin(job["conversation"], job["message"], run.photo, values), []
        if name == "apply_style":
            return await service.apply_style(job, values), []
        if name == "view_photo":
            return await self._view_photo(values)
        if name in ("analyze_photo", "compare_reference"):
            selection = job["preferences"]["context"]
            if name in ("analyze_photo", "compare_reference") and not selection.get("analysis", True):
                raise ValueError("AI_ANALYSIS_DISABLED")
            if name == "compare_reference" and not run.reference:
                raise ValueError("AI_REFERENCE_REQUIRED")
            source = values.get("source")
            if service.material_provider is None:
                raise ValueError("AI_IMAGE_UNAVAILABLE")
            materials = await service.material_provider(run.photo, {
                "image_source": source, "analysis": name != "view_photo",
                "faces": not run.photo.tools.get("beauty", "disabled"), "baseline": False}, job["cancel_event"])
            if not run.active:
                raise ValueError("AI_CANCELLED")
            if job["goal_version"] != run.goal_version:
                raise ValueError("AI_AGENT_REDIRECT")
            run.faces = materials.get("faces")
            run.regions = tuple(materials.get("regions", ()))
            images = []
            self._message()["analysis"] = materials.get("analysis") or self._message().get("analysis")
            if name == "compare_reference":
                return {"comparison": reference_difference(materials["analysis"], run.reference["analysis"]),
                        "goal_version": run.goal_version}, []
            return {"revision": run.photo.revision_id, "source": source,
                    "analysis": analysis_summary(materials.get("analysis")), "faces": run.faces, "image_count": len(images)}, images
        group = "color" if name == "adjust_color" else "beauty"
        # 只拦本任务自己在等用户选择的那一组；更早任务留下的组在编辑器一变时自然失效，不挡新的直接调整
        if any(pair["status"] in ("pending", "restored") and pair["run_id"] == run.id
               for pair in variant_groups(service.records[job["conversation"]])):
            raise ValueError("AI_VARIANT_PENDING")
        available, _reasons = available_tools(run.photo, run.faces)
        if group not in available:
            raise ValueError("AI_TOOL_UNAVAILABLE")
        adjustments = changed_only(run.photo, {group: values["values"]})
        if not adjustments:
            return {"changed": False, "revision": run.photo.revision_id}, []
        previous = self._message().get("action") or {}
        self._message()["action"] = {**previous, "adjustments": adjustments, "outcome": "pending",
                                      "photo": previous.get("photo") if previous.get("after_revision") else run.photo.identity(),
                                      "faces": run.faces,
                                      "changes": previous.get("changes", []),
                                      "before_revision": previous.get("before_revision") or run.photo.revision_id,
                                      "after_revision": previous.get("after_revision", "")}
        future = asyncio.get_running_loop().create_future()
        proposal = {**job, "photo": run.photo, "adjustments": adjustments, "write_future": future,
                    "purpose": values.get("purpose")}
        service.proposals[job["message"]] = proposal
        self._publish()
        service.emit("proposal", {"conversation_id": job["conversation"], "message_id": job["message"],
                                   "request_id": job["request"], "photo": run.photo, "adjustments": adjustments,
                                   "model": run.model.model, "run_id": run.id, "faces": run.faces,
                                   "goal_version": run.goal_version})
        result = await future
        review = result.get("review") or {}
        self._remember_frames(review.get("frames", ()))
        frame = next(iter(review.get("frames", ())), {})
        data = {"changed": True, "revision": result["revision_id"], "changes": result["changes"],
                "purpose": values.get("purpose"),
                "analysis": analysis_summary(review.get("analysis")), "current_adjustments": {
                    key: current_values(run.photo, key) for key in ("color", "beauty")},
                "observation_frames": review.get("frames", []),
                "images": [{**described_image(image, run.model), "source": "current", "quality": "preview",
                            "frame_id": frame.get("frame_id", ""), "region": "whole" if index == 0 else "face"}
                           for index, image in enumerate(review.get("images", []))]}
        if result.get("error"):
            data["operation_error"] = result["error"]
        return data, review.get("images", [])

    async def _view_batch_photo(self, values):
        run, service = self.run, self.service
        if not run.sends_image:
            raise ImageObservationError("AI_IMAGE_DISABLED")
        if service.batch_provider is None or service.observation_provider is None:
            raise ImageObservationError("AI_IMAGE_UNAVAILABLE")
        version = run.goal_version
        cancelled = lambda: run.cancelled.is_set() or run.goal_version != version
        result = await service.batch_provider("batch_photo_context", {"photo": values["photo"]}, run.batch_context,
                                               is_cancelled=cancelled, progress=lambda summary: None)
        photo = result["photo_context"]
        frames = run.runtime_state.setdefault("batch_frames", {})
        regions = tuple(ObservationRegion(item["name"], tuple(item["box"])) for item in values.get("regions", ()))
        if regions:
            frame = frames.get(values["frame_id"])
            if (frame is None or frame["source_hash"] != photo.source_hash
                    or frame["workflow_hash"] != photo.workflow.workflow_hash() or frame["goal_version"] != version):
                raise ImageObservationError("AI_OBSERVATION_FRAME")
        request = ObservationRequest(photo,
            (ObservationSource("current", photo, photo.workflow, (photo.width, photo.height)),),
            regions, values.get("quality", "preview"), run.model, run.id, version)
        data, images = await service.observation_provider(request, is_cancelled=cancelled, progress=lambda stage: None)
        if cancelled():
            raise ImageObservationError("AI_CANCELLED")
        data["photo"] = values["photo"]
        for frame in data.get("frames", ()):
            frames[frame["frame_id"]] = frame
        return data, images

    async def _view_photo(self, values):
        run, service = self.run, self.service
        if not run.sends_image:
            raise ImageObservationError("AI_IMAGE_DISABLED")
        if service.observation_provider is None:
            raise ImageObservationError("AI_IMAGE_UNAVAILABLE")
        source = values["source"]
        sources = {}
        for name, photo, workflow in (("current", run.photo, run.photo.workflow),
                                      ("start", run.start, run.start.workflow),
                                      ("original", run.photo, WorkflowGraph())):
            known = next((frame for frame in reversed(list(run.observation_frames.values()))
                          if frame["source"] == name and frame["workflow_hash"] == workflow.workflow_hash()), None)
            size = tuple(known["full_size"]) if known else (photo.width, photo.height)
            sources[name] = ObservationSource(name, photo, workflow, size)
        regions = tuple(ObservationRegion(region["name"], tuple(region["box"])) for region in values.get("regions", ()))
        version = run.goal_version
        if regions:
            frame = run.observation_frames.get(values["frame_id"])
            if (frame is None or frame["source"] != source or frame["goal_version"] != version
                    or frame["source_hash"] != run.photo.source_hash
                    or frame["workflow_hash"] != sources[source].workflow.workflow_hash()):
                raise ImageObservationError("AI_OBSERVATION_FRAME", source=source,
                                            available_frames=[item for item in run.observation_frames.values()
                                                              if item["source"] == source and item["goal_version"] == version])
        request = ObservationRequest(run.photo, tuple(sources[name] for name in (*values.get("compare_to", ()), source)),
                                     regions, values.get("quality", "preview"), run.model, run.id, version)

        def progress(stage):
            if run.active and run.goal_version == version:
                run.phase = stage
                self._publish()

        try:
            data, images = await service.observation_provider(request,
                is_cancelled=lambda: run.cancelled.is_set() or run.goal_version != version, progress=progress)
        except ValueError:
            if run.active and run.goal_version != version:
                raise ImageObservationError("AI_AGENT_REDIRECT") from None
            if not run.active:
                raise ImageObservationError("AI_CANCELLED") from None
            raise
        if not run.active:
            raise ImageObservationError("AI_CANCELLED")
        if run.goal_version != version:
            raise ImageObservationError("AI_AGENT_REDIRECT")
        self._remember_frames(data["frames"])
        return data, images
