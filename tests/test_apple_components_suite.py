"""Apple HIG 规范化解耦组件体系全量综合测试套件。"""

import os
import pytest
from PIL import Image
from PySide6.QtCore import Qt, QPoint, QPointF
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QStackedWidget

from src.ui.widgets import (
    AppleBrandButton,
    AppleButton,
    ApplePrimaryButton,
    AppleCompactButton,
    AppleDestructiveButton,
    AppleGhostButton,
    AppleSegmentedControl,
    AppleSlider,
    AppleDropDown,
    AppleMenu,
)
from src.ui.dialogs.settings_dialog import SettingsDialog
from src.ui.dialogs.compress_dialog import ImageProcessingDialog
from src.ui.theme import ThemeManager, set_theme_manager


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_apple_button_hierarchy(qapp):
    """验证 AppleButton 家族层级、对象名与光标。"""
    btn_std = AppleButton("Standard")
    btn_pri = ApplePrimaryButton("Primary")
    btn_cpt = AppleCompactButton("Compact")
    btn_gst = AppleGhostButton("Ghost")

    try:
        assert btn_std.objectName() == "appleButton"
        assert btn_pri.objectName() == "applePrimaryButton"
        assert btn_cpt.objectName() == "appleCompactButton"
        assert btn_gst.objectName() == "appleGhostButton"

        from resources.styles.theme_variables import CONTROL_HEIGHT, CONTROL_HEIGHT_COMPACT
        assert btn_cpt.height() == CONTROL_HEIGHT_COMPACT
        assert btn_std.minimumHeight() == CONTROL_HEIGHT

        # 外观只由全局样式表决定，实例不带内联样式
        assert not any(button.styleSheet() for button in (btn_std, btn_pri, btn_cpt, btn_gst))
    finally:
        btn_std.close()
        btn_pri.close()
        btn_cpt.close()
        btn_gst.close()


def test_apple_segmented_control_interaction(qapp):
    """验证 AppleSegmentedControl 的项添加、切换、键盘事件与 QStackedWidget 联动。"""
    seg = AppleSegmentedControl()
    stack = QStackedWidget()

    try:
        assert seg.count() == 0
        seg.add_segment("First", data=101)
        seg.add_segment("Second", data=102)
        seg.add_segment("Third", data=103)

        assert seg.count() == 3
        assert seg.currentIndex() == 0
        assert seg.currentText() == "First"
        assert seg.currentData() == 101

        changes = []
        seg.currentIndexChanged.connect(lambda i: changes.append(i))

        # 切换到第二个
        seg.setCurrentIndex(1)
        assert seg.currentIndex() == 1
        assert seg.currentText() == "Second"
        assert seg.currentData() == 102
        assert changes == [1]

        # 键盘导航：按下 Right
        key_event = QKeyEvent(
            QKeyEvent.Type.KeyPress,
            Qt.Key.Key_Right,
            Qt.KeyboardModifier.NoModifier,
        )
        seg.keyPressEvent(key_event)
        assert seg.currentIndex() == 2
        assert changes == [1, 2]

        # 联动 QStackedWidget
        seg.bind_stacked_widget(stack)
        seg.setCurrentIndex(0)
        assert seg.currentIndex() == 0
    finally:
        seg.close()
        stack.close()


def test_apple_slider_double_click_reset(qapp):
    """验证 AppleSlider 的属性与双击恢复默认值功能。"""
    slider = AppleSlider(Qt.Orientation.Horizontal, default_value=50)
    try:
        slider.setRange(0, 100)
        slider.setValue(88)
        assert slider.value() == 88

        reset_signals = []
        slider.resetRequested.connect(lambda: reset_signals.append(True))

        # 触发重置
        slider.setValue(slider.default_value)
        assert slider.value() == 50
        assert not slider.styleSheet()
    finally:
        slider.close()


def test_settings_dialog_components_upgrade(qapp):
    """设置窗口的选择用下拉组件；没有“保存 / 取消”，每一项改了就生效。"""
    dlg = SettingsDialog()
    try:
        assert isinstance(dlg.language_combo, AppleDropDown)
        assert isinstance(dlg.theme_combo, AppleDropDown)
        assert isinstance(dlg.reset_shortcuts, AppleButton)
        assert not hasattr(dlg, "btn_save") and not hasattr(dlg, "btn_cancel")
    finally:
        dlg.close()


def test_image_processing_dialog_components(qapp):
    """图片格式处理按指定的初始格式与质量打开，用下拉、滑块与按钮组件。"""
    dlg = ImageProcessingDialog(initial_paths=["sample.jpg"], initial_format="png", initial_quality=95)
    try:
        assert isinstance(dlg.format_combo, AppleDropDown) and dlg.format_combo.currentData() == "png"
        assert isinstance(dlg.quality_slider, AppleSlider) and dlg.quality_row.value() == 95
        assert isinstance(dlg.btn_action, AppleBrandButton)
        assert isinstance(dlg.btn_clear, AppleCompactButton)
    finally:
        dlg.close()


def test_apple_button_signals_and_clicks(qapp):
    """验证 AppleButton 家族的信号发射与点击交互。"""
    btn = AppleButton("ClickMe")
    clicks = []
    btn.clicked.connect(lambda: clicks.append(True))

    btn.click()
    assert clicks == [True]

    btn.setEnabled(False)
    btn.click()
    # 禁用状态下点击不应触发信号
    assert clicks == [True]


