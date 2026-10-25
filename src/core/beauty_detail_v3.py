from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass

import cv2
import numpy as np

from .beauty_detail_v2 import (CALIBRATION as SMOOTH_CALIBRATION, color_delta as previous_color_delta,
                             execute as previous_execute, working_memory as previous_working_memory)
from .beauty_v2 import (CALIBRATION as BASE_CALIBRATION, _gamut, composition_bounds,
                       execute as execute_detail, lab_linear, linear_lab, skin_weight, working_area)
from .face_regions import BROWS, EYES, LIPS, NOSTRILS, OVAL, FaceRegions, eye_geometry, face_bounds
from .photo_engine import guided_filter, guided_filter_working_memory, linear_luma


@dataclass(frozen=True)
class BeautyCalibration:
    forehead_extension: float = 0.28
    outer_feather: float = 0.20
    boundary_blend: float = 0.25
    color_margin: float = 0.65
    seed_limit: int = 4096
    seed_minimum: int = 32
    eye_feather: float = 0.10
    eye_lower_feather: float = 0.05
    eye_extension: float = 0.10
    eye_reference: tuple[float, float] = (0.12, 0.25)
    eye_luma: float = 0.60
    eye_chroma: float = 0.45
    eye_low_frequency: float = 0.08


CALIBRATION = BeautyCalibration()
UNDER_EYE_CHAINS = (
    ((130, 25, 110, 24, 23, 22, 26, 112, 243),
     (143, 111, 117, 118, 119, 120, 121, 128, 245)),
    ((359, 255, 339, 254, 253, 252, 256, 341, 463),
     (372, 340, 346, 347, 348, 349, 350, 357, 465)),
)
SKIN_PARAMETERS = ("skin_smooth", "skin_brighten", "skin_rosy")


@dataclass(frozen=True)
class ColorRegions:
    bounds: tuple[int, int, int, int]
    points: np.ndarray
    distance: float
    legacy: FaceRegions

    @property
    def legacy_slice(self):
        left, top, right, bottom = self.legacy.bounds
        x, y = self.bounds[:2]
        return slice(top - y, bottom - y), slice(left - x, right - x)


def _check(cancelled):
    if cancelled():
        raise CancelledError()


def _smooth(value):
    value = np.clip(value, 0, 1)
    return value * value * (3 - 2 * value)


def color_bounds(points, size):
    return face_bounds(points, size, margin=CALIBRATION.color_margin)


def color_regions(points, size, legacy):
    bounds = color_bounds(points, size)
    pixels = (np.asarray(points, np.float32) * size - bounds[:2]).astype(np.float32)
    return ColorRegions(bounds, pixels, legacy.distance, legacy)


def _embed(value, regions, shape):
    result = np.zeros((*shape, *value.shape[2:]), value.dtype)
    result[regions.legacy_slice] = value
    return result


def _polygon(points, indices, shape):
    mask = np.zeros(shape, np.uint8)
    cv2.fillPoly(mask, [np.rint(points[list(indices)]).astype(np.int32)], 1)
    return mask


def _inward(mask, feather):
    mask = mask.copy()
    mask[[0, -1], :] = 0
    mask[:, [0, -1]] = 0
    return _smooth(cv2.distanceTransform(mask, cv2.DIST_L2, 5) / max(1.0, feather))


def _axes(points):
    centers, distance = eye_geometry(points)
    axis = (centers[1] - centers[0]) / max(distance, 1e-6)
    normal = np.array([-axis[1], axis[0]], np.float32)
    if np.dot(normal, points[152] - centers.mean(axis=0)) < 0:
        normal = -normal
    return centers.mean(axis=0), axis, normal


def _coordinates(shape, origin, axis, normal):
    x = np.arange(shape[1], dtype=np.float32)[None, :] - origin[0]
    y = np.arange(shape[0], dtype=np.float32)[:, None] - origin[1]
    return x * axis[0] + y * axis[1], x * normal[0] + y * normal[1]


