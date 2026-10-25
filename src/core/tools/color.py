from __future__ import annotations

import copy
import math

from ..mixer_sampling import MIXER_CHANNELS
from ..local_masks import prepare_masks
from .catalog import ParameterContract, ParameterField


MASK_BAND_ROWS = 64
LEGACY_OPS_LOOK = "ops_base"
OPS_LOOK = "ops_v2"
COLOR_LOOK_ALIASES = {"opv_base": LEGACY_OPS_LOOK, "opv_v2": OPS_LOOK}
_LEGACY_LOOKS = tuple(COLOR_LOOK_ALIASES)
COLOR_LOOKS = ("none", LEGACY_OPS_LOOK, OPS_LOOK)


def canonical_look(look):
    return COLOR_LOOK_ALIASES.get(look, look)


def color_look_options(look, process_version, *, version=None):
    contract = color_engine_contract({"look": look, "process_version": process_version}, version)
    current = _LEGACY_LOOKS[1] if contract.version < 7 else OPS_LOOK
    return ("none", current) + ((look,) if canonical_look(look) == LEGACY_OPS_LOOK else ())

CURVE_CHANNELS = ("rgb", "r", "g", "b")
MIXER_PARTS = ("hue", "sat", "lum")
NEUTRAL_CURVE = ((0, 0), (255, 255))


NUMERIC_RANGES = {
    "exposure": (-20, 20), "brightness": (0, 16), "contrast": (0, 3),
    "saturation": (0, 3), "sharpness": (0, 3),
    **{key: (-100, 100) for key in ("temperature", "tint", "highlights", "shadows",
                                    "whites", "blacks", "texture", "clarity", "dehaze", "vibrance")},
}

UI_RANGES = {
    "temperature": (-100, 100), "tint": (-100, 100), "exposure_amt": (-300, 300),
    **{key: (-100, 100) for key in ("contrast_amt", "highlights", "shadows", "whites", "blacks",
                                   "texture_amt", "clarity_amt", "dehaze_amt", "vibrance_amt", "saturation_amt")},
}


def number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("调色参数数值或范围无效")


def point(value, high=1):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("坐标必须包含两个数值")
    for component in value:
        number(component, 0, high)


def validate_legacy_params(params):
    if set(params) - (set(NUMERIC_RANGES) | {"curves", "color_mixer", "masks", "look", "process_version"}):
        raise ValueError("未知调色参数")
    if "look" in params and params["look"] not in COLOR_LOOKS and params["look"] not in COLOR_LOOK_ALIASES:
        raise ValueError("观感无效")
    if "process_version" in params and (type(params["process_version"]) is not int or params["process_version"] not in (1, 2)):
        raise ValueError("曝光处理版本无效")
    for key, bounds in NUMERIC_RANGES.items():
        if key in params:
            number(params[key], *bounds)
    curves = params.get("curves")
    if curves is not None:
        if not isinstance(curves, dict) or set(curves) - set(CURVE_CHANNELS):
            raise ValueError("曲线通道无效")
        for points in curves.values():
            if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= 256:
                raise ValueError("曲线至少需要两个点")
            for value in points:
                point(value, 255)
            if any(a[0] >= b[0] for a, b in zip(points, points[1:])):
                raise ValueError("曲线横坐标必须递增")
    mixer = params.get("color_mixer")
    if mixer is not None:
        if not isinstance(mixer, dict) or set(mixer) - set(MIXER_CHANNELS):
            raise ValueError("色彩混合器通道无效")
        for values in mixer.values():
            if not isinstance(values, dict) or set(values) - set(MIXER_PARTS):
                raise ValueError("色彩混合器参数无效")
            for value in values.values():
                number(value, -100, 100)
    masks = params.get("masks")
    if masks is not None:
        if not isinstance(masks, list) or len(masks) > 128:
            raise ValueError("蒙版必须为列表")
        for mask in masks:
            if not isinstance(mask, dict) or mask.get("type", "linear") not in ("linear", "radial"):
                raise ValueError("蒙版类型无效")
            if set(mask) - {"id", "name", "type", "enabled", "p0", "p1", "center", "radius", "angle", "feather", "params"}:
                raise ValueError("未知蒙版字段")
            if type(mask.get("enabled", True)) is not bool:
                raise ValueError("蒙版开关无效")
            for key in ("p0", "p1", "center", "radius"):
                if key in mask:
                    point(mask[key])
            if "angle" in mask:
                number(mask["angle"], -360, 360)
            if "feather" in mask:
                number(mask["feather"], 0, 1)
            adjustments = mask.get("params", {})
            if not isinstance(adjustments, dict) or set(adjustments) - {"temperature", "tint", "exposure", "saturation", "highlights", "shadows", "whites", "blacks"}:
                raise ValueError("局部调整参数无效")
            for key, value in adjustments.items():
                number(value, *NUMERIC_RANGES[key])


