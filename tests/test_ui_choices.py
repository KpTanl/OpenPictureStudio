"""下拉与菜单、复选与单选、开关、步进器：外观取自主题，交互区分用户操作与程序回填。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from resources.styles.theme_variables import CONTROL_HEIGHT, get_theme_qss  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.ui.widgets import (AppleCheckBox, AppleDropDown, AppleMenu, AppleRadioButton, AppleSpinBox,  # noqa: E402
                            LabeledToggle, ToggleSwitch)

THEMES = ("dark", "light", "beige")


@pytest.fixture
def theme():
    app = QApplication.instance() or QApplication([])
    previous = app.styleSheet(), app.property("ops.theme"), QPalette(app.palette())
    manager = ThemeManager()
    manager.apply("dark")
    yield manager
    app.setStyleSheet(previous[0])
    app.setProperty("ops.theme", previous[1])
    app.setPalette(previous[2])


def _host(widget, width=None, height=None):
    host = QWidget()
    widget.setParent(host)
    widget.move(20, 20)
    hint = widget.sizeHint()
    widget.resize(width or hint.width(), height or hint.height())
    host.resize(widget.width() + 40, widget.height() + 40)
    host.show()
    QApplication.processEvents()
    return host


def _pixel(widget, x, y):
    return widget.window().grab().toImage().pixelColor(widget.x() + int(x), widget.y() + int(y)).name()


def _token(key):
    return QColor(get_theme_tokens()[key]).name()


@pytest.mark.parametrize("name", THEMES)
def test_stylesheet_styles_dropdown_stepper_and_menu(name):
    qss = get_theme_qss(name)
    for selector in ("QPushButton#appleDropDown {", "QPushButton#appleDropDown:focus {",
                     'QPushButton#appleDropDown[open="true"] {',
                     'QPushButton#appleDropDown[flat="true"] {', "QSpinBox#appleStepper {",
                     "QSpinBox#appleStepper:focus {", "QPushButton#stepperButton {", "QMenu#appleMenu::item:selected {"):
        assert selector in qss, selector


def test_dropdown_and_menu_carry_no_inline_style(theme):
    dropdown = AppleDropDown()
    dropdown.addItems(["甲", "乙"])
    assert not dropdown.styleSheet() and not dropdown._menu.styleSheet()
    assert not AppleMenu().styleSheet()
    assert not hasattr(dropdown, "apply_theme")


def test_dropdown_paints_from_the_theme_and_follows_changes(theme):
    dropdown = AppleDropDown()
    dropdown.addItems(["通用增强", "人像增强"])
    host = _host(dropdown, 220)
    assert dropdown.height() == CONTROL_HEIGHT
    for name in THEMES:
        theme.apply(name)
        QApplication.processEvents()
        assert _pixel(dropdown, 6, CONTROL_HEIGHT // 2) == _token("page")
    host.close()


def test_selected_item_is_marked_with_a_check_icon(theme):
    dropdown = AppleDropDown()
    dropdown.addItems(["甲", "乙", "丙"])
    dropdown.setCurrentIndex(1)
    icons = [item["action"].icon() for item in dropdown._items]

    def has_ink(icon):
        image = icon.pixmap(16, 16).toImage()
        return any(image.pixelColor(x, y).alpha() > 200 for x in range(16) for y in range(16))

    assert [has_ink(icon) for icon in icons] == [False, True, False]


def test_dropdown_note_is_kept_apart_from_the_selection(theme):
    dropdown = AppleDropDown()
    dropdown.addItems(["人像增强"])
    dropdown.set_note("未安装")
    assert dropdown.note() == "未安装"
    assert dropdown.currentText() == dropdown.text() == "人像增强"
    host = _host(dropdown, 120)
    assert dropdown._button_option().text != "人像增强"
    dropdown.set_note("")
    assert dropdown._button_option().text == "人像增强"
    host.close()


def test_flat_dropdown_text_is_the_selected_item(theme):
    dropdown = AppleDropDown(flat=True)
    dropdown.addItems(["全部图片", "5 星"])
    assert dropdown.property("flat") is True
    assert dropdown.text() == "全部图片"
    dropdown.setCurrentIndex(1)
    assert dropdown.text() == dropdown.currentText() == "5 星"


def _dropdown_window(current=2):
    window = QWidget()
    dropdown = AppleDropDown(window)
    dropdown.addItems(["原彩", "OPS 观感", "胶片", "黑白"])
    dropdown.setCurrentIndex(current)
    dropdown.setGeometry(20, 20, 220, CONTROL_HEIGHT)
    window.resize(300, 300)
    window.show()
    QTest.qWaitForWindowExposed(window)
    activations = []
    dropdown.activated.connect(activations.append)
    return window, dropdown, activations


def _mouse(kind, window, widget):
    point = widget.mapTo(window, widget.rect().center())
    kind(window.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    QTest.qWait(20)


def test_dropdown_opens_on_press_with_the_current_item_highlighted(theme):
    window, dropdown, activations = _dropdown_window()
    _mouse(QTest.mousePress, window, dropdown)
    assert dropdown._menu.isVisible() and dropdown.property("open") is True
    assert dropdown._menu.activeAction().text() == "胶片"
    _mouse(QTest.mouseRelease, window, dropdown)
    assert dropdown._menu.isVisible() and activations == []
    dropdown.hidePopup()
    QTest.qWait(20)
    assert dropdown.property("open") is False
    window.close()


def test_pressing_the_open_dropdown_closes_it_without_reopening(theme):
    window, dropdown, activations = _dropdown_window()
    _mouse(QTest.mousePress, window, dropdown)
    _mouse(QTest.mouseRelease, window, dropdown)
    _mouse(QTest.mousePress, window, dropdown)
    assert not dropdown._menu.isVisible()
    # 关闭菜单的那次按下随后会再送到按钮，此时不应重新弹出
    QTest.mousePress(dropdown, Qt.MouseButton.LeftButton)
    assert not dropdown._menu.isVisible() and not dropdown.isDown()
    QTest.mouseRelease(dropdown, Qt.MouseButton.LeftButton)
    QTest.qWait(250)
    QTest.mousePress(dropdown, Qt.MouseButton.LeftButton)
    assert dropdown._menu.isVisible()
    dropdown.hidePopup()
    assert activations == []
    window.close()


@pytest.mark.parametrize("key", [Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_F4])
def test_keys_open_the_list_instead_of_changing_the_selection(theme, key):
    window, dropdown, activations = _dropdown_window()
    dropdown.setFocus()
    QTest.keyClick(dropdown, key)
    assert dropdown._menu.isVisible()
    assert dropdown.currentText() == "胶片" and activations == []
    QTest.keyClick(dropdown._menu, Qt.Key.Key_Down)
    assert dropdown._menu.activeAction().text() == "黑白"
    QTest.keyClick(dropdown._menu, Qt.Key.Key_Return)
    QTest.qWait(20)
    assert not dropdown._menu.isVisible()
    assert dropdown.currentText() == "黑白" and activations == [3]
    window.close()


def test_escape_closes_the_list_and_keeps_the_selection(theme):
    window, dropdown, activations = _dropdown_window()
    dropdown.show_popup()
    QTest.keyClick(dropdown._menu, Qt.Key.Key_Up)
    QTest.keyClick(dropdown._menu, Qt.Key.Key_Escape)
    QTest.qWait(20)
    assert not dropdown._menu.isVisible()
    assert dropdown.currentText() == "胶片" and activations == []
    window.close()


def test_checkbox_fills_with_the_accent_when_checked(theme):
    box = AppleCheckBox("保留选区内")
    host = _host(box)
    middle = box.height() // 2 - 5  # 方框内靠上的位置，避开对勾
    assert _pixel(box, 4, middle) == _token("page")
    states = []
    box.stateChanged.connect(states.append)
    box.setChecked(True)
    QApplication.processEvents()
    for name in THEMES:
        theme.apply(name)
        QApplication.processEvents()
        assert _pixel(box, 4, middle) == _token("accent")
    box.setTristate(True)
    box.nextCheckState()
    assert box.checkState() == Qt.CheckState.Unchecked
    box.nextCheckState()
    assert box.checkState() == Qt.CheckState.PartiallyChecked and box.isChecked()
    assert states == [Qt.CheckState.Checked.value, Qt.CheckState.Unchecked.value,
                      Qt.CheckState.PartiallyChecked.value]
    host.close()


def test_radio_buttons_are_exclusive_and_draw_an_accent_dot(theme):
    holder = QWidget()
    first, second = AppleRadioButton("透明", holder), AppleRadioButton("纯色", holder)
    first.move(10, 10)
    second.move(120, 10)
    first.resize(first.sizeHint())
    second.resize(second.sizeHint())
    holder.resize(260, 50)
    holder.show()
    first.setChecked(True)
    second.setChecked(True)
    QApplication.processEvents()
    assert not first.isChecked() and second.isChecked()
    assert _pixel(second, 10, second.height() // 2) == _token("accent")
    assert _pixel(first, 10, first.height() // 2) == _token("page")
    holder.close()


def test_choice_widgets_are_keyboard_focusable(theme):
    for widget in (AppleCheckBox("甲"), AppleRadioButton("乙"), ToggleSwitch(), AppleDropDown(), AppleSpinBox()):
        assert widget.focusPolicy() & Qt.FocusPolicy.TabFocus, type(widget).__name__


def test_switch_track_follows_the_state_and_the_theme(theme):
    switch = ToggleSwitch()
    host = _host(switch)
    middle = switch.height() // 2
    right = switch.width() - 8
    assert _pixel(switch, right, middle) == _token("track")
    events = []
    switch.toggled.connect(events.append)
    switch.set_checked_silent(True)
    QApplication.processEvents()
    assert events == [] and switch.handle_position == 1.0
    for name in THEMES:
        theme.apply(name)
        QApplication.processEvents()
        assert _pixel(switch, 8, middle) == _token("accent")
        assert _pixel(switch, right, middle) == _token("knob")
    switch.setChecked(False)
    assert events == [False]
    host.close()


def test_labeled_toggle_starts_in_its_given_state_without_animating(theme):
    toggle = LabeledToggle("保持宽高比", "说明", True)
    assert toggle.isChecked() and toggle.switch.handle_position == 1.0
    assert not toggle._title_label.styleSheet() and not toggle._desc_label.styleSheet()
    assert toggle._desc_label.objectName() == "toggleDescription"


def test_stepper_buttons_edit_and_respect_the_range(theme):
    stepper = AppleSpinBox()
    stepper.setRange(1, 3)
    stepper.setValue(2)
    edits = []
    stepper.valueEdited.connect(edits.append)
    host = _host(stepper, 120)
    assert stepper.height() >= CONTROL_HEIGHT
    assert stepper._minus.isEnabled() and stepper._plus.isEnabled()
    stepper._plus.click()
    assert stepper.value() == 3 and not stepper._plus.isEnabled() and stepper._minus.isEnabled()
    stepper._minus.click()
    stepper._minus.click()
    assert stepper.value() == 1 and not stepper._minus.isEnabled()
    assert edits == [3, 2, 1]
    stepper.setValue(2)
    assert edits == [3, 2, 1] and stepper._minus.isEnabled() and stepper._plus.isEnabled()
    assert stepper._minus.geometry().left() < stepper._plus.geometry().left()
    stepper.setEnabled(False)
    assert not stepper._minus.isEnabled() and not stepper._plus.isEnabled()
    host.close()


def test_labeled_toggle_row_does_not_toggle_a_disabled_switch(theme):
    toggle = LabeledToggle("放大", "这张照片太大", switch_on_left=True)
    host = _host(toggle, 260)
    events = []
    toggle.toggled.connect(events.append)
    assert toggle.description() == "这张照片太大"
    toggle.switch.setEnabled(False)
    QTest.mouseClick(toggle, Qt.MouseButton.LeftButton)
    assert events == [] and not toggle.isChecked()
    toggle.switch.setEnabled(True)
    QTest.mouseClick(toggle, Qt.MouseButton.LeftButton)
    assert events == [True] and toggle.isChecked()
    host.close()
