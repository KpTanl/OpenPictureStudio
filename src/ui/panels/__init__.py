"""按需加载侧栏面板。"""

from importlib import import_module

_PANELS = {
    "BaseToolPanel": "base_tool_panel", "ColorAdjustPanel": "color_adjust_panel",
    "BeautyPanel": "managed_model_panels", "AIEnhancePanel": "neural_enhance_panel",
    "BGRemovePanel": "managed_model_panels", "DenoisePanel": "denoise_panel",
    "IDPhotoPanel": "id_photo_panel", "CropPanel": "crop_panel",
    "GridCropPanel": "grid_crop_panel", "ShapeCropPanel": "shape_crop_panel",
    "LassoPanel": "lasso_panel",
}
__all__ = list(_PANELS)


def __getattr__(name):
    if name not in _PANELS:
        raise AttributeError(name)
    return getattr(import_module(f".{_PANELS[name]}", __name__), name)
