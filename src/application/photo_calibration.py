from __future__ import annotations

import copy
import math
import os
import uuid
from collections import OrderedDict
from concurrent.futures import CancelledError
from dataclasses import dataclass
from threading import Event, RLock

import numpy as np

from ..core.photo_engine import white_balance_gain
from ..core.raw_calibration import effective_raw_matrix
from ..core.tools.color import color_identity, engine_to_ui
from .capture_metadata import CaptureMetadata, read_capture_metadata
from .color_initialization import baseline_color_settings
from .export_store import ExportPreset
from .image_source_service import SourceIdentity
from .output_writer import OperationCancellation
from .photo_batch import BatchItem


CALIBRATION_TOOLS = ("get_photo_groups", "create_photo_group", "update_photo_group", "suggest_photo_groups",
                     "propose_photo_calibration", "get_photo_calibration", "apply_photo_calibration")


class CalibrationCancellation(OperationCancellation):
    def __init__(self, cancelled):
        super().__init__()
        self._host_cancelled = cancelled

    def is_cancelled(self):
        return super().is_cancelled() or self._host_cancelled()


@dataclass
class PhotoGroup:
    id: str
    name: str
    paths: tuple
    sample: object
    sample_path: str
    generation: int = 0
    expired: bool = False


@dataclass(frozen=True)
class CalibrationGuard:
    group: str
    source: SourceIdentity
    session_id: str | None
    state_version: int | None
    workflow_hash: str
    draft_path: str
    draft_version: int
    tools: int
    display: tuple
    cancelled: Event


def _wb_proposal(reference, target, sample_ui):
    ref_matrix, ref_gain = reference
    matrix, gains = target
    anchor = 1 / ref_gain
    anchor /= anchor[1]
    fixed = white_balance_gain(sample_ui["temperature"], sample_ui["tint"]).astype(np.float64)
    w = fixed * (ref_matrix @ (ref_gain * anchor))
    v = matrix @ (gains * anchor)
    if np.any(w <= 0) or np.any(v <= 0) or not np.isfinite(w / v).all():
        raise ValueError("calibration.wb_residual")
    q = w / v
    red, blue = math.log2(q[0] / q[1]), math.log2(q[2] / q[1])
    temperature, tint = 100 * (red - blue) / 2.7, 100 * (red + blue) / 1.6
    if not -100 <= temperature <= 100 or not -100 <= tint <= 100:
        raise ValueError("calibration.wb_range")
    temperature, tint = round(temperature), round(tint)
    proposed = white_balance_gain(temperature, tint).astype(np.float64)
    vectors = np.array([(1, 1, 1), (.5, 1, 1), (1, .5, 1), (1, 1, .5),
                        (1, .5, .5), (.5, 1, .5), (.5, .5, 1)]) * anchor
    before = (vectors * ref_gain) @ ref_matrix.T * fixed
    after = (vectors * gains) @ matrix.T * proposed
    if np.any(before <= 0) or np.any(after <= 0) or not np.isfinite(before).all() or not np.isfinite(after).all():
        raise ValueError("calibration.wb_residual")
    ratios = np.log2(after / before)
    errors = {"anchor_ev": float(np.ptp(ratios[0])), "chroma_ev": float(np.ptp(ratios, axis=1).max()),
              "scale_ev": float(np.abs(ratios.mean(axis=1)).max())}
    if errors["anchor_ev"] > .02 or errors["chroma_ev"] > .03 or errors["scale_ev"] > .04:
        return {}, errors
    return {"temperature": temperature, "tint": tint}, errors


