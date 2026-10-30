from __future__ import annotations

import copy
import os
from threading import RLock
from types import SimpleNamespace

from ..core.contracts.preview import PreviewTarget
from ..core.contracts.result import CoreResult, ErrorCode
from ..core.tools.color import (COLOR_UI_PARAMETERS, color_ui_contract, engine_to_ui,
                                migrate_legacy_color, reset_color_params)
from .color_initialization import (baseline_color_settings, initial_color_identity,
                                   color_baseline_workflow, write_session_drafts)
from .image_source_service import path_key
from ..core.tools.stage_defaults import (is_color_default as _is_color_default, is_beauty_default as _is_beauty_default,
                                         is_denoise_default as _is_denoise_default, is_crop_default as _is_crop_default)
from .workflow_mapping import (WorkflowParameterError, restore_workflow,
                               to_workflow, workflow_stages, stage_identity, stage_order)

STAGE_SPECS = {
    "color_adjust": ("color_params", _is_color_default),
    "beauty": ("beauty_params", _is_beauty_default),
    "denoise": ("denoise_params", _is_denoise_default),
    "crop": ("crop_params", _is_crop_default),
}


class EditSession:
    def __init__(self, core, project):
        self.image_path = project.active_source.path
        self.color_params, self.beauty_params, self.denoise_params = {}, {}, {}
        self.crop_params, self.tool_params = {}, {}
        self.core = core
        self.core_id = project.session_id
        self.state_version = project.state_version
        self.project_path = None
        self._baseline_workflow = core.baseline_workflow(self.core_id).unwrap()
        self.saved_hash = self._baseline_workflow.workflow_hash()
        self.saved_version = project.state_version
        self.refresh()

    def snapshot(self):
        return copy.deepcopy({"color": self.color_params, "beauty": self.beauty_params,
                              "denoise": self.denoise_params, "crop": self.crop_params,
                              "tools": self.tool_params})

    def _restore_snapshot(self, state):
        color = state.get("color", {})
        self.color_params = migrate_legacy_color(color, ui=True) if color else {}
        self.beauty_params = copy.deepcopy(state.get("beauty", {}))
        self.denoise_params = copy.deepcopy(state.get("denoise", {}))
        self.crop_params = copy.deepcopy(state.get("crop", {}))
        self.tool_params = copy.deepcopy(state.get("tools", {}))

    def detached(self):
        clone = copy.copy(self)
        clone._restore_snapshot(self.snapshot())
        return clone

    def set_stage(self, tool_id, params):
        if tool_id in STAGE_SPECS:
            setattr(self, STAGE_SPECS[tool_id][0], copy.deepcopy(params))
        elif tool_id == "lasso" and not params.get("regions") and not params.get("draft_points"):
            self.tool_params.pop(tool_id, None)
        else:
            self.tool_params[tool_id] = copy.deepcopy(params)

    def refresh(self):
        result = self.core.get_session(self.core_id)
        if not result.success:
            raise ValueError(result.error.code)
        self._raw_workflow = copy.deepcopy(result.data.draft_workflow)
        inspected = self.core.project_capabilities(self.core_id, verify_resources=False)
        self.issues = inspected.data["issues"] if inspected.success else [inspected.error.to_dict()]
        from ..core.tools.enhancement import model_gaps_editable
        self.read_only = bool(self.issues) and not model_gaps_editable(self._raw_workflow, self.issues)
        if not self.read_only:
            try:
                restore_workflow(self, self._raw_workflow)
            except ValueError:
                self.read_only = True
                self.issues = [{"code": "UI_WORKFLOW_UNSUPPORTED", "message_key": "studio.unsupported_workflow", "details": {}}]
        if self.read_only:
            self._restore_snapshot({})
        self.history_revision = result.data.current_revision.revision_id
        self.state_version = result.data.state_version
        self.source_size = self.core.source_size(self.core_id)
        self.has_history_image = bool(result.data.current_revision.artifact_refs)
        self._mapped_generation = self.core.tools.generation


    def refresh_availability(self):
        current = self.core.get_session(self.core_id)
        if not current.success:
            return self.read_only
        inspected = self.core.project_capabilities(self.core_id, verify_resources=False)
        issues = inspected.data["issues"] if inspected.success else [inspected.error.to_dict()]
        if issues or self.read_only:
            self.refresh()
        else:
            self.issues = issues
        return self.read_only


    def sync(self, label="adjust", *, edit_group=None, expected_version=None, expected_tools=None, expected_source=None):
        expected_version = self.state_version if expected_version is None else expected_version
        if edit_group is None:
            ended = self.core.end_edit_group(self.core_id)
            if not ended.success:
                return ended
        if self.read_only:
            return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "studio.project_read_only")
        if (self._mapped_snapshot == self.snapshot()
                and self._mapped_generation == self.core.tools.generation):
            current = self.core.get_session(self.core_id)
            if current.success and current.data.state_version == self.state_version:
                return current
        try:
            workflow = to_workflow(self)
        except WorkflowParameterError as error:
            return error.result
        except ValueError:
            return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible")
        result = self.core.edit_workflow(self.core_id, workflow, label=label, edit_group=edit_group,
            expected_version=expected_version, expected_tools=expected_tools, expected_source=expected_source)
        if result.success:
            self._raw_workflow = copy.deepcopy(result.data.draft_workflow)
            self._mapped_workflow = copy.deepcopy(result.data.draft_workflow)
            self._mapped_snapshot = self.snapshot()
            inspected = self.core.project_capabilities(self.core_id, verify_resources=False)
            self.issues = inspected.data["issues"] if inspected.success else [inspected.error.to_dict()]
            self.history_revision = result.data.current_revision.revision_id
            self.state_version = result.data.state_version
            self.has_history_image = bool(result.data.current_revision.artifact_refs)
            self._mapped_generation = self.core.tools.generation
        return result


    def render_snapshot(self, editing_tool=None, *, preview_target=None, color_candidate=None):
        preview_target = preview_target or PreviewTarget()
        target_session = self
        if color_candidate is not None:
            target_session = self.detached()
            target_session.color_params = copy.deepcopy(color_candidate)
        return SimpleNamespace(core_id=self.core_id, workflow=to_workflow(target_session, editing_tool, preview_target),
                               read_only=self.read_only, revision_id=self.history_revision,
                               state_version=self.state_version, preview_target=preview_target)


    def mixer_sampling_request(self, snapshot, epoch):
        from ..core.mixer_sampling import MixerSamplingRequest
        for node in snapshot.workflow.nodes:
            if stage_identity(node) == "color_adjust":
                return MixerSamplingRequest(target_node=node.node_id, epoch=epoch)
        _, ordered = stage_order(self)
        position = ordered.index("color_adjust")
        before = next((node.node_id for node in snapshot.workflow.nodes
                       if stage_identity(node) in ordered
                       and ordered.index(stage_identity(node)) > position), None)
        return MixerSamplingRequest(before_node=before, epoch=epoch)


    def stages(self):
        """当前草稿里的各步骤及其面板参数，按处理顺序。"""
        return workflow_stages(self._raw_workflow)


    def stage_node(self, tool_id):
        return next((copy.deepcopy(node) for node in self._raw_workflow.nodes if stage_identity(node) == tool_id), None)


    def revision_stages(self, revision_id):
        """某个历史修订里的各步骤及其面板参数；修订不存在时为空。"""
        result = self.core.revision_workflow(self.core_id, revision_id)
        return workflow_stages(result.data) if result.success else {}


    def timeline(self):
        """当前这条历史线：自旧到新的修订与当前修订的位置；会话已不存在时为 None。"""
        result = self.core.history_timeline(self.core_id)
        return result.data if result.success else None


    def checkout(self, revision_id):
        """回到历史线上的某一步；往回走时沿途各步仍可以重做。"""
        result = self.core.checkout(self.core_id, revision_id, expected_state=self.state_version, keep_redo_path=True)
        if result.success:
            self.refresh()
        return result


    def assistant_proposal(self, adjustments, defaults, *, base_workflow=None):
        """把 AI 按工具分组的改动叠到当前参数上，得到候选工作流；会话本身不变。

        adjustments 形如 {"color": {...}, "beauty": {...}}；defaults 是还没进入本次编辑的工具的起始参数
        （例如美颜所选的人脸模型）。没有美颜步骤时按默认次序加入。"""
        proposal = self.detached()
        if base_workflow is not None:
            restore_workflow(proposal, base_workflow)
        color = adjustments.get("color", {})
        if color:
            proposal.color_params = {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version),
                                     **proposal.color_params, **color}
        beauty = adjustments.get("beauty", {})
        if beauty:
            proposal.beauty_params = {**defaults.get("beauty", {}), **proposal.beauty_params, **beauty}
        return to_workflow(proposal)


    def undo_redo_state(self):
        result = self.core.history_state(self.core_id)
        return ((result.data["can_undo"], result.data["can_redo"])
                if result.success else (False, False))


    def can_undo(self):
        return self.undo_redo_state()[0]


    def can_redo(self):
        return self.undo_redo_state()[1]


    def undo(self):
        result = self.core.undo(self.core_id)
        if result.success:
            self.refresh()
        return result.success


    def redo(self):
        result = self.core.redo(self.core_id)
        if result.success:
            self.refresh()
        return result.success


    def has_unsaved_changes(self):
        state = self.core.get_session(self.core_id)
        return state.success and (getattr(self, "source_relocated", False) and bool(self.project_path)
                                  or state.data.draft_workflow.workflow_hash() != self.saved_hash)


    @property
    def baseline_color_params(self):
        return engine_to_ui(baseline_color_settings(self._baseline_workflow))


    def is_dirty(self):
        return to_workflow(self).workflow_hash() != self._baseline_workflow.workflow_hash()


    def color_modified(self):
        contract = color_ui_contract(self.color_params)
        current = contract.normalize(self.color_params, contract.version).unwrap()
        return current != self.baseline_color_params


    def reset_all(self):
        if self.read_only:
            return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "studio.project_read_only")
        result = self.core.edit_workflow(self.core_id, self._baseline_workflow, label="reset", expected_version=self.state_version)
        if result.success:
            self.refresh()
        return result


    def reset_stage(self, stage):
        if self.read_only:
            return
        attributes = {"color_adjust": "color_params", "beauty": "beauty_params",
                      "denoise": "denoise_params", "crop": "crop_params"}
        if stage in attributes:
            current = getattr(self, attributes[stage])
            neutral = ({key: 0 if type(value) is int else value for key, value in current.items()}
                       if stage == "beauty" else {})
            setattr(self, attributes[stage], reset_color_params(self.baseline_color_params, self.color_params)
                    if stage == "color_adjust" else neutral)
        else:
            self.tool_params.pop(stage, None)