def is_neutral_curve(points) -> bool:
    """曲线不改变画面：缺省、为空，或控制点按值恰为 (0, 0)、(255, 255)。

    元组与列表等同；改变黑白场的直线、数学上恒等但多于两点的曲线都不算中性。"""
    return not points or [tuple(point) for point in points] == list(NEUTRAL_CURVE)


def curve_channels(curves) -> dict:
    """四条曲线逐通道的控制点，写成 [x, y] 列表；缺省或为空的通道取中性曲线。"""
    curves = curves or {}
    return {channel: [list(point) for point in curves.get(channel) or NEUTRAL_CURVE] for channel in CURVE_CHANNELS}


def mixer_values(mixer) -> dict:
    """八色混合器逐颜色、逐分量的取值；缺省为 0。"""
    mixer = mixer or {}
    return {color: {part: (mixer.get(color) or {}).get(part, 0) for part in MIXER_PARTS} for color in MIXER_CHANNELS}


def merge_color_components(current, values):
    result = copy.deepcopy(values)
    if values.get("curves"):
        result["curves"] = {**copy.deepcopy(current.get("curves") or {}), **copy.deepcopy(values["curves"])}
    if values.get("color_mixer"):
        mixer = copy.deepcopy(current.get("color_mixer") or {})
        for color, parts in values["color_mixer"].items():
            mixer[color] = {**(mixer.get(color) or {}), **copy.deepcopy(parts)}
        result["color_mixer"] = mixer
    return result


def is_color_neutral(params, *, ui=False, include_look=True):
    if include_look and params.get("look", "none") != "none":
        return False
    ranges = UI_RANGES if ui else NUMERIC_RANGES
    if any(params.get(key, 1.0 if key in ("brightness", "contrast", "saturation", "sharpness") else 0)
           != (1.0 if key in ("brightness", "contrast", "saturation", "sharpness") else 0) for key in ranges):
        return False
    mixer = params.get("color_mixer") or {}
    return (not params.get("curves")
            and not any(any(values.get(key, 0) != 0 for key in ("hue", "sat", "lum")) for values in mixer.values())
            and not any(mask.get("enabled", True) for mask in params.get("masks") or ()))


def is_color_default_ui(params):
    return (not any(params.get(key) for key in ("curves", "color_mixer", "masks"))
            and params.get("process_version", 4) == 4 and is_color_neutral(params, ui=True))


def is_ui_color_params(params):
    return bool(params.keys() & (UI_RANGES.keys() - NUMERIC_RANGES.keys()))


def color_identity(params, *, ui=False):
    """已校验参数的观感与处理版本；两个字段都缺时按旧参数映射。"""
    if "look" in params or "process_version" in params:
        return params.get("look", "none"), params.get("process_version", 2)
    return ("none", 2) if is_color_neutral(params, ui=ui, include_look=False) else (_LEGACY_LOOKS[0], 1)


def migrate_legacy_color(raw, *, ui=False):
    if not isinstance(raw, dict):
        raise ValueError("调色参数必须为对象")
    params = copy.deepcopy(raw)
    if "look" not in params and "process_version" not in params:
        if ui:
            contract = color_ui_contract(params)
            checked = contract.normalize(params, contract.version)
            if not checked.success:
                raise ValueError(checked.error.message_key)
        else:
            validate_legacy_params(params)
        look, process_version = color_identity(params, ui=ui)
        params.update(look=look, process_version=process_version)
    return params


def preserve_color_identity(params, current):
    return {**params, **{key: current[key] for key in ("look", "process_version")}}


def reset_color_params(baseline, current):
    return {**copy.deepcopy(baseline), "process_version": current["process_version"], "masks": None}