def test_apple_segmented_control_bounds_and_mouse(qapp):
    """验证 AppleSegmentedControl 的边界防护与鼠标点击交互。"""
    seg = AppleSegmentedControl()
    seg.resize(300, 30)
    seg.show()
    qapp.processEvents()

    try:
        # 空状态防护
        assert seg.count() == 0
        seg.setCurrentIndex(1)
        assert seg.currentIndex() == 0

        seg.add_segment("Alpha")
        seg.add_segment("Beta")
        seg.add_segment("Gamma")

        # 越界 index 防护
        seg.setCurrentIndex(-1)
        assert seg.currentIndex() == 0
        seg.setCurrentIndex(100)
        assert seg.currentIndex() == 0

        # setCurrentText 正常与非预期
        seg.setCurrentText("Beta")
        assert seg.currentIndex() == 1
        seg.setCurrentText("NonExistent")
        assert seg.currentIndex() == 1  # 保持原样

        # 模拟鼠标点击第 3 个 segment (x 约为 250)
        press_event = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(250.0, 15.0),
            seg.mapToGlobal(QPointF(250.0, 15.0)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        seg.mousePressEvent(press_event)
        assert seg.currentIndex() == 2
    finally:
        seg.close()


def test_apple_slider_double_click_event(qapp):
    """验证 AppleSlider 实际接收双击事件时的重置机制。"""
    slider = AppleSlider(Qt.Orientation.Horizontal, default_value=42)
    slider.setRange(0, 100)
    slider.setValue(99)

    resets = []
    slider.resetRequested.connect(lambda: resets.append(True))

    # 在圆钮上双击才重置；圆钮位置随当前值变化
    slider.resize(200, 20)
    position = slider._handle_rect().center()
    dbl_event = QMouseEvent(
        QMouseEvent.Type.MouseButtonDblClick,
        position,
        slider.mapToGlobal(position),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    slider.mouseDoubleClickEvent(dbl_event)

    assert slider.value() == 42
    assert len(resets) == 1


def test_image_processing_dialog_interaction_flow(qapp):
    """图片格式处理的交互：切换格式、调整质量，无损格式收起质量一行并说明。"""
    dlg = ImageProcessingDialog(initial_paths=["img1.png", "img2.jpg"], initial_format="png", initial_quality=95)
    try:
        dlg.show()
        assert dlg.quality_setting_row.isHidden() and dlg.format_row.message()
        dlg.format_combo.setCurrentIndex(dlg.format_combo.findData("webp"))
        assert dlg.format_combo.currentData() == "webp"
        assert dlg.quality_slider.isEnabled() and not dlg.quality_setting_row.isHidden()
        assert dlg.format_row.message() == ""
        dlg.quality_slider.setValue(80)
        assert dlg.quality_row.value() == 80
        # 目标大小只对 JPG 可用
        assert not dlg.target_row.isEnabled()
        dlg.format_combo.setCurrentIndex(dlg.format_combo.findData("jpg"))
        assert dlg.target_row.isEnabled()
    finally:
        dlg.close()


def test_image_processing_dialog_status_and_quality_projection(qapp):
    """目标大小的状态写在那一行下面；匹配结果回填质量不算用户改动，用户再动质量才作废。"""
    dlg = ImageProcessingDialog(initial_paths=["img1.jpg"], initial_format="jpg", initial_quality=85)
    try:
        dlg.show()
        assert dlg.target_row.message() and dlg.target_row._message.property("kind") == "info"
        dlg.target_edit.setText("abc")
        dlg._auto_match_quality()
        assert dlg.target_row._message.property("kind") == "error"

        dlg._set_status("compress.status.match_result", "success", size_kb=120.0)
        dlg.quality_row.set_value(72)
        assert dlg.quality_row.value() == 72 and dlg.target_row._message.property("kind") == "success"
        dlg.quality_slider.edit_value(60)
        assert dlg.target_row._message.property("kind") == "info"

        # 尺寸填错时在尺寸模式一行下面说明，并且不能开始
        dlg.width_edit.setText("-5")
        assert dlg.resize_mode_row._message.property("kind") == "error" and not dlg.btn_action.isEnabled()
        dlg.width_edit.setText("")
        assert dlg.btn_action.isEnabled()
    finally:
        dlg.close()


def test_settings_dialog_shortcut_compact_buttons(qapp):
    """验证 SettingsDialog 中快捷键列表的所有重置按钮规范均为 AppleCompactButton。"""
    from src.ui.dialogs.settings_dialog import SettingsValues
    from src.ui.window_actions import ShortcutRow
    dlg = SettingsDialog(values=SettingsValues(shortcuts=(
        ShortcutRow("File/Open File", "File", "Open Files", "Ctrl+O", "Ctrl+O"),
        ShortcutRow("Edit/Manual Crop", "Edit", "Crop", "", "Ctrl+Shift+C"))))
    try:
        compact_buttons = dlg.findChildren(AppleCompactButton)
        assert len(compact_buttons) > 0
        from resources.styles.theme_variables import CONTROL_HEIGHT_COMPACT
        for b in compact_buttons:
            assert b.height() == CONTROL_HEIGHT_COMPACT
            assert not b.styleSheet()
    finally:
        dlg.close()
