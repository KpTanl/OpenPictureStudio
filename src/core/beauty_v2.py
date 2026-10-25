from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass, replace

import cv2
import numpy as np

from .contracts.beauty import BEAUTY_MODEL_FIELDS, BeautyEffectStatus
from .face_regions import JAW, eye_geometry, face_regions, face_bounds
from .photo_engine import guided_filter, guided_filter_working_memory, linear_luma


@dataclass(frozen=True)
class BeautyCalibration:
    fine_radius: float = 0.015
    coarse_radius: float = 0.08
    fine_eps: float = 0.02 ** 2
    coarse_eps: float = 0.04 ** 2
    smooth: float = 0.65
    brighten: float = 0.25
    rosy: float = 3.0
    eye_bag: float = 0.12
    teeth_yellow: float = 0.40
    teeth_light: float = 4.0
    sharpen: float = 0.25
    spot_fine: float = 0.01
    spot_coarse: float = 0.04
    spot_threshold: float = 0.02
    spot_mad: float = 2.5
    donor_inner: float = 0.05
    donor_outer: float = 0.20
    face_slim: float = 0.04
    eye_scale: float = 1.08
    eye_distance: float = 0.025
    nose_slim: float = 0.025
    field_side: int = 512


CALIBRATION = BeautyCalibration()
RECOMMENDED = {"skin_smooth": 50, "skin_brighten": 30, "skin_rosy": 20, "sharpen": 15}
_XYZ = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                 [0.0193339, 0.1191920, 0.9503041]], np.float32)
_RGB = np.linalg.inv(_XYZ).astype(np.float32)
_WHITE = np.array([0.95047, 1, 1.08883], np.float32)


def _check(cancelled):
    if cancelled():
        raise CancelledError()


def linear_lab(rgb):
    xyz = rgb @ _XYZ.T / _WHITE
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack((116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])), axis=-1).astype(np.float32)


def lab_linear(lab):
    fy = (lab[..., 0] + 16) / 116
    f = np.stack((fy + lab[..., 1] / 500, fy, fy - lab[..., 2] / 200), axis=-1)
    xyz = np.where(f > 6 / 29, f ** 3, (116 * f - 16) / (24389 / 27)) * _WHITE
    return (xyz @ _RGB.T).astype(np.float32)


def _gamut(rgb):
    y = np.clip(linear_luma(rgb), 0, 1)[..., None]
    chroma = rgb - linear_luma(rgb)[..., None]
    positive = np.where(chroma > 1e-8, (1 - y) / np.maximum(chroma, 1e-8), 1e6)
    negative = np.where(chroma < -1e-8, -y / np.minimum(chroma, -1e-8), 1e6)
    scale = np.clip(np.minimum(positive, negative).min(axis=-1, keepdims=True), 0, 1)
    return (y + chroma * scale).astype(np.float32)


def _skin_color(lab):
    a, b = lab[..., 1], lab[..., 2]
    # 软色度与亮度选择，降低胡须、眼镜和深暗遮挡的影响。
    return (np.clip((a + 4) / 12, 0, 1) * np.clip((38 - a) / 12, 0, 1)
            * np.clip((b + 5) / 12, 0, 1) * np.clip((55 - b) / 15, 0, 1)
            * np.clip((lab[..., 0] - 3) / 12, 0, 1)).astype(np.float32)


def skin_weight(lab, regions):
    return regions.skin * _skin_color(lab)


def spot_candidates(y, skin, distance, cancelled):
    cfg = CALIBRATION
    fine = guided_filter(y, y, max(1, round(cfg.spot_fine * distance)), cfg.fine_eps)
    _check(cancelled)
    base = guided_filter(y, y, max(1, round(cfg.spot_coarse * distance)), cfg.coarse_eps)
    residual = fine - base
    healthy = skin > 0.8
    if not np.any(healthy):
        return None
    values = residual[healthy]
    median = np.median(values)
    threshold = max(cfg.spot_threshold, cfg.spot_mad * float(np.median(np.abs(values - median))))
    gradient = np.hypot(cv2.Sobel(y, cv2.CV_32F, 1, 0, ksize=3) / 8,
                        cv2.Sobel(y, cv2.CV_32F, 0, 1, ksize=3) / 8)
    candidates = ((residual < median - threshold) & healthy & (gradient < 0.05)).astype(np.uint8)
    return base, gradient, healthy, candidates


