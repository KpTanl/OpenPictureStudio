"""AI 可调工具：每项声明给模型看的参数、范围与说明，由此生成提示词、回复结构和校验。

会话、上下文与解析都从这里取，不按工具名各写一份。参数的类型与范围来自工具自己的参数契约。
"""

from __future__ import annotations

import json
import math
from .assistant_images import MIN_REGION_SIDE, OBSERVATION_IMAGE_LIMIT
from dataclasses import dataclass
from typing import Mapping

from ..core.mixer_sampling import MIXER_CHANNELS
from ..core.tools.beauty import BEAUTY_PARAMETERS
from ..core.tools.color import (COLOR_UI_PARAMETERS, CURVE_CHANNELS, MIXER_PARTS, UI_RANGES, curve_channels,
                                is_neutral_curve, mixer_values, validate_params, merge_color_components,
                                COLOR_SYNC_FIELDS, exposure_centiev)

# AI 写入曲线时每通道的控制点数。Core 允许更多点，已有的曲线读回时照原样。
AGENT_CURVE_POINTS = (2, 16)


class ToolArgumentError(ValueError):
    """工具参数不合约：str() 仍是错误代码，details 写明字段路径与原因，交回模型。"""

    def __init__(self, field: str, reason: str):
        super().__init__("AI_TOOL_ARGUMENTS")
        self.details = {"field": field, "reason": reason}


def _curves(raw) -> dict:
    """AI 给出的曲线通道：整条替换，每通道 2—16 个 0—255 的整数点，从 x=0 到 x=255，横坐标严格递增。"""
    if not isinstance(raw, dict):
        raise ToolArgumentError("curves", "must be an object keyed by channel")
    low, high = AGENT_CURVE_POINTS
    result = {}
    for channel, points in raw.items():
        path = "curves." + str(channel)
        if channel not in CURVE_CHANNELS:
            raise ToolArgumentError(path, "unknown channel; use " + ", ".join(CURVE_CHANNELS))
        if not isinstance(points, list) or not low <= len(points) <= high:
            raise ToolArgumentError(path, f"must list {low}-{high} points")
        for index, point in enumerate(points):
            if (not isinstance(point, list) or len(point) != 2
                    or any(type(value) is not int or not 0 <= value <= 255 for value in point)):
                raise ToolArgumentError(f"{path}[{index}]", "must be [x, y] with integers 0-255")
        if points[0][0] != 0 or points[-1][0] != 255:
            raise ToolArgumentError(path, "must start at x=0 and end at x=255")
        if any(a[0] >= b[0] for a, b in zip(points, points[1:])):
            raise ToolArgumentError(path, "x must be strictly increasing")
        result[channel] = [list(point) for point in points]
    return result


def _mixer(raw) -> dict:
    """AI 给出的混合器分量：各为 −100—100 的整数绝对值。"""
    if not isinstance(raw, dict):
        raise ToolArgumentError("color_mixer", "must be an object keyed by color")
    result = {}
    for color, parts in raw.items():
        path = "color_mixer." + str(color)
        if color not in MIXER_CHANNELS:
            raise ToolArgumentError(path, "unknown color; use " + ", ".join(MIXER_CHANNELS))
        if not isinstance(parts, dict):
            raise ToolArgumentError(path, "must be an object of " + ", ".join(MIXER_PARTS))
        for part, value in parts.items():
            if part not in MIXER_PARTS:
                raise ToolArgumentError(f"{path}.{part}", "unknown component; use " + ", ".join(MIXER_PARTS))
            if type(value) is not int or not -100 <= value <= 100:
                raise ToolArgumentError(f"{path}.{part}", "must be an integer within -100..100")
        result[color] = dict(parts)
    return result


@dataclass(frozen=True)
class AssistantTool:
    id: str                       # 回复里的分组名
    tool_id: str                  # 工作流与面板里的工具 ID
    ranges: Mapping[str, tuple]   # 开放给 AI 的数值参数与闭区间
    guidance: str                 # 给模型的说明（英文，与其余提示词一致）
    nested: tuple[str, ...] = ()  # 开放给 AI 的嵌套参数：曲线与混合器，按通道、分量局部修改

    def normalize(self, values: dict) -> dict:
        """按工具的参数契约校验一组绝对取值；只保留开放给 AI 的参数。

        曲线与混合器只校验 AI 给出的那几项，按当前状态展开成完整取值在 changed_only() 里做。"""
        if not isinstance(values, dict):
            raise ToolArgumentError("values", "must be an object")
        unknown = sorted(set(values) - self.ranges.keys() - set(self.nested))
        if unknown:
            raise ToolArgumentError(str(unknown[0]), "is not an adjustable parameter")
        values = {key: value for key, value in values.items() if value is not None}
        numbers = {}
        for key, value in values.items():
            if key in self.nested:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ToolArgumentError(key, "must be a number")
            low, high = self.ranges[key]
            numbers[key] = round(value)
            if not low <= numbers[key] <= high:
                raise ToolArgumentError(key, f"must be within {low}..{high}")
        contract = COLOR_UI_PARAMETERS if self.id == "color" else BEAUTY_PARAMETERS
        checked = contract.normalize(numbers, contract.version)
        if not checked.success:
            raise ToolArgumentError("values", "rejected by the parameter contract")
        result = {key: checked.data[key] for key in numbers}
        if "curves" in values:
            result["curves"] = _curves(values["curves"])
        if "color_mixer" in values:
            result["color_mixer"] = _mixer(values["color_mixer"])
        return result


COLOR = AssistantTool(
    "color", "color_adjust",
    {item.name: (item.minimum, item.maximum) for item in COLOR_UI_PARAMETERS.fields if item.name in UI_RANGES},
    "Global color adjustments in panel units; exposure_amt is in 0.01 EV.",
    nested=("curves", "color_mixer"))

