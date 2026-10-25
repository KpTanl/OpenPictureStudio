from __future__ import annotations

from dataclasses import replace

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .catalog import ParameterContract, ParameterField
from .color import number, point


def validate_crop(params):
    box = params["crop_rect"]
    if box is not None:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise ValueError("裁剪边界无效")
        for value in box:
            number(value, 0, 1)
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("裁剪边界无效")
    if params["aspect_ratio"] is not None:
        number(params["aspect_ratio"], 0.01, 100)


def validate_shape(params):
    rect = params["rect"]
    if len(rect) != 4:
        raise ValueError("形状边界无效")
    for value in rect:
        number(value, 0, 1)
    x, y, width, height = rect
    if width <= 0 or height <= 0 or x + width > 1.00000001 or y + height > 1.00000001:
        raise ValueError("形状超出图像边界")


def validate_lasso(params):
    if len(params["regions"]) > 128:
        raise ValueError("选区数量超限")
    count = len(params["draft_points"])
    for item in params["draft_points"]:
        point(item)
    for region in params["regions"]:
        if (not isinstance(region, dict) or set(region) != {"points", "op"}
                or region["op"] not in ("add", "subtract")
                or not isinstance(region["points"], (list, tuple)) or len(region["points"]) < 3):
            raise ValueError("套索选区无效")
        count += len(region["points"])
        for item in region["points"]:
            point(item)
    if count > 16384:
        raise ValueError("选区点数超限")


def validate_grid(params):
    for key in ("h_lines", "v_lines"):
        values = params[key]
        if values is None:
            continue
        if len(values) > 9:
            raise ValueError("分割线数量超限")
        for value in values:
            number(value, 0.001, 0.999)
        if any(a >= b for a, b in zip(values, values[1:])):
            raise ValueError("分割线必须递增")


CROP_PARAMETERS = ParameterContract((
    ParameterField("crop_rect", (list, tuple, type(None)), None),
    ParameterField("rotation_angle", (int, float), 0, -360, 360),
    ParameterField("aspect_ratio", (int, float, type(None)), None),
), version=2, validate=validate_crop,
    neutral=lambda p: not p["rotation_angle"] and (p["crop_rect"] is None or list(p["crop_rect"]) == [0, 0, 1, 1]))

SHAPE_PARAMETERS = ParameterContract((
    ParameterField("shape", str, "circle", choices=("circle", "ellipse", "round_rect", "rect")),
    ParameterField("corner_radius", (int, float), 20, 0, 100),
    ParameterField("rect", (list, tuple), [0.15, 0.15, 0.7, 0.7]),
), version=2, validate=validate_shape)

LASSO_PARAMETERS = ParameterContract((
    ParameterField("feather", (int, float), 0, 0, 50),
    ParameterField("keep_inside", bool, True),
    ParameterField("regions", list, []),
    ParameterField("draft_points", list, []),
), version=2, validate=validate_lasso,
    neutral=lambda p: not p["regions"] and len(p["draft_points"]) < 3)

GRID_PARAMETERS = ParameterContract((
    ParameterField("rows", int, 3, 1, 10),
    ParameterField("cols", int, 3, 1, 10),
    ParameterField("h_lines", (list, type(None)), None),
    ParameterField("v_lines", (list, type(None)), None),
), validate=validate_grid)


def checked(contract, params):
    result = contract.normalize(params, contract.version)
    if not result.success:
        raise ValueError(result.error.message_key)
    return result.data


def crop_box(box, size):
    width, height = size
    left, top, right, bottom = box or (0, 0, 1, 1)
    x = min(width - 1, round(left * width))
    y = min(height - 1, round(top * height))
    return x, y, max(x + 1, min(width, round(right * width))), max(y + 1, min(height, round(bottom * height)))


def rotated_size(size, angle):
    import math
    angle %= 360
    width, height = size
    if angle in (0, 180):
        return size
    if angle in (90, 270):
        return height, width
    sine = round(math.sin(math.radians(angle)), 15)
    cosine = round(math.cos(math.radians(angle)), 15)
    span_x = abs(width * cosine) + abs(height * sine)
    span_y = abs(height * cosine) + abs(width * sine)
    return (math.ceil((width + span_x) / 2) - math.floor((width - span_x) / 2),
            math.ceil((height + span_y) / 2) - math.floor((height - span_y) / 2))


