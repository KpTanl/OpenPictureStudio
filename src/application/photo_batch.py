from __future__ import annotations

import copy
import os
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from ..core.contracts import WorkflowGraph, WorkflowNode
from ..core.contracts.result import CoreOperationError
from ..core.tools.color import (COLOR_SYNC_FIELDS, COLOR_STYLE_FIELDS, color_identity, engine_to_ui,
                                exposure_centiev, normalize_sync_fields, sync_color_settings, sync_field_values,
                                COLOR_SYNC_COMPATIBILITY, ui_to_engine)
from .workflow_mapping import to_workflow
from .color_initialization import baseline_color_settings


@dataclass(frozen=True)
class PhotoSelection:
    selected: tuple[str, ...]
    filtered: tuple[str, ...]
    active: str | None
    list_version: int
    filter_revision: int


@dataclass
class BatchItem:
    path: str
    drafts: dict
    session_id: str | None = None
    before_version: int | None = None
    before_revision: str = ""
    after_revision: str = ""
    source: object = None
    candidate: WorkflowGraph | None = None
    status: str = "pending"
    error: dict | None = None
    draft_version: int | None = None
    undone: bool = False
    warnings: tuple[str, ...] = ()
    before_hash: str = ""
    render_contract: str = ""
    captured_source_hash: str = ""


class BatchValidationError(ValueError):
    def __init__(self, key, **details):
        super().__init__(key)
        self.details = details


@dataclass
class PhotoBatch:
    id: str
    operation: str
    items: tuple[BatchItem, ...]
    sample_ui: dict
    sample_version: int
    sample_name: str
    fields: tuple[str, ...] = ()
    delta: int = 0
    origin: str = "manual"
    active: bool = False
    save_state: str = "pending"
    save_error: dict | None = None
    tool_generation: int = 0
    started: bool = False
    raw_display_mode: str = "ops"
    non_raw_display_mode: str = "original"
    expired: bool = False


