"""显示 sRGB8 的测量规格 1；完整计数留在本机，摘要与差异供助手参考。"""

from __future__ import annotations

from io import BytesIO
import base64
import math

import numpy as np

from ..core.mixer_sampling import MIXER_CHANNELS, mixer_weights, rgb_to_hsl

_LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)
ANALYSIS_EDGE = 1600
SAMPLE_LIMIT = 200000
ANALYSIS_WORKING_BYTES = 64 * 1024 * 1024
REFERENCE_THUMBNAIL_EDGE = 96
MEASUREMENT_SPEC = {"name": "ops-display-srgb8", "version": 1}
_TONE_PERCENTILES = (1, 5, 10, 25, 50, 75, 90, 95, 99)
_CHROMA_PERCENTILES = (50, 75, 90, 99)


def display_lab(rgb: np.ndarray) -> np.ndarray:
    """显示编码 sRGB（0—1）到 CIELAB D65。"""
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = linear @ np.array([[0.4124564, 0.2126729, 0.0193339],
                             [0.3575761, 0.7151522, 0.1191920],
                             [0.1804375, 0.0721750, 0.9503041]], np.float32)
    xyz /= np.array([0.95047, 1.0, 1.08883], np.float32)
    e, k = 216.0 / 24389.0, 24389.0 / 27.0
    f = np.where(xyz > e, np.cbrt(xyz), (k * xyz + 16.0) / 116.0)
    return np.stack([116.0 * f[..., 1] - 16.0, 500.0 * (f[..., 0] - f[..., 1]), 200.0 * (f[..., 1] - f[..., 2])], -1)


def _percentiles(values, points):
    return {"p" + str(point): round(float(value), 6)
            for point, value in zip(points, np.percentile(values, points))}


def _sample_step(height, width):
    step = max(1, math.ceil(math.sqrt(height * width / SAMPLE_LIMIT)))
    while math.ceil(height / step) * math.ceil(width / step) > SAMPLE_LIMIT:
        step += 1
    return step


def photo_statistics(rgb8: np.ndarray) -> dict:
    height, width = rgb8.shape[:2]
    step = _sample_step(height, width)
    sampled = rgb8[::step, ::step].reshape(-1, rgb8.shape[2])
    if sampled.shape[1] == 4:
        sampled = sampled[sampled[:, 3] >= 128]
    sample8 = sampled[:, :3]
    count = len(sample8)
    result = {"spec": dict(MEASUREMENT_SPEC), "size": [width, height], "sample_count": count,
              "sample_step": step, "alpha_min": 128, "status": "available" if count else "missing",
              "histogram": {name: [0] * 256 for name in ("r", "g", "b", "luma")}}
    if not count:
        return result
    encoded = sample8.astype(np.float32)
    luma255 = _LUMA[0] * encoded[:, 0] + _LUMA[1] * encoded[:, 1] + _LUMA[2] * encoded[:, 2]
    luma = luma255 / 255.0
    sample = encoded / 255.0
    lab = display_lab(sample)
    chroma = np.hypot(lab[:, 1], lab[:, 2])
    angle = np.arctan2(lab[:, 2], lab[:, 1])
    for index, name in enumerate(("r", "g", "b")):
        result["histogram"][name] = np.bincount(sample8[:, index], minlength=256).tolist()
    result["histogram"]["luma"] = np.bincount(
        np.clip(np.rint(luma255), 0, 255).astype(np.uint8), minlength=256).tolist()
    result.update(luma_percentiles=_percentiles(luma, _TONE_PERCENTILES),
                  chroma_percentiles=_percentiles(chroma, _CHROMA_PERCENTILES),
                  near_black_share=float(np.mean(sample8.max(axis=1) <= 2)),
                  near_white_share=float(np.mean(sample8.min(axis=1) >= 253)))
    split = {}
    for name, segment in (("shadows", luma < .25), ("midtones", (luma >= .25) & (luma <= .75)),
                          ("highlights", luma > .75)):
        neutral = segment & (chroma < 10)
        share = float(np.mean(neutral))
        row = {"status": "weak" if share >= .005 else "missing", "share": share}
        if share >= .005:
            row.update(a=float(np.mean(lab[neutral, 1])), b=float(np.mean(lab[neutral, 2])),
                       L=float(np.mean(lab[neutral, 0])), rgb=np.mean(sample[neutral], axis=0).tolist(),
                       luma=float(np.mean(luma[neutral])))
        split[name] = row
    hue, saturation, _lightness = rgb_to_hsl(sample)
    hues = {}
    for name, weights in mixer_weights(hue, saturation):
        total = float(np.sum(weights, dtype=np.float64))
        row = {"share": total / count, "status": "available" if total > 0 else "missing"}
        if total > 0:
            sine = float(np.sum(weights * np.sin(angle), dtype=np.float64))
            cosine = float(np.sum(weights * np.cos(angle), dtype=np.float64))
            row.update(L=float(np.sum(weights * lab[:, 0], dtype=np.float64) / total),
                       C=float(np.sum(weights * chroma, dtype=np.float64) / total),
                       hab=math.degrees(math.atan2(sine, cosine)) % 360 if math.hypot(sine, cosine) > total * 1e-7 else None)
        hues[name] = row
    result.update(split_tone=split, hues=hues)
    return result


