"""Comprehensive automated tests for Apple choice widgets, buttons, and migrated dialogs."""

from __future__ import annotations

import os
import pytest
from PIL import Image

from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication, QGroupBox, QWidget, QHBoxLayout

from src.ui.widgets.apple_choice import AppleCheckBox, AppleRadioButton
from src.ui.widgets.apple_button import (
    AppleBrandButton,
    AppleButton,
    AppleCompactButton,
    AppleDestructiveButton,
    AppleIconButton,
)
from src.ui.widgets.apple_menu import AppleDropDown
from src.ui.widgets.apple_slider import AppleSlider
from src.ui.widgets.segmented_control import AppleSegmentedControl
from src.ui.theme import get_theme_manager

from src.ui.dialogs.enhancement_dialog import EnhancementDialog
from src.ui.dialogs.compress_dialog import ImageProcessingDialog
from src.ui.dialogs.pdf_dialog import PDFDialog
from src.ui.dialogs.wallpaper_dialog import WallpaperDialog
from src.ui.dialogs.file_association_page import FileAssociationPage

from src.ui.panels.lasso_panel import LassoPanel
from src.ui.panels.grid_crop_panel import GridCropPanel


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def sample_image_path(tmp_path):
    img_path = str(tmp_path / "test_sample.png")
    img = Image.new("RGB", (200, 200), color=(120, 150, 180))
    img.save(img_path)
    return img_path


# ==============================================================================
# 1. Apple Choice Widgets Tests (AppleCheckBox & AppleRadioButton)
# ==============================================================================

def test_apple_checkbox_state_and_interaction(qapp):
    """验证 AppleCheckBox 的状态切换、键盘空格触发及信号。"""
    cb = AppleCheckBox("Test CheckBox")
    try:
        assert cb.objectName() == "appleCheckBox"
        assert not cb.isChecked()

        toggled_states = []
        cb.toggled.connect(lambda state: toggled_states.append(state))

        # 点击切换
        cb.click()
        assert cb.isChecked()
        assert toggled_states == [True]

        # 键盘 Space 键切换
        key_event = QKeyEvent(
            QKeyEvent.Type.KeyPress,
            Qt.Key.Key_Space,
            Qt.KeyboardModifier.NoModifier,
        )
        cb.keyPressEvent(key_event)
        assert not cb.isChecked()
        assert toggled_states == [True, False]

        # 半选中态测试
        cb.setTristate(True)
        cb.setCheckState(Qt.CheckState.PartiallyChecked)
        assert cb.checkState() == Qt.CheckState.PartiallyChecked
    finally:
        cb.close()


def test_apple_radio_button_state_and_interaction(qapp):
    """验证 AppleRadioButton 的单选状态切换与对象名。"""
    container = QWidget()
    layout = QHBoxLayout(container)
    rb1 = AppleRadioButton("Option 1", container)
    rb2 = AppleRadioButton("Option 2", container)
    layout.addWidget(rb1)
    layout.addWidget(rb2)
    try:
        assert rb1.objectName() == "appleRadioButton"
        assert rb2.objectName() == "appleRadioButton"

        rb1.setChecked(True)
        assert rb1.isChecked()
        assert not rb2.isChecked()

        rb2.click()
        assert not rb1.isChecked()
        assert rb2.isChecked()
    finally:
        container.close()


def test_apple_choice_widgets_paint_and_theme(qapp):
    """验证 AppleCheckBox 与 AppleRadioButton 的自绘事件与主题切换监听。"""
    cb = AppleCheckBox("CheckBox")
    rb = AppleRadioButton("RadioButton")
    cb.resize(150, 26)
    rb.resize(150, 26)
    cb.show()
    rb.show()
    qapp.processEvents()

    try:
        tm = get_theme_manager()
        for theme in ["dark", "light", "beige"]:
            if tm:
                tm.apply(theme)
            qapp.processEvents()

            # 触发自绘
            cb.repaint()
            rb.repaint()
    finally:
        cb.close()
        rb.close()


# ==============================================================================
# 2. Apple Destructive & Icon Button Tests
# ==============================================================================

def test_apple_destructive_button(qapp):
    """验证 AppleDestructiveButton 的对象名；外观由全局样式表决定。"""
    btn = AppleDestructiveButton("Delete")
    try:
        assert btn.objectName() == "appleDestructiveButton"
        assert not btn.styleSheet()
    finally:
        btn.close()


def test_apple_icon_button(qapp):
    """验证 AppleIconButton 的固定大小与对象名。"""
    btn = AppleIconButton(size=28)
    try:
        assert btn.objectName() == "appleIconButton"
        assert btn.width() == 28
        assert btn.height() == 28
        assert not btn.styleSheet()
    finally:
        btn.close()


