"""调色滑块定义与读数格式化、解析的契约。"""

import pytest

from src.core.tools.color import COLOR_UI_PARAMETERS, UI_RANGES
from src.ui.widgets.color_sliders import PRESENCE_SLIDER_DEFS, TONE_SLIDER_DEFS, WB_SLIDER_DEFS
from src.ui.widgets.value_editing import format_slider_value, parse_slider_value

ALL_DEFS = WB_SLIDER_DEFS + TONE_SLIDER_DEFS + PRESENCE_SLIDER_DEFS


def test_slider_defs_follow_the_color_contract():
    """范围与默认值只来自调色参数契约，滑块定义不另存一份。"""
    defaults = COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version)
    keys = [spec.key for spec in ALL_DEFS]
    assert len(keys) == len(set(keys))
    for spec in ALL_DEFS:
        assert (spec.minimum, spec.maximum) == tuple(UI_RANGES[spec.key])
        assert spec.default == defaults[spec.key]
        assert spec.minimum <= spec.default <= spec.maximum


@pytest.mark.parametrize("fmt, raw, text", [
    ("ev", 150, "+1.50"),
    ("ev", 0, "+0.00"),
    ("ev", -25, "-0.25"),
    ("int_signed", 12, "+12"),
    ("int_signed", 0, "0"),
    ("int_signed", -5, "-5"),
    ("kelvin", 0, "5500K"),
    ("kelvin", 100, "11000K"),
    ("kelvin", -100, "2750K"),
    ("int", 12, "12"),
])
def test_format_slider_value(fmt, raw, text):
    assert format_slider_value(fmt, raw) == text


@pytest.mark.parametrize("fmt, text, expected", [
    ("ev", "+1.50", 150),
    ("ev", " -0.25 ", -25),
    ("int_signed", "+12", 12),
    ("int_signed", "12.6", 13),
    ("kelvin", "5500K", 0),
    ("kelvin", "11000", 100),
    ("kelvin", "2750k", -100),
])
def test_parse_slider_value(fmt, text, expected):
    assert parse_slider_value(fmt, text, -300, 300) == expected


@pytest.mark.parametrize("text", ["", "   ", "abc", "1e999", "nan", "+-3"])
def test_parse_rejects_invalid_text(text):
    assert parse_slider_value("int_signed", text, -100, 100) is None


def test_parse_clamps_to_slider_range():
    assert parse_slider_value("ev", "9", -300, 300) == 300
    assert parse_slider_value("int_signed", "-500", -100, 100) == -100
    assert parse_slider_value("kelvin", "50000", -100, 100) == 100


@pytest.mark.parametrize("key, low, high, default, fmt", [
    (spec.key, spec.minimum, spec.maximum, spec.default, spec.value_format) for spec in ALL_DEFS
])
def test_displayed_value_parses_back_to_the_same_raw_value(key, low, high, default, fmt):
    """读数框显示的文本原样提交时，滑块值不应漂移。"""
    # 色温以整数开尔文显示，再经对数映射取整，允许 1 个滑块单位的往返误差
    tolerance = 1 if fmt == "kelvin" else 0
    for raw in sorted({low, high, default, (low + high) // 2, low + 1, high - 1}):
        parsed = parse_slider_value(fmt, format_slider_value(fmt, raw), low, high)
        assert parsed is not None and abs(parsed - raw) <= tolerance, (key, raw, parsed)
