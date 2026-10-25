from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
from PIL import Image

from ..photo_engine import _pil_from, _src_from_pil
from ..studio_effects import render_tool
from ..tools.id_photo import ID_PHOTO_PARAMETERS


EFFECTS = {"beauty", "denoise", "ai_enhance", "bg_remove", "id_photo", "shape_crop", "lasso"}
PARAMETER_CONTRACTS = {"id_photo": ID_PHOTO_PARAMETERS}


def validate_params(params):
    if set(params) != {"tool", "settings"} or params["tool"] not in EFFECTS:
        raise ValueError("不支持的图像工具")
    if not isinstance(params["settings"], dict):
        raise ValueError("工具参数无效")
    contract = PARAMETER_CONTRACTS.get(params["tool"])
    if contract is not None and not contract.normalize(params["settings"]).success:
        raise ValueError("工具参数无效")


def execute(source, params, *, background_remover=None, sampling=None):
    validate_params(params)
    tool, settings = params["tool"], params["settings"]
    image = _pil_from(source)
    if tool == "beauty":
        from ..beauty_enhancer import BeautyConfig, BeautyEnhancer
        from dataclasses import fields
        config = {field.name: settings.get(field.name, 0) for field in fields(BeautyConfig)}
        options = {"sampling": sampling} if sampling is not None and sampling.active else {}
        result = BeautyEnhancer().enhance(image, BeautyConfig(**config), **options)
    elif tool == "denoise":
        from ..denoiser import DenoiseConfig, ImageDenoiser, get_recommended_model_path
        method = settings.get("method", "bilateral")
        result = ImageDenoiser().denoise(image, DenoiseConfig(
            mode="ai" if method == "ai" else "basic",
            basic_method=method if method in ("gaussian", "median", "bilateral") else "bilateral",
            luma_strength=int(settings.get("strength", 40)),
            chroma_strength=int(settings.get("color_strength", 30)),
            model_path=settings.get("model_path") or str(get_recommended_model_path()),
            tile_size=settings.get("tile_size", 512),
        ))
    else:
        result = render_tool(tool, image, settings, background_remover=background_remover, sampling=sampling)
    if result is None:
        raise ValueError("图像处理失败")
    if tool in ("beauty", "denoise") and image.mode == "RGBA":
        result = result.convert("RGBA")
        result.putalpha(image.getchannel("A"))
    return _src_from_pil(result, source_path=source.source_path, is_proxy=source.is_proxy)


def validate_geometry(params):
    if set(params) - {"crop_rect", "rotation_angle", "aspect_ratio"}:
        raise ValueError("裁切参数无效")
    rotation = params.get("rotation_angle", 0)
    if type(rotation) not in (float, int) or not math.isfinite(rotation) or abs(rotation) > 360:
        raise ValueError("旋转角度无效")
    box = params.get("crop_rect")
    if box is not None:
        if not isinstance(box, (list, tuple)) or len(box) != 4 or any(type(v) is not int for v in box):
            raise ValueError("裁切坐标无效")
        if min(box) < 0 or box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("裁切范围无效")


def execute_geometry(source, params, *, sampling=None):
    angle = params.get("rotation_angle", 0)
    pixels, alpha = source.pixels, source.alpha
    if abs(angle) > 0.0001:
        def rotate(channel):
            return np.asarray(Image.fromarray(channel).rotate(
                angle, resample=Image.Resampling.BICUBIC, expand=True), dtype=np.float32)
        pixels = np.stack([rotate(pixels[:, :, c]) for c in range(3)], axis=-1)
        alpha = None if alpha is None else np.clip(rotate(alpha), 0, 1)
        if sampling is not None and sampling.active:
            sampling.rotate(angle, output_size=(pixels.shape[1], pixels.shape[0]))
    box = params.get("crop_rect")
    if box:
        height, width = pixels.shape[:2]
        left, top = max(0, min(width - 1, box[0])), max(0, min(height - 1, box[1]))
        right, bottom = max(left + 1, min(width, box[2])), max(top + 1, min(height, box[3]))
        pixels = pixels[top:bottom, left:right].copy()
        alpha = None if alpha is None else alpha[top:bottom, left:right].copy()
        if sampling is not None and sampling.active:
            sampling.crop((left, top, right, bottom))
    return replace(source, pixels=pixels, alpha=alpha, orig_size=(pixels.shape[1], pixels.shape[0]))