class EditSessionService:
    def __init__(self, core, drafts, *, supported_tools, resolve_tool=lambda tool: tool):
        self.core, self.drafts = core, drafts
        self.supported_tools = frozenset(supported_tools) - {"grid_crop"}
        self.resolve_tool = resolve_tool
        self._sessions = {}
        self._registry_lock = RLock()

    @staticmethod
    def key(path):
        return path_key(path)

    def get(self, path):
        with self._registry_lock:
            return self._sessions.get(self.key(path)) if path else None

    def session_id(self, path):
        with self._registry_lock:
            session = self._sessions.get(self.key(path)) if path else None
            return session.core_id if session is not None else None

    def sessions(self):
        with self._registry_lock:
            return tuple(self._sessions.values())

    def initial_workflow(self, path, *, raw_display_mode, non_raw_display_mode, drafts=None):
        current = self.get(path)
        if current is not None:
            return self.core.baseline_workflow(current.core_id).unwrap()
        values = self.drafts.for_image(os.path.normpath(path)) if drafts is None else drafts
        identity = initial_color_identity(values,
            is_raw=self.core.image_sources.supports_quick_preview(path), raw_display_mode=raw_display_mode,
            non_raw_display_mode=non_raw_display_mode)
        return color_baseline_workflow(self.core.tools, identity)

    def adopt(self, project, *, restore_drafts=True, path=None):
        path = os.path.normpath(path or project.active_source.path)
        current = self.get(path)
        if current is not None:
            if current.core_id != project.session_id:
                return CoreResult.fail(ErrorCode.STALE_CANDIDATE, "error.agent.stale_candidate")
            current.refresh()
            return CoreResult.ok(current)
        session = EditSession(self.core, project)
        session.image_path = path
        if restore_drafts and not session.read_only:
            for tool_id, params in self.drafts.for_image(path).items():
                tool_id = self.resolve_tool(tool_id)
                if tool_id in self.supported_tools:
                    session.set_stage(tool_id, params)
            synced = session.sync()
            if not synced.success:
                session.refresh()
                return synced
        with self._registry_lock:
            self._sessions[self.key(path)] = session
        return CoreResult.ok(session)

    def workflow_from_drafts(self, initial, drafts):
        projection = EditSession.__new__(EditSession)
        projection.core = self.core
        projection.read_only = False
        restore_workflow(projection, initial)
        for tool_id, params in drafts.items():
            tool_id = self.resolve_tool(tool_id)
            if tool_id in self.supported_tools:
                projection.set_stage(tool_id, params)
        return to_workflow(projection)

    def attach(self, session):
        previous = self.get(session.image_path)
        if previous is not None and previous.core_id != session.core_id:
            self.core.close_session(previous.core_id)
        with self._registry_lock:
            self._sessions[self.key(session.image_path)] = session

    def close(self, path):
        with self._registry_lock:
            session = self._sessions.pop(self.key(path), None)
        if session is not None:
            self.core.close_session(session.core_id)
        return session

    def rekey(self, old_path, new_path, session_id):
        with self._registry_lock:
            previous = self._sessions.get(self.key(old_path))
            target = self._sessions.get(self.key(new_path))
            if previous is None or previous.core_id != session_id or target is not None and target is not previous:
                raise ValueError("library.target_occupied")
            self._sessions.pop(self.key(old_path))
            previous.image_path = new_path
            previous.refresh()
            previous.source_relocated = True
            self._sessions[self.key(new_path)] = previous
            return previous

    def write_drafts(self, session):
        if session.read_only:
            return CoreResult.ok(self.drafts.version)
        updates = {tool: getattr(session, attr) if not neutral(getattr(session, attr)) else None
                   for tool, (attr, neutral) in STAGE_SPECS.items()}
        if "face_locator_model" in session.beauty_params:
            updates["beauty"] = session.beauty_params
        for tool in self.supported_tools - STAGE_SPECS.keys():
            updates[tool] = session.tool_params.get(tool)
        baseline = session.baseline_color_params
        written = write_session_drafts(self.drafts, session.image_path, updates,
            color_params=session.color_params, baseline_identity=(baseline["look"], baseline["process_version"]))
        return CoreResult.ok(self.drafts.version) if written.success else written

    def sync(self, session, label="adjust", **options):
        result = session.sync(label, **options)
        if not result.success:
            session.refresh()
            return result
        written = self.write_drafts(session)
        return result if written.success else written

    def refresh(self, session_id):
        session = next((value for value in self._sessions.values() if value.core_id == session_id), None)
        if session is None:
            return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
        session.refresh()
        written = self.write_drafts(session)
        return CoreResult.ok(session) if written.success else written

