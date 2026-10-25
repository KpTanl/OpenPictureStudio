from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from uuid import UUID, uuid4

import numpy as np


MASK_LIMIT = 128
MASK_BAND_ROWS = 64
LOCAL_KEYS = (
    "temperature", "tint", "exposure", "contrast", "highlights", "shadows",
    "whites", "blacks", "texture", "clarity", "dehaze", "vibrance", "saturation",
)
LOCAL_RANGES = {key: (-100, 100) for key in LOCAL_KEYS}
LOCAL_RANGES.update(exposure=(-20, 20), contrast=(-1, 2), saturation=(-1, 2))
EFFECTIVE_RANGES = {**LOCAL_RANGES, "exposure": (-10, 10), "contrast": (0, 3), "saturation": (0, 3)}
LOCAL_UI_FIELDS = {key: (key + "_amt" if key in ("exposure", "contrast", "texture", "clarity", "dehaze", "vibrance", "saturation") else key)
                   for key in LOCAL_KEYS}
LOCAL_UI_SCALES = {key: (100 if key == "exposure" else 280 if key == "contrast" else 200 if key == "saturation" else 1)
                   for key in LOCAL_KEYS}


@dataclass(frozen=True)
class Mask:
    identity: str
    kind: int
    geometry: tuple[float, ...]
    delta: tuple[float, ...]


def _number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("error.mask.parameter")
    return float(value)


def _point(value, low=-1, high=2):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("error.mask.geometry")
    return tuple(_number(item, low, high) for item in value)


def prepare_masks(masks, *, active_only=True):
    if masks is None:
        return ()
    if not isinstance(masks, list) or len(masks) > MASK_LIMIT:
        raise ValueError("error.mask.limit")
    prepared, identities = [], set()
    for mask in masks:
        if not isinstance(mask, dict):
            raise ValueError("error.mask.parameter")
        try:
            identity = str(UUID(mask["id"]))
        except (KeyError, AttributeError, TypeError, ValueError):
            raise ValueError("error.mask.identity") from None
        if identity != mask["id"] or identity in identities:
            raise ValueError("error.mask.identity")
        identities.add(identity)
        name = mask.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 80 or name != name.strip():
            raise ValueError("error.mask.name")
        kind = mask.get("type")
        allowed = {"id", "name", "enabled", "type", "params"}
        if kind == "linear":
            allowed |= {"p0", "p1"}
            p0, p1 = _point(mask["p0"]), _point(mask["p1"])
            if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) <= 1e-6:
                raise ValueError("error.mask.geometry")
            geometry = (*p0, *p1)
        elif kind == "radial":
            allowed |= {"center", "radius", "angle", "feather", "invert"}
            if type(mask.get("invert", False)) is not bool:
                raise ValueError("error.mask.parameter")
            angle = _number(mask["angle"], 0, 360)
            if angle == 360:
                raise ValueError("error.mask.geometry")
            geometry = (*_point(mask["center"]), *_point(mask["radius"], 1e-6, 2),
                        angle, _number(mask["feather"], 0, 1), float(mask.get("invert", False)))
        else:
            raise ValueError("error.mask.type")
        if set(mask) - allowed or type(mask.get("enabled", True)) is not bool:
            raise ValueError("error.mask.parameter")
        adjustments = mask.get("params", {})
        if not isinstance(adjustments, dict) or set(adjustments) - set(LOCAL_KEYS):
            raise ValueError("error.mask.parameter")
        delta = tuple(_number(adjustments.get(key, 0), *LOCAL_RANGES[key]) for key in LOCAL_KEYS)
        if not active_only or mask.get("enabled", True) and any(delta):
            prepared.append(Mask(identity, int(kind == "radial"), geometry, delta))
    return tuple(sorted(prepared, key=lambda item: item.identity))