class PhotoBatchService:
    def __init__(self, sessions, *, agent=None):
        self.sessions = sessions
        self.core = sessions.core
        self._contexts = OrderedDict()
        self._batches = OrderedDict()
        self.agent = agent
        self.calibration_guard = lambda batch_id: True
        self.calibration_preflight = lambda batch_id, cancelled: None

    def capture(self, selection, *, raw_display_mode="ops", non_raw_display_mode="original"):
        sample = self.sessions.get(selection.active)
        sample_ui = copy.deepcopy(sample.color_params if sample is not None else {})
        if sample is not None:
            sample_ui = {**sample.baseline_color_params, **sample_ui}
        scope = uuid.uuid4().hex
        context = {"selection": selection, "sample_ui": sample_ui,
                   "sample_version": color_identity(sample_ui, ui=True)[1],
                   "sample_name": os.path.basename(selection.active or ""),
                   "sample_snapshot": scope + ":sample" if sample is not None else "",
                   "sample_session": sample.core_id if sample is not None else None,
                   "sample_revision": sample.history_revision if sample is not None else "",
                   "sample_state": sample.state_version if sample is not None else None,
                   "raw": raw_display_mode, "non_raw": non_raw_display_mode}
        self._contexts[scope] = context
        while len(self._contexts) > 32:
            self._contexts.popitem(last=False)
        return self.context(scope)

    def context(self, scope):
        context = self._contexts.get(scope)
        if context is None:
            raise ValueError("batch.scope_expired")
        selection = context["selection"]
        return {"scope": scope, "selected_count": len(selection.selected),
                "filtered_count": len(selection.filtered), "sample_name": context["sample_name"],
                "sample_snapshot": context["sample_snapshot"], "fields": list(COLOR_SYNC_FIELDS),
                "sample_revision": context["sample_revision"],
                "default_fields": list(COLOR_STYLE_FIELDS), "list_version": selection.list_version,
                "filter_revision": selection.filter_revision, "raw": context["raw"], "non_raw": context["non_raw"]}

    def context_current(self, scope, selection):
        """捕获后样板没有新编辑、选片范围没有变化。"""
        context = self._contexts.get(scope)
        if context is None or context["selection"] != selection:
            return False
        if context["sample_session"] is None:
            return True
        state = self.core.get_session(context["sample_session"])
        return state.success and state.data.state_version == context["sample_state"]

    def create(self, scope, operation, *, fields=(), delta_ev=0, range_kind="selected",
               sample_snapshot=None, origin="manual"):
        context = self._contexts.get(scope)
        if context is None:
            raise ValueError("batch.scope_expired")
        if operation not in ("sync", "exposure") or range_kind not in ("selected", "filtered"):
            raise ValueError("batch.invalid_fields")
        selection = context["selection"]
        paths = selection.selected if range_kind == "selected" else selection.filtered
        if operation == "sync":
            if not context["sample_snapshot"] or sample_snapshot != context["sample_snapshot"]:
                raise ValueError("batch.sample_expired")
            fields = normalize_sync_fields(fields)
            paths = tuple(path for path in paths if self.sessions.key(path) != self.sessions.key(selection.active))
        delta = exposure_centiev(delta_ev) if operation == "exposure" else 0
        if not paths:
            raise ValueError("batch.no_targets")
        items, seen = [], set()
        for path in paths:
            key = self.sessions.key(path)
            if key in seen:
                continue
            seen.add(key)
            item = BatchItem(path, self.sessions.drafts.for_image(os.path.normpath(path)))
            session = self.sessions.get(path)
            if session is not None:
                self._capture_item(item, session)
            items.append(item)
        batch = PhotoBatch(uuid.uuid4().hex, operation, tuple(items), copy.deepcopy(context["sample_ui"]),
                           context["sample_version"], context["sample_name"], tuple(fields), delta, origin,
                           tool_generation=self.core.tools.generation, raw_display_mode=context["raw"],
                           non_raw_display_mode=context["non_raw"])
        if operation == "exposure":
            # 曝光增减与样板无关：以本批目标中张数最多的处理版本为参照（并列取较新），其余逐张跳过
            versions = []
            for item in batch.items:
                try:
                    versions.append(self._target_identity(batch, item)[1])
                except (ValueError, CoreOperationError):
                    continue
            batch.sample_version = (max(set(versions), key=lambda version: (versions.count(version), version))
                                    if versions else None)
        self._batches[batch.id] = batch
        return batch.id

    def get(self, batch_id):
        batch = self._batches.get(batch_id)
        if batch is None:
            raise ValueError("batch.expired")
        return batch

    def expire_path(self, path):
        key = self.sessions.key(path)
        for scope, context in tuple(self._contexts.items()):
            selection = context["selection"]
            if any(self.sessions.key(p) == key for p in (*selection.selected, *selection.filtered, selection.active or "")):
                self._contexts.pop(scope)
        for batch in self._batches.values():
            if any(self.sessions.key(item.path) == key for item in batch.items):
                batch.expired = True

    def _capture_item(self, item, session):
        state = self.core.get_session(session.core_id).unwrap()
        item.session_id = session.core_id
        item.before_version = state.state_version
        item.before_revision = state.current_revision.revision_id
        item.source = self.core.source_identity(session.core_id)

    def _target_identity(self, batch, item):
        session = self.sessions.get(item.path)
        if session is not None:
            if session.read_only:
                raise ValueError("studio.project_read_only")
            return color_identity(session.color_params or session.baseline_color_params, ui=True)
        initial = self.sessions.initial_workflow(item.path, drafts=item.drafts,
            raw_display_mode=batch.raw_display_mode, non_raw_display_mode=batch.non_raw_display_mode)
        return color_identity(item.drafts.get("color_adjust") or baseline_color_settings(initial), ui=True)

    def preflight(self, batch_id):
        batch = self.get(batch_id)
        fields = batch.fields if batch.operation == "sync" else ("exposure_amt",)
        for item in batch.items:
            try:
                look, version = self._target_identity(batch, item)
                if batch.operation == "exposure":
                    self._check_exposure_version(batch, version)
                elif look != color_identity(batch.sample_ui, ui=True)[0]:
                    item.warnings = ("batch.look_difference",)
                if not set(fields) <= COLOR_SYNC_COMPATIBILITY.get((batch.sample_version, version), frozenset()):
                    raise ValueError("batch.incompatible_version")
            except ValueError as error:
                self._fail(item, "failed", str(error), getattr(error, "details", {}))
            except CoreOperationError as error:
                self._fail(item, "failed", error.error.message_key)
        return self.summary(batch_id)

    @staticmethod
    def _check_exposure_version(batch, version):
        if batch.sample_version is not None and version != batch.sample_version:
            raise BatchValidationError("batch.exposure_version", version=version, reference=batch.sample_version)

    def apply_item(self, batch_id, index, binding, cancellation):
        if not self.prepare_item(batch_id, index, binding, cancellation):
            return
        batch, item = self.get(batch_id), self.get(batch_id).items[index]
        result = self.core.edit_workflow(item.session_id, item.candidate,
            expected_version=item.before_version, expected_source=item.source,
            expected_tools=batch.tool_generation, cancellation=cancellation,
            label=("ai.batch." if batch.origin == "agent" else "batch.") + batch.operation)
        if not result.success:
            return self.fail_result(item, result) if cancellation.is_cancelled() else self._result_error(item, result)
        self.complete_item(batch_id, index, result.data.current_revision.revision_id)

    def prepare_item(self, batch_id, index, binding, cancellation):
        batch = self.get(batch_id)
        item = batch.items[index]
        if item.status in ("success", "unchanged") or item.after_revision:
            return
        if cancellation.is_cancelled():
            item.status = "cancelled"
            return
        if item.captured_source_hash and binding.file_hash != item.captured_source_hash:
            return self._fail(item, "conflict", "error.source.changed")
        session = self.sessions.get(item.path)
        if item.session_id is None:
            if self.sessions.drafts.for_image(os.path.normpath(item.path)) != item.drafts:
                return self._fail(item, "conflict", "batch.conflict")
            if session is None:
                initial = self.sessions.initial_workflow(item.path, drafts=item.drafts,
                    raw_display_mode=batch.raw_display_mode, non_raw_display_mode=batch.non_raw_display_mode)
                created = self.core.create_bound_session(binding, initial_workflow=initial)
                if not created.success:
                    return self._result_error(item, created)
                adopted = self.sessions.adopt(created.data, path=item.path)
                if not adopted.success:
                    self.core.close_session(created.data.session_id)
                    return self._result_error(item, adopted)
                session = adopted.data
            self._capture_item(item, session)
        current = self.core.get_session(item.session_id)
        if (session is None or session.core_id != item.session_id or not current.success
                or current.data.state_version != item.before_version
                or self.core.source_identity(item.session_id) != item.source
                or binding.file_hash != item.source.file_hash
                or item.before_hash and current.data.draft_workflow.workflow_hash() != item.before_hash
                or batch.operation == "calibration" and not self.calibration_guard(batch_id)):
            return self._fail(item, "conflict", "batch.conflict")
        session.refresh()
        if session.read_only:
            return self._fail(item, "failed", "studio.project_read_only")
        try:
            if item.candidate is None:
                item.candidate = self._candidate(batch, session, current.data.draft_workflow)
        except ValueError as error:
            return self._fail(item, "failed", str(error), getattr(error, "details", {}))
        return True

    def complete_item(self, batch_id, index, revision):
        batch, item = self.get(batch_id), self.get(batch_id).items[index]
        session = self.sessions.get(item.path)
        item.after_revision = revision
        item.status = "unchanged" if item.after_revision == item.before_revision else "success"
        item.candidate = None
        item.drafts = {}
        session.refresh()
        written = self.sessions.write_drafts(session)
        if written.success:
            item.draft_version = written.data
        else:
            batch.save_state, batch.save_error = "failed", written.error.to_dict()

    def _candidate(self, batch, session, workflow):
        color_node = next((node for node in workflow.nodes if self._is_color(node)), None)
        original = (color_node.params["settings"] if color_node.node_type == "tool" else color_node.params
                    ) if color_node is not None else ui_to_engine(session.baseline_color_params)
        sample = copy.deepcopy(batch.sample_ui)
        fields = batch.fields
        if batch.operation == "exposure":
            self._check_exposure_version(batch, color_identity(original)[1])
            sample = engine_to_ui(original)
            value = sample["exposure_amt"] + batch.delta
            if not -300 <= value <= 300:
                raise BatchValidationError("batch.ev_range", before=f"{sample['exposure_amt'] / 100:+.2f}",
                                             proposed=f"{value / 100:+.2f}")
            sample["exposure_amt"] = value
            fields = ("exposure_amt",)
        settings = sync_color_settings(original, sample, fields, sample_version=batch.sample_version)
        if sync_field_values(engine_to_ui(original), fields) == sync_field_values(sample, fields):
            return workflow
        if color_node is None:
            projection = session.detached()
            projection.color_params = engine_to_ui(settings)
            return to_workflow(projection)
        nodes = []
        for node in workflow.nodes:
            if not self._is_color(node):
                nodes.append(node)
                continue
            params = {**copy.deepcopy(node.params), "settings": settings} if node.node_type == "tool" else settings
            nodes.append(WorkflowNode(node.node_id, node.node_type, params, inputs=node.inputs, version=node.version))
        return WorkflowGraph(nodes=tuple(nodes), edges=workflow.edges, schema_version=workflow.schema_version)

    @staticmethod
    def _is_color(node):
        return node.node_type == "color_adjust" or node.node_type == "tool" and node.params.get("tool_id") == "color_adjust"

    @staticmethod
    def _fail(item, status, key, details=None):
        item.status, item.error = status, {"code": status.upper(), "message_key": key, "details": details or {}}

    def _result_error(self, item, result):
        item.status = "conflict" if result.error.code in ("CANCELLED", "STALE_CANDIDATE", "SOURCE_CHANGED") else "failed"
        item.error = result.error.to_dict()

    def fail_result(self, item, result):
        self._result_error(item, result)
        if result.error.code == "CANCELLED":
            item.status = "cancelled"

    def restore_item(self, batch_id, index, redo, cancellation):
        batch, item = self.get(batch_id), self.get(batch_id).items[index]
        if item.status != "success":
            return
        current = self.core.get_session(item.session_id)
        expected = item.before_revision if redo else item.after_revision
        target = item.after_revision if redo else item.before_revision
        if current.success and current.data.current_revision.revision_id == target:
            item.undone, item.error = not redo, None
            return
        if (not current.success or current.data.current_revision.revision_id != expected
                or self.core.source_identity(item.session_id) != item.source
                or not self.core.image_sources.matches_file(item.source)):
            item.error = {"code": "CONFLICT", "message_key": "batch.conflict", "details": {}}
            return
        restored = self.core.checkout(item.session_id, target, expected_state=current.data.state_version,
                                       cancellation=cancellation, keep_redo_path=not redo)
        if not restored.success:
            item.error = restored.error.to_dict()
            return
        item.undone, item.error = not redo, None
        refreshed = self.sessions.refresh(item.session_id)
        if refreshed.success:
            item.draft_version = self.sessions.drafts.version
        else:
            batch.save_state, batch.save_error = "failed", refreshed.error.to_dict()

    def summary(self, batch_id, *, offset=0, limit=50):
        batch = self.get(batch_id)
        counts = {status: sum(item.status == status for item in batch.items)
                  for status in ("pending", "preparing", "success", "unchanged", "conflict", "failed", "cancelled", "skipped")}
        items = []
        for item in batch.items[offset:offset + limit]:
            current = self.core.get_session(item.session_id) if item.session_id else None
            revision = current.data.current_revision.revision_id if current is not None and current.success else ""
            history = ("unavailable" if not revision else "applied" if revision == item.after_revision
                       or item.status == "unchanged" and revision == item.before_revision
                       else "undone" if revision == item.before_revision else "changed")
            if item.source is not None and not self.core.image_sources.matches_file(item.source):
                history = "unavailable"
            items.append({"name": os.path.basename(item.path), "status": item.status, "error": item.error,
                          "before_revision": item.before_revision, "after_revision": item.after_revision,
                          "history": history, "warnings": list(item.warnings),
                          "photo": batch.id + ":" + str(offset + len(items))})
        return {"batch": batch.id, "operation": batch.operation, "total": len(batch.items), "counts": counts,
                "expired": batch.expired,
                "reference_version": batch.sample_version if batch.operation == "exposure" else None,
                "active": batch.active, "started": batch.started, "save_state": batch.save_state, "save_error": batch.save_error,
                "sample_name": batch.sample_name, "offset": offset, "items": items,
                "has_more": offset + limit < len(batch.items), "runtime_only": True}

    def batches(self):
        return tuple(self._batches)

    def add_calibration(self, items, sample_name, *, raw_mode, image_mode, origin="manual"):
        batch = PhotoBatch(uuid.uuid4().hex, "calibration", tuple(items), {}, 0, sample_name,
                           origin=origin, tool_generation=self.core.tools.generation,
                           raw_display_mode=raw_mode, non_raw_display_mode=image_mode)
        self._batches[batch.id] = batch
        return batch.id

    def calibration_workflow(self, workflow, updates):
        node = next((node for node in workflow.nodes if self._is_color(node)), None)
        original = (node.params["settings"] if node.node_type == "tool" else node.params
                    ) if node is not None else baseline_color_settings(workflow)
        sample = {**engine_to_ui(original), **updates}
        settings = sync_color_settings(original, sample, tuple(updates), sample_version=color_identity(sample, ui=True)[1])
        if node is None:
            return self.sessions.workflow_from_drafts(workflow, {"color_adjust": engine_to_ui(settings)})
        params = {**copy.deepcopy(node.params), "settings": settings} if node.node_type == "tool" else settings
        nodes = tuple(WorkflowNode(value.node_id, value.node_type, params, inputs=value.inputs, version=value.version)
                      if value.node_id == node.node_id else value for value in workflow.nodes)
        return WorkflowGraph(nodes=nodes, edges=workflow.edges, schema_version=workflow.schema_version)

    def selection_paths(self, scope, range_kind):
        context = self._contexts.get(scope)
        if context is None:
            raise ValueError("batch.scope_expired")
        selection = context["selection"]
        if range_kind == "current":
            return (selection.active,) if selection.active else ()
        if range_kind not in ("selected", "filtered"):
            raise ValueError("batch.invalid_fields")
        return selection.selected if range_kind == "selected" else selection.filtered

    def paths(self, batch_id):
        return tuple(item.path for item in self.get(batch_id).items)

    def has_photo(self, path):
        key = self.sessions.key(path)
        return any(item.after_revision and self.sessions.key(item.path) == key
                   for batch in self._batches.values() for item in batch.items)

    def retry_draft(self, batch_id, index):
        batch, item = self.get(batch_id), self.get(batch_id).items[index]
        if item.after_revision and item.draft_version is None:
            result = self.sessions.refresh(item.session_id)
            if result.success:
                item.draft_version = self.sessions.drafts.version
            else:
                batch.save_state, batch.save_error = "failed", result.error.to_dict()

    def photo_session(self, reference):
        identity, index = reference.rsplit(":", 1)
        if index == "sample":
            context = self._contexts.get(identity)
            session = self.sessions.get(context["selection"].active) if context is not None else None
            if session is not None and session.core_id != context["sample_session"]:
                session = None
        else:
            batch = self.get(identity)
            position = int(index)
            if not 0 <= position < len(batch.items):
                raise ValueError("batch.expired")
            item = batch.items[position]
            session = self.sessions.get(item.path)
            if session is not None and item.session_id and session.core_id != item.session_id:
                session = None
        if session is None:
            raise ValueError("batch.expired")
        return session

    def photo_snapshot(self, reference):
        session = self.photo_session(reference)
        session.refresh()
        state = self.core.get_session(session.core_id).unwrap()
        return state, session.snapshot(), session.project_path

    def sample_values(self, scope, *, offset=0, limit=20):
        context = self._contexts.get(scope)
        if context is None:
            raise ValueError("batch.scope_expired")
        if not context["sample_snapshot"]:
            return []
        fields = COLOR_SYNC_FIELDS[offset:offset + limit]
        values = sync_field_values(context["sample_ui"], fields)
        return [{"field": field, "value": value} for field, value in zip(fields, values)]