def paired_style_measurements(before, result, without_master):
    if before.shape != result.shape or before.shape != without_master.shape:
        raise ValueError("AI_STYLE_GEOMETRY")
    step = _sample_step(*before.shape[:2])
    frames = [frame[::step, ::step].reshape(-1, frame.shape[2]) for frame in (before, result, without_master)]
    valid = np.logical_and.reduce([frame[:, 3] >= 128 for frame in frames])
    if not np.any(valid):
        raise ValueError("AI_FIT_MEASUREMENT")
    a, b, prime = [frame[valid, :3].astype(np.float32) / 255 for frame in frames]
    luma_a, luma_b, luma_prime = [frame @ _LUMA for frame in (a, b, prime)]
    bins = np.clip(np.rint(luma_prime * 255), 0, 255).astype(np.uint8)
    indices = np.unique(bins)
    medians = np.array([np.median(luma_b[bins == index]) for index in indices])
    curve = np.interp(np.arange(256), indices, medians)
    weights = np.maximum(np.bincount(bins, minlength=256), 1)
    blocks = []
    for index, (value, weight) in enumerate(zip(curve, weights)):
        blocks.append([value * weight, float(weight), index, index + 1])
        while len(blocks) >= 2 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            right, left = blocks.pop(), blocks.pop()
            blocks.append([left[0] + right[0], left[1] + right[1], left[2], right[3]])
    for total, weight, start, end in blocks:
        curve[start:end] = total / weight
    lab_a, lab_b = display_lab(a), display_lab(b)
    chroma_a, chroma_b = [np.hypot(lab[:, 1], lab[:, 2]) for lab in (lab_a, lab_b)]
    split = {}
    for name, segment in (("shadows", luma_a < .25), ("midtones", (luma_a >= .25) & (luma_a <= .75)),
                          ("highlights", luma_a > .75)):
        neutral = segment & (chroma_a < 10)
        share = float(np.mean(neutral))
        split[name] = ({"status": "paired", "share": share,
                        "a": float(np.mean(lab_b[neutral, 1] - lab_a[neutral, 1])),
                        "b": float(np.mean(lab_b[neutral, 2] - lab_a[neutral, 2]))}
                       if share >= .005 else {"status": "missing", "share": share})
    hue, saturation, _lightness = rgb_to_hsl(a)
    hues = {}
    for name, weight in mixer_weights(hue, saturation):
        total = float(np.sum(weight, dtype=np.float64))
        row = {"status": "paired" if total / len(a) >= .02 else "missing", "share": total / len(a)}
        if row["status"] == "paired":
            angles = []
            for lab in (lab_a, lab_b):
                angle = np.arctan2(lab[:, 2], lab[:, 1])
                sine, cosine = [float(np.sum(weight * values, dtype=np.float64)) for values in (np.sin(angle), np.cos(angle))]
                angles.append(math.degrees(math.atan2(sine, cosine)) if math.hypot(sine, cosine) > total * 1e-7 else None)
            row.update(L=float(np.sum(weight * (lab_b[:, 0] - lab_a[:, 0]), dtype=np.float64) / total),
                       C=float(np.sum(weight * (chroma_b - chroma_a), dtype=np.float64) / total),
                       hab=(angles[1] - angles[0] + 180) % 360 - 180 if all(value is not None for value in angles) else None)
        hues[name] = row
    narrow = float(np.percentile(luma_prime, 99) - np.percentile(luma_prime, 1)) < .02
    if narrow:
        curve = np.linspace(0, 1, 256)
    return {"spec": dict(MEASUREMENT_SPEC), "sample_count": len(a), "sample_step": step,
            "transfer_status": "narrow_luma_range" if narrow else "available",
            "transfer": np.clip(curve, 0, 1).tolist(), "split_tone": split, "hues": hues}


def analysis_summary(analysis):
    if analysis is None:
        return None
    return {key: analysis_summary(value) if isinstance(value, dict) else value
            for key, value in analysis.items() if key != "histogram"}


