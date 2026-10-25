from __future__ import annotations

from typing import Any, Dict

from .color import is_color_default_ui


def is_color_default(params: Dict[str, Any]) -> bool:
    """检查调色参数是否全为默认值。"""
    return is_color_default_ui(params)


def is_denoise_default(params: Dict[str, Any]) -> bool:
    """检查降噪参数是否为中性状态。"""
    if not params:
        return True
    return all(params.get(key, 0) == 0 for key in ("strength", "color_strength", "sharpen"))


_BEAUTY_DEFAULTS = {
    "skin_smooth": 50,
    "skin_brighten": 30,
    "skin_rosy": 20,
    "sharpen": 15,
    "eye_enlarge": 0,
    "eye_distance": 0,
    "face_slim": 0,
    "nose_slim": 0,
    "teeth_whiten": 0,
    "spot_remove": 0,
    "eye_bag_remove": 0,
}


def is_beauty_default(params: Dict[str, Any]) -> bool:
    """检查美颜参数是否为默认或未启用。"""
    if not params:
        return True
    if all(params.get(k, 0) == 0 for k in _BEAUTY_DEFAULTS):
        return True
    return False


def is_crop_default(params: Dict[str, Any]) -> bool:
    """检查裁切参数是否为未裁切、未旋转。"""
    if not params:
        return True
    crop_rect = params.get("crop_rect")
    rotation = params.get("rotation_angle", 0.0)
    return crop_rect is None and abs(rotation) < 1e-4


