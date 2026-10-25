"""按用途命名的界面颜色：三套主题各给一组语义取值，另有与主题无关、表示数据含义的固定颜色。"""

from __future__ import annotations

import re

BRAND_GRADIENT = ("#2f6bff", "#8a45ff", "#e03fb6")

# 表示数据含义的颜色，与主题无关：带颜色的滑轨与色彩混合的八个通道
TRACK_GRADIENTS = {
    "temperature": ("#3f7dff", "#dcdcdc", "#ffab2e"),
    "saturation": ("#8c8c8c", "#ff5a5a", "#ffd23f", "#4ad37a", "#4aa8ff", "#b06bff"),
}
MIXER_CHANNEL_COLORS = {
    "red": "#e74c3c",
    "orange": "#e67e22",
    "yellow": "#f1c40f",
    "green": "#2ecc71",
    "cyan": "#1abc9c",
    "blue": "#3498db",
    "purple": "#9b59b6",
    "magenta": "#e84393",
}
# 红、绿、蓝三个通道在直方图与曲线里的颜色
CHANNEL_COLORS = {"r": "#e5484d", "g": "#3dbb61", "b": "#4a8cf7"}


def mixer_track_colors(channel: str) -> dict[str, tuple[str, ...]]:
    """某个混色通道下色相、饱和度、明度三条滑轨的色标。"""
    names = list(MIXER_CHANNEL_COLORS)
    index = names.index(channel)
    color = MIXER_CHANNEL_COLORS[channel]
    return {
        "hue": (MIXER_CHANNEL_COLORS[names[index - 1]], color, MIXER_CHANNEL_COLORS[names[(index + 1) % len(names)]]),
        "sat": ("#8c8c8c", color),
        "lum": ("#1a1a1a", color, "#f2f2f2"),
    }


# 与主题无关的固定画布底色；未列出的名称（如 "theme"）表示跟随主题的 canvas 取值
CANVAS_BACKGROUNDS = {"dark": "#1a1a1a", "mid": "#767676", "light": "#d8d8d8", "beige": "#e9e1d0"}


_TRANSLUCENT = re.compile(r"rgba\((\d+), (\d+), (\d+), ([01]\.\d\d)\)")


