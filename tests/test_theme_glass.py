"""Unit tests for iOS 26 Liquid Glass styling, theme tokens, and DWM backdrop integration."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch
import pytest

from resources.styles.theme_variables import (
    DARK,
    LIGHT,
    BEIGE,
    REQUIRED_TOKENS,
    ACCENT_KEYS,
    PALETTE_KEYS,
    _build_qss,
    build_theme,
    get_theme_qss,
    get_dark_qss,
    get_light_qss,
    get_theme_tokens_by_name,
)
from src.ui.dwm_helper import apply_title_bar_colors


class TestThemeGlassTokens:
    """测试主题 Token 契约与 QSS 生成。"""

    def test_required_tokens_consistency(self):
        """验证所有内置主题包含全部必需 Token。"""
        assert frozenset(DARK.keys()) == REQUIRED_TOKENS
        assert frozenset(LIGHT.keys()) == REQUIRED_TOKENS
        assert frozenset(BEIGE.keys()) == REQUIRED_TOKENS

    def test_palette_and_accent_split(self):
        """验证 Palette 与 Accent 键拆分及重组完整性。"""
        assert ACCENT_KEYS <= REQUIRED_TOKENS
        assert PALETTE_KEYS == REQUIRED_TOKENS - ACCENT_KEYS
        assert build_theme("dark", "blue") == DARK
        assert build_theme("light", "blue") == LIGHT

    def test_build_qss_contains_glass_rules(self):
        """验证 QSS 生成器包含玻璃拟态选择器与关键属性。"""
        qss_dark = _build_qss(DARK)
        assert isinstance(qss_dark, str)
        assert len(qss_dark) > 1000
        # 验证包含玻璃圆角与关键控件选择器
        assert "QSlider::groove:horizontal" in qss_dark
        assert "QSlider::handle:horizontal" in qss_dark
        assert "QSplitter::handle" in qss_dark
        assert "QPushButton#primaryBtn" in qss_dark
        assert "QLineEdit:focus" in qss_dark
        assert "QMenu" in qss_dark

        qss_light = _build_qss(LIGHT)
        assert "QSlider::groove:horizontal" in qss_light
        assert "QPushButton#primary" in qss_light

    def test_get_theme_qss_helpers(self):
        """验证便捷主题 QSS 获取函数。"""
        assert 't["surface"]' not in get_theme_qss("dark")  # tokens interpolated
        assert len(get_dark_qss()) > 0
        assert len(get_light_qss()) > 0
        extra_qss = "QLabel#customLabel { color: red; }"
        with_extra = get_theme_qss("dark", extra=extra_qss)
        assert extra_qss in with_extra


class TestTitleBarColors:
    """系统标题栏画成与界面同色；不支持时安全返回 False。"""

    COLORS = dict(dark_mode=False, caption=(0xF5, 0xF0, 0xE6), text=(0x52, 0x49, 0x3F))

    def test_non_windows_graceful_fallback(self):
        with patch.object(sys, "platform", "linux"):
            assert apply_title_bar_colors(MagicMock(), **self.COLORS) is False

    def test_invalid_window_handle_safe_handling(self):
        mock_win = MagicMock()
        mock_win.winId.side_effect = RuntimeError("Widget not created")
        assert apply_title_bar_colors(mock_win, **self.COLORS) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上设置窗口属性")
    def test_sets_caption_and_text_colors(self):
        mock_win = MagicMock()
        mock_win.winId.return_value = 123456
        calls = {}

        def record(hwnd, attribute, value, size):
            calls[attribute] = value._obj.value
            return 0

        with patch("ctypes.windll.dwmapi.DwmSetWindowAttribute", side_effect=record):
            assert apply_title_bar_colors(mock_win, **self.COLORS) is True
        # 颜色按 0x00BBGGRR 传入；不用系统的背景材质
        assert calls[35] == 0xE6F0F5 and calls[36] == 0x3F4952
        assert calls[20] == 0 and calls[38] == 1

    @pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上设置窗口属性")
    def test_reports_failure_when_colors_are_unsupported(self):
        mock_win = MagicMock()
        mock_win.winId.return_value = 123456
        with patch("ctypes.windll.dwmapi.DwmSetWindowAttribute",
                   side_effect=lambda hwnd, attribute, value, size: 0 if attribute in (20, 38) else -1):
            assert apply_title_bar_colors(mock_win, **self.COLORS) is False
