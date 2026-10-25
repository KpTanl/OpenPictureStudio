"""Unit tests for modern borderless floating toolbar, tool buttons, and zoom pill styling."""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QToolBar, QToolButton

from resources.styles.theme_variables import (
    DARK,
    LIGHT,
    BEIGE,
    _build_qss,
    get_theme_qss,
)
from src.ui.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class TestModernBorderlessToolbar:
    """测试现代无边框工具栏及按钮样式规范与结构。"""

    @pytest.mark.parametrize("theme_dict", [DARK, LIGHT, BEIGE])
    def test_borderless_toolbutton_qss_rules(self, theme_dict):
        """验证所有主题下 QToolButton 均为无边框 (ghost button) 规范。"""
        qss = _build_qss(theme_dict)

        assert "QToolButton {" in qss
        assert "background-color: transparent;" in qss
        assert "border: 1px solid transparent;" in qss
        assert "QToolButton:hover {" in qss
        assert "QToolButton:pressed {" in qss
        assert "QToolButton:checked {" in qss

    def test_settings_dialog_navigation_structure(self, qapp):
        """设置窗口左侧是导航，右侧先是常规、图片显示、快捷键、性能与缓存四个表单页。"""
        from src.ui.dialogs.settings_dialog import SettingsDialog
        dialog = SettingsDialog()
        try:
            assert [key for key, _, _ in dialog.PAGES][:4] == ["general", "images", "shortcuts", "performance"]
            assert dialog._stack.count() == len(dialog.PAGES)
            assert dialog.language_row.title() and dialog.theme_row.title()
            assert dialog._pages["shortcuts"].widget().objectName() == "settingsPage"
        finally:
            dialog.close()

    @pytest.mark.parametrize("theme_name", ["dark", "light", "beige"])
    def test_unified_button_system_qss(self, theme_name):
        """验证所有主题下均包含标准按钮、主操作按钮、紧凑按钮与幽灵按钮规范。"""
        qss = get_theme_qss(theme_name)

        # 基础按钮（规则可与其他选择器并列）
        import re
        for state in ("", ":hover", ":pressed", ":disabled"):
            assert re.search(rf"^QPushButton{state}[,\s]", qss, re.MULTILINE), state

        # 主要按钮
        assert "QPushButton#primary" in qss or "QPushButton#primaryBtn" in qss

        # 紧凑工具按钮
        assert "QPushButton#compactBtn" in qss
        assert "QPushButton#fitThumbBtn" in qss
        assert "QPushButton#fitBtn" in qss
        assert "QPushButton#resetBtn" in qss
        assert "QPushButton#appleCompactButton" in qss

        # 幽灵按钮
        assert "QPushButton#ghostBtn" in qss

    @pytest.mark.parametrize("theme_dict", [DARK, LIGHT, BEIGE])
    def test_compact_and_reset_button_borderless_pill(self, theme_dict):
        """验证所有主题下 compact/reset/appleCompactButton 均为无边框胶囊规范。"""
        qss = _build_qss(theme_dict)

        assert "QPushButton#appleCompactButton" in qss
        assert "QPushButton#resetBtn" in qss
        assert "QPushButton#compactBtn" in qss
        assert "QPushButton#appleCompactButton:hover" in qss
        assert "QPushButton#appleCompactButton:pressed" in qss
        assert "QPushButton#appleCompactButton:disabled" in qss
        assert theme_dict["control"] in qss
