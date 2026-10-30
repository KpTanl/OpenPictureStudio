"""Apple HIG 风格 ToggleSwitch 与 LabeledToggle 胶囊滑块组件专项测试。"""

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from src.ui.widgets.toggle_switch import LabeledToggle, ToggleSwitch
from src.ui.theme import ThemeManager, set_theme_manager


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_apple_toggle_switch_lifecycle(qapp):
    """测试 ToggleSwitch 基础生命周期、信号、属性与动画插值。"""
    switch = ToggleSwitch(width=44, height=24)
    assert switch.isCheckable()
    assert not switch.isChecked()
    assert switch.handle_position == 0.0

    # 信号监听
    toggled_records = []
    state_changed_records = []
    switch.toggled.connect(toggled_records.append)
    switch.stateChanged.connect(state_changed_records.append)

    # 开启切换
    switch.setChecked(True)
    assert switch.isChecked()
    assert len(toggled_records) == 1
    assert toggled_records[-1] is True
    assert len(state_changed_records) == 1
    assert state_changed_records[-1] is True

    # 静音关闭
    switch.set_checked_silent(False)
    assert not switch.isChecked()
    assert switch.handle_position == 0.0
    # 静音不触发信号
    assert len(toggled_records) == 1

    # 可用键盘聚焦
    assert switch.focusPolicy() == Qt.FocusPolicy.StrongFocus


def test_apple_toggle_switch_mouse_and_drag(qapp):
    """测试 ToggleSwitch 点击与水平拖拽手势。"""
    switch = ToggleSwitch(width=44, height=24)
    switch.show()

    # 1. 模拟鼠标左键点击
    press_event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(10, 12),
        switch.mapToGlobal(QPointF(10, 12)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    switch.mousePressEvent(press_event)
    assert switch._is_pressed

    release_event = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(10, 12),
        switch.mapToGlobal(QPointF(10, 12)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    switch.mouseReleaseEvent(release_event)
    assert not switch._is_pressed
    assert switch.isChecked()

    # 2. 模拟鼠标拖拽
    switch.mousePressEvent(press_event)
    # 向左拖拽超过阈值
    move_event = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        QPointF(0, 12),
        switch.mapToGlobal(QPointF(0, 12)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    switch.mouseMoveEvent(move_event)
    assert switch._is_dragging

    # 释放吸附
    switch.mouseReleaseEvent(release_event)
    assert not switch._is_dragging

    switch.close()


def test_apple_toggle_switch_keyboard_and_paint(qapp):
    """测试 ToggleSwitch 键盘 Space/Enter 切换与多主题重绘无异常。"""
    switch = ToggleSwitch(width=44, height=24)
    switch.show()

    # 键盘 Space 键
    key_event = QKeyEvent(
        QKeyEvent.Type.KeyPress,
        Qt.Key.Key_Space,
        Qt.KeyboardModifier.NoModifier,
    )
    switch.keyPressEvent(key_event)
    assert switch.isChecked()

    # 键盘 Enter 键
    key_return = QKeyEvent(
        QKeyEvent.Type.KeyPress,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.NoModifier,
    )
    switch.keyPressEvent(key_return)
    assert not switch.isChecked()

    # 多主题重绘
    mgr = ThemeManager()
    set_theme_manager(mgr)
    for t_name in ["dark", "light", "beige"]:
        mgr.apply(t_name)
        switch.repaint()

    switch.close()


def test_labeled_toggle_composite(qapp):
    """测试 LabeledToggle 组合控件的文本设置、说明、整行点击和级联禁用。"""
    toggle = LabeledToggle(
        title="开启特性",
        description="启用后将开启增强处理",
        checked=False,
        switch_on_left=True,
    )
    toggle.show()

    assert not toggle.isChecked()
    assert toggle.text() == "开启特性"
    assert toggle._desc_label.isVisible()

    # 信号监听
    events = []
    toggle.toggled.connect(events.append)
    toggle.stateChanged.connect(events.append)

    # 点击整行切换
    press_event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(20, 10),
        toggle.mapToGlobal(QPointF(20, 10)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    toggle.mousePressEvent(press_event)
    assert toggle.isChecked()
    assert len(events) == 2

    # 更新文本
    toggle.setText("新标题")
    assert toggle.text() == "新标题"
    toggle.setDescription("")
    assert not toggle._desc_label.isVisible()

    # 级联禁用
    toggle.setEnabled(False)
    assert not toggle.isEnabled()
    assert not toggle.switch.isEnabled()

    toggle.close()


def test_dialogs_using_apple_toggles(qapp, tmp_path):
    """图片格式处理通过尺寸模式选择保持比例或指定宽高。"""
    from PIL import Image
    from src.ui.dialogs.compress_dialog import ImageProcessingDialog

    dummy_img = tmp_path / "dummy.png"
    Image.new("RGB", (100, 100), color="blue").save(dummy_img)
    comp_dlg = ImageProcessingDialog(str(dummy_img))
    modes = comp_dlg.resize_mode_combo
    assert [modes.itemData(index) for index in range(modes.count())] == ["contain", "cover", "pad", "stretch"]
    modes.setCurrentIndex(modes.findData("contain"))
    comp_dlg.width_edit.setText("80")
    comp_dlg.height_edit.setText("60")
    options = comp_dlg._resize_options()
    from src.core.image_resize import plan_resize
    assert options.mode == "contain" and options.width == 80 and options.height == 60
    assert plan_resize((100, 100), options).output_size == (60, 60)
    modes.setCurrentIndex(modes.findData("stretch"))
    assert plan_resize((100, 100), comp_dlg._resize_options()).output_size == (80, 60)
    comp_dlg.close()
