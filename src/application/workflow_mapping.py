from __future__ import annotations

import copy
import math

from ..core.contracts import WorkflowGraph, WorkflowNode
from ..core.contracts.tools import ToolReference
from ..core.contracts.preview import PreviewTarget
from ..core.contracts.result import CoreResult
from ..core.tools.denoise import DENOISE_PARAMETERS
from ..core.tools.color import (COLOR_PARAMETERS, color_engine_contract, color_identity, preserve_unedited_color_fields,
                                ui_to_engine, engine_to_ui)
from ..core.tools.stage_defaults import (is_color_default as _is_color_default, is_beauty_default as _is_beauty_default,
                                         is_denoise_default as _is_denoise_default, is_crop_default as _is_crop_default)

CANVAS_TOOLS = ("crop", "shape_crop", "lasso", "id_photo")
FINAL_STAGES = ("id_photo",)


class WorkflowParameterError(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__(result.error.message_key)


def mapped_stage_node(session, tool_id, state=None):
    workflow = getattr(session, "_mapped_workflow", None)
    snapshot = getattr(session, "_mapped_snapshot", None)
    if workflow is None or snapshot is None:
        return None
    key = {"color_adjust": "color"}.get(tool_id, tool_id)
    state = session.snapshot() if state is None else state
    current = state.get(key, state["tools"].get(tool_id, {}))
    previous = snapshot.get(key, snapshot["tools"].get(tool_id, {}))
    if current != previous:
        return None
    for node in workflow.nodes:
        if (tool_id == "color_adjust" and node.node_type == "color_adjust"
                or tool_id == "crop" and node.node_type == "crop"):
            return node
        if node.node_type == "tool" and node.params.get("tool_id") == tool_id:
            return node
    return None


def _versioned_tool_params(session, tool_id, settings):
    if tool_id == "color_adjust":
        for node in session._mapped_workflow.nodes:
            if stage_identity(node) != tool_id:
                continue
            original_settings = node.params["settings"] if node.node_type == "tool" else node.params
            settings = preserve_unedited_color_fields(settings, original_settings,
                session._mapped_snapshot["color"], session.color_params)
            if node.node_type == "tool":
                reference = ToolReference.from_dict(node.params)
            elif node.node_type == "color_adjust":
                reference = ToolReference("color_adjust", 1, "color_adjust", "legacy-1")
            else:
                break
            if color_identity(original_settings) != (settings.get("look"), settings.get("process_version")):
                contract = color_engine_contract(settings, reference.params_version)
                if reference.params_version <= 2 or not contract.normalize(settings, contract.version).success:
                    break
            saved = copy.deepcopy(settings)
            if reference.params_version <= 2:
                saved.pop("look", None)
                saved.pop("process_version", None)
            return reference.to_params(saved)
    if tool_id == "beauty":
        workflow = getattr(session, "_mapped_workflow", None)
        for node in workflow.nodes if workflow is not None else ():
            if node.node_type == "tool" and node.params.get("tool_id") == tool_id:
                reference = ToolReference.from_dict(node.params)
                result = session.core.tools.effect_reference(tool_id, settings, reference=reference)
                if not result.success:
                    raise WorkflowParameterError(result)
                return result.data.to_params(copy.deepcopy(settings))
    result = session.core.tools.effect_reference(tool_id, settings)
    if not result.success:
        raise WorkflowParameterError(result)
    return result.data.to_params(copy.deepcopy(settings))


def stage_identity(node):
    if node.node_type == "tool":
        return node.params["tool_id"]
    return node.node_type


def stage_params(node):
    """这一步在面板上的参数。"""
    kind = node.node_type
    if kind == "color_adjust":
        return engine_to_ui(node.params)
    if kind == "crop":
        return {"crop_rect": copy.deepcopy(node.params["box"])}
    settings = copy.deepcopy(node.params["settings"])
    if kind == "tool" and node.params["tool_id"] == "color_adjust":
        return engine_to_ui(settings)
    return settings


def workflow_stages(workflow):
    """工作流里的各步骤及其面板参数，按处理顺序：{工具: 参数}。读不出参数的步骤取空字典。"""
    stages = {}
    for node in workflow.nodes:
        if node.node_type in ("source", "export"):
            continue
        try:
            stages[stage_identity(node)] = stage_params(node)
        except (KeyError, TypeError, ValueError):
            stages[node.params.get("tool_id") or node.params.get("tool") or node.node_type] = {}
    return stages


def stage_order(session):
    mapped = getattr(session, "_mapped_workflow", None)
    originals = {stage_identity(node): node for node in mapped.nodes
                 if node.node_type not in ("source", "export")} if mapped else {}
    ordered = list(originals)
    # 已有工作流里的先后不动；新加入的步骤按这个次序插入
    defaults = ["color_adjust", "denoise", "beauty", "crop",
                *(name for name in session.tool_params if name not in FINAL_STAGES),
                *(name for name in FINAL_STAGES if name in session.tool_params)]
    for name in defaults:
        if name in ordered:
            continue
        rank = defaults.index(name)
        position = next((index for index, current in enumerate(ordered)
                         if current in defaults and defaults.index(current) > rank), len(ordered))
        ordered.insert(position, name)
    return originals, ordered


def to_workflow(session, editing_tool=None, preview_target=None):
    preview_target = preview_target or PreviewTarget()
    if getattr(session, "read_only", False):
        if preview_target.mode != "full":
            raise ValueError("只读工作流不支持阶段编辑")
        return copy.deepcopy(session._raw_workflow)
    state = session.snapshot()
    if editing_tool is None and preview_target.mode == "full" and getattr(session, "_mapped_snapshot", None) == state:
        return copy.deepcopy(session._mapped_workflow)
    originals, ordered = stage_order(session)
    nodes = []
    target_reached = preview_target.mode == "full"
    for tool in ordered:
        if tool == editing_tool and tool in CANVAS_TOOLS:
            break
        node = mapped_stage_node(session, tool, state)
        if node is None:
            if tool == "color_adjust":
                if _is_color_default(session.color_params):
                    if preview_target != PreviewTarget.after_node(originals[tool].node_id if tool in originals else "color"):
                        continue
                params = ui_to_engine(session.color_params)
            elif tool in ("denoise", "beauty"):
                from ..core.tools.managed_models import MANAGED_BEAUTY_PARAMETERS
                params = session.denoise_params if tool == "denoise" else session.beauty_params
                neutral = _is_denoise_default if tool == "denoise" else _is_beauty_default
                if params != {}:
                    contract = DENOISE_PARAMETERS if tool == "denoise" else MANAGED_BEAUTY_PARAMETERS
                    checked = contract.normalize(params, contract.version)
                    if not checked.success:
                        raise WorkflowParameterError(CoreResult.fail(
                            checked.error.code, checked.error.message_key,
                            details={**checked.error.details, "tool_id": tool},
                        ))
                    params = checked.data
                if neutral(params) and not (tool == "beauty" and (tool in originals or "face_locator_model" in params)):
                    continue
            elif tool == "crop":
                if _is_crop_default(session.crop_params):
                    continue
                params = session.crop_params
            else:
                params = session.tool_params.get(tool)
                if params is None:
                    continue
            original = originals.get(tool)
            node = WorkflowNode(original.node_id if original else {"color_adjust": "color", "crop": "geometry"}.get(tool, tool),
                                "tool", _versioned_tool_params(session, tool, params))
        nodes.append(WorkflowNode(node.node_id, node.node_type, copy.deepcopy(node.params),
                                  inputs=(nodes[-1].node_id,) if nodes else (), version=node.version))
        if preview_target == PreviewTarget.after_node(node.node_id):
            target_reached = True
            break
    if not target_reached:
        raise ValueError("预览终点不存在")
    return WorkflowGraph(nodes=tuple(nodes), schema_version=getattr(session, "_mapped_workflow", WorkflowGraph()).schema_version)


def restore_workflow(session, workflow):
    color, beauty, denoise, crop, extras = {}, {}, {}, {}, {}
    seen = set()
    previous = None
    edges = set(workflow.edges)
    expected = []
    for node in workflow.nodes:
        parents = set(node.inputs) | {a for a, b in edges if b == node.node_id}
        if parents != ({previous} if previous else set()):
            raise ValueError("此工作流需要专业工作流编辑器")
        previous = node.node_id
        kind = node.node_type
        if kind in ("source", "export"):
            continue
        identity = kind
        if kind == "tool":
            identity = node.params.get("tool_id")
        if identity in seen:
            raise ValueError("此工作流包含重复处理阶段")
        seen.add(identity)
        if kind == "color_adjust":
            color = stage_params(node)
            remapped = ui_to_engine(color)
            for key, value in node.params.items():
                restored = remapped.get(key)
                equal = (math.isclose(value, restored, abs_tol=1e-8)
                         if type(value) in (int, float) and type(restored) in (int, float)
                         else value == restored)
                if not equal:
                    raise ValueError("调色参数超出当前面板精度")
            expected.append((kind, None))
        elif kind == "crop":
            crop = stage_params(node)
            expected.append((kind, None))
        elif kind == "tool":
            reference = ToolReference.from_dict(node.params)
            if not (reference.params_version in (1, 2)
                    or identity in ("color_adjust", "ai_enhance", "beauty", "bg_remove") and reference.params_version == 3
                    or identity == "color_adjust" and reference.params_version in (4, 5, 6, COLOR_PARAMETERS.version)):
                raise ValueError("此工具版本需要专业工作流编辑器")
            settings = copy.deepcopy(node.params["settings"])
            if identity == "beauty":
                beauty = settings
            elif identity == "denoise":
                denoise = settings
            elif identity == "color_adjust":
                color = stage_params(node)
                remapped = ui_to_engine(color)
                if any(not (math.isclose(value, remapped[key], abs_tol=1e-8)
                            if type(value) in (int, float) and type(remapped.get(key)) in (int, float)
                            else value == remapped.get(key)) for key, value in settings.items()):
                    raise ValueError("调色参数超出面板精度")
            elif identity == "crop":
                crop = settings
            else:
                extras[identity] = settings
            expected.append((kind, identity))
        else:
            raise ValueError("此工作流需要专业工作流编辑器")
    session._restore_snapshot({"color": color, "beauty": beauty, "denoise": denoise,
                               "crop": crop, "tools": extras})
    session._mapped_workflow = copy.deepcopy(workflow)
    session._mapped_snapshot = session.snapshot()
    normalized = to_workflow(session, editing_tool="")
    actual = [(node.node_type, node.params.get("tool_id"))
              for node in normalized.nodes]
    if actual != expected:
        raise ValueError("此工作流的处理顺序不支持当前工具面板")