# 眼距不开放给 AI。
BEAUTY_KEYS = ("skin_smooth", "skin_brighten", "skin_rosy", "sharpen", "spot_remove", "eye_bag_remove",
               "teeth_whiten", "eye_enlarge", "face_slim", "nose_slim")
BEAUTY = AssistantTool(
    "beauty", "beauty",
    {item.name: (item.minimum, item.maximum) for item in BEAUTY_PARAMETERS.fields if item.name in BEAUTY_KEYS},
    "Portrait retouching strengths, 0 means off. Natural results usually stay within 10-40; "
    "eye_enlarge, face_slim and nose_slim change facial shape, keep them subtle (usually 0-20).")

TOOLS = {tool.id: tool for tool in (COLOR, BEAUTY)}


def _object(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict
    writes: bool = False
    photo: bool = False


_POINT = {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "integer", "minimum": 0, "maximum": 255}}
_NESTED_SCHEMAS = {
    "curves": _object({channel: {"type": "array", "minItems": AGENT_CURVE_POINTS[0], "maxItems": AGENT_CURVE_POINTS[1],
                                 "items": _POINT} for channel in CURVE_CHANNELS}),
    "color_mixer": _object({color: _object({part: {"type": "integer", "minimum": -100, "maximum": 100}
                                            for part in MIXER_PARTS}) for color in MIXER_CHANNELS}),
}


def _adjustment_schema(tool):
    properties = {key: {"type": "integer", "minimum": low, "maximum": high} for key, (low, high) in tool.ranges.items()}
    properties.update({key: _NESTED_SCHEMAS[key] for key in tool.nested})
    return _object({"values": _object(properties), "purpose": _PURPOSE_SCHEMA}, ("values",))


FEEDBACK_DIMENSIONS = ("overall", "tone", "white_balance", "saturation", "skin", "split_tone", "retouch",
                       *("color:" + color for color in MIXER_CHANNELS))
_PURPOSE_SCHEMA = {"type": "string", "enum": ["correction", "style"]}
FIT_PRESERVE_FIELDS = ("curves", *("curves." + channel for channel in CURVE_CHANNELS), "color_mixer",
                       *("color_mixer." + color for color in MIXER_CHANNELS),
                       *("color_mixer." + color + "." + part for color in MIXER_CHANNELS for part in MIXER_PARTS),
                       "vibrance_amt", "saturation_amt")
STYLE_PRESERVE_FIELDS = (*FIT_PRESERVE_FIELDS, *(key for key in COLOR.ranges if key not in FIT_PRESERVE_FIELDS),
                         "beauty", *("beauty." + key for key in BEAUTY_KEYS))
_VARIANT_SCHEMA = _object({"description": {"type": "string", "minLength": 1, "maxLength": 80},
                           "color": _adjustment_schema(COLOR)["properties"]["values"],
                           "beauty": _adjustment_schema(BEAUTY)["properties"]["values"]}, ("description",))
_EVALUATION_VERSION = _object({"kind": {"type": "string", "enum": ["revision", "variant"]},
    "revision": {"type": "string", "minLength": 1, "maxLength": 128},
    "group": {"type": "string", "minLength": 1, "maxLength": 128},
    "variant": {"type": "string", "enum": ["A", "B"]}}, ("kind",))
_OBSERVATION_REGIONS = {"type": "array", "minItems": 1, "maxItems": OBSERVATION_IMAGE_LIMIT, "items": _object({
    "name": {"type": "string", "minLength": 1, "maxLength": 120},
    "box": {"type": "array", "minItems": 4, "maxItems": 4,
            "items": {"type": "number", "minimum": 0, "maximum": 1}}}, ("name", "box"))}


_BROWSER_SCOPE = {"type": "string", "minLength": 1}
_BROWSER_PHOTOS = {"type": "array", "maxItems": 100, "items": {"type": "string", "minLength": 1}, "uniqueItems": True}
_BROWSER_FILTERS = _object({
    "rating": {"type": "string", "enum": ["all", "rated", "unrated", "0", "1", "2", "3", "4", "5", "1+", "2+", "3+", "4+", "5+"]},
    "mark": {"type": "string", "enum": ["all", "keep", "reject", "unmarked"]},
    "kind": {"type": "string", "enum": ["all", "raw", "image"]},
    "edited": {"type": "string", "enum": ["all", "edited", "unedited"]},
    "delivery": {"type": "string", "enum": ["all", "recorded", "unrecorded", "modified", "unknown"]}})

_LIBRARY_RULES = _object({
    "rating": {"type": "array", "maxItems": 6, "items": {"type": "integer", "minimum": 0, "maximum": 5}},
    "mark": {"type": "array", "items": {"type": "string", "enum": ["keep", "reject", "unmarked"]}},
    "kind": {"type": "array", "items": {"type": "string", "enum": ["raw", "image"]}},
    "edited": {"type": "array", "maxItems": 2, "items": {"type": "boolean"}},
    "delivery": {"type": "array", "items": {"type": "string", "enum": ["recorded", "unrecorded", "modified"]}},
    "availability": {"type": "array", "items": {"type": "string", "enum": ["available", "missing", "changed"]}},
    "keywords": _BROWSER_PHOTOS, "exclude_keywords": _BROWSER_PHOTOS,
    "keyword_mode": {"type": "string", "enum": ["all", "any"]}})

