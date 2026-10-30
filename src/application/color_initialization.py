from ..core.contracts import WorkflowGraph, WorkflowNode
from ..core.contracts.image_display import (DEFAULT_NON_RAW_DISPLAY_MODE, DEFAULT_RAW_DISPLAY_MODE,
                                            NonRawDisplayMode, RawDisplayMode)
from ..core.tools.color import (COLOR_PARAMETERS, COLOR_UI_PARAMETERS, OPS_LOOK, is_color_default_ui,
                               migrate_legacy_color, color_ui_contract, color_engine_contract, canonical_look)
from .result_types import CoreOperationError, CoreResult, ErrorCode


def display_look(*, is_raw, raw_display_mode=DEFAULT_RAW_DISPLAY_MODE,
                 non_raw_display_mode=DEFAULT_NON_RAW_DISPLAY_MODE):
    mode = RawDisplayMode(raw_display_mode) if is_raw else NonRawDisplayMode(non_raw_display_mode)
    return "none" if mode == RawDisplayMode.ORIGINAL else OPS_LOOK


def color_draft_has_state(params):
    return bool({"look", "process_version"} & params.keys()) or not is_color_default_ui(params)


def initial_color_identity(drafts, *, is_raw, raw_display_mode,
                           non_raw_display_mode=DEFAULT_NON_RAW_DISPLAY_MODE):
    color = drafts.get("color_adjust")
    if color is not None:
        migrated = migrate_legacy_color(color, ui=True)
        contract = color_ui_contract(migrated)
        checked = contract.normalize(migrated, contract.version)
        if not checked.success:
            raise CoreOperationError(checked.error)
        if color_draft_has_state(color):
            return checked.data["look"], checked.data["process_version"]
        drafts = {tool: params for tool, params in drafts.items() if tool != "color_adjust"}
    defaults = COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version)
    if not drafts:
        return display_look(is_raw=is_raw, raw_display_mode=raw_display_mode,
                            non_raw_display_mode=non_raw_display_mode), defaults["process_version"]
    return defaults["look"], defaults["process_version"]


def color_baseline_workflow(tools, identity):
    look, process_version = identity
    contract = color_engine_contract({"look": look, "process_version": process_version})
    defaults = contract.defaults(contract.version)
    latest = COLOR_PARAMETERS.defaults(COLOR_PARAMETERS.version)
    if look == latest["look"] and process_version == latest["process_version"]:
        return WorkflowGraph()
    settings = {**defaults, "look": look, "process_version": process_version}
    reference = tools.effect_reference("color_adjust", settings).unwrap()
    return WorkflowGraph(nodes=(WorkflowNode("color", "tool", reference.to_params(settings)),))


def baseline_color_settings(workflow):
    for node in workflow.nodes:
        if node.node_type == "color_adjust":
            settings = node.params
        elif node.node_type == "tool" and node.params.get("tool_id") == "color_adjust":
            settings = node.params["settings"]
        else:
            continue
        migrated = migrate_legacy_color(settings)
        contract = color_engine_contract(migrated)
        return contract.normalize(migrated, contract.version).unwrap()
    return COLOR_PARAMETERS.defaults(COLOR_PARAMETERS.version)


def viewer_matches_baseline(workflow, *, is_raw, raw_display_mode,
                            non_raw_display_mode=DEFAULT_NON_RAW_DISPLAY_MODE):
    if is_raw and RawDisplayMode(raw_display_mode) == RawDisplayMode.CAMERA:
        return False
    return canonical_look(baseline_color_settings(workflow)["look"]) == display_look(
        is_raw=is_raw, raw_display_mode=raw_display_mode, non_raw_display_mode=non_raw_display_mode)


def write_session_drafts(drafts, image_path, updates, *, color_params, baseline_identity):
    defaults = COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version)
    explicit = (bool(color_params) or drafts.has_draft(image_path, "color_adjust")
                or baseline_identity != (defaults["look"], defaults["process_version"]))
    if explicit:
        try:
            migrated = migrate_legacy_color(color_params, ui=True)
            contract = color_ui_contract(migrated)
            checked = contract.normalize(migrated, contract.version)
        except ValueError as error:
            return CoreResult.from_exception(error, code=ErrorCode.PARAMETER_INVALID,
                                             message_key="error.workflow.params")
        if not checked.success:
            return checked
        updates = {**updates, "color_adjust": checked.data}
    drafts.update_drafts(image_path, updates)
    return CoreResult.ok()