def preserve_unedited_color_fields(converted, original, previous_ui, current_ui):
    fields = {key: (key,) for key in (*UI_RANGES, "curves", "color_mixer", "masks")}
    fields.update(exposure_amt=("exposure", "brightness"), texture_amt=("texture", "sharpness"),
                  **{key + "_amt": (key,) for key in ("contrast", "saturation", "clarity", "dehaze", "vibrance")})
    result = copy.deepcopy(converted)
    for ui_key, engine_keys in fields.items():
        if previous_ui.get(ui_key) == current_ui.get(ui_key):
            for key in engine_keys:
                if key in original:
                    result[key] = copy.deepcopy(original[key])
                else:
                    result.pop(key, None)
    return result


_ENGINE_FIELDS = (
    *(ParameterField(key, (int, float), 1.0 if key in ("brightness", "contrast", "saturation", "sharpness") else 0,
                     low, high) for key, (low, high) in NUMERIC_RANGES.items()),
    ParameterField("curves", (dict, type(None)), None),
    ParameterField("color_mixer", (dict, type(None)), None),
    ParameterField("masks", (list, type(None)), None),
)

_LEGACY_IDENTITY_FIELDS = (
    ParameterField("look", str, "none", choices=("none", _LEGACY_LOOKS[0])),
    ParameterField("process_version", int, 2, choices=(1, 2)),
)

_IDENTITY_FIELDS = (
    ParameterField("look", str, "none", choices=("none", *_LEGACY_LOOKS)),
    _LEGACY_IDENTITY_FIELDS[1],
)

_COLOR_PARAMETERS_V4 = ParameterContract(_ENGINE_FIELDS + _IDENTITY_FIELDS, version=4, validate=validate_legacy_params)


def _validate_legacy(params):
    if {"look", "process_version"} & params.keys():
        raise ValueError("旧版调色参数包含未知字段")
    validate_legacy_params(params)


# 旧版本只校验；观感与处理版本由引擎入口按 color_identity 映射。
LEGACY_COLOR_PARAMETERS = {
    1: ParameterContract(version=1, validate=_validate_legacy, allow_unknown=True),
    2: ParameterContract(_ENGINE_FIELDS, version=2, validate=_validate_legacy),
    3: ParameterContract(_ENGINE_FIELDS + _LEGACY_IDENTITY_FIELDS, version=3, validate=validate_legacy_params),
    4: _COLOR_PARAMETERS_V4,
}

_UI_FIELDS = (
    *(ParameterField(key, (int, float), 0, low, high) for key, (low, high) in UI_RANGES.items()),
    ParameterField("curves", (dict, type(None)), None),
    ParameterField("color_mixer", (dict, type(None)), None),
    ParameterField("masks", (list, type(None)), None),
)

LEGACY_COLOR_UI_PARAMETERS = ParameterContract(_UI_FIELDS + _IDENTITY_FIELDS, version=4,
    validate=lambda p: validate_legacy_params({key: p[key] for key in ("curves", "color_mixer", "masks", "look", "process_version")}))


def validate_local_params(params):
    if type(params.get("process_version")) is not int or params["process_version"] != 3:
        raise ValueError("曝光处理版本无效")
    validate_legacy_params({**params, "process_version": 2, "masks": None})
    prepare_masks(params.get("masks"), active_only=False)


def validate_params(params):
    if params.get("process_version") == 4:
        validate_component_params(params)
    elif params.get("process_version") == 3:
        validate_local_params(params)
    else:
        validate_legacy_params(params)


_LOCAL_IDENTITY_FIELDS = (_IDENTITY_FIELDS[0], ParameterField("process_version", int, 3, choices=(3,)))
LEGACY_COLOR_PARAMETERS[5] = ParameterContract(_ENGINE_FIELDS + _LOCAL_IDENTITY_FIELDS, version=5, validate=validate_local_params)
LOCAL_COLOR_UI_PARAMETERS = ParameterContract(_UI_FIELDS + _LOCAL_IDENTITY_FIELDS, version=5,
    validate=lambda p: validate_local_params({key: p[key] for key in ("curves", "color_mixer", "masks", "look", "process_version")}))


