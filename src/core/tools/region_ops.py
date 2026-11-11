from dataclasses import replace

import numpy as np

from .geometry import crop_box, rotated_size, shape_mask
from ..execution_memo import memo


def color_input(params, bounds, size):
    if (params.get("masks") or params.get("sharpness", 1) != 1
            or any(params.get(key, 0) for key in ("highlights", "shadows", "whites", "blacks", "texture", "clarity", "dehaze"))):
        return None
    return bounds


def color_region(source, params, context):
    from ..photo_engine import render_source
    return render_source(source, params, threads=context["threads"], cancelled=context["is_cancelled"])


def crop_size(params, size):
    size = rotated_size(size, params["rotation_angle"])
    left, top, right, bottom = crop_box(params["crop_rect"], size)
    return right-left, bottom-top


def crop_input(params, bounds, size):
    if params["rotation_angle"]:
        return None
    left, top, _, _ = crop_box(params["crop_rect"], size)
    return bounds[0]+left, bounds[1]+top, bounds[2]+left, bounds[3]+top


def preserve(source, params, context):
    return replace(source, orig_size=context["full_output_size"])


def shape_bounds(params, size):
    x, y, width, height = params["rect"]
    return crop_box((x, y, min(1, x+width), min(1, y+height)), size)


def shape_size(params, size):
    left, top, right, bottom = shape_bounds(params, size)
    return right-left, bottom-top


def shape_input(params, bounds, size):
    left, top, _, _ = shape_bounds(params, size)
    return bounds[0]+left, bounds[1]+top, bounds[2]+left, bounds[3]+top


def shape_region(source, params, context):
    size = context["full_input_size"]
    mask, _ = memo("shape.mask", (size, size, params), lambda: shape_mask(params, size, size))
    left, top, right, bottom = context["input_bounds"]
    coverage = mask[top:bottom, left:right].astype(np.float32)/np.float32(255)
    alpha = coverage if source.alpha is None else source.alpha*coverage
    return replace(source, alpha=alpha, orig_size=context["full_output_size"])


def lasso_region(source, params, context):
    from .geometry import execute_lasso
    from ..photo_engine import RenderSource
    size = context["full_input_size"]
    def prepare():
        pixels = np.broadcast_to(np.zeros((1, 1, 3), np.float32), (size[1], size[0], 3))
        full = RenderSource(pixels, None, source.source_format, source.source_path, False, size, source.colorspace)
        return execute_lasso(full, params).alpha
    mask = memo("lasso.mask", (size, params), prepare)
    if mask is None:
        return source
    left, top, right, bottom = context["input_bounds"]
    coverage = mask[top:bottom, left:right]
    alpha = coverage if source.alpha is None else source.alpha*coverage
    return replace(source, alpha=alpha)