def mask_weight(mask, shape, start=0, stop=None, *, reference_size=None):
    height, width = shape
    if mask.kind == 2:
        if mask.raster is None:
            raise ValueError("error.mask.asset")
        return mask.raster[start:height if stop is None else stop]
    ref_width, ref_height = reference_size or (width, height)
    stop = height if stop is None else stop
    one = np.float32(1)
    x = (np.arange(width, dtype=np.float32)[None, :] + np.float32(0.5)) * np.float32(ref_width / width)
    y = (np.arange(start, stop, dtype=np.float32)[:, None] + np.float32(0.5)) * np.float32(ref_height / height)
    geometry = np.asarray(mask.geometry, dtype=np.float32)
    if mask.kind == 0:
        x0, y0, x1, y1 = geometry * np.array((ref_width, ref_height, ref_width, ref_height), dtype=np.float32)
        dx, dy = x1 - x0, y1 - y0
        t = np.clip(((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy), 0, 1)
        return one - t * t * (np.float32(3) - np.float32(2) * t)
    cx, cy, rx, ry, angle, feather, invert = geometry
    angle = np.float32(angle * np.float32(np.pi / 180))
    cosine, sine = np.cos(angle), np.sin(angle)
    dx, dy = x - cx * np.float32(ref_width), y - cy * np.float32(ref_height)
    xx = (cosine * dx + sine * dy) / (rx * np.float32(ref_width))
    yy = (-sine * dx + cosine * dy) / (ry * np.float32(ref_height))
    radius = np.sqrt(xx * xx + yy * yy)
    if feather == 0:
        result = (radius <= one).astype(np.float32)
    else:
        t = np.clip((radius - (one - feather)) / feather, 0, 1)
        result = one - t * t * (np.float32(3) - np.float32(2) * t)
    return one - result if invert else result


def default_mask(kind, center, reference_size, name):
    width, height = reference_size
    short = min(width, height)
    result = {"id": str(uuid4()), "name": name, "enabled": True, "type": kind, "params": {}}
    if kind == "linear":
        half = short / 6 / height
        result.update(p0=[center[0], max(-1, center[1] - half)], p1=[center[0], min(2, center[1] + half)])
    elif kind == "radial":
        result.update(center=list(center), radius=[short / 4 / width, short / 4 / height], angle=0, feather=0.5, invert=False)
    else:
        raise ValueError("error.mask.type")
    return result


def copy_mask(mask, name):
    return {**copy.deepcopy(mask), "id": str(uuid4()), "name": name}


def local_ui_values(mask):
    params = mask.get("params", {})
    return {LOCAL_UI_FIELDS[key]: params.get(key, 0) * LOCAL_UI_SCALES[key] for key in LOCAL_KEYS}


def local_engine_key(ui_key):
    return next(key for key in LOCAL_KEYS if LOCAL_UI_FIELDS[key] == ui_key)


def upgrade_masks(masks, reference_size):
    if not masks:
        return []
    width, height = reference_size
    if width < 2 or height < 2:
        raise ValueError("error.mask.upgrade_size")
    size = np.array((width, height), dtype=np.float64)
    span = size - 1
    upgraded, identities = [], set()
    for index, old in enumerate(masks or ()):
        identity = old.get("id")
        try:
            valid = str(UUID(identity)) == identity and identity not in identities
        except (TypeError, AttributeError, ValueError):
            valid = False
        if not valid:
            identity = str(uuid4())
        identities.add(identity)
        name = old.get("name")
        name = name.strip()[:80] if isinstance(name, str) else ""
        result = {"id": identity, "name": name or str(index + 1), "type": old.get("type", "linear"),
                  "enabled": old.get("enabled", True), "params": copy.deepcopy(old.get("params", {}))}
        if "saturation" in result["params"]:
            result["params"]["saturation"] -= 1
        if result["type"] == "linear":
            p0 = np.asarray(old.get("p0", (.5, 0)), dtype=np.float64)
            p1 = np.asarray(old.get("p1", (.5, 1)), dtype=np.float64)
            direction = p1 - p0
            length = float(direction @ direction)
            if length < 1e-8:
                result.update(p0=[1, .5], p1=[2, .5])
            else:
                gradient = direction / length / span
                start = .5 + p0 * span
                end = start + gradient / float(gradient @ gradient)
                result.update(p0=(start / size).tolist(), p1=(end / size).tolist())
        else:
            center = .5 + np.asarray(old.get("center", (.5, .5)), dtype=np.float64) * span
            radii = np.maximum(old.get("radius", (.3, .3)), 1e-4)
            angle = math.radians(old.get("angle", 0))
            cosine, sine = math.cos(angle), math.sin(angle)
            rotation = np.array(((cosine, sine), (-sine, cosine)))
            inverse = np.diag(1 / span)
            quadratic = inverse @ rotation.T @ np.diag(1 / radii ** 2) @ rotation @ inverse
            values, axes = np.linalg.eigh(quadratic)
            result.update(center=(center / size).tolist(), radius=(1 / np.sqrt(values) / size).tolist(),
                          angle=math.degrees(math.atan2(axes[1, 0], axes[0, 0])) % 360,
                          feather=max(.01, min(1, old.get("feather", .5))), invert=False)
        upgraded.append(result)
    prepare_masks(upgraded, active_only=False)
    return upgraded