def validate_component_params(params):
    from ..mask_components import prepare_components
    if type(params.get("process_version")) is not int or params["process_version"] != 4:
        raise ValueError("曝光处理版本无效")
    validate_legacy_params({**params, "process_version": 2, "masks": None})
    prepare_components(params.get("masks"), active_only=False)


_COMPONENT_IDENTITY_FIELDS = (_IDENTITY_FIELDS[0], ParameterField("process_version", int, 4, choices=(4,)))
LEGACY_COLOR_PARAMETERS[6] = ParameterContract(_ENGINE_FIELDS + _COMPONENT_IDENTITY_FIELDS, version=6, validate=validate_component_params)
COMPONENT_COLOR_UI_PARAMETERS = ParameterContract(_UI_FIELDS + _COMPONENT_IDENTITY_FIELDS, version=6,
    validate=lambda p: validate_component_params({key: p[key] for key in ("curves", "color_mixer", "masks", "look", "process_version")}))


_OPS_IDENTITY_FIELDS = (ParameterField("look", str, "none", choices=COLOR_LOOKS), _COMPONENT_IDENTITY_FIELDS[1])
COLOR_PARAMETERS = ParameterContract(_ENGINE_FIELDS + _OPS_IDENTITY_FIELDS, version=7, validate=validate_component_params)
COLOR_UI_PARAMETERS = ParameterContract(_UI_FIELDS + _OPS_IDENTITY_FIELDS, version=7,
    validate=lambda p: validate_component_params({key: p[key] for key in ("curves", "color_mixer", "masks", "look", "process_version")}))


def color_ui_contract(params, version=None):
    version = color_engine_contract(params, version).version
    return {5: LOCAL_COLOR_UI_PARAMETERS, 6: COMPONENT_COLOR_UI_PARAMETERS,
            7: COLOR_UI_PARAMETERS}.get(version, LEGACY_COLOR_UI_PARAMETERS)


def color_engine_contract(params, version=None):
    if version is not None:
        return COLOR_PARAMETERS if version == COLOR_PARAMETERS.version else LEGACY_COLOR_PARAMETERS[version]
    if params.get("process_version") == 4:
        return LEGACY_COLOR_PARAMETERS[6] if params.get("look") in COLOR_LOOK_ALIASES else COLOR_PARAMETERS
    return LEGACY_COLOR_PARAMETERS[5 if params.get("process_version") == 3 else 4]


def ui_to_engine(raw):
    migrated = migrate_legacy_color(raw, ui=True)
    contract = color_ui_contract(migrated)
    checked = contract.normalize(migrated, contract.version)
    if not checked.success:
        raise ValueError(checked.error.message_key)
    ui = checked.data
    params = {"brightness": 1.0, "sharpness": 1.0,
              "contrast": 1.0 + ui["contrast_amt"] / 280.0,
              "saturation": 1.0 + ui["saturation_amt"] / 200.0,
              "exposure": ui["exposure_amt"] / 100.0}
    for key in ("texture", "clarity", "dehaze", "vibrance"):
        params[key] = ui[key + "_amt"]
    for key in ("temperature", "tint", "blacks", "shadows", "highlights", "whites", "curves", "color_mixer", "masks", "look", "process_version"):
        params[key] = copy.deepcopy(ui[key])
    return params


def engine_to_ui(workflow_params):
    ui_shaped = is_ui_color_params(workflow_params)
    eng = migrate_legacy_color(workflow_params, ui=ui_shaped)
    ui = COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version)

    # Already-UI shaped dict (e.g. auto params adapted for the UI).
    if ui_shaped:
        for key in ui:
            if key in eng:
                ui[key] = eng[key]
        return ui

    brightness = eng.get("brightness", 1.0) or 1.0
    explicit_exposure = eng.get("exposure", 0) or 0
    extra_ev = math.log2(brightness) if brightness > 0 and brightness != 1.0 else 0.0
    ui["exposure_amt"] = max(-300, min(300, int(round((extra_ev + explicit_exposure) * 100))))

    ui["contrast_amt"] = max(-100, min(100, int(round((eng.get("contrast", 1.0) - 1.0) * 280))))
    ui["saturation_amt"] = max(-100, min(100, int(round((eng.get("saturation", 1.0) - 1.0) * 200))))
    ui["texture_amt"] = max(-100, min(100, int(round((eng.get("sharpness", 1.0) - 1.0) * 250))))
    for key in ("texture", "clarity", "dehaze", "vibrance"):
        if key in eng:
            ui[key + "_amt"] = max(-100, min(100, int(round(eng[key]))))
    ui["temperature"] = int(eng.get("temperature", 0) or 0)
    ui["tint"] = int(eng.get("tint", 0) or 0)
    ui["blacks"] = int(eng.get("blacks", 0) or 0)
    ui["shadows"] = int(eng.get("shadows", 0) or 0)
    ui["highlights"] = int(eng.get("highlights", 0) or 0)
    ui["whites"] = int(eng.get("whites", 0) or 0)
    ui["curves"] = eng.get("curves")
    ui["color_mixer"] = eng.get("color_mixer")
    ui["masks"] = eng.get("masks")
    for key in ("look", "process_version"):
        if key in eng:
            ui[key] = eng[key]
    return ui