def _channels(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def color_channels(value: str) -> tuple[int, int, int, float]:
    """把取值（#rrggbb 或 translucent() 的输出）拆成红、绿、蓝（0–255）与不透明度（0–1）。"""
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return (*_channels(value), 1.0)
    match = _TRANSLUCENT.fullmatch(value)
    if match is None:
        raise ValueError(f"无法解析的颜色取值: {value!r}")
    red, green, blue, alpha = match.groups()
    return int(red), int(green), int(blue), float(alpha)


def mix(base: str, other: str, amount: float) -> str:
    """两个 #rrggbb 颜色按比例混合，amount 为 other 的占比。"""
    return "#" + "".join(f"{round(a + (b - a) * amount):02x}"
                         for a, b in zip(_channels(base), _channels(other)))


def brand_gradient_states(surface: str) -> dict[str, tuple[str, str, str]]:
    """主操作按钮各状态的渐变色标；禁用态向界面底色淡出。"""
    return {
        "rest": BRAND_GRADIENT,
        "hover": tuple(mix(color, "#ffffff", 0.12) for color in BRAND_GRADIENT),
        "pressed": tuple(mix(color, "#000000", 0.14) for color in BRAND_GRADIENT),
        "disabled": tuple(mix(color, surface, 0.6) for color in BRAND_GRADIENT),
    }


def translucent(color: str, alpha: float) -> str:
    red, green, blue = _channels(color)
    return f"rgba({red}, {green}, {blue}, {alpha:.2f})"


def _semantic(*, canvas, surface, page, control, well, segment_selected, hairline, text_primary,
              text_secondary, text_caption, track, accent, accent_soft, on_accent, success,
              warning, warning_surface, danger) -> dict[str, str]:
    """hairline 与 accent_soft 以 (颜色, 不透明度) 给出；其余为 #rrggbb。"""
    accent_disabled = mix(accent, surface, 0.6)
    return {
        "canvas": canvas,
        "surface": surface,
        "page": page,
        "control": control,
        "control_disabled": mix(surface, control, 0.25),
        "well": well,
        "segment_selected": segment_selected,
        "hairline": translucent(*hairline),
        "hairline_solid": mix(surface, *hairline),
        "text_primary": text_primary,
        "text_secondary": text_secondary,
        "text_caption": text_caption,
        "text_disabled": mix(text_caption, surface, 0.5),
        "track": track,
        "track_hover": mix(track, text_caption, 0.5),
        "accent": accent,
        "accent_hover": mix(accent, text_primary, 0.14),
        "accent_pressed": mix(accent, "#000000", 0.16),
        "accent_disabled": accent_disabled,
        "accent_soft": translucent(accent, accent_soft),
        "on_accent": on_accent,
        "on_accent_disabled": mix(on_accent, accent_disabled, 0.5),
        "success": success,
        "warning": warning,
        "warning_surface": warning_surface,
        "danger": danger,
        "danger_hover": mix(danger, "#000000", 0.12),
        "danger_disabled": mix(danger, surface, 0.6),
        "on_danger": "#ffffff",
        "on_brand": "#ffffff",
        "on_filled_disabled": mix("#ffffff", surface, 0.4),
        "knob": "#ffffff",
        "knob_pressed": "#f0f0f0",
        "knob_border": translucent("#000000", 0.18),
        "rating": "#f0ad2e",
        "shadow": translucent("#000000", 0.20),
    }


# 画在照片上的覆盖层（裁切框、网格线、套索轮廓）的颜色，与主题无关：照片的明暗不随界面主题变
OVERLAY_COLORS = {
    "mask": "rgba(238, 66, 86, 0.35)",
    "scrim": translucent("#000000", 0.55),
    "frame": translucent("#ffffff", 0.90),
    "guide": translucent("#ffffff", 0.27),
    "handle": "#ffffff",
    "grid_line": translucent("#ff9500", 0.86),
    "grid_frame": translucent("#ff9500", 0.71),
    "selection_fill": translucent("#007aff", 0.24),
    "selection_stroke": translucent("#007aff", 0.78),
    "draft_stroke": translucent("#ff9500", 0.90),
    "draft_hint": translucent("#ff9500", 0.47),
    # 证件照的人像参考线（头顶、下巴与头部范围）与它们的文字
    "portrait_guide": translucent("#ffc457", 0.86),
    "portrait_label": translucent("#ffdc96", 0.94),
}

SEMANTIC_DARK = _semantic(
    canvas="#1a1a1a", surface="#252525", page="#1e1e1e", control="#383838", well="#1a1a1a",
    segment_selected="#4a4a4a", hairline=("#ffffff", 0.10), text_primary="#f4f4f4",
    text_secondary="#c4c4c4", text_caption="#a0a0a0", track="#404040", accent="#8098ff",
    accent_soft=0.20, on_accent="#0f1430", success="#56c784", warning="#ebb24a",
    warning_surface="#3a2f18", danger="#e5534b")

SEMANTIC_LIGHT = _semantic(
    canvas="#e2e2e2", surface="#f8f8f8", page="#ffffff", control="#e7e7e7", well="#e2e2e2",
    segment_selected="#ffffff", hairline=("#000000", 0.11), text_primary="#1a1a1c",
    text_secondary="#474747", text_caption="#666666", track="#d2d2d2", accent="#3553e8",
    accent_soft=0.12, on_accent="#ffffff", success="#1e8a4c", warning="#845300",
    warning_surface="#f6ead0", danger="#c93a32")

SEMANTIC_BEIGE = _semantic(
    canvas="#e9e1d0", surface="#f6f1e7", page="#fcfaf4", control="#e8dfcf", well="#ebe4d6",
    segment_selected="#fffdf8", hairline=("#503c1e", 0.17), text_primary="#2a251f",
    text_secondary="#52493f", text_caption="#6c6155", track="#d8cdb9", accent="#4a4fbe",
    accent_soft=0.12, on_accent="#ffffff", success="#1f7a45", warning="#7d4f00",
    warning_surface="#f1e2c0", danger="#b93a30")

SEMANTIC_KEYS = frozenset(SEMANTIC_DARK)
