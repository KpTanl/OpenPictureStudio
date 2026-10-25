"""工具面板注册表：每个工具都能解析出面板类，标题有中英文翻译。"""

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ui.panels.base_tool_panel import BaseToolPanel  # noqa: E402
from src.ui.panels.registry import (  # noqa: E402
    PANEL_TYPES,
    PanelTypes,
    create_tool_panel,
    resolve_tool_id,
)

I18N = Path(__file__).resolve().parents[1] / "resources" / "i18n"

EXPECTED = {
    "color_adjust": "ColorAdjustPanel",
    "beauty": "BeautyPanel",
    "ai_enhance": "AIEnhancePanel",
    "bg_remove": "BGRemovePanel",
    "denoise": "DenoisePanel",
    "id_photo": "IDPhotoPanel",
    "crop": "CropPanel",
    "grid_crop": "GridCropPanel",
    "shape_crop": "ShapeCropPanel",
    "lasso": "LassoPanel",
}


def test_registry_lists_exactly_the_known_tools():
    assert set(PANEL_TYPES) == set(EXPECTED)
    assert len(PANEL_TYPES) == len(EXPECTED)


@pytest.mark.parametrize("tool_id, class_name", sorted(EXPECTED.items()))
def test_each_tool_resolves_to_its_panel_class(tool_id, class_name):
    panel_class = PANEL_TYPES[tool_id]
    assert panel_class.__name__ == class_name
    assert issubclass(panel_class, BaseToolPanel)


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_panel_titles_are_translated(language):
    translations = json.loads((I18N / f"{language}.json").read_text(encoding="utf-8"))
    for tool_id in PANEL_TYPES:
        title_key = PANEL_TYPES.title_key(tool_id)
        assert title_key and translations.get(title_key), (tool_id, title_key)


def test_legacy_tool_id_alias():
    assert resolve_tool_id("remove_bg") == "bg_remove"
    assert resolve_tool_id("crop") == "crop"
    assert resolve_tool_id("unknown") == "unknown"


def test_duplicate_registration_is_rejected():
    registry = PanelTypes()
    registry.register("crop", ".crop_panel", "CropPanel")
    with pytest.raises(ValueError):
        registry.register("crop", ".crop_panel", "CropPanel")


def test_unknown_tool_has_no_title_and_creates_no_panel():
    assert PANEL_TYPES.title_key("unknown") is None
    assert "unknown" not in PANEL_TYPES
    assert create_tool_panel("unknown", "image.png") is None
