"""语义颜色：三套主题的键一致、文字对比度，以及主题取值只有语义键。"""

import re

import pytest

from resources.styles.semantic_colors import (SEMANTIC_BEIGE, SEMANTIC_DARK, SEMANTIC_KEYS, SEMANTIC_LIGHT,
                                              mix, translucent)
from resources.styles.theme_variables import BEIGE, DARK, LIGHT, get_theme_qss

SEMANTIC = {"dark": SEMANTIC_DARK, "light": SEMANTIC_LIGHT, "beige": SEMANTIC_BEIGE}
THEMES = {"dark": DARK, "light": LIGHT, "beige": BEIGE}
HEX = re.compile(r"#[0-9a-f]{6}")
COLOR = re.compile(r"#[0-9a-f]{6}|rgba\(\d+, \d+, \d+, [01]\.\d\d\)")


def _luminance(color):
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    red, green, blue = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first, second):
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_mix_and_translucent():
    assert mix("#000000", "#ffffff", 0.0) == "#000000"
    assert mix("#000000", "#ffffff", 1.0) == "#ffffff"
    assert mix("#102030", "#304050", 0.5) == "#203040"
    assert translucent("#8098ff", 0.2) == "rgba(128, 152, 255, 0.20)"


def test_themes_share_one_semantic_key_set():
    assert all(frozenset(tokens) == SEMANTIC_KEYS for tokens in SEMANTIC.values())


@pytest.mark.parametrize("theme", SEMANTIC)
@pytest.mark.parametrize("text", ["text_primary", "text_secondary", "text_caption"])
@pytest.mark.parametrize("background", ["surface", "page"])
def test_text_meets_the_reading_contrast(theme, text, background):
    tokens = SEMANTIC[theme]
    assert _contrast(tokens[text], tokens[background]) >= 4.5


@pytest.mark.parametrize("theme", SEMANTIC)
def test_accent_is_readable_as_text_and_as_a_button(theme):
    tokens = SEMANTIC[theme]
    assert _contrast(tokens["accent"], tokens["surface"]) >= 4.5
    assert all(_contrast(tokens["on_accent"], tokens[key]) >= 4.5
               for key in ("accent", "accent_hover", "accent_pressed"))


@pytest.mark.parametrize("theme", THEMES)
def test_theme_is_exactly_the_semantic_set(theme):
    assert THEMES[theme] == SEMANTIC[theme] and set(THEMES[theme]) == SEMANTIC_KEYS
    # 按控件命名的旧键已经退出
    assert not [key for key in THEMES[theme] if key.startswith(("bg_", "border_", "glass_", "status_"))]


@pytest.mark.parametrize("theme", THEMES)
def test_every_value_is_a_color_qt_can_parse(theme):
    tokens = THEMES[theme]
    assert all(COLOR.fullmatch(value) for value in tokens.values())
    assert all(HEX.fullmatch(tokens[key]) for key in tokens if key.startswith("text_"))


@pytest.mark.parametrize("theme", THEMES)
def test_canvas_has_its_own_color_in_the_stylesheet(theme):
    qss = get_theme_qss(theme)
    rule = qss[qss.index("QScrollArea#imageScrollArea,"):]
    assert "QScrollArea#studioScrollArea" in rule[:rule.index("{")]
    assert f"background-color: {THEMES[theme]['canvas']};" in rule[:rule.index("}")]