def _spot_delta(rgb, y, lab, skin, distance, cancelled):
    cfg = CALIBRATION
    detected = spot_candidates(y, skin, distance, cancelled)
    delta = np.zeros_like(rgb)
    total_weight = np.zeros_like(y)
    if detected is None:
        return delta, 0
    base, gradient, healthy, candidates = detected
    count, labels, stats, centers = cv2.connectedComponentsWithStats(candidates, connectivity=8)
    donor_skin = healthy & (labels == 0) & (gradient < 0.05)
    height, width = y.shape
    skipped = 0
    for index in range(1, count):
        _check(cancelled)
        x, yy, w, h, area = stats[index]
        diameter = 2 * np.sqrt(area / np.pi)
        if not cfg.spot_fine * distance <= diameter <= cfg.spot_coarse * distance:
            continue
        pad = max(1, round(0.03 * distance))
        x0, y0 = max(0, x - pad), max(0, yy - pad)
        x1, y1 = min(width, x + w + pad), min(height, yy + h + pad)
        pw, ph = x1 - x0, y1 - y0
        cx, cy = centers[index]
        outer = cfg.donor_outer * distance
        xs = np.arange(max(pw // 2, int(cx - outer)), min(width - (pw + 1) // 2 + 1, int(cx + outer) + 1))
        ys = np.arange(max(ph // 2, int(cy - outer)), min(height - (ph + 1) // 2 + 1, int(cy + outer) + 1))
        if not len(xs) or not len(ys):
            skipped += 1
            continue
        gx, gy = np.meshgrid(xs, ys)
        radius = np.hypot(gx - cx, gy - cy)
        valid = (radius >= cfg.donor_inner * distance) & (radius <= outer)
        sx0, sy0 = int(xs[0]) - pw // 2, int(ys[0]) - ph // 2
        sx1, sy1 = int(xs[-1]) + (pw + 1) // 2, int(ys[-1]) + (ph + 1) // 2
        safe = cv2.erode(donor_skin[sy0:sy1, sx0:sx1].astype(np.uint8), np.ones((ph, pw), np.uint8),
                         borderType=cv2.BORDER_CONSTANT, borderValue=0)
        ly, lx = gy - sy0, gx - sx0
        valid &= safe[ly, lx] > 0
        if not valid.any():
            skipped += 1
            continue
        local = labels[y0:y1, x0:x1] == index
        target_lab = lab[y0:y1, x0:x1][local].mean(axis=0)
        target_b = float(base[y0:y1, x0:x1][local].mean())
        target_g = float(gradient[y0:y1, x0:x1][local].mean())
        mean_lab = cv2.boxFilter(lab[sy0:sy1, sx0:sx1], -1, (pw, ph), normalize=True)
        mean_b = cv2.boxFilter(base[sy0:sy1, sx0:sx1], -1, (pw, ph), normalize=True)
        mean_g = cv2.boxFilter(gradient[sy0:sy1, sx0:sx1], -1, (pw, ph), normalize=True)
        score = ((mean_b[ly, lx] - target_b) / 0.1) ** 2
        score += ((mean_lab[ly, lx, 1] - target_lab[1]) / 10) ** 2
        score += ((mean_lab[ly, lx, 2] - target_lab[2]) / 10) ** 2
        score += ((mean_g[ly, lx] - target_g) / 0.05) ** 2
        score[~valid] = np.inf
        iy, ix = np.unravel_index(np.argmin(score), score.shape)
        dx, dy = int(gx[iy, ix]) - pw // 2, int(gy[iy, ix]) - ph // 2
        donor = rgb[dy:dy + ph, dx:dx + pw]
        mask = np.zeros((ph, pw), np.uint8)
        mask[local] = 1
        feather = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), max(1, 0.01 * distance))
        feather *= skin[y0:y1, x0:x1]
        delta[y0:y1, x0:x1] += (donor - rgb[y0:y1, x0:x1]) * feather[..., None]
        total_weight[y0:y1, x0:x1] += feather
    delta /= np.maximum(1, total_weight)[..., None]
    return delta, skipped


def color_delta(rgb, regions, params, cancelled):
    cfg = CALIBRATION
    lab = linear_lab(rgb)
    skin = skin_weight(lab, regions)
    y = np.clip(linear_luma(rgb), 0, 1)
    delta = np.zeros_like(rgb)
    support = np.zeros_like(y)
    fine = base = None
    if params["skin_smooth"] or params["sharpen"]:
        fine = guided_filter(y, y, max(1, round(cfg.fine_radius * regions.distance)), cfg.fine_eps)
        _check(cancelled)
    if params["skin_smooth"] or params["skin_brighten"] or params["eye_bag_remove"]:
        base = guided_filter(y, y, max(1, round(cfg.coarse_radius * regions.distance)), cfg.coarse_eps)
        _check(cancelled)
    dy = np.zeros_like(y)
    if params["skin_smooth"]:
        dy -= cfg.smooth * (params["skin_smooth"] / 100) * skin * (fine - base)
        support = np.maximum(support, skin)
    if params["skin_brighten"]:
        dy += cfg.brighten * (params["skin_brighten"] / 100) * skin * base * (1 - np.clip(base, 0, 1))
        support = np.maximum(support, skin)
    if params["eye_bag_remove"] and np.any(skin > 0.5):
        healthy = (skin > 0.5) & (regions.under_eyes == 0)
        radius = max(1, round(regions.distance * 0.05))
        sums = cv2.boxFilter(base * healthy, -1, (2 * radius + 1,) * 2, normalize=False)
        weights = cv2.boxFilter(healthy.astype(np.float32), -1, (2 * radius + 1,) * 2, normalize=False)
        eyes, distance = eye_geometry(regions.points)
        offset = (eyes[1] - eyes[0]) / max(distance, 1e-6) * (0.18 * distance)
        shape = (y.shape[1], y.shape[0])
        side_sum, side_weight = np.zeros_like(y), np.zeros_like(y)
        for sign in (-1, 1):
            matrix = np.array([[1, 0, sign * offset[0]], [0, 1, sign * offset[1]]], np.float32)
            side_sum += cv2.warpAffine(sums, matrix, shape, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                       borderMode=cv2.BORDER_CONSTANT)
            side_weight += cv2.warpAffine(weights, matrix, shape, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                          borderMode=cv2.BORDER_CONSTANT)
        reference = np.divide(side_sum, side_weight, out=base.copy(), where=side_weight > 0)
        dy += np.clip(reference - base, 0, cfg.eye_bag) * (params["eye_bag_remove"] / 100) * regions.under_eyes
        support = np.maximum(support, regions.under_eyes)
    if params["sharpen"]:
        dy += cfg.sharpen * (params["sharpen"] / 100) * regions.features * (y - fine)
        support = np.maximum(support, regions.features)
    delta += dy[..., None]
    if params["skin_rosy"]:
        changed = lab.copy()
        changed[..., 1] += cfg.rosy * (params["skin_rosy"] / 100) * skin
        active = skin > 0
        delta[active] += lab_linear(changed[active]) - rgb[active]
        support = np.maximum(support, skin)
    if params["teeth_whiten"]:
        chroma = np.hypot(lab[..., 1], lab[..., 2])
        teeth = regions.mouth * np.clip((lab[..., 0] - 35) / 30, 0, 1) * np.clip((35 - chroma) / 20, 0, 1)
        changed = lab.copy()
        changed[..., 2] *= 1 - cfg.teeth_yellow * (params["teeth_whiten"] / 100) * teeth
        changed[..., 0] += cfg.teeth_light * (params["teeth_whiten"] / 100) * teeth
        active = teeth > 0
        delta[active] += lab_linear(changed[active]) - rgb[active]
        support = np.maximum(support, teeth)
    skipped = 0
    if params["spot_remove"]:
        spots, skipped = _spot_delta(rgb, y, lab, skin, regions.distance, cancelled)
        delta += spots * (params["spot_remove"] / 100)
        support = np.maximum(support, skin)
    return delta, support, skipped


def displacement(regions, params):
    cfg = CALIBRATION
    height, width = regions.skin.shape
    scale = min(1, cfg.field_side / max(width, height))
    sw, sh = max(1, round(width * scale)), max(1, round(height * scale))
    # 网格按目标像素坐标定义，低分辨率仅存平滑位移。
    xx, yy = np.meshgrid((np.arange(sw, dtype=np.float32) + 0.5) * width / sw - 0.5,
                         (np.arange(sh, dtype=np.float32) + 0.5) * height / sh - 0.5)
    grid = np.stack((xx, yy), axis=-1)
    field = np.zeros_like(grid)
    points, d = regions.points, regions.distance
    eyes, _ = eye_geometry(points)
    direction = (eyes[1] - eyes[0]) / max(d, 1e-6)

    def bump(center, radius):
        squared = np.sum((grid - center) ** 2, axis=-1) / max(radius * radius, 1)
        return np.maximum(0, 1 - squared) ** 2

    if params["face_slim"]:
        center = points[1]
        for jaw in JAW:
            anchor = points[list(jaw)].mean(axis=0)
            vector = anchor - center
            vector /= max(float(np.linalg.norm(vector)), 1e-6)
            field += bump(anchor, d * 0.65)[..., None] * vector * (cfg.face_slim * d * params["face_slim"] / 100)
    for index, center in enumerate(eyes):
        if params["eye_enlarge"]:
            factor = 1 + (cfg.eye_scale - 1) * params["eye_enlarge"] / 100
            field += (grid - center) * (1 / factor - 1) * bump(center, d * 0.35)[..., None]
        if params["eye_distance"]:
            sign = 1 if index == 0 else -1
            field += bump(center, d * 0.4)[..., None] * direction * (sign * cfg.eye_distance * d * params["eye_distance"] / 100)
    if params["nose_slim"]:
        center = (points[48] + points[278]) / 2
        for index in (48, 278):
            vector = points[index] - center
            vector /= max(float(np.linalg.norm(vector)), 1e-6)
            field += bump(points[index], d * 0.22)[..., None] * vector * (cfg.nose_slim * d * params["nose_slim"] / 100)
    field = cv2.resize(field, (width, height), interpolation=cv2.INTER_LINEAR)
    return field.astype(np.float32), regions.support


def remap_once(rgb, alpha, field, cancelled=lambda: False):
    height, width = rgb.shape[:2]
    output = rgb.copy()
    coverage = None if alpha is None else alpha.copy()
    for start in range(0, height, 64):
        stop = min(height, start + 64)
        for left in range(0, width, 1024):
            _check(cancelled)
            right = min(width, left + 1024)
            offsets = field[start:stop, left:right]
            active = np.any(offsets != 0, axis=-1)
            if not active.any():
                continue
            xx, yy = np.meshgrid(np.arange(left, right, dtype=np.float32), np.arange(start, stop, dtype=np.float32))
            mx = np.clip(xx + offsets[..., 0], 0, width - 1)
            my = np.clip(yy + offsets[..., 1], 0, height - 1)
            x0, y0 = max(0, int(mx.min()) - 2), max(0, int(my.min()) - 2)
            x1, y1 = min(width, int(mx.max()) + 4), min(height, int(my.max()) + 4)
            local_rgb = rgb[y0:y1, x0:x1]
            local_a = np.ones(local_rgb.shape[:2], np.float32) if alpha is None else alpha[y0:y1, x0:x1]
            px, py = mx - x0, my - y0
            a = cv2.remap(local_a, px, py, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
            weighted = cv2.remap(local_rgb * local_a[..., None], px, py, cv2.INTER_CUBIC,
                                 borderMode=cv2.BORDER_REPLICATE)
            amin, amax = np.full(a.shape, np.inf, np.float32), np.full(a.shape, -np.inf, np.float32)
            lo, hi = np.full(weighted.shape, np.inf, np.float32), np.full(weighted.shape, -np.inf, np.float32)
            bx, by = np.floor(mx).astype(np.int32), np.floor(my).astype(np.int32)
            for dy in (-1, 0, 1, 2):
                for dx in (-1, 0, 1, 2):
                    sx, sy = np.clip(bx + dx, 0, width - 1), np.clip(by + dy, 0, height - 1)
                    ca = np.ones_like(a) if alpha is None else alpha[sy, sx]
                    np.minimum(amin, ca, out=amin)
                    np.maximum(amax, ca, out=amax)
                    crgb = rgb[sy, sx]
                    np.minimum(lo, np.where(ca[..., None] > 1e-6, crgb, np.inf), out=lo)
                    np.maximum(hi, np.where(ca[..., None] > 1e-6, crgb, -np.inf), out=hi)
            np.clip(a, amin, amax, out=a)
            lo[~np.isfinite(lo)] = 0
            hi[~np.isfinite(hi)] = 0
            np.clip(weighted, lo * a[..., None], hi * a[..., None], out=weighted)
            np.divide(weighted, a[..., None], out=weighted, where=a[..., None] > 1e-6)
            weighted[a <= 1e-6] = 0
            output[start:stop, left:right][active] = weighted[active]
            if coverage is not None:
                coverage[start:stop, left:right][active] = a[active]
    return output, coverage


def working_area(source, snapshot=None, *, bounds_factory=face_bounds):
    height, width = source.pixels.shape[:2]
    pixels = height * width
    roi = pixels
    if snapshot is not None:
        areas = []
        for face in snapshot.faces:
            if face.status == "ready":
                left, top, right, bottom = bounds_factory(face.points, (width, height))
                areas.append(max(0, right - left) * max(0, bottom - top))
        roi = max(areas, default=0)
    return roi


def composition_bounds(source, snapshot, *, bounds_factory=face_bounds):
    height, width = source.pixels.shape[:2]
    bounds = [bounds_factory(face.points, (width, height)) for face in snapshot.faces if face.status == "ready"]
    bounds = [(left, top, right, bottom) for left, top, right, bottom in bounds if right > left and bottom > top]
    if not bounds:
        return 0, 0, 0, 0
    return (min(box[0] for box in bounds), min(box[1] for box in bounds),
            max(box[2] for box in bounds), max(box[3] for box in bounds))


def working_memory(source, params, snapshot=None):
    height, width = source.pixels.shape[:2]
    pixels = height * width
    if not any(value for key, value in params.items() if key not in BEAUTY_MODEL_FIELDS):
        return 0
    roi = working_area(source, snapshot)
    if not roi:
        return 0
    # 最终累计与输出、单脸区域/频段/供体、分块三次采样的同时存活上界。
    saving = 0
    has_color = any(params[key] for key in ("skin_smooth", "skin_brighten", "skin_rosy", "sharpen",
                                           "teeth_whiten", "spot_remove", "eye_bag_remove"))
    if snapshot is not None and has_color:
        left, top, right, bottom = composition_bounds(source, snapshot)
        saving = (pixels - (right - left) * (bottom - top)) * 16
    filters = guided_filter_working_memory(roi) if has_color else 0
    return (pixels * 48 - saving + roi * (264 if params["spot_remove"] else 200)
            + width * min(height, 64) * 224 + 1024 * 1024 + filters)


def analyze_auto(source, snapshot, *, preset=False):
    from .tools.beauty import BEAUTY_PARAMETERS
    from .contracts.result import CoreError, CoreOperationError, ErrorCode
    from .mixer_sampling import linear_to_srgb
    if not snapshot.processed:
        raise CoreOperationError(CoreError(ErrorCode.INVALID_INPUT, "beauty.detail." + snapshot.status,
                                          {"tool_id": "beauty", "processed": 0, "skipped": snapshot.skipped}))
    values = {**BEAUTY_PARAMETERS.defaults(2), **RECOMMENDED}
    if preset:
        return values
    face = max((face for face in snapshot.faces if face.status == "ready"),
               key=lambda face: np.ptp(np.asarray(face.points), axis=0).prod())
    regions = face_regions(face.points, (source.pixels.shape[1], source.pixels.shape[0]))
    left, top, right, bottom = regions.bounds
    rgb = source.pixels[top:bottom, left:right]
    selected = skin_weight(linear_lab(rgb), regions) > 0.5
    if selected.any():
        display = np.clip(linear_to_srgb(rgb[selected]), 0, 1) * 255
        luma = display @ np.array([0.299, 0.587, 0.114], np.float32)
        median, std = float(np.median(luma)), float(np.std(luma))
        values["skin_brighten"] = int(np.clip(round((145 - median) * 0.5), 10, 55))
        values["skin_smooth"] = int(np.clip(round(50 + (std - 38) * (0.8 if std > 38 else 0.4)), 40, 75))
        r, g, b = display.mean(axis=0)
        warmth = r - (g + b) / 2
        values["skin_rosy"] = int(np.clip(round(25 + (15 - warmth) * 0.5), 20, 40)) if warmth < 15 else 15
    return {**values, "eye_enlarge": 18, "face_slim": 20, "nose_slim": 10,
            "teeth_whiten": 25, "spot_remove": 35, "eye_bag_remove": 30}


def face_rectangles(source, snapshot):
    result = []
    for face in snapshot.faces:
        if face.status == "ready":
            points = np.asarray(face.points)
            result.append(tuple(float(value) for value in (*np.clip(points.min(axis=0), 0, 1),
                                                           *np.clip(points.max(axis=0), 0, 1))))
    return tuple(result)


def execute(source, params, snapshot, *, cancelled=lambda: False, sampling=None, node_id="", color_processor=None,
            color_regions=None, color_bounds=face_bounds):
    _check(cancelled)
    if not any(value for key, value in params.items() if key not in BEAUTY_MODEL_FIELDS):
        return source
    if source.is_proxy and snapshot.reference_size != source.orig_size:
        raise ValueError("美颜快照参考尺寸不匹配")
    status = BeautyEffectStatus(node_id, snapshot.status, snapshot.processed, snapshot.skipped)
    if not snapshot.processed:
        return replace(source, effect_status=(*source.effect_status, status))
    height, width = source.pixels.shape[:2]
    has_color = any(params[key] for key in ("skin_smooth", "skin_brighten", "skin_rosy", "sharpen",
                                           "teeth_whiten", "spot_remove", "eye_bag_remove"))
    has_warp = any(params[key] for key in ("face_slim", "eye_enlarge", "eye_distance", "nose_slim"))
    cl, ct, cr, cb = composition_bounds(source, snapshot, bounds_factory=color_bounds)
    total_delta = np.zeros((cb - ct, cr - cl, 3), source.pixels.dtype) if has_color else None
    color_weight = np.zeros((cb - ct, cr - cl), np.float32) if has_color else None
    total_field = np.zeros((height, width, 2), np.float32) if has_warp else None
    warp_weight = np.zeros((height, width), np.float32) if has_warp else None
    skipped = 0
    for face in snapshot.faces:
        _check(cancelled)
        if face.status != "ready":
            continue
        regions = face_regions(face.points, (width, height))
        left, top, right, bottom = regions.bounds
        if right <= left or bottom <= top:
            continue
        if has_color:
            colors = regions if color_regions is None else color_regions(face.points, (width, height), regions)
            left, top, right, bottom = colors.bounds
            processor = color_delta if color_processor is None else color_processor
            delta, support, missed = processor(source.pixels[top:bottom, left:right], colors, params, cancelled)
            # delta 已包含各区域权重；以整体支持度作重叠归一化。
            total_delta[top - ct:bottom - ct, left - cl:right - cl] += delta
            color_weight[top - ct:bottom - ct, left - cl:right - cl] += support
            skipped += missed
            del delta, support, colors
        if has_warp:
            left, top, right, bottom = regions.bounds
            field, support = displacement(regions, params)
            total_field[top:bottom, left:right] += field * support[..., None]
            warp_weight[top:bottom, left:right] += support
            del field, support
        del regions
    _check(cancelled)
    pixels, alpha = source.pixels, source.alpha
    if has_color:
        pixels = source.pixels.copy()
        delta = active = original = None
        for block_top in range(ct // 64 * 64, cb, 64):
            for block_left in range(cl // 1024 * 1024, cr, 1024):
                _check(cancelled)
                top, left = max(ct, block_top), max(cl, block_left)
                bottom, right = min(cb, block_top + 64), min(cr, block_left + 1024)
                delta = total_delta[top - ct:bottom - ct, left - cl:right - cl] / np.maximum(
                    1, color_weight[top - ct:bottom - ct, left - cl:right - cl])[..., None]
                active = np.any(delta != 0, axis=-1)
                original = source.pixels[top:bottom, left:right]
                pixels[top:bottom, left:right][active] = _gamut(original[active] + delta[active])
        del delta, active, original
        del total_delta, color_weight
    if has_warp:
        total_field /= np.maximum(1, warp_weight)[..., None]
        del warp_weight
        pixels, alpha = remap_once(pixels, alpha, total_field, cancelled)
        if sampling is not None and sampling.active:
            sampling.beauty_detail(total_field, cancelled)
    _check(cancelled)
    return replace(source, pixels=pixels, alpha=alpha,
                   effect_status=(*source.effect_status, replace(status, spots_skipped=skipped)))
