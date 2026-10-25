from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass

import cv2
import numpy as np

from .beauty_v2 import (CALIBRATION as LEGACY_CALIBRATION, color_delta as legacy_color_delta,
                       execute as execute_detail, linear_lab, lab_linear, skin_weight,
                       spot_candidates, working_area, working_memory as legacy_working_memory)
from .photo_engine import guided_filter, linear_luma


@dataclass(frozen=True)
class BeautyCalibration:
    smooth: float = 0.95
    fine_detail: float = 0.18
    chroma: float = 0.4
    chroma_limit: float = 4.0
    spot_feather: float = 0.01
    spot_background: float = 0.02
    donor_color: tuple[float, float, float] = (8.0, 6.0, 6.0)
    donor_color_limit: float = 4.0
    donor_luma_limit: float = 0.08
    donor_chroma_limit: float = 4.0
    texture_min: float = 0.5
    texture_max: float = 1.5


CALIBRATION = BeautyCalibration()


def _check(cancelled):
    if cancelled():
        raise CancelledError()


def _blur(values, sigma):
    return cv2.GaussianBlur(values, (0, 0), max(1.0, sigma))


def _spot_delta(rgb, y, lab, skin, regions, cancelled):
    detected = spot_candidates(y, skin, regions.distance, cancelled)
    delta = np.zeros_like(rgb)
    total_weight = np.zeros_like(y)
    if detected is None:
        return delta, total_weight, 0
    base, gradient, healthy, candidates = detected
    count, labels, stats, centers = cv2.connectedComponentsWithStats(candidates, connectivity=8)
    donor_skin = healthy & (labels == 0) & (gradient < 0.05)
    coarse_color = _blur(lab, LEGACY_CALIBRATION.spot_coarse * regions.distance)
    donor_skin &= np.all(np.abs(lab[..., 1:] - coarse_color[..., 1:]) < CALIBRATION.donor_chroma_limit, axis=-1)
    del coarse_color
    _check(cancelled)
    used = np.zeros_like(candidates)
    height, width = y.shape
    distance = regions.distance
    cfg = CALIBRATION
    skipped = 0
    for index in range(1, count):
        _check(cancelled)
        x, yy, w, h, area = stats[index]
        diameter = 2 * np.sqrt(area / np.pi)
        if not LEGACY_CALIBRATION.spot_fine * distance <= diameter <= LEGACY_CALIBRATION.spot_coarse * distance:
            continue
        pad = max(1, round(0.03 * distance))
        x0, y0 = max(0, x - pad), max(0, yy - pad)
        x1, y1 = min(width, x + w + pad), min(height, yy + h + pad)
        pw, ph = x1 - x0, y1 - y0
        local = labels[y0:y1, x0:x1] == index
        outward = cv2.distanceTransform((~local).astype(np.uint8), cv2.DIST_L2, 5)
        feather = max(2.0, cfg.spot_feather * distance)
        ring = ((outward > feather) & (outward <= pad) & donor_skin[y0:y1, x0:x1])
        if np.count_nonzero(ring) < 8:
            skipped += 1
            continue
        target = rgb[y0:y1, x0:x1]
        target_lab = lab[y0:y1, x0:x1][ring].mean(axis=0)
        target_base = float(base[y0:y1, x0:x1][ring].mean())
        target_gradient = float(gradient[y0:y1, x0:x1][ring].mean())
        cx, cy = centers[index]
        outer = LEGACY_CALIBRATION.donor_outer * distance
        xs = np.arange(max(pw // 2, int(cx - outer)), min(width - (pw + 1) // 2 + 1, int(cx + outer) + 1))
        ys = np.arange(max(ph // 2, int(cy - outer)), min(height - (ph + 1) // 2 + 1, int(cy + outer) + 1))
        if not len(xs) or not len(ys):
            skipped += 1
            continue
        gx, gy = np.meshgrid(xs, ys)
        radius = np.hypot(gx - cx, gy - cy)
        valid = (radius >= LEGACY_CALIBRATION.donor_inner * distance) & (radius <= outer)
        sx0, sy0 = int(xs[0]) - pw // 2, int(ys[0]) - ph // 2
        sx1, sy1 = int(xs[-1]) + (pw + 1) // 2, int(ys[-1]) + (ph + 1) // 2
        available = donor_skin[sy0:sy1, sx0:sx1] & (used[sy0:sy1, sx0:sx1] == 0)
        safe = cv2.erode(available.astype(np.uint8), np.ones((ph, pw), np.uint8),
                         borderType=cv2.BORDER_CONSTANT, borderValue=0)
        ly, lx = gy - sy0, gx - sx0
        valid &= safe[ly, lx] > 0
        if not valid.any():
            skipped += 1
            continue
        kernel = ring.astype(np.float32) / np.float32(np.count_nonzero(ring))
        mean_lab = cv2.filter2D(lab[sy0:sy1, sx0:sx1], -1, kernel)[ly, lx]
        _check(cancelled)
        mean_base = cv2.filter2D(base[sy0:sy1, sx0:sx1], -1, kernel)[ly, lx]
        mean_gradient = cv2.filter2D(gradient[sy0:sy1, sx0:sx1], -1, kernel)[ly, lx]
        color_score = np.sum(((mean_lab - target_lab) / np.asarray(cfg.donor_color, np.float32)) ** 2, axis=-1)
        valid &= (color_score <= cfg.donor_color_limit) & (np.abs(mean_base - target_base) <= cfg.donor_luma_limit)
        if not valid.any():
            skipped += 1
            continue
        score = color_score + ((mean_base - target_base) / 0.05) ** 2
        score += ((mean_gradient - target_gradient) / 0.02) ** 2
        score[~valid] = np.inf
        iy, ix = np.unravel_index(np.argmin(score), score.shape)
        dx, dy = int(gx[iy, ix]) - pw // 2, int(gy[iy, ix]) - ph // 2
        donor = rgb[dy:dy + ph, dx:dx + pw]
        ramp = np.clip(outward / feather, 0, 1)
        blend = (1 - ramp * ramp * (3 - 2 * ramp)) * np.clip(skin[y0:y1, x0:x1] / 0.8, 0, 1)
        weights = ring.astype(np.float32)
        mass = _blur(weights, cfg.spot_background * distance)
        if np.any((blend > 0) & (mass <= 1e-6)):
            skipped += 1
            continue
        background = _blur(target * weights[..., None], cfg.spot_background * distance)
        np.divide(background, mass[..., None], out=background, where=mass[..., None] > 1e-6)
        texture = donor - _blur(donor, LEGACY_CALIBRATION.spot_fine * distance)
        texture -= texture[ring].mean(axis=0)
        target_rms = float(np.sqrt(np.mean((target[ring] - background[ring]) ** 2)))
        donor_rms = float(np.sqrt(np.mean(texture[ring] ** 2)))
        scale = np.float32(np.clip(target_rms / max(donor_rms, 1e-6), cfg.texture_min, cfg.texture_max))
        repaired = background + texture * scale
        delta[y0:y1, x0:x1] += (repaired - target) * blend[..., None]
        total_weight[y0:y1, x0:x1] += blend
        used[dy:dy + ph, dx:dx + pw] = 1
    delta /= np.maximum(1, total_weight)[..., None]
    return delta, np.minimum(1, total_weight), skipped


def color_delta(rgb, regions, params, cancelled):
    if not (params["skin_smooth"] or params["spot_remove"]):
        return legacy_color_delta(rgb, regions, params, cancelled)
    cfg = CALIBRATION
    remaining = {**params, "skin_smooth": 0, "spot_remove": 0}
    if any(params[key] for key in ("skin_brighten", "skin_rosy", "sharpen", "teeth_whiten", "eye_bag_remove")):
        delta, support, _ = legacy_color_delta(rgb, regions, remaining, cancelled)
    else:
        delta = np.zeros_like(rgb)
        support = np.zeros(rgb.shape[:2], np.float32)
    lab = linear_lab(rgb)
    skin = skin_weight(lab, regions)
    y = np.clip(linear_luma(rgb), 0, 1)
    if params["skin_smooth"]:
        t = params["skin_smooth"] / 100
        strength = t * (0.6 + 0.4 * t)
        fine = guided_filter(y, y, max(1, round(LEGACY_CALIBRATION.fine_radius * regions.distance)),
                             LEGACY_CALIBRATION.fine_eps)
        _check(cancelled)
        base = guided_filter(y, y, max(1, round(LEGACY_CALIBRATION.coarse_radius * regions.distance)),
                             LEGACY_CALIBRATION.coarse_eps)
        dy = -cfg.smooth * strength * skin * (fine - base)
        high_strength = max(0, (t - 0.5) * 2) ** 2
        dy -= cfg.fine_detail * high_strength * skin * (y - fine)
        delta += dy[..., None]
        _check(cancelled)
        sigma = LEGACY_CALIBRATION.spot_coarse * regions.distance
        mass = _blur(skin, sigma)
        changed = lab.copy()
        for channel in (1, 2):
            local = _blur(lab[..., channel] * skin, sigma)
            np.divide(local, mass, out=local, where=mass > 1e-6)
            changed[..., channel] += (np.clip(local - lab[..., channel], -cfg.chroma_limit, cfg.chroma_limit)
                                      * cfg.chroma * t * t * skin)
        active = skin > 0
        delta[active] += lab_linear(changed[active]) - rgb[active]
        support = np.maximum(support, skin)
    skipped = 0
    if params["spot_remove"]:
        _check(cancelled)
        spots, spot_support, skipped = _spot_delta(rgb, y, lab, skin, regions, cancelled)
        delta += spots * (params["spot_remove"] / 100)
        support = np.maximum(support, spot_support)
    return delta, support, skipped


def working_memory(source, params, snapshot=None):
    base = legacy_working_memory(source, params, snapshot)
    if not base or not (params["skin_smooth"] or params["spot_remove"]):
        return base
    return base + working_area(source, snapshot) * 128


def execute(source, params, snapshot, *, cancelled=lambda: False, sampling=None, node_id=""):
    return execute_detail(source, params, snapshot, cancelled=cancelled, sampling=sampling,
                          node_id=node_id, color_processor=color_delta)