def _skin_distribution(samples):
    cfg = CALIBRATION
    if len(samples) < cfg.seed_minimum:
        return None
    samples = samples[::max(1, int(np.ceil(len(samples) / cfg.seed_limit)))]
    center = np.median(samples, axis=0)
    spread = np.maximum(1.4826 * np.median(np.abs(samples - center), axis=0), (4, 2, 2))
    samples = samples[np.all(np.abs(samples - center) < 3 * spread, axis=1)]
    if len(samples) < cfg.seed_minimum:
        return None
    center = np.median(samples, axis=0)
    residual = samples - center
    covariance = residual.T @ residual / len(samples) + np.diag((36, 9, 9))
    return center, np.linalg.inv(covariance).astype(np.float32), max(4, float(np.sqrt(covariance[0, 0])))


def _skin_probability(lab, distribution, distance):
    center, precision, spread = distribution
    residual = lab - center
    mahalanobis = np.sum((residual @ precision) * residual, axis=-1)
    probability = 1 - _smooth((mahalanobis - 4) / 12)
    dark = _smooth((lab[..., 0] - (center[0] - max(12, 2.5 * spread))) / max(6, spread))
    probability *= dark
    probability = guided_filter(np.clip(lab[..., 0] / 100, 0, 1), probability,
                                max(1, round(0.015 * distance)), 0.02 ** 2)
    return np.clip(probability, 0, 1) * dark


def skin_weights(lab, regions, cancelled):
    shape = lab.shape[:2]
    points, d = regions.points, regions.distance
    original = _embed(regions.legacy.skin, regions, shape)
    old_lab = lab[regions.legacy_slice]
    old_skin = _embed(skin_weight(old_lab, regions.legacy), regions, shape)
    center, axis, normal = _axes(points)
    u, v = _coordinates(shape, center, axis, normal)
    gradient = np.hypot(cv2.Sobel(lab[..., 0], cv2.CV_32F, 1, 0) / 8,
                        cv2.Sobel(lab[..., 0], cv2.CV_32F, 0, 1) / 8)
    seeds = ((original > 0.9) & (v > 0.35 * d) & (v < 1.1 * d)
             & (np.abs(u) > 0.28 * d) & (np.abs(u) < 0.8 * d) & (gradient < 5))
    distribution = _skin_distribution(lab[seeds])
    _check(cancelled)
    probability = None
    if distribution is not None:
        probability = _skin_probability(lab, distribution, d)
        forehead = (original > 0.9) & (v < -0.35 * d) & (probability > 0.8) & (gradient < 5)
        refined = _skin_distribution(lab[seeds | forehead])
        if refined is not None:
            probability = _skin_probability(lab, refined, d)
    oval = _polygon(points, OVAL, shape)
    inside = cv2.distanceTransform(oval, cv2.DIST_L2, 5)
    upper = 1 - _smooth((v / max(d, 1e-6) - 0.30) / 0.40)
    blend = upper * (1 - _smooth(inside / max(1.0, CALIBRATION.boundary_blend * d)))
    protected = np.maximum.reduce([*(_polygon(points, eye, shape) for eye in EYES),
                                   *(_polygon(points, brow, shape) for brow in BROWS),
                                   _polygon(points, LIPS, shape),
                                   *(_polygon(points, nose, shape) for nose in NOSTRILS)])
    expansion = max(1, round(0.02 * d))
    protected = cv2.dilate(protected, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * expansion + 1,) * 2))
    protection = _inward(1 - protected, 0.03 * d)
    if probability is None:
        skin = old_skin * (1 - blend + blend * _inward(oval, CALIBRATION.outer_feather * d))
        return skin, old_skin * (gradient < 5), protection
    contour = points[list(OVAL)].copy()
    height = (contour - center) @ normal
    lift = CALIBRATION.forehead_extension * d * _smooth((-height / max(d, 1e-6) - 0.05) / 0.60)
    contour -= lift[:, None] * normal
    expanded = np.zeros(shape, np.uint8)
    cv2.fillPoly(expanded, [np.rint(contour).astype(np.int32)], 1)
    extended = _inward(expanded, CALIBRATION.outer_feather * d) * protection * probability
    skin = old_skin * (1 - blend) + extended * blend
    healthy = probability * original * (gradient < 5)
    _check(cancelled)
    return skin, healthy, protection