# ==============================================================================
# 3. Migrated BaseDialogs Component Standards Tests
# ==============================================================================

def test_enhancement_dialog_components(qapp, sample_image_path):
    """批量画质增强窗口用共享的画质增强面板，右下角是取消与导出。"""
    dlg = EnhancementDialog([sample_image_path])
    try:
        from src.ui.panels.neural_enhance_panel import AIEnhancePanel
        assert isinstance(dlg.panel, AIEnhancePanel)
        assert dlg.paths == (sample_image_path,)
        assert set(dlg.panel._rows) == {"luminance", "chroma", "deblur"}
        assert isinstance(dlg.panel._footer.primary_button, AppleButton)
        assert isinstance(dlg.save, AppleBrandButton) and type(dlg.cancel) is AppleButton
        assert dlg.panel.parameters["target"] == "original"
    finally:
        dlg.close()


def test_compress_dialog_components(qapp, sample_image_path):
    """图片格式处理窗口：行内操作是紧凑按钮，质量是参数行，右下角是取消与唯一的主操作。"""
    dlg = ImageProcessingDialog(sample_image_path)
    try:
        assert isinstance(dlg.btn_browse, AppleCompactButton)
        assert isinstance(dlg.btn_add, AppleCompactButton)
        # 清空列表不是不可撤回的操作，不用危险按钮
        assert isinstance(dlg.btn_clear, AppleCompactButton)
        assert isinstance(dlg.quality_slider, AppleSlider) and dlg.quality_row.slider is dlg.quality_slider
        assert isinstance(dlg.btn_action, AppleBrandButton) and type(dlg.btn_cancel) is AppleButton
        assert not dlg.findChildren(QGroupBox) and not dlg.styleSheet()
    finally:
        dlg.close()


def test_pdf_dialog_components(qapp, sample_image_path):
    """导出 PDF 窗口右下角是取消与唯一的主操作。"""
    dlg = PDFDialog([sample_image_path])
    try:
        assert type(dlg.cancel_btn) is AppleButton
        assert isinstance(dlg.export_btn, AppleBrandButton) and dlg.export_btn.isDefault()
    finally:
        dlg.close()


def test_wallpaper_dialog_components(qapp, sample_image_path):
    """验证 WallpaperDialog 集成 AppleButton 与 ApplePrimaryButton。"""
    dlg = WallpaperDialog(sample_image_path)
    try:
        assert isinstance(dlg._cancel_btn, AppleButton)
        assert isinstance(dlg._ok_btn, AppleBrandButton)
    finally:
        dlg.close()


def test_file_association_page_components(qapp):
    """验证设置里的文件关联页集成 AppleSegmentedControl 与 AppleCompactButton，没有实底按钮。"""
    dlg = FileAssociationPage()
    try:
        assert isinstance(dlg.filter_segment, AppleSegmentedControl)
        assert dlg.filter_segment.count() >= 3
        assert isinstance(dlg.btn_enable_all, AppleCompactButton)
        assert isinstance(dlg.btn_disable_all, AppleCompactButton)
        assert type(dlg.btn_system) is AppleButton
    finally:
        dlg.close()


# ==============================================================================
# 4. Panels Components Tests
# ==============================================================================

def test_lasso_panel_components(qapp, sample_image_path):
    """验证 LassoPanel 用分段开关选保留哪一侧、用参数行调羽化。"""
    panel = LassoPanel(sample_image_path)
    try:
        assert isinstance(panel._keep_segments, AppleSegmentedControl)
        assert isinstance(panel._feather_row.slider, AppleSlider)
    finally:
        panel.cleanup()
        panel.close()


def test_grid_crop_panel_components(qapp, sample_image_path):
    """验证 GridCropPanel 集成 AppleDropDown。"""
    panel = GridCropPanel(sample_image_path)
    try:
        assert isinstance(panel._fmt_combo, AppleDropDown)
    finally:
        panel.cleanup()
        panel.close()


# ==============================================================================
# 5. Multi-Theme Switching Dynamic Stress Test
# ==============================================================================

def test_multi_theme_dynamic_switching(qapp, sample_image_path):
    """验证对话框在频繁切换主题（dark/light/beige）时正常响应。"""
    compress_dlg = ImageProcessingDialog(sample_image_path)

    try:
        tm = get_theme_manager()
        for theme_name in ["dark", "light", "beige", "dark"]:
            if tm:
                tm.apply(theme_name)
            compress_dlg.apply_theme(theme_name)
            qapp.processEvents()
    finally:
        compress_dlg.close()
