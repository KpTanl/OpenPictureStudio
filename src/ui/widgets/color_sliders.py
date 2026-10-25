"""调色面板的滑块声明：文案与显示格式在这里，范围与默认值取自调色参数契约。"""

from __future__ import annotations

from resources.styles.semantic_colors import TRACK_GRADIENTS

from ...core.tools.color import COLOR_UI_PARAMETERS, UI_RANGES
from .parameter_row import ParameterSpec


def _specs(items) -> tuple[ParameterSpec, ...]:
    defaults = COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version)
    return tuple(ParameterSpec(key, label_key, fallback, *UI_RANGES[key], defaults[key], value_format, track)
                 for key, label_key, fallback, value_format, track in items)


WB_SLIDER_DEFS = _specs([
    ("temperature", "color_adjust.slider.temperature", "Temperature", "kelvin", TRACK_GRADIENTS["temperature"]),
    ("tint", "color_adjust.slider.tint", "Tint", "int_signed", ()),
])

TONE_SLIDER_DEFS = _specs([
    ("exposure_amt", "color_adjust.slider.exposure_amt", "Exposure", "ev", ()),
    ("contrast_amt", "color_adjust.slider.contrast_amt", "Contrast", "int_signed", ()),
    ("highlights", "color_adjust.slider.highlights", "Highlights", "int_signed", ()),
    ("shadows", "color_adjust.slider.shadows", "Shadows", "int_signed", ()),
    ("whites", "color_adjust.slider.whites", "Whites", "int_signed", ()),
    ("blacks", "color_adjust.slider.blacks", "Blacks", "int_signed", ()),
])

PRESENCE_SLIDER_DEFS = _specs([
    ("texture_amt", "color_adjust.slider.texture", "Texture", "int_signed", ()),
    ("clarity_amt", "color_adjust.slider.clarity", "Clarity", "int_signed", ()),
    ("dehaze_amt", "color_adjust.slider.dehaze", "Dehaze", "int_signed", ()),
    ("vibrance_amt", "color_adjust.slider.vibrance", "Vibrance", "int_signed", ()),
    ("saturation_amt", "color_adjust.slider.saturation_amt", "Saturation", "int_signed",
     TRACK_GRADIENTS["saturation"]),
])