COLOR_SYNC_FIELDS = (*UI_RANGES, *("curves." + channel for channel in CURVE_CHANNELS),
                     *("color_mixer." + color + "." + part for color in MIXER_CHANNELS for part in MIXER_PARTS))
COLOR_STYLE_FIELDS = (*("curves." + channel for channel in CURVE_CHANNELS),
                     *(field for field in COLOR_SYNC_FIELDS if field.startswith("color_mixer.")),
                     "vibrance_amt", "saturation_amt")
COLOR_SYNC_COMPATIBILITY = {(version, version): frozenset(COLOR_SYNC_FIELDS) for version in (1, 2, 3, 4)}


def normalize_sync_fields(fields):
    if not isinstance(fields, (list, tuple)) or not fields:
        raise ValueError("batch.invalid_fields")
    result = set()
    for field in fields:
        if not isinstance(field, str):
            raise ValueError("batch.invalid_fields")
        matched = {item for item in COLOR_SYNC_FIELDS if item == field or item.startswith(field + ".")}
        if not matched:
            raise ValueError("batch.invalid_fields")
        result.update(matched)
    return tuple(field for field in COLOR_SYNC_FIELDS if field in result)


def sync_field_values(params, fields):
    curves, mixer = curve_channels(params.get("curves")), mixer_values(params.get("color_mixer"))
    result = []
    for field in fields:
        parts = field.split(".")
        result.append(params[parts[0]] if len(parts) == 1 else curves[parts[1]] if parts[0] == "curves"
                      else mixer[parts[1]][parts[2]])
    return result


def exposure_centiev(value):
    from decimal import Decimal, InvalidOperation
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("batch.invalid_ev")
    try:
        scaled = Decimal(str(value)) * 100
        if scaled != scaled.to_integral_value():
            raise ValueError("batch.invalid_ev")
        return int(scaled)
    except InvalidOperation:
        raise ValueError("batch.invalid_ev") from None


def sync_color_settings(original, sample_ui, fields, *, sample_version):
    fields = normalize_sync_fields(fields)
    target_version = color_identity(original)[1]
    compatible = COLOR_SYNC_COMPATIBILITY.get((sample_version, target_version), frozenset())
    if not set(fields) <= compatible:
        raise ValueError("batch.incompatible_version")
    converted = ui_to_engine(sample_ui)
    scalar_keys = {field: (field,) for field in UI_RANGES}
    scalar_keys.update(exposure_amt=("exposure", "brightness"), texture_amt=("texture", "sharpness"),
        **{key + "_amt": (key,) for key in ("contrast", "saturation", "clarity", "dehaze", "vibrance")})
    result = copy.deepcopy(original)
    nested = {}
    curves, mixer = curve_channels(sample_ui.get("curves")), mixer_values(sample_ui.get("color_mixer"))
    for field in fields:
        if field in scalar_keys:
            for key in scalar_keys[field]:
                result[key] = copy.deepcopy(converted[key])
        else:
            parts = field.split(".")
            if parts[0] == "curves":
                nested.setdefault("curves", {})[parts[1]] = curves[parts[1]]
            else:
                nested.setdefault("color_mixer", {}).setdefault(parts[1], {})[parts[2]] = mixer[parts[1]][parts[2]]
    result.update(merge_color_components(original, nested))
    return result
