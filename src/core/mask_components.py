from __future__ import annotations

import copy
import hashlib
import json
import re
from concurrent.futures import CancelledError
from dataclasses import dataclass
from uuid import UUID, uuid4

import numpy as np

from .local_masks import (MASK_LIMIT, LOCAL_KEYS, LOCAL_RANGES, _number, _point,
                          mask_weight, prepare_masks)


COMPONENT_LIMIT = 64
NODE_COMPONENT_LIMIT = 512
POINT_LIMIT = 65536
ASSET_TYPE = "mask-gray16"
GENERATED_KINDS = ("subject", "skin", "eyes", "lips")
STROKE_KEYS = frozenset({"points", "radius", "hardness", "opacity"})


@dataclass(frozen=True)
class CompositeMask:
    identity: str
    delta: tuple
    components: tuple
    kind: int = 2
    geometry: tuple = ()
    raster: object = None


def _identity(value, seen):
    try:
        valid = str(UUID(value)) == value and value not in seen
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("error.mask.identity")
    seen.add(value)
    return value


def _named(item):
    name = item.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 80 or name != name.strip():
        raise ValueError("error.mask.name")
    if type(item.get("enabled")) is not bool:
        raise ValueError("error.mask.parameter")


def brush_strokes(component):
    """画笔区域里的各笔；早先一笔一个区域的写法按只有一笔读。"""
    if "strokes" in component:
        return component["strokes"]
    return [{key: component[key] for key in STROKE_KEYS}]


def _stroke_points(stroke):
    if not isinstance(stroke, dict) or set(stroke) != STROKE_KEYS:
        raise ValueError("error.mask.parameter")
    _number(stroke["radius"], .001, .5)
    _number(stroke["hardness"], 0, 1)
    _number(stroke["opacity"], 0, 1)
    path = stroke["points"]
    if not isinstance(path, list) or not path:
        raise ValueError("error.mask.geometry")
    for point in path:
        _point(point)
    return len(path)


def prepare_components(masks, *, active_only=True):
    if masks is None:
        return ()
    if not isinstance(masks, list) or len(masks) > MASK_LIMIT:
        raise ValueError("error.mask.limit")
    result, identities, component_ids = [], set(), set()
    count = points = 0
    for mask in masks:
        if not isinstance(mask, dict) or set(mask) != {"id", "name", "enabled", "params", "components"}:
            raise ValueError("error.mask.parameter")
        identity = _identity(mask["id"], identities)
        _named(mask)
        params = mask["params"]
        if not isinstance(params, dict) or set(params) - set(LOCAL_KEYS):
            raise ValueError("error.mask.parameter")
        delta = tuple(_number(params.get(key, 0), *LOCAL_RANGES[key]) for key in LOCAL_KEYS)
        components = mask["components"]
        if not isinstance(components, list) or len(components) > COMPONENT_LIMIT:
            raise ValueError("error.mask.component_limit")
        count += len(components)
        checked = []
        for component in components:
            if not isinstance(component, dict):
                raise ValueError("error.mask.parameter")
            _identity(component.get("id"), component_ids)
            _named(component)
            if component.get("op") not in ("add", "subtract"):
                raise ValueError("error.mask.parameter")
            kind = component.get("type")
            base = {"id", "name", "enabled", "op", "type"}
            if kind in ("linear", "radial"):
                if "params" in component:
                    raise ValueError("error.mask.parameter")
                geometry = {key: value for key, value in component.items() if key != "op"}
                geometry["params"] = {}
                prepare_masks([geometry], active_only=False)
            elif kind == "all":
                if set(component) != base:
                    raise ValueError("error.mask.parameter")
            elif kind == "brush":
                if set(component) == base | {"strokes"}:
                    if not isinstance(component["strokes"], list) or not component["strokes"]:
                        raise ValueError("error.mask.geometry")
                elif set(component) != base | STROKE_KEYS:
                    raise ValueError("error.mask.parameter")
                for stroke in brush_strokes(component):
                    points += _stroke_points(stroke)
                    if points > POINT_LIMIT:
                        raise ValueError("error.mask.point_limit")
            elif kind in GENERATED_KINDS:
                if set(component) != base | {"asset", "size", "model", "source"}:
                    raise ValueError("error.mask.parameter")
                if not isinstance(component["asset"], str) or not re.fullmatch(r"[0-9a-f]{64}", component["asset"]):
                    raise ValueError("error.mask.asset")
                size = component["size"]
                if not isinstance(size, list) or len(size) != 2 or any(type(n) is not int or not 1 <= n <= 1600 for n in size):
                    raise ValueError("error.mask.asset")
                for key in ("model", "source"):
                    if not isinstance(component[key], str) or not 1 <= len(component[key]) <= 1024:
                        raise ValueError("error.mask.asset")
            else:
                raise ValueError("error.mask.type")
            checked.append(copy.deepcopy(component))
        if count > NODE_COMPONENT_LIMIT:
            raise ValueError("error.mask.component_limit")
        has_addition = any(component["enabled"] and component["op"] == "add" for component in checked)
        if not active_only or mask["enabled"] and any(delta) and has_addition:
            result.append(CompositeMask(identity, delta, tuple(checked)))
    return tuple(sorted(result, key=lambda mask: mask.identity))


