"""Panel registry: maps tool ids to their sidebar panel classes.

Keeping construction here lets ``MainWindow`` stay focused on workspace
coordination instead of tool parameter logic.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtWidgets import QWidget

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module

from .base_tool_panel import BaseToolPanel


class PanelTypes(Mapping):
    def __init__(self):
        self._factories = {}

    def register(self, tool_id, module, name, *, title_key=None):
        if tool_id in self._factories:
            raise ValueError("工具面板重复注册")
        self._factories[tool_id] = (module, name, title_key)

    def __getitem__(self, tool_id):
        module, name, _ = self._factories[tool_id]
        return getattr(import_module(module, __package__), name)

    def title_key(self, tool_id):
        entry = self._factories.get(tool_id)
        return entry[2] if entry is not None else None

    def __iter__(self):
        return iter(self._factories)

    def __len__(self):
        return len(self._factories)

    def __contains__(self, tool_id):
        return tool_id in self._factories


PANEL_TYPES = PanelTypes()
for _tool_id, _module, _name, _title_key in (
    ("color_adjust", ".color_adjust_panel", "ColorAdjustPanel", "menu.edit.color_lab"),
    ("beauty", ".managed_model_panels", "BeautyPanel", "menu.edit.beauty"),
    ("ai_enhance", ".neural_enhance_panel", "AIEnhancePanel", "menu.edit.ai_enhance"),
    ("bg_remove", ".managed_model_panels", "BGRemovePanel", "menu.edit.remove_bg"),
    ("denoise", ".denoise_panel", "DenoisePanel", "menu.edit.denoise"),
    ("id_photo", ".id_photo_panel", "IDPhotoPanel", "menu.edit.id_photo"),
    ("crop", ".crop_panel", "CropPanel", "menu.edit.crop"),
    ("grid_crop", ".grid_crop_panel", "GridCropPanel", "menu.edit.grid_crop"),
    ("shape_crop", ".shape_crop_panel", "ShapeCropPanel", "menu.edit.shape_crop"),
    ("lasso", ".lasso_panel", "LassoPanel", "menu.edit.lasso"),
):
    PANEL_TYPES.register(_tool_id, _module, _name, title_key=_title_key)


@dataclass(frozen=True)
class ToolEntry:
    """编辑工作区工具栏的一个入口：同类工具归在一起，入口内的工具用分段开关切换。"""

    entry_id: str
    icon: str
    title_key: str
    title_default: str
    tools: tuple[tuple[str, str, str], ...]  # (tool_id, 分段文案键, 默认文案)

    @property
    def tool_ids(self) -> tuple[str, ...]:
        return tuple(tool_id for tool_id, _, _ in self.tools)


TOOL_ENTRIES: tuple[ToolEntry, ...] = (
    ToolEntry("color", "adjustments-horizontal", "rail.color", "Color",
              (("color_adjust", "rail.color", "Color"),)),
    ToolEntry("crop", "crop", "rail.crop", "Crop",
              (("crop", "rail.crop.free", "Free"), ("grid_crop", "rail.crop.grid", "Grid"),
               ("shape_crop", "rail.crop.shape", "Shape"))),
    ToolEntry("cutout", "lasso", "rail.cutout", "Cutout",
              (("bg_remove", "rail.cutout.auto", "Remove BG"), ("lasso", "rail.cutout.lasso", "Lasso"))),
    ToolEntry("repair", "sparkles", "rail.repair", "Repair",
              (("ai_enhance", "rail.repair", "Repair"),)),
    ToolEntry("beauty", "mood-smile", "rail.beauty", "Beauty",
              (("beauty", "rail.beauty", "Beauty"),)),
    ToolEntry("id_photo", "id", "rail.id_photo", "ID Photo",
              (("id_photo", "rail.id_photo", "ID Photo"),)),
)


def tool_entry_for(tool_id: str) -> Optional[ToolEntry]:
    """工具所属的入口；不在工具栏里的工具没有入口。"""
    return next((entry for entry in TOOL_ENTRIES if tool_id in entry.tool_ids), None)


# Legacy/alternate tool ids used by the studio sidebar.
TOOL_ID_ALIASES: dict[str, str] = {
    "remove_bg": "bg_remove",
}


def resolve_tool_id(tool_id: str) -> str:
    return TOOL_ID_ALIASES.get(tool_id, tool_id)


def create_tool_panel(
    tool_id: str,
    image_path: str,
    *,
    parent: Optional[QWidget] = None,
    edit_state_store=None,
    services=None,
) -> Optional[BaseToolPanel]:
    try:
        panel_cls = PANEL_TYPES.get(resolve_tool_id(tool_id))
    except ImportError:
        logging.getLogger(__name__).exception("工具面板加载失败：%s", tool_id)
        return None
    if panel_cls is None:
        return None
    return panel_cls(image_path, parent=parent, edit_state_store=edit_state_store, **(services or {}))