def _skin_delta(rgb, lab, skin, distance, params, cancelled):
    cfg, legacy = SMOOTH_CALIBRATION, BASE_CALIBRATION
    y = np.clip(linear_luma(rgb), 0, 1)
    delta = np.zeros_like(rgb)
    dy = np.zeros_like(y)
    if params["skin_smooth"] or params["skin_brighten"]:
        base = guided_filter(y, y, max(1, round(legacy.coarse_radius * distance)), legacy.coarse_eps)
        _check(cancelled)
    if params["skin_smooth"]:
        t = params["skin_smooth"] / 100
        fine = guided_filter(y, y, max(1, round(legacy.fine_radius * distance)), legacy.fine_eps)
        dy -= cfg.smooth * t * (0.6 + 0.4 * t) * skin * (fine - base)
        dy -= cfg.fine_detail * max(0, (t - 0.5) * 2) ** 2 * skin * (y - fine)
        sigma = max(1.0, legacy.spot_coarse * distance)
        mass = cv2.GaussianBlur(skin, (0, 0), sigma)
        changed = lab.copy()
        for channel in (1, 2):
            _check(cancelled)
            local = cv2.GaussianBlur(lab[..., channel] * skin, (0, 0), sigma)
            np.divide(local, mass, out=local, where=mass > 1e-6)
            changed[..., channel] += (np.clip(local - lab[..., channel], -cfg.chroma_limit, cfg.chroma_limit)
                                      * cfg.chroma * t * t * skin)
        active = skin > 0
        delta[active] += lab_linear(changed[active]) - rgb[active]
    if params["skin_brighten"]:
        dy += legacy.brighten * (params["skin_brighten"] / 100) * skin * base * (1 - np.clip(base, 0, 1))
    delta += dy[..., None]
    if params["skin_rosy"]:
        changed = lab.copy()
        changed[..., 1] += legacy.rosy * (params["skin_rosy"] / 100) * skin
        active = skin > 0
        delta[active] += lab_linear(changed[active]) - rgb[active]
    return delta


def _chain_height(chain, points, origin, axis, normal, u):
    offsets = points[list(chain)] - origin
    x, y = offsets @ axis, offsets @ normal
    order = np.argsort(x, kind="stable")
    return np.interp(u, x[order], y[order]).astype(np.float32)


def eye_correction(candidate, regions, healthy, protection, strength, cancelled):
    cfg = CALIBRATION
    lab = linear_lab(candidate)
    guide = np.clip(lab[..., 0] / 100, 0, 1)
    radius = max(1, round(cfg.eye_low_frequency * regions.distance))
    base = np.empty_like(lab)
    for channel in range(3):
        _check(cancelled)
        base[..., channel] = guided_filter(guide, lab[..., channel], radius, 0.04 ** 2)
    delta = np.zeros_like(candidate)
    support = np.zeros(candidate.shape[:2], np.float32)
    points, d = regions.points, regions.distance
    for (upper, lower), (outer, inner) in zip(UNDER_EYE_CHAINS, ((33, 133), (263, 362))):
        _check(cancelled)
        origin = points[outer]
        width = float(np.linalg.norm(points[inner] - origin))
        axis = (points[inner] - origin) / max(width, 1e-6)
        normal = np.array([-axis[1], axis[0]], np.float32)
        if np.dot(normal, points[152] - origin) < 0:
            normal = -normal
        u, v = _coordinates(guide.shape, origin, axis, normal)
        first = _chain_height(upper, points, origin, axis, normal, u)
        third = _chain_height(lower, points, origin, axis, normal, u)
        lateral = _smooth(np.minimum(u, width - u) / max(1, 0.15 * width))
        band = (_smooth((v - first) / max(1, cfg.eye_feather * d))
                * (1 - _smooth((v - third + (cfg.eye_lower_feather - cfg.eye_extension) * d)
                              / max(1, cfg.eye_lower_feather * d))) * lateral * protection)
        reference = ((v >= third + cfg.eye_reference[0] * d)
                     & (v <= third + cfg.eye_reference[1] * d) & (healthy > 0.6))
        sites = np.linspace(0, width, 9, dtype=np.float32)
        targets = np.zeros((len(sites), 3), np.float32)
        confidence = np.zeros(len(sites), np.float32)
        for index, site in enumerate(sites):
            selected = reference & (np.abs(u - site) < 0.08 * d)
            count = int(np.count_nonzero(selected))
            if count < 8:
                continue
            values = base[selected]
            target = np.median(values, axis=0)
            spread = np.maximum(1.4826 * np.median(np.abs(values - target), axis=0), (3, 2, 2))
            values = values[np.all(np.abs(values - target) < 2.5 * spread, axis=1)]
            if len(values) < 8:
                continue
            targets[index] = np.median(values, axis=0)
            confidence[index] = min(1, len(values) / max(8, 0.008 * d * d))
        valid = confidence > 0
        if np.count_nonzero(valid) < 2:
            continue
        target = np.stack([np.interp(u, sites[valid], targets[valid, channel]) for channel in range(3)], axis=-1)
        confidence_map = np.interp(u, sites, confidence).astype(np.float32)
        gap = np.maximum(0, target[..., 0] - base[..., 0])
        weight = band * confidence_map * _smooth(gap / 4) * cfg.eye_luma * (strength / 100)
        active = weight > 0
        if not np.any(active):
            continue
        correction = (target[active] - base[active]).astype(np.float32)
        correction[:, 0] = np.maximum(0, correction[:, 0])
        correction[:, 1:] *= cfg.eye_chroma
        changed = lab[active] + correction * weight[active, None]
        delta[active] += lab_linear(changed) - candidate[active]
        support = np.maximum(support, weight)
    _check(cancelled)
    return delta, support


