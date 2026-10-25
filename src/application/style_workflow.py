from __future__ import annotations

import copy

import numpy as np

from ..core.photo_engine import curve_lut
from ..core.mixer_sampling import MIXER_CHANNELS
from ..core.tools.beauty import BEAUTY_PARAMETERS
from ..core.tools.color import COLOR_UI_PARAMETERS, curve_channels, mixer_values
from .assistant_tools import TOOLS
from .photo_analysis import photo_statistics, paired_style_measurements, reference_thumbnail, encode_jpeg
from .style_fit import ReferenceFitter, simplify_curve, FIT_HUE_SHARE, FIT_VALIDATION_EDGE


def style_parameters(before, after):
    result = {}
    for tool, contract in (("color", COLOR_UI_PARAMETERS), ("beauty", BEAUTY_PARAMETERS)):
        defaults = contract.defaults(contract.version)
        result[tool] = {key: after[tool].get(key, defaults[key]) - before[tool].get(key, defaults[key])
                        for key in TOOLS[tool].ranges
                        if after[tool].get(key, defaults[key]) != before[tool].get(key, defaults[key])}
    a, b = mixer_values(before["color"].get("color_mixer")), mixer_values(after["color"].get("color_mixer"))
    result["mixer"] = {color: {part: b[color][part] - a[color][part] for part in a[color]
                                if b[color][part] != a[color][part]} for color in a if a[color] != b[color]}
    return result


def capture_style(before, after, workflow, build, render, analyze):
    full_before = {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version), **before["color"]}
    full_after = {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version), **after["color"]}
    prime = copy.deepcopy(full_after)
    prime["curves"] = curve_channels(prime.get("curves"))
    prime["curves"]["rgb"] = curve_channels(full_before.get("curves"))["rgb"]
    arrays, thumbnails, assets = [], {}, {}
    for label, candidate in (("before", build(full_before, workflow=workflow)), ("result", workflow),
                             ("without_master", build(prime, workflow=workflow))):
        with render(candidate, 1600) as image, image.convert("RGBA") as rgba:
            arrays.append(np.array(rgba))
            if label != "without_master":
                thumbnails[label] = reference_thumbnail(image)
                assets[label] = encode_jpeg(image, 1536, 90)
    paired, statistics = analyze(arrays)
    if curve_channels(full_before.get("curves"))["rgb"] == curve_channels(full_after.get("curves"))["rgb"]:
        paired["transfer"] = np.linspace(0, 1, 256).tolist()
    sample = {"delta": style_parameters(before, after), "paired": paired,
              "before_analysis": statistics[0], "result_analysis": statistics[1],
              "full_color": full_after, "full_beauty": copy.deepcopy(after["beauty"]), "thumbnails": thumbnails}
    return sample, assets


def analyze_style_frames(arrays):
    return paired_style_measurements(*arrays), [photo_statistics(frame) for frame in arrays[:2]]


def select_style_sample(style, current, requested=""):
    samples = style["samples"]
    if requested:
        selected = next((sample for sample in samples if sample["id"] == requested), None)
        if selected is None:
            raise ValueError("AI_STYLE_MISSING")
        return selected, {"code": "explicit_sample"}
    if not samples:
        raise ValueError("AI_STYLE_EMPTY")
    scores = []
    for sample in samples:
        baseline = sample["before_analysis"]
        tone = float(np.mean([abs(current["luma_percentiles"][key] - baseline["luma_percentiles"][key])
                              for key in current["luma_percentiles"]]))
        a = np.array([current["hues"][name]["share"] for name in MIXER_CHANNELS])
        b = np.array([baseline["hues"][name]["share"] for name in MIXER_CHANNELS])
        overlap = float(np.minimum(a / a.sum(), b / b.sum()).sum()) if a.sum() and b.sum() else 0.0
        face = (abs(bool(current["face_count"]) - bool(baseline["face_count"]))
                if "face_count" in current and "face_count" in baseline else 0)
        scores.append((tone + (1 - overlap) + face, sample, {"code": "measured_similarity", "tone_distance": tone,
                       "color_overlap": overlap, "face_mismatch": bool(face),
                       "interpretation": "Measured resemblance does not establish semantic comparability."}))
    _score, selected, reason = min(scores, key=lambda item: item[0])
    return selected, reason