def execute_crop(source, params, *, sampling=None):
    params = checked(CROP_PARAMETERS, params)
    if CROP_PARAMETERS.is_neutral(params, 2):
        return source
    pixels, alpha = source.pixels, source.alpha
    formal_size = source.orig_size
    angle = params["rotation_angle"]
    if angle:
        output_size = rotated_size((pixels.shape[1], pixels.shape[0]), angle)
        if output_size[0] * output_size[1] > 100_000_000:
            raise ValueError("error.export.size")
        coverage = np.ones(pixels.shape[:2], dtype=np.float32) if alpha is None else alpha
        def rotate(channel):
            return np.asarray(Image.fromarray(channel).rotate(
                angle, resample=Image.Resampling.BICUBIC, expand=True), dtype=np.float32)
        premultiplied = pixels * coverage[..., None]
        alpha = np.clip(rotate(coverage), 0, 1)
        pixels = np.stack([rotate(premultiplied[..., i]) for i in range(3)], axis=-1)
        pixels = np.divide(pixels, alpha[..., None], out=np.zeros_like(pixels), where=alpha[..., None] > 1e-8)
        if sampling is not None and sampling.active:
            sampling.rotate(angle, output_size=(pixels.shape[1], pixels.shape[0]))
        formal_size = rotated_size(formal_size, angle) if source.is_proxy else (pixels.shape[1], pixels.shape[0])
    left, top, right, bottom = crop_box(params["crop_rect"], (pixels.shape[1], pixels.shape[0]))
    fl, ft, fr, fb = crop_box(params["crop_rect"], formal_size)
    if sampling is not None and sampling.active:
        sampling.crop((left, top, right, bottom))
    return replace(source, pixels=pixels[top:bottom, left:right].copy(),
                   alpha=None if alpha is None else alpha[top:bottom, left:right].copy(),
                   orig_size=(fr - fl, fb - ft))


def _masked(source, mask, *, sampling=None):
    coverage = np.asarray(mask, dtype=np.float32) / np.float32(255)
    if sampling is not None and sampling.active:
        sampling.apply_mask(coverage)
    alpha = coverage if source.alpha is None else source.alpha * coverage
    return replace(source, alpha=alpha)


def execute_shape(source, params, *, sampling=None):
    params = checked(SHAPE_PARAMETERS, params)
    width, height = source.pixels.shape[1], source.pixels.shape[0]
    x, y, w, h = params["rect"]
    left, top, right, bottom = crop_box((x, y, min(1, x + w), min(1, y + h)), (width, height))
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    box = (left, top, right - 1, bottom - 1)
    bounds = (left, top, right, bottom)
    if params["shape"] == "circle":
        side = min(right - left, bottom - top)
        left += (right - left - side) // 2
        top += (bottom - top - side) // 2
        draw.ellipse((left, top, left + side - 1, top + side - 1), fill=255)
    elif params["shape"] == "ellipse":
        draw.ellipse(box, fill=255)
    elif params["shape"] == "round_rect":
        ratio = min(width / source.orig_size[0], height / source.orig_size[1])
        draw.rounded_rectangle(box, radius=params["corner_radius"] * ratio, fill=255)
    else:
        draw.rectangle(box, fill=255)
    masked = _masked(source, mask, sampling=sampling)
    left, top, right, bottom = bounds
    if sampling is not None and sampling.active:
        sampling.crop(bounds)
    fl, ft, fr, fb = crop_box((x, y, min(1, x + w), min(1, y + h)), source.orig_size)
    return replace(masked, pixels=source.pixels[top:bottom, left:right].copy(),
                   alpha=masked.alpha[top:bottom, left:right].copy(), orig_size=(fr - fl, fb - ft))


def execute_lasso(source, params, *, sampling=None):
    from ..lasso_tool import LassoTool

    params = checked(LASSO_PARAMETERS, params)
    if LASSO_PARAMETERS.is_neutral(params, 2):
        return source
    width, height = source.pixels.shape[1], source.pixels.shape[0]
    regions = list(params["regions"])
    if len(params["draft_points"]) >= 3:
        regions.append({"points": params["draft_points"], "op": "add"})
    regions = [{"points": [(x * width, y * height) for x, y in item["points"]], "op": item["op"]}
               for item in regions]
    mask = LassoTool().build_selection_mask((width, height), regions)
    if not params["keep_inside"]:
        mask = ImageChops.invert(mask)
    ratio = min(width / source.orig_size[0], height / source.orig_size[1])
    if params["feather"]:
        mask = mask.filter(ImageFilter.GaussianBlur(params["feather"] * ratio))
    return _masked(source, mask, sampling=sampling)