def upgrade_components(masks):
    prepare_masks(masks, active_only=False)
    result = []
    for old in masks or ():
        geometry = {key: copy.deepcopy(value) for key, value in old.items() if key not in ("params", "id")}
        geometry.update(id=str(uuid4()), op="add", enabled=True)
        result.append({"id": old["id"], "name": old["name"], "enabled": old.get("enabled", True),
                       "params": copy.deepcopy(old.get("params", {}))})
        result[-1]["components"] = [geometry]
    return result


def new_mask(name):
    return {"id": str(uuid4()), "name": name, "enabled": True, "params": {}, "components": []}


def component_from_shape(shape, op="add"):
    return {**{key: copy.deepcopy(value) for key, value in shape.items() if key != "params"}, "op": op}


def geometry_hash(mask):
    components = [{key: value for key, value in component.items() if key not in ("id", "name", "model", "source")}
                  for component in mask.components]
    return hashlib.sha256(json.dumps(components, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def asset_references(settings):
    if settings.get("process_version") != 4:
        return {}
    prepare_components(settings.get("masks"), active_only=False)
    result = {}
    for mask in settings.get("masks") or ():
        for component in mask["components"]:
            if "asset" in component:
                entry = {"type": ASSET_TYPE, "size": component["size"]}
                previous = result.setdefault(component["asset"], entry)
                if previous != entry:
                    raise ValueError("error.mask.asset")
    return result


def _raster_band(array, shape, start, stop):
    height, width = shape
    ah, aw = array.shape
    xs = np.clip((np.arange(width, dtype=np.float32) + .5) * (aw / width) - .5, 0, aw - 1)
    ys = np.clip((np.arange(start, stop, dtype=np.float32) + .5) * (ah / height) - .5, 0, ah - 1)
    x0, y0 = xs.astype(np.intp), ys.astype(np.intp)
    x1, y1 = np.minimum(x0 + 1, aw - 1), np.minimum(y0 + 1, ah - 1)
    fx, fy = xs - x0, (ys - y0)[:, None]
    a = array[y0[:, None], x0] * (1 - fx) + array[y0[:, None], x1] * fx
    b = array[y1[:, None], x0] * (1 - fx) + array[y1[:, None], x1] * fx
    return np.asarray(a * (1 - fy) + b * fy, dtype=np.float32)


def component_weight(component, shape, start, stop, reference_size, assets, cancelled):
    kind = component["type"]
    if kind == "all":
        return np.ones((stop - start, shape[1]), np.float32)
    if kind in ("linear", "radial"):
        geometry = {key: value for key, value in component.items() if key != "op"}
        geometry["params"] = {}
        mask = prepare_masks([geometry], active_only=False)[0]
        return mask_weight(mask, shape, start, stop, reference_size=reference_size)
    if kind != "brush":
        return _raster_band(assets[component["asset"]], shape, start, stop)
    result = None
    for stroke in brush_strokes(component):
        weights = _stroke_weight(stroke, shape, start, stop, reference_size, cancelled)
        result = weights if result is None else np.maximum(result, weights, out=result)
    return result


def _stroke_weight(stroke, shape, start, stop, reference_size, cancelled):
    height, width = shape
    ref_width, ref_height = reference_size
    x = (np.arange(width, dtype=np.float32)[None, :] + .5) * (ref_width / width)
    y = (np.arange(start, stop, dtype=np.float32)[:, None] + .5) * (ref_height / height)
    radius = np.float32(stroke["radius"] * min(reference_size))
    distance = np.full((stop - start, width), np.inf, np.float32)
    path = np.asarray(stroke["points"], dtype=np.float32) * (ref_width, ref_height)
    previous = path[0]
    for current in path:
        if cancelled():
            raise CancelledError()
        low, high = np.minimum(previous, current) - radius, np.maximum(previous, current) + radius
        if high[1] < y[0, 0] or low[1] > y[-1, 0]:
            previous = current
            continue
        left = max(0, int(low[0] * width / ref_width - .5))
        right = min(width, int(high[0] * width / ref_width + 1.5))
        if left < right:
            dx, dy = current - previous
            length = dx * dx + dy * dy
            xx, yy = x[:, left:right] - previous[0], y - previous[1]
            t = np.clip((xx * dx + yy * dy) / max(length, 1e-12), 0, 1)
            d = np.sqrt((xx - t * dx) ** 2 + (yy - t * dy) ** 2)
            np.minimum(distance[:, left:right], d, out=distance[:, left:right])
        previous = current
    hardness = stroke["hardness"]
    if hardness == 1:
        return (distance <= radius).astype(np.float32) * np.float32(stroke["opacity"])
    t = np.clip((distance / radius - hardness) / (1 - hardness), 0, 1)
    return (1 - t * t * (3 - 2 * t)) * np.float32(stroke["opacity"])


def composite_weight(mask, shape, start, stop, reference_size, assets, cancelled):
    result = np.zeros((stop - start, shape[1]), np.float32)
    for component in mask.components:
        if cancelled():
            raise CancelledError()
        if not component["enabled"]:
            continue
        weights = component_weight(component, shape, start, stop, reference_size, assets, cancelled)
        if component["op"] == "add":
            np.maximum(result, weights, out=result)
        else:
            result *= 1 - weights
    return result