class PhotoCalibrationService:
    def __init__(self, browser, batches, release_snapshot):
        self.browser, self.batches = browser, batches
        self.core, self.sessions = batches.core, batches.sessions
        self.release_snapshot = release_snapshot
        self.groups = OrderedDict()
        self.plans = OrderedDict()
        self._guards = {}
        self._guard_lock = RLock()
        self.busy = False
        batches.calibration_guard = self.batch_current
        batches.calibration_preflight = self.verify_batch_source

    def _group(self, identity):
        group = self.groups.get(identity)
        if group is None or group.expired:
            raise ValueError("calibration.expired")
        return group

    def expire_path(self, path):
        key = self.sessions.key(path)
        expired = set()
        samples = []
        with self._guard_lock:
            for group in self.groups.values():
                if any(self.sessions.key(p) == key for p in group.paths):
                    group.generation += 1
                    group.expired = True
                    expired.add(group.id)
                    samples.append(group.sample)
                    for guard in self._guards.values():
                        if guard.group == group.id:
                            guard.cancelled.set()
        for sample in samples:
            self.release_snapshot(sample)
        for plan in self.plans.values():
            if plan["group"] in expired:
                plan["state"], plan["error"] = "expired", "calibration.expired"
                self._release_rows(plan)

    def _sample(self, path):
        graph, source, _ = self.browser.workflow(path)
        try:
            return self.core.capture_export_workflow(path, graph, source_hash=source)
        except OSError as error:
            raise ValueError("browser.source_failed") from error

    def set_group(self, name, paths, sample_path, *, identity=None):
        if self.busy:
            raise ValueError("batch.busy")
        paths = tuple(dict.fromkeys(paths))
        if not isinstance(name, str) or not name.strip() or len(name) > 120 or sample_path not in paths or len(paths) < 2:
            raise ValueError("calibration.invalid_group")
        group = self._group(identity) if identity is not None else None
        sample = self._sample(sample_path)
        if group is None:
            group = PhotoGroup("group:" + uuid.uuid4().hex, name.strip(), paths, sample, sample_path)
            self.groups[group.id] = group
        else:
            with self._guard_lock:
                for guard in self._guards.values():
                    if guard.group == group.id:
                        guard.cancelled.set()
            self.release_snapshot(group.sample)
            group.name, group.paths, group.sample = name.strip(), paths, sample
            group.sample_path = sample_path
            group.generation += 1
        return self.group_summary(group)

    def group_summary(self, group):
        return {"group": group.id, "name": group.name, "generation": group.generation,
                "expired": group.expired,
                "photos": [{"photo": self.browser.reference(path), "name": os.path.basename(path)} for path in group.paths],
                "sample": {"photo": self.browser.reference(group.sample_path), "name": os.path.basename(group.sample_path),
                           "workflow_hash": group.sample.workflow_hash}, "runtime_only": True}

    def context(self):
        return {"groups": [self.group_summary(group) for group in self.groups.values()],
                "plans": [self.summary(identity, limit=0) for identity in self.plans], "runtime_only": True}

    def capture_plan(self, identity, operations):
        if self.busy:
            raise ValueError("batch.busy")
        operations = tuple(dict.fromkeys(operations))
        if not operations or set(operations) - {"exposure", "white_balance"}:
            raise ValueError("batch.invalid_fields")
        group = self._group(identity)
        rows = []
        try:
            for path in group.paths:
                if self.sessions.key(path) == self.sessions.key(group.sample_path):
                    continue
                item = BatchItem(path, copy.deepcopy(self.sessions.drafts.for_image(os.path.normpath(path))))
                row = {"path": path, "item": item, "base": None, "preview": None,
                       "status": "pending", "details": {}, "updates": {}, "error": ""}
                rows.append(row)
                try:
                    graph, _, version = self.browser.workflow(path)
                    item.before_hash = graph.workflow_hash()
                    session = self.sessions.get(path)
                    if session is not None:
                        state = self.core.get_session(session.core_id).unwrap()
                        item.session_id, item.before_version = session.core_id, version
                        item.before_revision = state.current_revision.revision_id
                        item.source = self.core.source_identity(session.core_id)
                    row["base"] = self._sample(path)
                except (ValueError, OSError, KeyError, RuntimeError) as error:
                    row["status"] = "failed"
                    row["error"] = str(error) if isinstance(error, ValueError) else "browser.source_failed"
            current_sample = self.browser.workflow(group.sample_path)
            sample_session = self.sessions.get(group.sample_path)
            plan = {"id": "calibration:" + uuid.uuid4().hex, "group": group.id, "generation": group.generation,
                    "sample": group.sample, "sample_current": (current_sample[0].workflow_hash(),
                        current_sample[2] if sample_session is not None else None),
                    "sample_session": sample_session.core_id if sample_session is not None else None,
                    "sample_path": group.sample_path,
                    "operations": operations, "rows": rows, "tools": self.core.tools.generation,
                    "raw": self.browser.raw_mode, "image": self.browser.image_mode,
                    "state": "preparing", "batch": "", "error": ""}
            self.plans[plan["id"]] = plan
            self._trim_plans()
            return plan["id"]
        except Exception:
            for row in rows:
                if row["base"] is not None:
                    self.release_snapshot(row["base"])
            raise

    def _release_rows(self, plan):
        for row in plan["rows"]:
            for field in ("base", "preview"):
                if row[field] is not None:
                    self.release_snapshot(row[field])
                    row[field] = None

    def _trim_plans(self, limit=8):
        # 未应用的建议最多保留 limit 个，淘汰最旧的并释放其固定快照；准备中的不动
        waiting = [identity for identity, plan in self.plans.items() if not plan["batch"] and plan["state"] != "preparing"]
        for identity in waiting[:max(0, len(waiting) - limit)]:
            self._release_rows(self.plans.pop(identity))

    def _capture(self, path, cancelled=lambda: False):
        try:
            sources = self.core.image_sources
            with sources.resources.working_memory(16 * 1024 ** 2, cancelled), sources.compute.borrow(cancelled, priority=2, threads=1):
                return read_capture_metadata(path)
        except (OSError, ValueError, SyntaxError, TypeError, KeyError):
            return CaptureMetadata()

    def suggest_groups(self, paths, references, *, minutes=30, confirm_local=False, is_cancelled=lambda: False):
        if type(minutes) is not int or not 1 <= minutes <= 1440 or type(confirm_local) is not bool:
            raise ValueError("batch.invalid_fields")
        buckets, skipped = {}, []
        for path in paths:
            if is_cancelled():
                raise CancelledError()
            data = self._capture(path, is_cancelled)
            reference = references[path]
            if data.captured is None or not data.model or not data.lens:
                skipped.append({"photo": reference, "reason": "calibration.group_metadata"})
                continue
            if data.captured.tzinfo is None and not confirm_local:
                skipped.append({"photo": reference, "reason": "calibration.timezone"})
                continue
            key = (data.make.casefold(), data.model.casefold(), data.lens.casefold(), data.serial,
                   data.captured.tzinfo is None)
            buckets.setdefault(key, []).append((data.captured, path, data))
        groups = []
        def append_group(items, key):
            if len(items) < 2:
                return
            data = items[0][2]
            groups.append({"photos": [references[item[1]] for item in items],
                           "names": [os.path.basename(item[1]) for item in items],
                           "camera": " ".join((data.make, data.model)).strip(), "lens": data.lens,
                           "captured_from": items[0][0].isoformat(), "captured_to": items[-1][0].isoformat(),
                           "same_body_verified": bool(key[3]), "local_time_confirmed": key[4]})
        for key, values in buckets.items():
            current, previous = [], None
            for captured, path, data in sorted(values, key=lambda value: (value[0], value[1])):
                if previous is not None and (captured - previous).total_seconds() > minutes * 60:
                    append_group(current, key)
                    current = []
                current.append((captured, path, data))
                previous = captured
            append_group(current, key)
        return {"groups": groups, "skipped": skipped, "minutes": minutes, "runtime_only": True}

    def _wb(self, snapshot, cancelled):
        sources = self.core.image_sources
        identity = sources.identity(snapshot.path, is_cancelled=cancelled)
        if identity.file_hash != snapshot.source_hash:
            raise ValueError("error.source.changed")
        with sources.borrow_linear(snapshot.path, identity=identity, priority=2, is_cancelled=cancelled) as lease:
            if lease.value.color_pipeline != "libraw":
                raise ValueError("calibration.raw_unsupported")
            with sources.resources.working_memory(16 * 1024 ** 2, cancelled), sources.compute.borrow(cancelled, priority=2, threads=1):
                matrix, gains = effective_raw_matrix(snapshot.path, lease.value.raw_decode_meta)
        if not sources.matches_file(identity) or cancelled():
            raise ValueError("error.source.changed")
        return matrix, gains

    def prepare_plan(self, identity, cancelled):
        plan = self.plans[identity]
        sample = self.core.prepare_export_snapshot(plan["sample"], is_cancelled=cancelled)
        capture = self._capture(sample.path, cancelled)
        sample_settings = baseline_color_settings(sample.workflow)
        sample_ui = engine_to_ui(sample_settings)
        excluded = {"exposure", "brightness", "temperature", "tint"}
        sample_style = {key: value for key, value in sample_settings.items() if key not in excluded}
        sample_wb, wb_error = None, ""
        if "white_balance" in plan["operations"]:
            try:
                sample_wb = self._wb(sample, cancelled)
            except ValueError as error:
                wb_error = str(error)
        rows = []
        for source in plan["rows"]:
            if cancelled():
                for previous in rows:
                    if previous["preview"] is not None:
                        self.core.release_export_snapshot(previous["preview"])
                raise CancelledError()
            row = {**source, "item": copy.deepcopy(source["item"]), "details": {}, "updates": {}}
            if row["base"] is None:
                rows.append(row)
                continue
            preview = None
            try:
                base = self.core.prepare_export_snapshot(row["base"], is_cancelled=cancelled)
                row["base"] = base
                row["item"].captured_source_hash = base.source_hash
                metadata = self._capture(base.path, cancelled)
                settings = baseline_color_settings(base.workflow)
                ui = engine_to_ui(settings)
                row["before"] = {key: ui[key] for key in ("exposure_amt", "temperature", "tint")}
                if (not self.core.image_sources.supports_quick_preview(base.path) or not capture.model
                        or not metadata.model or (capture.make.casefold(), capture.model.casefold())
                        != (metadata.make.casefold(), metadata.model.casefold())):
                    raise ValueError("calibration.camera_mismatch")
                if (base.render_json != sample.render_json or color_identity(ui, ui=True) != color_identity(sample_ui, ui=True)
                        or {key: value for key, value in settings.items() if key not in excluded} != sample_style):
                    raise ValueError("calibration.incompatible")
                for operation in plan["operations"]:
                    detail = {"status": "skipped", "reason": ""}
                    try:
                        if operation == "exposure":
                            if color_identity(sample_ui, ui=True)[1] == 1:
                                raise ValueError("calibration.incompatible")
                            ref_k, ref_precision, ref_conflict = capture.exposure()
                            k, precision, conflict = metadata.exposure()
                            brightness = float(sample_settings.get("brightness", 1))
                            if brightness <= 0:
                                raise ValueError("calibration.incompatible")
                            reference_ev = float(sample_settings.get("exposure", 0)) + math.log2(brightness)
                            value = reference_ev + ref_k - k
                            if not -3 <= value <= 3:
                                raise ValueError("calibration.ev_range")
                            rounded = round(value * 100)
                            if not -300 <= rounded <= 300:
                                raise ValueError("calibration.ev_range")
                            row["updates"]["exposure_amt"] = rounded
                            detail.update(metadata_resolution_ev=precision + ref_precision,
                                          metadata_discrepancy_ev=max(conflict, ref_conflict),
                                          quantization_ev=abs(rounded / 100 - value), formula="E_ref + K_ref - K_target",
                                          reference_apex=capture.apex_raw, target_apex=metadata.apex_raw)
                        else:
                            if wb_error:
                                raise ValueError(wb_error)
                            updates, residual = _wb_proposal(sample_wb, self._wb(base, cancelled), sample_settings)
                            detail.update(residual)
                            if not updates:
                                raise ValueError("calibration.wb_residual")
                            row["updates"].update(updates)
                        detail["status"] = "ready"
                    except ValueError as error:
                        detail["reason"] = str(error)
                    row["details"][operation] = detail
                if row["updates"]:
                    graph = self.batches.calibration_workflow(base.workflow, row["updates"])
                    preview = self.core.capture_export_workflow(base.path, graph, source_hash=base.source_hash,
                                                               expected_tools=plan["tools"])
                    preview = self.core.prepare_export_snapshot(preview, is_cancelled=cancelled)
                    rendered = self.core.preview_export_snapshot(preview, ExportPreset(), cancellation=CalibrationCancellation(cancelled), max_dimension=1600)
                    if not rendered.success:
                        raise ValueError(rendered.error.message_key)
                    rendered.data[2].close()
                    del rendered
                    row["item"].candidate, row["item"].render_contract = graph, base.render_json
                    row["status"] = "unchanged" if graph.workflow_hash() == base.workflow_hash else "ready"
                    row["preview"] = preview
                else:
                    row["status"] = "skipped"
            except (CancelledError, InterruptedError):
                if preview is not None:
                    self.core.release_export_snapshot(preview)
                for previous in rows:
                    if previous["preview"] is not None:
                        self.core.release_export_snapshot(previous["preview"])
                raise
            except Exception as error:
                if preview is not None:
                    self.core.release_export_snapshot(preview)
                key = str(error) if isinstance(error, ValueError) else "batch.failed"
                row["status"], row["error"] = ("skipped" if key.startswith("calibration.") else "failed"), key
            rows.append(row)
        return {"rows": rows, "sample": sample}

    def plan_current(self, plan):
        group = self.groups.get(plan["group"])
        if (group is None or group.generation != plan["generation"] or self.core.tools.generation != plan["tools"]
                or (self.browser.raw_mode, self.browser.image_mode) != (plan["raw"], plan["image"])):
            return False
        try:
            sample_source = plan["sample"]
            if not self.core.image_sources.matches_file(SourceIdentity(sample_source.path, sample_source.source_hash, *sample_source.stamp)):
                return False
            sample = self.browser.workflow(group.sample_path)
            current_sample = self.sessions.get(group.sample_path)
            if (sample[0].workflow_hash() != plan["sample_current"][0]
                    or plan["sample_session"] is not None and (current_sample is None
                        or current_sample.core_id != plan["sample_session"] or sample[2] != plan["sample_current"][1])):
                return False
            for row in plan["rows"]:
                item = row["item"]
                if row["status"] != "ready" or item.after_revision:
                    continue
                base = row["base"]
                if not self.core.image_sources.matches_file(SourceIdentity(base.path, base.source_hash, *base.stamp)):
                    return False
                graph, _, version = self.browser.workflow(row["path"])
                session = self.sessions.get(row["path"])
                if (graph.workflow_hash() != item.before_hash
                        or item.session_id is not None and (session is None or session.core_id != item.session_id
                                                           or version != item.before_version)):
                    return False
            return True
        except (ValueError, KeyError, OSError, RuntimeError):
            return False

    def batch_current(self, identity):
        with self._guard_lock:
            guard = self._guards.get(identity)
        if (guard is None or guard.cancelled.is_set() or self.core.tools.generation != guard.tools
                or (self.browser.raw_mode, self.browser.image_mode) != guard.display):
            return False
        if not self.core.image_sources.matches_file(guard.source):
            return False
        if self.sessions.drafts.path_version(guard.draft_path) != guard.draft_version:
            return False
        session_id = self.sessions.session_id(guard.source.path)
        if guard.session_id is not None:
            return session_id == guard.session_id and self.core.matches_edit_state(
                guard.session_id, guard.state_version, guard.source)
        if session_id is None:
            return True
        state = self.core.get_session(session_id)
        return bool(state.success and state.data.active_source.file_hash == guard.source.file_hash
                    and state.data.draft_workflow.workflow_hash() == guard.workflow_hash)

    def verify_batch_source(self, identity, cancelled):
        with self._guard_lock:
            guard = self._guards.get(identity)
        if guard is None or not self.batch_current(identity):
            raise ValueError("calibration.expired")
        sources = self.core.image_sources
        with sources.compute.borrow(cancelled, priority=2, threads=1):
            source = sources.identity(guard.source.path, is_cancelled=cancelled)
        if source != guard.source:
            guard.cancelled.set()
            raise ValueError("error.source.changed")

    def apply(self, identity, *, origin="manual"):
        plan = self.plans.get(identity)
        if plan is None or plan["state"] != "ready":
            raise ValueError("calibration.expired")
        if plan["batch"]:
            return plan["batch"]
        if not self.plan_current(plan):
            raise ValueError("calibration.expired")
        if not any(row["status"] == "ready" for row in plan["rows"]):
            raise ValueError("calibration.no_suggestions")
        sample = plan["sample"]
        sample_source = SourceIdentity(sample.path, sample.source_hash, *sample.stamp)
        sample_id = self.sessions.session_id(sample.path)
        sample_state = self.core.get_session(sample_id).unwrap() if sample_id is not None else None
        if sample_state is not None and (
                self.core.source_identity(sample_id) != sample_source
                or sample_state.draft_workflow.workflow_hash() != plan["sample_current"][0]
                or plan["sample_session"] is not None and (sample_id != plan["sample_session"]
                    or sample_state.state_version != plan["sample_current"][1])):
            raise ValueError("calibration.expired")
        draft_path = os.path.normpath(plan["sample_path"])
        guard = CalibrationGuard(group=plan["group"], source=sample_source, session_id=sample_id,
            state_version=sample_state.state_version if sample_state is not None else None,
            workflow_hash=plan["sample_current"][0], draft_path=draft_path,
            draft_version=self.sessions.drafts.path_version(draft_path), tools=plan["tools"],
            display=(plan["raw"], plan["image"]), cancelled=Event())
        items = []
        for row in plan["rows"]:
            item = row["item"]
            if row["status"] == "unchanged":
                item.status, item.candidate = "unchanged", None
            elif row["status"] != "ready":
                item.status = "skipped"
                item.error = {"code": "SKIPPED", "message_key": row["error"] or "calibration.no_suggestions", "details": row["details"]}
            items.append(item)
        plan["batch"] = self.batches.add_calibration(items, os.path.basename(plan["sample"].path),
            raw_mode=plan["raw"], image_mode=plan["image"], origin=origin)
        with self._guard_lock:
            self._guards[plan["batch"]] = guard
        # 批次持有候选工作流与渲染契约，建议用的固定快照不再需要
        self._release_rows(plan)
        return plan["batch"]

    def summary(self, identity, *, offset=0, limit=50):
        plan = self.plans.get(identity)
        if plan is None:
            raise ValueError("calibration.expired")
        rows = [{"photo": identity + ":" + str(index), "name": os.path.basename(row["path"]),
                 "status": row["status"], "before": row.get("before", {}), "proposed": row["updates"],
                 "details": row["details"], "error": row["error"], "previewed": row["preview"] is not None}
                for index, row in enumerate(plan["rows"]) if offset <= index < offset + limit]
        return {"calibration": identity, "group": plan["group"], "state": plan["state"], "batch": plan["batch"],
                "operations": plan["operations"], "error": plan["error"], "total": len(plan["rows"]),
                "offset": offset, "photos": rows, "has_more": offset + limit < len(plan["rows"]),
                "runtime_only": True, "applied": any(row["item"].after_revision for row in plan["rows"]),
                "applicable": (plan["state"] == "ready" and not plan["batch"]
                               and any(row["status"] == "ready" for row in plan["rows"]) and self.plan_current(plan)),
                "application": self.batches.summary(plan["batch"], offset=offset, limit=limit) if plan["batch"] else None,
                "exposure_match": "midtones"}

    def shutdown(self):
        with self._guard_lock:
            for guard in self._guards.values():
                guard.cancelled.set()
            self._guards.clear()
        for plan in self.plans.values():
            for row in plan["rows"]:
                for snapshot in (row["base"], row["preview"]):
                    if snapshot is not None:
                        self.release_snapshot(snapshot)
        for group in self.groups.values():
            self.release_snapshot(group.sample)
        self.plans.clear()
        self.groups.clear()