_LIBRARY_SPECS = (
    ("get_photo_library", "Read stable photo IDs, metadata revisions, keywords, collections, pending recovery and host-selected target references. Unknown state is pending, never a negative match.",
     {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, (), False),
    ("register_library_photos", "Register the fixed current browser selection using full source hashes. Identical copies at different paths keep distinct IDs. new_content=true requires the user's explicit request to register replaced content as a new photo.",
     {"scope": _BROWSER_SCOPE, "photos": _BROWSER_PHOTOS, "new_content": {"type": "boolean"}}, ("scope",), True),
    ("edit_library_metadata", "Change only specified metadata fields of stable library photo IDs from the current library scope. Does not edit image pixels or history.",
     {"scope": _BROWSER_SCOPE, "photos": _BROWSER_PHOTOS, "rating": {"type": "integer", "minimum": 0, "maximum": 5},
      "mark": {"type": "string", "enum": ["keep", "reject", "unmarked"]}, "add": _BROWSER_PHOTOS, "remove": _BROWSER_PHOTOS}, ("scope", "photos"), True),
    ("manage_library_keyword", "Create, rename or delete a flat keyword. Deletion removes keyword associations only; first explain the affected photo count to the user.",
     {"scope": _BROWSER_SCOPE, "action": {"type": "string", "enum": ["create", "rename", "delete"]}, "name": {"type": "string", "maxLength": 120}, "keyword": _BROWSER_SCOPE}, ("scope", "action"), True),
    ("manage_photo_collection", "Create or manage an ordinary or smart collection. Ordinary members are stable library IDs; add preserves existing positions. Smart rules use only fixed intersection dimensions. Deleting a collection keeps all photos.",
     {"scope": _BROWSER_SCOPE, "action": {"type": "string", "enum": ["create", "rename", "delete", "rules", "add", "remove", "reorder"]},
      "collection": _BROWSER_SCOPE, "name": {"type": "string", "maxLength": 120}, "kind": {"type": "string", "enum": ["ordinary", "smart"]}, "rules": _LIBRARY_RULES, "members": _BROWSER_PHOTOS}, ("scope", "action"), True),
    ("preview_photo_collection", "Read matched and pending stable photo IDs for saved collection or proposed rules, without changing browser selection.",
     {"scope": _BROWSER_SCOPE, "collection": _BROWSER_SCOPE, "rules": _LIBRARY_RULES}, ("scope",), False),
    ("show_photo_collection", "Set the shared browser collection filter. Omit collection to clear it. Existing fixed batch scopes are preserved.",
     {"scope": _BROWSER_SCOPE, "collection": _BROWSER_SCOPE}, ("scope",), True),
    ("find_missing_photos", "Verify registered sources in the background; return missing or changed stable photo IDs. Does not decode RGB or relocate files.", {}, (), False),
    ("prepare_photo_relocation", "Find exact-hash compatible candidates using a file/directory reference already selected by the user in the host. Review old/new path before confirming. Never invent target references.",
     {"scope": _BROWSER_SCOPE, "photo": _BROWSER_SCOPE, "target": _BROWSER_SCOPE}, ("scope", "photo", "target"), False),
    ("confirm_photo_relocation", "Only after explicit user confirmation of the reviewed candidate, change source binding while preserving photo/session/history identity. Does not move physical files. Running output/batch operations must first be handled.",
     {"proposal": _BROWSER_SCOPE}, ("proposal",), True),
    ("cancel_photo_relocation", "Discard one relocation proposal without changing source bindings.", {"proposal": _BROWSER_SCOPE}, ("proposal",), True),
    ("backup_photo_library", "Save library metadata to the host-selected new backup target. Photos, projects and drafts keep their own storage.",
     {"scope": _BROWSER_SCOPE, "target": _BROWSER_SCOPE}, ("scope", "target"), True),
    ("inspect_library_backup", "Validate the host-selected library backup read-only and return counts for user review.",
     {"scope": _BROWSER_SCOPE, "target": _BROWSER_SCOPE}, ("scope", "target"), False),
    ("restore_photo_library", "Only after user approval of inspected backup counts, replace library metadata and preserve a backup of the prior library. Does not rewrite physical photos, projects or drafts.",
     {"scope": _BROWSER_SCOPE, "target": _BROWSER_SCOPE, "confirmation": {"type": "string", "enum": ["restore"]}}, ("scope", "target", "confirmation"), True),
)


AGENT_TOOLS = {tool.name: tool for tool in (
    *(AgentTool(name, description, _object(properties, required), writes=writes)
      for name, description, properties, required, writes in _LIBRARY_SPECS),
    AgentTool("get_photo_groups", "Read runtime photo groups, fixed group samples and calibration plan references. Groups are independent of browser selection and style reference attachments.", _object()),
    AgentTool("create_photo_group", "Create a runtime group from managed browser photo references. Select a sample member. Does not edit photos. Get current browser scope first.",
              _object({"scope": _BROWSER_SCOPE, "name": {"type": "string", "minLength": 1, "maxLength": 120},
                       "photos": _BROWSER_PHOTOS, "sample": {"type": "string", "minLength": 1}}, ("scope", "name", "photos", "sample")), writes=True),
    AgentTool("update_photo_group", "Replace a group's name, members and fixed sample using the current browser scope. Invalidates old suggestions. Does not edit photos.",
              _object({"group": {"type": "string", "minLength": 1}, "scope": _BROWSER_SCOPE,
                       "name": {"type": "string", "minLength": 1, "maxLength": 120}, "photos": _BROWSER_PHOTOS,
                       "sample": {"type": "string", "minLength": 1}}, ("group", "scope", "name", "photos", "sample")), writes=True),
    AgentTool("suggest_photo_groups", "Suggest groups from source capture time, camera model, lens and available body serial. Does not create groups or identify same bodies by model. Missing timezone requires explicit user confirmation of a shared local clock before confirm_local_time=true. Never infer it. Omitted photos uses browser selection.",
              _object({"scope": _BROWSER_SCOPE, "photos": _BROWSER_PHOTOS, "minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                       "confirm_local_time": {"type": "boolean"}}, ("scope",))),
    AgentTool("propose_photo_calibration", "Prepare read-only, full Core-rendered per-photo exposure and/or RAW white-balance suggestions for a fixed group sample. Does not apply. Unknown metadata, unsupported RAW/modes, incompatible versions, excessive residuals and ranges are skipped explicitly. Exposure matches midtones; actual shooting precision is not 0.01 EV.",
              _object({"group": {"type": "string", "minLength": 1}, "operations": {"type": "array", "minItems": 1, "maxItems": 2,
                       "items": {"type": "string", "enum": ["exposure", "white_balance"]}}}, ("group", "operations"))),
    AgentTool("get_photo_calibration", "Read real per-photo calibration values, preview readiness, metadata uncertainty, quantization error, WB residuals and skip reasons. A suggestion is not an applied result.",
              _object({"calibration": {"type": "string", "minLength": 1}, "offset": {"type": "integer", "minimum": 0},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ("calibration",))),
    AgentTool("apply_photo_calibration", "Apply a prepared calibration only when the user requests calibration edits. Revalidates source, sample, workflow, decoder and tools; each photo follows propose, preview and commit. Returns a standard batch receipt, with retry_batch/undo_batch/redo_batch. Repeated application reuses the same batch and never repeats successful items.",
              _object({"calibration": {"type": "string", "minLength": 1}}, ("calibration",)), writes=True),
    AgentTool("get_browser_context", "Read the current shared photo-selection scope, filters, selected photos and managed photo references. "
              "Delivery recorded means a saved completion fact, not a fresh verification of the output file. "
              "Use the returned scope for browser commands; refresh after any list, selection or filter change. "
              "Browser changes affect future batch captures; existing get_batch_context and queued exports retain their fixed scopes.",
              _object({"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}})),
    AgentTool("set_photo_marks", "Set keep, reject or unmarked on managed photos, or the selected photos when photos is omitted. "
              "Reject only filters; no source files are moved or deleted. Uses the same storage as manual selection.",
              _object({"scope": _BROWSER_SCOPE, "photos": _BROWSER_PHOTOS,
                       "mark": {"type": "string", "enum": ["keep", "reject", "unmarked"]}}, ("scope", "mark")), writes=True),
    AgentTool("rate_photos", "Set 0 through 5 stars on managed photos, or the selected photos when omitted. 0 clears the rating.",
              _object({"scope": _BROWSER_SCOPE, "photos": _BROWSER_PHOTOS,
                       "rating": {"type": "integer", "minimum": 0, "maximum": 5}}, ("scope", "rating")), writes=True),
    AgentTool("filter_photos", "Update shared intersection filters for grid, filmstrip, navigation and future batch scopes. "
              "Unspecified filters remain. Unknown delivery cannot be assumed exported or unexported; metadata is prepared in the background.",
              _object({"scope": _BROWSER_SCOPE, "filters": _BROWSER_FILTERS}, ("scope", "filters")), writes=True),
    AgentTool("select_photos", "Replace the shared selection using photo references from the current scope. Empty photos clears selection. "
              "Does not open, decode or edit all selected photos.",
              _object({"scope": _BROWSER_SCOPE, "photos": _BROWSER_PHOTOS}, ("scope", "photos")), writes=True),
    AgentTool("compare_photos", "Open a read-only comparison of two selected/managed photos, or one photo against the fixed sample. "
              "Uses actual edited workflows and linked original-size zoom; does not send photos to the model.",
              _object({"scope": _BROWSER_SCOPE, "photos": _BROWSER_PHOTOS}, ("scope",)), writes=True),
    AgentTool("set_sample_photo", "Set or update a fixed browser sample from exactly one managed photo, or clear it. "
              "Independent of batch-sync sample snapshots and assistant style-reference attachments. Runtime-only and read-only.",
              _object({"scope": _BROWSER_SCOPE, "photos": _BROWSER_PHOTOS,
                       "action": {"type": "string", "enum": ["set", "update", "clear"]}}, ("scope", "action")), writes=True),
    AgentTool("set_browser_view", "Switch to the shared grid or return to the single-photo workspace without changing photo edits.",
              _object({"scope": _BROWSER_SCOPE, "view": {"type": "string", "enum": ["grid", "single"]}}, ("scope", "view")), writes=True),
    AgentTool("get_export_context", "Read the host-selected export directory and output specification, recent runtime queues and storage availability. "
              "Only the user configures paths and presets in Batch Export. Ask request_input if no destination is configured.", _object()),
    AgentTool("enqueue_exports", "Only for an explicit user request to export, enqueue the host-captured scope using its selected output settings. "
              "Captures full per-photo workflows; later editing does not change queued files. Returns queued, not exported. "
              "Use get_export_status for actual results. One new queue per user goal version; retry existing failures with control_exports. "
              "A queue continues after the assistant reply. Cancel it explicitly with control_exports.",
              _object({"scope": {"type": "string", "minLength": 1},
                       "range": {"type": "string", "enum": ["current", "selected", "filtered"]}}, ("scope",)), writes=True),
    AgentTool("get_export_status", "Read actual per-file export, cancellation and completion-record states. success means file published; "
              "record_state may still fail. Expired runtime queues cannot resume after restart.",
              _object({"export": {"type": "string", "minLength": 1}, "offset": {"type": "integer", "minimum": 0},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ("export",))),
    AgentTool("control_exports", "Pause before the next file, resume, cancel remaining unpublished files, or retry only failures of an existing queue. "
              "Completed files remain; retry does not render successful/cancelled files or repeat a failed completion-record write.",
              _object({"export": {"type": "string", "minLength": 1},
                       "action": {"type": "string", "enum": ["pause", "resume", "cancel", "retry"]}}, ("export", "action")), writes=True),
    AgentTool("get_export_history", "Read persisted completed export facts and runtime published files. Records are not proof that files still exist. "
              "Verification is explicit and never assumed after restart.",
              _object({"offset": {"type": "integer", "minimum": 0},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 100}})),
    AgentTool("verify_export", "Hash and verify one completed output returned by get_export_history. Returns actual file and render-contract state.",
              _object({"record": {"type": "string", "minLength": 1}}, ("record",))),
    AgentTool("repair_export_record", "For a published runtime output whose record write failed, verify its exact product hash and retry only recording. "
              "Never render again or record a changed/missing file.",
              _object({"record": {"type": "string", "minLength": 1}}, ("record",)), writes=True),
    AgentTool("view_batch_photo", "Read-only observation of one requested batch target or the fixed sample. "
              "Use a photo reference returned by get_batch_status or sample_snapshot returned by get_batch_context. "
              "Observe a whole preview first, then use its frame_id for normalized regions. native requires regions. "
              "Sharing settings, model capability, image budgets and source/revision checks apply. "
              "Never infer quality for unobserved photos from a small sample.",
              _object({"photo": {"type": "string", "minLength": 1},
                       "quality": {"type": "string", "enum": ["preview", "native"]},
                       "frame_id": {"type": "string", "minLength": 1, "maxLength": 64},
                       "regions": _OBSERVATION_REGIONS}, ("photo",))),
    AgentTool("get_batch_context", "Read the host's fixed selected/filtered photo scope, sample_photo snapshot, supported fields and recent runtime batches. "
              "Use for an explicit batch request. sample_snapshot is independent of the style reference attachment. "
              "Batch sync replaces selected global parameters, including neutral values, while preserving other edits. "
              "Only sync_batch_adjustments and adjust_batch_exposure use parameter-only commits. "
              "Sample parameter fields are paginated by offset/limit in panel units. "
              "Ask request_input when the requested scope is unclear; never infer whole-library permission.",
              _object({"offset": {"type": "integer", "minimum": 0},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 100}})),
    AgentTool("sync_batch_adjustments", "For an explicit user request, copy the fixed sample_photo's selected global fields to the captured scope. "
              "Excludes the sample itself. Omitted fields and each target's masks, crop, beauty, look and effect versions remain. "
              "Waits for actual per-photo commits and draft save results. No automatic bulk image sharing.",
              _object({"scope": {"type": "string", "minLength": 1}, "sample_snapshot": {"type": "string", "minLength": 1},
                       "fields": {"type": "array", "minItems": 1, "maxItems": len(COLOR_SYNC_FIELDS),
                                  "items": {"type": "string", "enum": [*COLOR_SYNC_FIELDS, "curves", "color_mixer"]}},
                       "range": {"type": "string", "enum": ["selected", "filtered"]}},
                      ("scope", "sample_snapshot", "fields")), writes=True),
    AgentTool("adjust_batch_exposure", "For an explicit user request, add the same EV delta to each captured target's current exposure. "
              "delta_ev must be a multiple of 0.01; results outside -3..3 EV are skipped. "
              "At most one new relative exposure batch per user goal version. Use retry_batch to continue failed items; "
              "a different tool call ID does not authorize another increment. The user must send a new request for another increment.",
              _object({"scope": {"type": "string", "minLength": 1}, "delta_ev": {"type": "number"},
                       "range": {"type": "string", "enum": ["selected", "filtered"]}}, ("scope", "delta_ev")), writes=True),
    AgentTool("get_batch_status", "Read actual runtime batch results and history positions, paginated. Old saved chat receipts cannot restore expired batches.",
              _object({"batch": {"type": "string", "minLength": 1}, "offset": {"type": "integer", "minimum": 0},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ("batch",))),
    *(AgentTool(name, description, _object({"batch": {"type": "string", "minLength": 1}}, ("batch",)), writes=True)
      for name, description in (
          ("retry_batch", "Continue only uncommitted failed/cancelled items of the original batch; retain original candidates. "
                          "Applied, unchanged and previously undone successes are skipped; save failures retry only saving."),
          ("undo_batch", "Undo the requested runtime batch using original per-photo history. Later edits remain and report conflicts."),
          ("redo_batch", "Redo the requested runtime batch where the original undo state remains valid. Reports actual conflicts."))),
    AgentTool("evaluate_versions", "Read-only independent evaluation of two committed revisions or saved variants against the "
              "user's goal and current reference. Revision may be current, start, or a historical revision ID; a variant needs "
              "group and variant A/B. Main backend swaps image order and accepts only consistent choices; Decisions is optional "
              "and needs a separately selected connection. A call counts once, all requests count toward time and usage. "
              "Image sharing and model image capability apply. Never interpret this as adoption, approval or proof of success. "
              "Use only when the user asks for an independent comparison; evidence insufficiency is a valid result.",
              _object({"a": _EVALUATION_VERSION, "b": _EVALUATION_VERSION,
                       "dimensions": {"type": "array", "minItems": 1, "maxItems": len(FEEDBACK_DIMENSIONS),
                                      "items": {"type": "string", "enum": list(FEEDBACK_DIMENSIONS)}},
                       "backend": {"type": "string", "enum": ["main", "decisions"]}}, ("a", "b")), photo=True),
    AgentTool("list_styles", "List saved style names, versions, enabled status and sample scene descriptions when the user names or selects a style. Do not choose a style automatically.", _object()),
    AgentTool("remember_style", "Only after the user explicitly asks to save this style or remember a long-term photography preference. "
              "Opens a confirmation card and waits; no storage happens until the user confirms. Supply a brief suggested name and scene "
              "for a style, or the user's preference text. A style needs the current photo and at least two historical revisions.",
              _object({"kind": {"type": "string", "enum": ["style", "preference"]},
                       "name": {"type": "string", "maxLength": 80}, "scene": {"type": "string", "maxLength": 240},
                       "text": {"type": "string", "minLength": 1, "maxLength": 2000},
                       "style_id": {"type": "string", "minLength": 1}}, ("kind",))),
    AgentTool("apply_style", "Read-only suggestions for an explicitly requested or selected saved style. Correct this photo's exposure "
              "and white balance first. Samples transfer paired processing changes, never the source photo's full settings. "
              "An optional sample_id selects a sample; otherwise measurements choose one and explain why. Sharing analysis must be enabled. "
              "Suggestions still need adjust_color/adjust_beauty or propose_variants to edit the photo.",
              _object({"style_id": {"type": "string", "minLength": 1}, "sample_id": {"type": "string", "minLength": 1},
                       "strength": {"type": "integer", "minimum": 0, "maximum": 100},
                       "preserve": {"type": "array", "maxItems": len(STYLE_PRESERVE_FIELDS),
                           "items": {"type": "string", "enum": list(STYLE_PRESERVE_FIELDS)}}}, ("style_id",)), photo=True),
    AgentTool("propose_variants", "Only when the user asks for two alternatives, render A and B on the same baseline without editing "
              "the editor. Then wait for the user's choice or feedback. Supply changed absolute parameters and a short direction "
              "for each. Optionally derive both from a saved group variant. Combining results means proposing a new pair. "
              "Feedback annotations describe the latest user's actual words, never invented approval.",
              _object({"A": _VARIANT_SCHEMA, "B": _VARIANT_SCHEMA, "purpose": _PURPOSE_SCHEMA,
                       "base": _object({"group": {"type": "string", "minLength": 1},
                                        "variant": {"type": "string", "enum": ["A", "B"]}}, ("group", "variant")),
                       "feedback": _object({"group": {"type": "string", "minLength": 1},
                            "selection": {"type": "string", "enum": ["A", "B", "neither", "unselected"]},
                            "dimensions": {"type": "array", "maxItems": 15, "items": _object({
                                "name": {"type": "string", "enum": list(FEEDBACK_DIMENSIONS)},
                                "direction": {"type": "string", "enum": ["A_better", "B_better", "too_much", "too_little"]}},
                                ("name", "direction"))}}, ("group", "selection", "dimensions"))}, ("A", "B")),
              writes=True, photo=True),
    AgentTool("inspect_photo", "Read the current photo identity, revision, editing parameters and available capabilities.", _object(), photo=True),
    AgentTool("analyze_photo", "Measure the current image brightness and color and query available face analysis. No aesthetic score is inferred.", _object(), photo=True),
    AgentTool("compare_reference", "Compare the current displayed image with the current reference using measurement specification 1. "
              "Returns current-minus-reference luma, Lab chroma, weak neutral split-tone and comparable hue differences. "
              "Color distribution overlap is not semantic comparability or an aesthetic score. Analysis sharing must be enabled.",
              _object(), photo=True),
    AgentTool("fit_reference", "Read-only fit against the current reference. Returns absolute color values, measured residuals, "
              "accepted or reverted steps and fixed skip codes. Specify only requested dimensions; split_tone is weak evidence "
              "and requires the user's explicit request to match color toning. For hue supply the requested colors. Preserve "
              "locks exact parameter paths. Nothing is applied: use adjust_color or propose_variants to write selected suggestions. "
              "Each call counts once; internal Core renders consume task time. Small previews approximate spatial effects.",
              _object({"target": {"type": "string", "enum": ["reference"]},
                       "dimensions": {"type": "array", "minItems": 1, "maxItems": 4,
                                      "items": {"type": "string", "enum": ["tone", "split_tone", "hue", "chroma"]}},
                       "colors": {"type": "array", "minItems": 1, "maxItems": 8,
                                  "items": {"type": "string", "enum": list(MIXER_CHANNELS)}},
                       "strength": {"type": "integer", "minimum": 0, "maximum": 100},
                       "preserve": {"type": "array", "maxItems": len(FIT_PRESERVE_FIELDS),
                                    "items": {"type": "string", "enum": list(FIT_PRESERVE_FIELDS)}}}, ("dimensions",)), photo=True),
    AgentTool("view_photo", "Read-only observation of the current photo, its unedited original, or the exact task-start workflow. "
              "First inspect an overview and use its frame_id to select normalized regions. native requires explicit regions "
              "and configured effective image size; it crops full-resolution workflow output at 1:1 pixels. No resizing or "
              f"quality fallback occurs. Each sent region must be at least {MIN_REGION_SIDE} pixels on both sides and native must fit the "
              f"model's effective long edge. At most {OBSERVATION_IMAGE_LIMIT} images, divided by the number of sources. "
              "compare_to lists other sources, returned before source for each region. Matching geometry and pixel sizes "
              "are required for region comparisons. Original regions need an original frame_id; current face coordinates "
              "cannot be reused on original. Region requests send only the requested crops. All sharing choices apply.",
              _object({"source": {"type": "string", "enum": ["current", "original", "start"]},
                       "quality": {"type": "string", "enum": ["preview", "native"]},
                       "frame_id": {"type": "string", "minLength": 1, "maxLength": 64},
                       "regions": _OBSERVATION_REGIONS,
                       "compare_to": {"type": "array", "minItems": 1, "maxItems": 2,
                                      "items": {"type": "string", "enum": ["original", "start", "current"]}}},
                      ("source",)), photo=True),
    AgentTool("adjust_color", "Set changed global color parameters to absolute values in panel units. exposure_amt is 0.01 EV. "
              "curves replaces whole channels (rgb master, then r, g, b) with 2-16 integer [x, y] points on the display-encoded "
              "0-255 scale, starting at x=0 and ending at x=255 with strictly increasing x. color_mixer sets hue, sat and lum "
              "(-100 to 100) of the eight colors. Channels, colors and components you omit keep their current values; reset a "
              "curve with [[0, 0], [255, 255]] and a mixer component with 0. The edit is immediately committed to history.",
              _adjustment_schema(COLOR), writes=True, photo=True),
    AgentTool("adjust_beauty", "Set changed portrait parameters to absolute values. Preserve facial shape unless the user explicitly asks to change it. The edit is immediately committed to history.",
              _adjustment_schema(BEAUTY), writes=True, photo=True),
    AgentTool("inspect_history", "Read this task's completed edits, cumulative parameter changes and current revision.", _object(), photo=True),
    AgentTool("update_plan", "Maintain a brief task plan when useful. Simple requests may be handled without a plan.",
              _object({"steps": {"type": "array", "maxItems": 12, "items": _object({
                  "title": {"type": "string", "minLength": 1, "maxLength": 240},
                  "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, ("title", "status"))}}, ("steps",))),
    AgentTool("request_input", "Ask the user for information necessary to continue this task, then wait for their reply.",
              _object({"question": {"type": "string", "minLength": 1, "maxLength": 8000}}, ("question",))),
)}


def agent_functions(mode, photo, available=(), *, enabled=True, reference=False):
    if not enabled:
        return []
    return [{"name": tool.name, "description": tool.description, "parameters": tool.parameters, "strict": False}
            for tool in AGENT_TOOLS.values()
            if not (tool.writes and mode != "agent" or tool.photo and photo is None
                    or tool.name in ("compare_reference", "fit_reference") and not reference
                    or tool.name == "fit_reference" and "color" not in available
                    or tool.name == "adjust_beauty" and "beauty" not in available)]


def _validate_argument(value, schema, path=""):
    """按工具的参数结构校验；不合约时给出字段路径（如 values.curves.rgb[2]）与原因。"""
    kind = schema["type"]
    where = path or "arguments"
    if kind == "object":
        if not isinstance(value, dict):
            raise ToolArgumentError(where, "must be an object")
        extra = sorted(set(value) - schema["properties"].keys())
        if extra:
            raise ToolArgumentError(f"{path}.{extra[0]}" if path else extra[0], "is not allowed here")
        missing = sorted(set(schema["required"]) - value.keys())
        if missing:
            raise ToolArgumentError(f"{path}.{missing[0]}" if path else missing[0], "is required")
        for key, item in value.items():
            _validate_argument(item, schema["properties"][key], f"{path}.{key}" if path else key)
    elif kind == "array":
        low, high = schema.get("minItems", 0), schema.get("maxItems", 32)
        if not isinstance(value, list) or not low <= len(value) <= high:
            raise ToolArgumentError(where, f"must be a list of {low}-{high} items")
        for index, item in enumerate(value):
            _validate_argument(item, schema["items"], f"{where}[{index}]")
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 32000):
            raise ToolArgumentError(where, "must be a string of the allowed length")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolArgumentError(where, "must be one of " + ", ".join(schema["enum"]))
    elif kind == "integer":
        low, high = schema.get("minimum", -math.inf), schema.get("maximum", math.inf)
        if type(value) is not int or not low <= value <= high:
            raise ToolArgumentError(where, f"must be an integer within {low}..{high}")
    elif kind == "boolean":
        if type(value) is not bool:
            raise ToolArgumentError(where, "must be a boolean")
    elif kind == "number":
        low, high = schema.get("minimum", -math.inf), schema.get("maximum", math.inf)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ToolArgumentError(where, f"must be a finite number within {low}..{high}")


def agent_arguments(name, arguments, allowed):
    if name not in allowed or name not in AGENT_TOOLS:
        raise ValueError("AI_TOOL_UNAVAILABLE")
    try:
        values = json.loads(arguments)
    except (TypeError, ValueError):
        raise ToolArgumentError("arguments", "must be a JSON object") from None
    _validate_argument(values, AGENT_TOOLS[name].parameters)
    if name == "adjust_batch_exposure":
        try:
            exposure_centiev(values["delta_ev"])
        except ValueError:
            raise ToolArgumentError("delta_ev", "must be a finite multiple of 0.01 EV") from None
    if name in ("view_photo", "view_batch_photo"):
        comparisons = values.get("compare_to", [])
        if len(set(comparisons)) != len(comparisons) or values.get("source") in comparisons:
            raise ToolArgumentError("compare_to", "must contain unique sources other than source")
        regions = values.get("regions", [])
        if values.get("quality") == "native" and not regions:
            raise ToolArgumentError("regions", "is required for native quality")
        if regions and not values.get("frame_id"):
            raise ToolArgumentError("frame_id", "is required for regions")
        if len(regions) * (1 + len(comparisons)) > OBSERVATION_IMAGE_LIMIT:
            raise ToolArgumentError("regions", f"at most {OBSERVATION_IMAGE_LIMIT // (1 + len(comparisons))} regions for these sources")
        for index, region in enumerate(regions):
            left, top, right, bottom = region["box"]
            if left >= right or top >= bottom:
                raise ToolArgumentError(f"regions[{index}].box", "must have left < right and top < bottom")
    if name in ("adjust_color", "adjust_beauty"):
        values["values"] = TOOLS["color" if name == "adjust_color" else "beauty"].normalize(values["values"])
    if name == "fit_reference":
        if "hue" in values["dimensions"] and "colors" not in values:
            raise ToolArgumentError("colors", "is required when fitting hue")
        for key in ("dimensions", "colors", "preserve"):
            if key in values and len(set(values[key])) != len(values[key]):
                raise ToolArgumentError(key, "must contain unique items")
    if name == "remember_style" and values["kind"] == "preference" and not values.get("text", "").strip():
        raise ToolArgumentError("text", "is required for a long-term preference")
    if name == "evaluate_versions":
        for key in ("a", "b"):
            item = values[key]
            required = {"revision"} if item["kind"] == "revision" else {"group", "variant"}
            if set(item) != required | {"kind"}:
                raise ToolArgumentError(key, "must contain kind and " + ", ".join(sorted(required)))
        if len(set(values.get("dimensions", ()))) != len(values.get("dimensions", ())):
            raise ToolArgumentError("dimensions", "must contain unique items")
    return values


def grouped(adjustments: Mapping) -> dict:
    """按工具分组的调整；旧消息里只有调色参数的平铺结构归入调色。"""
    if adjustments and set(adjustments) <= TOOLS.keys():
        return {key: dict(value) for key, value in adjustments.items() if value}
    return {"color": dict(adjustments)} if adjustments else {}


def change_count(adjustments: Mapping) -> int:
    return sum(len(values) for values in grouped(adjustments).values())


def instructions(available: tuple[str, ...], unavailable: Mapping[str, str], *, review: bool) -> str:
    shape = ", ".join(f'"{tool}":{{...}}' for tool in available)
    text = ('Return only a JSON object {"summary":"brief description",' + ('"done":false,' if review else "")
            + '"adjustments":{' + shape + "}}. "
            "Adjustments are the desired absolute values after editing, relative to the current editing state, "
            "not increments. Return changed parameters only. Keep summary within two concise sentences; "
            "describe the changes without listing all current parameter values. ")
    for tool in available:
        item = TOOLS[tool]
        text += f"Allowed {tool} parameters and inclusive ranges: {json.dumps(dict(item.ranges))}. {item.guidance} "
    for tool, reason in unavailable.items():
        text += f"{tool} adjustments are unavailable now: {reason}. "
    text += ("No masks, crop, object removal or generated pixels. If the request cannot be done with these "
             "parameters, explain in summary and return empty adjustments. ")
    return text


def proposal_schema(available: tuple[str, ...], *, review: bool = False) -> dict:
    groups = {tool: {"type": "object", "properties": {
        key: {"anyOf": [{"type": "integer", "minimum": low, "maximum": high}, {"type": "null"}]}
        for key, (low, high) in TOOLS[tool].ranges.items()},
        "required": list(TOOLS[tool].ranges), "additionalProperties": False} for tool in available}
    properties = {"summary": {"type": "string"},
                  "adjustments": {"type": "object", "properties": groups, "required": list(available),
                                  "additionalProperties": False}}
    if review:
        properties["done"] = {"type": "boolean"}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _json_object(text: str):
    """整段是 JSON 时直接解析；否则取回复里第一个能解析的 JSON 对象（含代码块中的）。"""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _end = decoder.raw_decode(text, start)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
        start = text.find("{", start + 1)
    raise ValueError("AI_RESPONSE_INVALID")


def parse_proposal(text: str, available: tuple[str, ...] = ("color",)) -> dict:
    """解析调整回复，返回 {"summary", "adjustments": {工具: {参数: 值}}, "done"}。

    回复里没有可用的 JSON、或 JSON 不合约时抛出 AI_RESPONSE_INVALID。"""
    try:
        result = _json_object(text)
        if not isinstance(result, dict) or not {"summary", "adjustments"} <= set(result) \
                or set(result) - {"summary", "adjustments", "done"}:
            raise ValueError("AI_RESPONSE_INVALID")
        summary = result["summary"]
        if not isinstance(summary, str) or len(summary) > 8000:
            raise ValueError("AI_RESPONSE_INVALID")
        done = result.get("done")
        if done is not None and not isinstance(done, bool):
            raise ValueError("AI_RESPONSE_INVALID")
        values = result["adjustments"]
        if not isinstance(values, dict):
            raise ValueError("AI_RESPONSE_INVALID")
        if values and not set(values) <= TOOLS.keys():
            values = {"color": values}
        if set(values) - set(available):
            raise ValueError("AI_RESPONSE_INVALID")
        adjustments = {}
        for tool, group in values.items():
            checked = TOOLS[tool].normalize(group if group is not None else {})
            if checked:
                adjustments[tool] = checked
        return {"summary": summary, "adjustments": adjustments, "done": done}
    except (ValueError, TypeError, KeyError):
        raise ValueError("AI_RESPONSE_INVALID") from None


def current_values(photo, tool: str, overrides: Mapping | None = None) -> dict:
    """照片当前状态（叠加 overrides 之后）里这个工具开放给 AI 的参数取值。

    曲线只列非中性的通道，混合器只列非零的分量，没列出的即为中性；读回无损，已有的小数坐标与多点曲线照原样。"""
    state = {**(photo.color if tool == "color" else photo.beauty), **(overrides or {})}
    defaults = COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version) if tool == "color" \
        else BEAUTY_PARAMETERS.defaults(BEAUTY_PARAMETERS.version)
    values = {key: state.get(key, defaults[key]) for key in TOOLS[tool].ranges}
    if "curves" in TOOLS[tool].nested:
        values["curves"] = {channel: points for channel, points in curve_channels(state.get("curves")).items()
                            if not is_neutral_curve(points)}
    if "color_mixer" in TOOLS[tool].nested:
        values["color_mixer"] = {color: {part: value for part, value in parts.items() if value}
                                 for color, parts in mixer_values(state.get("color_mixer")).items() if any(parts.values())}
    return values


def changed_only(photo, adjustments: Mapping) -> dict:
    """去掉与照片当前状态相同的取值。

    曲线与混合器的局部修改在这里按当前状态展开：给出的通道整条替换、给出的分量写入绝对值，其余照原样，
    得到完整的 curves 与 color_mixer 顶层取值并整体校验；之后按顶层键合并即可。"""
    result = {}
    for tool, values in grouped(adjustments).items():
        current = current_values(photo, tool)
        state = photo.color if tool == "color" else photo.beauty
        changed = {key: value for key, value in values.items()
                   if key not in TOOLS[tool].nested and value != current.get(key)}
        merged = merge_color_components(state, values)
        for key in TOOLS[tool].nested:
            if values.get(key) and current_values(photo, tool, {key: merged[key]})[key] != current[key]:
                changed[key] = merged[key]
        try:
            validate_params({key: changed[key] for key in TOOLS[tool].nested if key in changed})
        except ValueError:
            raise ToolArgumentError("values", "the merged curves or color mixer are invalid") from None
        if changed:
            result[tool] = changed
    return result


def flat_values(values: Mapping) -> dict:
    """把一组取值里的曲线与混合器展开成 curves.rgb、color_mixer.orange.sat 这样的逐项取值，供界面逐行列出。"""
    result = {}
    for key, value in values.items():
        if not isinstance(value, dict):
            result[key] = value
            continue
        for sub, item in value.items():
            if isinstance(item, dict):
                result.update({f"{key}.{sub}.{part}": part_value for part, part_value in item.items()})
            else:
                result[f"{key}.{sub}"] = item
    return result
