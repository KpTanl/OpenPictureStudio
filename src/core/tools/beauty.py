"""Beauty tool v2 parameter contract and image renderer."""

from __future__ import annotations

from dataclasses import replace

from PIL import Image

from ..beauty_enhancer import LANDMARK_SETTINGS, BeautyConfig
from ..photo_engine import RenderSource, _pil_from, _src_from_pil
from .catalog import ParameterContract, ParameterField


BEAUTY_PARAMETERS = ParameterContract(
    (
        ParameterField("skin_smooth", int, 0, 0, 100),
        ParameterField("skin_brighten", int, 0, 0, 100),
        ParameterField("skin_rosy", int, 0, 0, 100),
        ParameterField("sharpen", int, 0, 0, 100),
        ParameterField("eye_enlarge", int, 0, 0, 100),
        ParameterField("eye_distance", int, 0, -100, 100),
        ParameterField("face_slim", int, 0, 0, 100),
        ParameterField("nose_slim", int, 0, 0, 100),
        ParameterField("teeth_whiten", int, 0, 0, 100),
        ParameterField("spot_remove", int, 0, 0, 100),
        ParameterField("eye_bag_remove", int, 0, 0, 100),
    ),
    version=2,
    neutral=lambda params: not any(params.values()),
)

BEAUTY_RECOMMENDED_PRESET = {
    **BEAUTY_PARAMETERS.defaults(2),
    "skin_smooth": 50,
    "skin_brighten": 30,
    "skin_rosy": 20,
    "sharpen": 15,
}


def normalize_params(params: dict) -> dict[str, int]:
    checked = BEAUTY_PARAMETERS.normalize(params, 2)
    if not checked.success:
        raise ValueError("美颜参数无效")
    return checked.data


def render_pil(image: Image.Image, params: dict, *, backend=None, processor=None, sampling=None) -> Image.Image:
    """Render a PIL preview or compatibility image with the v2 beauty settings."""
    checked = normalize_params(params)
    if BEAUTY_PARAMETERS.is_neutral(checked, 2):
        return image.copy()

    from .beauty_backend import preferred_backend

    owns_processor = processor is None
    if owns_processor:
        processor = (backend or preferred_backend()).create()
    try:
        with image.convert("RGB") as rgb:
            options = {"sampling": sampling} if sampling is not None and sampling.active else {}
            result = processor.enhance(rgb, BeautyConfig(**checked), **options)
            if result is rgb:
                result = rgb.copy()
    finally:
        if owns_processor:
            processor.close()
    if result is None or result.size != image.size:
        raise ValueError("美颜处理失败")
    if "A" in image.getbands():
        rgba = result.convert("RGBA")
        result.close()
        result = rgba
        with image.getchannel("A") as alpha:
            result.putalpha(alpha)
    return result


def execute(source: RenderSource, params: dict, *, backend=None, processor=None, sampling=None) -> RenderSource:
    """Render a workflow image while retaining source metadata and alpha coverage."""
    checked = normalize_params(params)
    if BEAUTY_PARAMETERS.is_neutral(checked, 2):
        return source

    rgb_source = replace(source, alpha=None) if source.alpha is not None else source
    with _pil_from(rgb_source) as image:
        with render_pil(image, checked, backend=backend, processor=processor, sampling=sampling) as result:
            converted = _src_from_pil(result, source_path=source.source_path, is_proxy=source.is_proxy)
    return replace(source, pixels=converted.pixels, alpha=source.alpha)


def analyze_auto(image: Image.Image, *, backend=None, processor=None) -> dict:
    from .beauty_backend import preferred_backend

    owns_processor = processor is None
    if owns_processor:
        processor = (backend or preferred_backend()).create()
    try:
        return processor.analyze_auto_beauty(image)
    finally:
        if owns_processor:
            processor.close()