def color_delta(rgb, regions, params, cancelled):
    remaining = {**params, **{key: 0 for key in (*SKIN_PARAMETERS, "eye_bag_remove")}}
    delta = np.zeros_like(rgb)
    support = np.zeros(rgb.shape[:2], np.float32)
    skipped = 0
    if any(remaining[key] for key in ("sharpen", "teeth_whiten", "spot_remove")):
        original_delta, original_support, skipped = previous_color_delta(
            rgb[regions.legacy_slice], regions.legacy, remaining, cancelled)
        delta[regions.legacy_slice] = original_delta
        support[regions.legacy_slice] = original_support
        del original_delta, original_support
    _check(cancelled)
    lab = linear_lab(rgb)
    skin, healthy, protection = skin_weights(lab, regions, cancelled)
    if any(params[key] for key in SKIN_PARAMETERS):
        delta += _skin_delta(rgb, lab, skin, regions.distance, params, cancelled)
        support = np.maximum(support, skin)
    if params["eye_bag_remove"]:
        candidate = rgb.copy()
        active = np.any(delta != 0, axis=-1)
        candidate[active] = _gamut(rgb[active] + delta[active])
        eyes, eye_support = eye_correction(candidate, regions, healthy, protection, params["eye_bag_remove"], cancelled)
        active = np.any(eyes != 0, axis=-1)
        delta[active] = candidate[active] + eyes[active] - rgb[active]
        support = np.maximum(support, eye_support)
    return delta, support, skipped


def _uses_color_regions(params):
    return any(params[key] for key in (*SKIN_PARAMETERS, "eye_bag_remove"))


def working_memory(source, params, snapshot=None):
    base = previous_working_memory(source, params, snapshot)
    if not base or not _uses_color_regions(params):
        return base
    area = working_area(source, snapshot, bounds_factory=color_bounds)
    extra_composition = 0
    if snapshot is not None:
        left, top, right, bottom = composition_bounds(source, snapshot)
        old = (right - left) * (bottom - top)
        left, top, right, bottom = composition_bounds(source, snapshot, bounds_factory=color_bounds)
        extra_composition = max(0, (right - left) * (bottom - top) - old) * 16
    filters = guided_filter_working_memory(max(0, area - working_area(source, snapshot)))
    return base + area * 384 + extra_composition + filters


def execute(source, params, snapshot, *, cancelled=lambda: False, sampling=None, node_id=""):
    if not _uses_color_regions(params):
        return previous_execute(source, params, snapshot, cancelled=cancelled, sampling=sampling, node_id=node_id)
    return execute_detail(source, params, snapshot, cancelled=cancelled, sampling=sampling, node_id=node_id,
                          color_processor=color_delta, color_regions=color_regions, color_bounds=color_bounds)
