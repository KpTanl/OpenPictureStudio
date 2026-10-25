"""Versioned denoise parameters and display-domain rendering."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from PIL import Image

from ..denoiser import (
    DenoiseConfig,
    ImageDenoiser,
    get_recommended_model_path,
    is_ai_model_available,
)
from ..photo_engine import RenderSource, _pil_from, srgb_to_linear
from .catalog import ParameterContract, ParameterField


class DenoiseBackendUnavailable(RuntimeError):
    pass


DENOISE_PARAMETERS = ParameterContract(
    (
        ParameterField("method", str, "bilateral", choices=("bilateral", "gaussian", "median", "ai")),
        ParameterField("strength", int, 0, 0, 100),
        ParameterField("color_strength", int, 0, 0, 100),
        ParameterField("sharpen", int, 0, 0, 100),
    ),
    version=2,
    neutral=lambda params: not (params["strength"] or params["color_strength"] or params["sharpen"]),
)


def _checked_params(params: dict) -> dict:
    checked = DENOISE_PARAMETERS.normalize(params, 2)
    if not checked.success:
        field = checked.error.details.get("field_path", "settings")
        raise ValueError(f"无效降噪参数：{field}")
    return checked.data


def render_pil(image: Image.Image, params: dict, model_path: str | None = None, *,
               processor=None, basic_version=2) -> Image.Image:
    """Render a versioned effect for preview and full-size application."""
    settings = _checked_params(params)
    if DENOISE_PARAMETERS.is_neutral(settings, 2):
        return image.copy()

    method = settings["method"]
    luma = settings["strength"]
    chroma = settings["color_strength"]
    sharpen = settings["sharpen"]
    source_alpha = image.getchannel("A") if image.mode == "RGBA" else None
    owns_processor = processor is None
    if owns_processor:
        processor = ImageDenoiser()
    try:
        with image.convert("RGB") as rgb:
            if luma or chroma:
                from .. import denoiser

                if denoiser.cv2 is None or denoiser.np is None:
                    raise DenoiseBackendUnavailable("OpenCV 和 NumPy 不可用")
                if method == "ai":
                    model_path = model_path or str(get_recommended_model_path())
                    if not is_ai_model_available(model_path):
                        raise FileNotFoundError("AI 降噪模型不可用")
                    result = processor.denoise_ai(rgb, model_path, luma, chroma)
                    sharpened = processor.apply_sharpen(result, sharpen)
                    if sharpened is not result:
                        result.close()
                    result = sharpened
                else:
                    result = processor.denoise(
                        rgb,
                        DenoiseConfig(
                            basic_method=method,
                            luma_strength=luma,
                            chroma_strength=chroma,
                            sharpen=sharpen,
                            basic_version=basic_version,
                        ),
                    )
            else:
                result = processor.apply_sharpen(rgb, sharpen)

            if result is rgb:
                result = rgb.copy()
            if source_alpha is not None:
                rgba = result.convert("RGBA")
                result.close()
                result = rgba
                result.putalpha(source_alpha)
            return result
    finally:
        if source_alpha is not None:
            source_alpha.close()
        if owns_processor:
            processor.close()


def execute(source: RenderSource, params: dict, model_path: str | None = None, *,
            processor=None, basic_version=2) -> RenderSource:
    """Process a float32 source through the declared sRGB 8-bit tool stage."""
    settings = _checked_params(params)
    if DENOISE_PARAMETERS.is_neutral(settings, 2):
        return source
    with _pil_from(source) as image:
        with render_pil(image, settings, model_path=model_path, processor=processor,
                        basic_version=basic_version) as rendered:
            with rendered.convert("RGB") as converted:
                rgb = np.asarray(converted, dtype=np.float32) / np.float32(255.0)
    pixels = srgb_to_linear(rgb)
    alpha = None if source.alpha is None else source.alpha.copy()
    return replace(source, pixels=pixels, alpha=alpha)