def _statistics_difference(current, reference):
    result = {"spec": dict(MEASUREMENT_SPEC), "direction": "current_minus_reference",
              "current": analysis_summary(current), "reference": analysis_summary(reference)}
    if current.get("spec") != MEASUREMENT_SPEC or reference.get("spec") != MEASUREMENT_SPEC:
        raise ValueError("AI_REFERENCE_SPEC")
    if current.get("status") != "available" or reference.get("status") != "available":
        result["status"] = "missing"
        return result
    result["status"] = "available"
    for key in ("luma_percentiles", "chroma_percentiles"):
        result[key] = {point: current[key][point] - reference[key][point] for point in current[key]}
    result["split_tone"] = {}
    for name, row in current["split_tone"].items():
        target = reference["split_tone"][name]
        result["split_tone"][name] = ({"status": "weak", "a": row["a"] - target["a"], "b": row["b"] - target["b"]}
                                        if row["status"] != "missing" and target["status"] != "missing"
                                        else {"status": "missing", "current": row["status"], "reference": target["status"]})
    result["hues"] = {}
    for name in MIXER_CHANNELS:
        row, target = current["hues"][name], reference["hues"][name]
        present, target_present = row["share"] >= .02, target["share"] >= .02
        if present and target_present:
            result["hues"][name] = {"status": "comparable", "L": row["L"] - target["L"], "C": row["C"] - target["C"],
                                    "hab": (row["hab"] - target["hab"] + 180) % 360 - 180
                                    if row["hab"] is not None and target["hab"] is not None else None}
        else:
            result["hues"][name] = {"status": "current_only" if present else "reference_only" if target_present else "below_threshold"}
    current_coverage = sum(current["hues"][name]["share"] for name in MIXER_CHANNELS)
    reference_coverage = sum(reference["hues"][name]["share"] for name in MIXER_CHANNELS)
    result["color_distribution_intersection"] = (min(1.0, sum(
        min(current["hues"][name]["share"] / current_coverage, reference["hues"][name]["share"] / reference_coverage)
        for name in MIXER_CHANNELS)) if current_coverage > 0 and reference_coverage > 0 else None)
    result["color_distribution_coverage"] = {"current": current_coverage, "reference": reference_coverage,
                                             "status": "available" if current_coverage > 0 and reference_coverage > 0 else "missing"}
    result["interpretation"] = ("Color distribution overlap is not semantic comparability. Scene content and grading both affect it. "
                                 "Split-tone measurements are weak evidence affected by lighting and neutral-pixel selection bias.")
    return result


def reference_difference(current, reference):
    result = _statistics_difference(current, reference)
    if result["status"] == "available":
        result["face_rectangle"] = (_statistics_difference(current["face_rectangle"], reference["face_rectangle"])
            if current.get("face_rectangle") and reference.get("face_rectangle") else {"status": "missing"})
    return result


def encode_jpeg(image, longest_edge: int, quality: int, *, subsampling=None) -> bytes:
    """长边不超过 longest_edge 的无元数据 JPEG；透明区域合成到白底。"""
    photo = _evidence_rgb(image)
    try:
        photo.thumbnail((longest_edge, longest_edge))
        buffer = BytesIO()
        photo.save(buffer, format="JPEG", quality=quality, **({"subsampling": subsampling} if subsampling is not None else {}))
        return buffer.getvalue()
    finally:
        photo.close()


def _evidence_rgb(image):
    if "A" not in image.getbands() and "transparency" not in image.info:
        return image.convert("RGB")
    from PIL import Image
    with image.convert("RGBA") as rgba:
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        try:
            with rgba.getchannel("A") as alpha:
                background.paste(rgba, mask=alpha)
            return background
        except Exception:
            background.close()
            raise


def reference_thumbnail(image):
    """小幅 RGB8 原始像素随记录保存，界面直接交付显示，不在主线程解码图片。"""
    with _evidence_rgb(image) as photo:
        photo.thumbnail((REFERENCE_THUMBNAIL_EDGE, REFERENCE_THUMBNAIL_EDGE))
        return {"size": list(photo.size), "rgb": base64.b64encode(photo.tobytes()).decode("ascii")}


def portrait_box(image_size, regions):
    if not regions:
        return None
    left, top, right, bottom = max(regions, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
    if not all(math.isfinite(value) for value in (left, top, right, bottom)) or right <= left or bottom <= top:
        return None
    width, height = image_size
    pad_x, pad_y = (right - left) * 0.15, (bottom - top) * 0.15
    box = (max(0, int((left - pad_x) * width)), max(0, int((top - pad_y) * height)),
           min(width, int(math.ceil((right + pad_x) * width))), min(height, int(math.ceil((bottom + pad_y) * height))))
    return box if box[2] > box[0] and box[3] > box[1] else None


def encode_evidence(image, regions=()):
    from .assistant_models import IMAGE_SOURCES

    edge, quality = IMAGE_SOURCES["current"]
    images = [encode_jpeg(image, edge, quality)]
    box = portrait_box(image.size, regions)
    if box:
        with image.crop(box) as portrait:
            images.append(encode_jpeg(portrait, 768, quality))
    return images


def analyzed_photo(image, regions=(), *, identity=None, display="current"):
    with image.convert("RGBA") as rgb:
        rgb.thumbnail((ANALYSIS_EDGE, ANALYSIS_EDGE))
        result = photo_statistics(np.asarray(rgb))
        result.update(identity=identity, display=display)
        box = portrait_box(rgb.size, regions)
        if box:
            with rgb.crop(box) as portrait:
                region = photo_statistics(np.asarray(portrait))
            region.update(identity=identity, display=display, region="face_rectangle_including_hair_and_background", box=list(box))
            result["face_rectangle"] = region
            result["face_count"] = len(regions)
        return result
