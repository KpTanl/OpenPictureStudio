"""画布底色：设置的读写、样式表取值，以及切换主题后保持所选底色。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from resources.styles.semantic_colors import CANVAS_BACKGROUNDS  # noqa: E402
from resources.styles.theme_variables import BEIGE, DARK, LIGHT, get_theme_qss  # noqa: E402
from src.application.image_display_settings import ImageDisplaySettings  # noqa: E402
from src.core.contracts.image_display import CanvasBackground, RawDisplayMode  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402


class _Settings:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        pass


def _canvas_rule(qss):
    rule = qss[qss.index("QScrollArea#imageScrollArea,"):]
    return rule[:rule.index("}")]


def test_fixed_backgrounds_match_the_setting_choices():
    assert set(CANVAS_BACKGROUNDS) == {choice.value for choice in CanvasBackground} - {"theme"}


def test_settings_default_to_following_the_theme():
    assert ImageDisplaySettings.load(_Settings()).canvas_background is CanvasBackground.THEME


def test_settings_round_trip_keeps_both_fields():
    store = _Settings()
    ImageDisplaySettings(RawDisplayMode.CAMERA, CanvasBackground.MID).save(store)
    assert ImageDisplaySettings.load(store) == ImageDisplaySettings(RawDisplayMode.CAMERA, CanvasBackground.MID)


def test_an_invalid_value_only_resets_its_own_field():
    loaded = ImageDisplaySettings.load(_Settings({"raw_mode": "camera", "canvas_background": "pink"}))
    assert loaded == ImageDisplaySettings(RawDisplayMode.CAMERA, CanvasBackground.THEME)
    loaded = ImageDisplaySettings.load(_Settings({"raw_mode": "nope", "canvas_background": "beige"}))
    assert loaded == ImageDisplaySettings(canvas_background=CanvasBackground.BEIGE)


@pytest.mark.parametrize("theme, tokens", [("dark", DARK), ("light", LIGHT), ("beige", BEIGE)])
def test_stylesheet_uses_the_theme_canvas_unless_a_fixed_one_is_chosen(theme, tokens):
    assert f"background-color: {tokens['canvas']};" in _canvas_rule(get_theme_qss(theme))
    assert f"background-color: {tokens['canvas']};" in _canvas_rule(get_theme_qss(theme, canvas_background="theme"))
    for name, color in CANVAS_BACKGROUNDS.items():
        qss = get_theme_qss(theme, canvas_background=name)
        assert f"background-color: {color};" in _canvas_rule(qss)
        assert f"background-color: {tokens['surface']};" in qss


def test_chosen_background_survives_theme_changes():
    app = QApplication.instance() or QApplication([])
    previous_sheet, previous_theme = app.styleSheet(), app.property("ops.theme")
    try:
        manager = ThemeManager()
        manager.set_canvas_background("mid")
        manager.apply("dark")
        assert f"background-color: {CANVAS_BACKGROUNDS['mid']};" in _canvas_rule(app.styleSheet())
        manager.apply("light")
        assert f"background-color: {CANVAS_BACKGROUNDS['mid']};" in _canvas_rule(app.styleSheet())
        manager.set_canvas_background("theme")
        assert f"background-color: {LIGHT['canvas']};" in _canvas_rule(app.styleSheet())
    finally:
        app.setStyleSheet(previous_sheet)
        app.setProperty("ops.theme", previous_theme)