def apply_style_suggestion(context, style, *, build, measure, is_cancelled, sample_id="", strength=60, preserve=(), beauty=True):
    initial = measure(context.workflow, FIT_VALIDATION_EDGE)
    if initial.get("status") != "available":
        raise ValueError("AI_FIT_MEASUREMENT")
    sample, reason = select_style_sample(style, initial, sample_id)
    if not strength:
        return {"status": "no_fittable_dimensions", "adjustments": {}, "sample": sample["id"], "selection": reason,
                "style": {key: style[key] for key in ("id", "name", "version")}, "steps": [],
                "skipped": [{"field": "strength", "code": "zero_strength"}]}
    scale = strength / 100
    color = {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version), **copy.deepcopy(dict(context.color))}
    retouch = {**BEAUTY_PARAMETERS.defaults(BEAUTY_PARAMETERS.version), **copy.deepcopy(dict(context.beauty))}
    skipped = []

    def locked(path):
        return any(path == key or path.startswith(key + ".") for key in preserve)

    def add(path, value, delta, bounds):
        if locked(path):
            skipped.append({"field": path, "code": "locked"})
            return value
        raw = value + delta * scale
        if not bounds[0] <= raw <= bounds[1]:
            skipped.append({"field": path, "code": "at_upper_limit" if raw > bounds[1] else "at_lower_limit"})
        return int(round(np.clip(raw, *bounds)))

    for key, delta in sample["delta"]["color"].items():
        color[key] = add(key, color[key], delta, TOOLS["color"].ranges[key])
    color["color_mixer"] = mixer_values(color.get("color_mixer"))
    for name, parts in sample["delta"]["mixer"].items():
        for part, delta in parts.items():
            color["color_mixer"][name][part] = add("color_mixer." + name + "." + part,
                                                 color["color_mixer"][name][part], delta, (-100, 100))
    for key, delta in sample["delta"]["beauty"].items():
        if beauty:
            retouch[key] = add("beauty." + key, retouch[key], delta, TOOLS["beauty"].ranges[key])
        else:
            skipped.append({"field": "beauty." + key, "code": "beauty_unavailable"})
    color["curves"] = curve_channels(color.get("curves"))
    transfer = np.asarray(sample["paired"]["transfer"])
    if sample["paired"].get("transfer_status") == "narrow_luma_range":
        skipped.append({"field": "curves.rgb", "code": "narrow_luma_range"})
    if not locked("curves.rgb") and np.max(np.abs(transfer - np.linspace(0, 1, 256))) > 1e-7:
        existing = curve_lut(color["curves"]["rgb"], 256)
        mapped = np.interp(existing * 255, np.arange(256), transfer)
        points = simplify_curve((existing * (1 - scale) + mapped * scale) * 255)
        if points is None:
            skipped.append({"field": "curves.rgb", "code": "curve_precision_limit"})
        else:
            color["curves"]["rgb"] = points
    target = copy.deepcopy(initial)
    for segment, delta in sample["paired"]["split_tone"].items():
        if delta["status"] != "missing" and target["split_tone"][segment]["status"] != "missing":
            for key in ("a", "b"):
                target["split_tone"][segment][key] += delta[key] * scale
        else:
            target["split_tone"][segment]["status"] = "missing"
    colors = []
    for name, delta in sample["paired"]["hues"].items():
        row = target["hues"][name]
        if delta["status"] != "missing" and row["share"] >= FIT_HUE_SHARE and row["hab"] is not None and delta["hab"] is not None:
            row.update(L=float(np.clip(row["L"] + delta["L"] * scale, 0, 100)),
                       C=max(0, row["C"] + delta["C"] * scale), hab=(row["hab"] + delta["hab"] * scale) % 360)
            colors.append(name)
        else:
            row["share"] = 0
    fitter = ReferenceFitter(lambda values: build(values, beauty=retouch), measure, is_cancelled)
    correction = fitter.fit(color, target, dimensions=["split_tone", "hue"], colors=colors,
                            strength=100 if strength else 0, preserve=preserve)
    for key, value in correction["values"].items():
        if key in ("curves", "color_mixer"):
            if key == "color_mixer":
                for name, parts in value.items():
                    color[key][name].update(parts)
            else:
                color[key].update(value)
        else:
            color[key] = value
    measured = measure(build(color, beauty=retouch), FIT_VALIDATION_EDGE)
    original = {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version), **dict(context.color)}
    changes = {key: value for key, value in color.items() if key in TOOLS["color"].ranges and value != original[key]}
    curves = {key: points for key, points in color["curves"].items() if points != curve_channels(original.get("curves"))[key]}
    mixer = {name: {part: value for part, value in parts.items()
                    if value != mixer_values(original.get("color_mixer"))[name][part]}
             for name, parts in color["color_mixer"].items()}
    if curves:
        changes["curves"] = curves
    if any(mixer.values()):
        changes["color_mixer"] = {name: parts for name, parts in mixer.items() if parts}
    original_beauty = {**BEAUTY_PARAMETERS.defaults(BEAUTY_PARAMETERS.version), **dict(context.beauty)}
    beauty_changes = {key: retouch[key] for key in TOOLS["beauty"].ranges if retouch[key] != original_beauty[key]} if beauty else {}
    adjustments = {tool: values for tool, values in (("color", changes), ("beauty", beauty_changes)) if values}
    return {"status": "suggested" if adjustments else "no_fittable_dimensions", "adjustments": adjustments,
            "style": {"id": style["id"], "name": style["name"], "version": style["version"]},
            "sample": sample["id"], "selection": reason, "steps": correction["steps"],
            "skipped": skipped + correction["skipped"], "before": correction.get("before"), "after": correction.get("after"),
            "analysis": measured, "validation_edge": FIT_VALIDATION_EDGE}
