"""Windows 标题栏外观：把系统标题栏画成与界面同色的实底。

只在 Windows 上起作用。Windows 10 没有标题栏颜色属性，这时只按主题切换深浅，标题栏保持系统默认的样子。
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

# DwmSetWindowAttribute 的属性编号
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_NONE = 1


def apply_title_bar_colors(window, *, dark_mode: bool, caption: tuple[int, int, int],
                           text: tuple[int, int, int]) -> bool:
    """把标题栏画成给定的实底颜色与文字颜色，不用系统的背景材质。

    caption 与 text 是 (红, 绿, 蓝)；dark_mode 决定窗口按钮用深色还是浅色的一套。
    系统不支持时返回 False。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import byref, c_int, c_uint, sizeof

        hwnd = int(window.winId())
        if not hwnd:
            return False
        dwmapi = ctypes.windll.dwmapi

        def set_attribute(attribute, value):
            return dwmapi.DwmSetWindowAttribute(hwnd, attribute, byref(value), sizeof(value)) == 0

        set_attribute(_DWMWA_USE_IMMERSIVE_DARK_MODE, c_int(1 if dark_mode else 0))
        set_attribute(_DWMWA_SYSTEMBACKDROP_TYPE, c_int(_DWMSBT_NONE))
        applied = True
        for attribute, (red, green, blue) in ((_DWMWA_CAPTION_COLOR, caption), (_DWMWA_TEXT_COLOR, text)):
            applied = set_attribute(attribute, c_uint(red | green << 8 | blue << 16)) and applied
        return applied
    except Exception as exc:
        logger.debug("标题栏颜色未能应用: %s", exc)
        return False
