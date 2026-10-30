"""开关按钮、标签、状态标记、提示条、进度条、悬停提示、列表行与分组标题：外观取自主题，用户操作与程序回填分开。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontMetrics, QHelpEvent, QIcon, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (QApplication, QLabel, QProgressBar, QPushButton, QToolTip,  # noqa: E402
                             QWidget)

from resources.styles.semantic_colors import color_channels, mix  # noqa: E402
from resources.styles.theme_variables import (CONTROL_HEIGHT, CONTROL_HEIGHT_COMPACT, get_theme_qss,  # noqa: E402
                                              get_theme_tokens_by_name)
from src.ui.icons import themed_icon  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.ui.widgets import (AppleIconButton, Chip, ChipGroup, GroupedOptionCard, NoticeBar, StatusBadge,  # noqa: E402
                            ToggleButton, set_tooltip)
from src.ui.widgets.apple_button import bold_text_extra  # noqa: E402
from src.ui.widgets.collapsible_card import CollapsibleCard  # noqa: E402
from src.ui.widgets.tool_sidebar import ToolSidebar  # noqa: E402

THEMES = ("dark", "light", "beige")


@pytest.fixture
def theme(qapp):
    previous = qapp.styleSheet(), qapp.property("ops.theme"), QPalette(qapp.palette())
    manager = ThemeManager()
    manager.apply("dark")
    yield manager
    qapp.setStyleSheet(previous[0])
    qapp.setProperty("ops.theme", previous[1])
    qapp.setPalette(previous[2])


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


def _over_surface(key):
    """半透明取值叠在界面底色上的结果。"""
    tokens = get_theme_tokens()
    red, green, blue, alpha = color_channels(tokens[key])
    base = QColor(tokens["surface"])
    return QColor(*(round(top * alpha + bottom * (1 - alpha))
                    for top, bottom in zip((red, green, blue), (base.red(), base.green(), base.blue())))).name()


def _close(color, expected, tolerance=2):
    first, second = QColor(color), QColor(expected)
    return all(abs(a - b) <= tolerance for a, b in zip(first.getRgb()[:3], second.getRgb()[:3]))


def _ink(image):
    """图里不透明度最高的像素的颜色。"""
    pixels = [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())]
    return max(pixels, key=lambda color: color.alpha())


@pytest.mark.parametrize("name", THEMES)
def test_stylesheet_styles_the_small_widgets(name):
    qss = get_theme_qss(name)
    tokens = get_theme_tokens_by_name(name)
    for selector in ("QPushButton#toggleButton:checked,", "QPushButton#chip:checked {", "QPushButton#chip:focus {",
                     'QFrame#noticeBar[kind="warning"] {', 'QFrame#noticeBar[kind="info"] {',
                     "QFrame#collapsibleCard {", "QPushButton#collapsibleHeaderBtn:focus {",
                     'QProgressBar[textVisible="false"] {', "QToolTip {"):
        assert selector in qss, selector
    assert "toolSidebarStashBanner" not in qss and "collapsibleIndicator" not in qss
    tooltip = qss[qss.index("QToolTip {"):]
    tooltip = tooltip[:tooltip.index("}")]
    assert f"background-color: {tokens['text_primary']};" in tooltip and f"color: {tokens['surface']};" in tooltip


def test_icon_takes_the_checked_color_only_when_on(theme):
    icon = themed_icon("layout-bottombar", checked_role="accent")
    for name in THEMES:
        theme.apply(name)
        off = _ink(icon.pixmap(16, 16, QIcon.Mode.Normal, QIcon.State.Off).toImage())
        on = _ink(icon.pixmap(16, 16, QIcon.Mode.Normal, QIcon.State.On).toImage())
        disabled = _ink(icon.pixmap(16, 16, QIcon.Mode.Disabled, QIcon.State.On).toImage())
        assert _close(off.name(), _token("text_secondary"))
        assert _close(on.name(), _token("accent"))
        assert _close(disabled.name(), _token("text_disabled"))
    with pytest.raises(KeyError):
        themed_icon("check", checked_role="no_such_role")


def test_toggle_button_follows_the_theme_and_reports_only_clicks(theme):
    button = ToggleButton("照片信息", "layout-sidebar-right")
    assert not button.styleSheet() and button.isCheckable()
    assert button.focusPolicy() & Qt.FocusPolicy.TabFocus
    host = _host(button)
    assert button.height() == CONTROL_HEIGHT
    clicks = []
    button.clicked[bool].connect(clicks.append)
    button.setChecked(True)
    assert clicks == []
    for name in THEMES:
        theme.apply(name)
        QApplication.processEvents()
        assert _close(_pixel(button, 3, CONTROL_HEIGHT // 2), _over_surface("accent_soft"))
    button.setChecked(False)
    QApplication.processEvents()
    assert _pixel(button, 3, CONTROL_HEIGHT // 2) == _token("surface")
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert clicks == [True] and button.isChecked()
    host.close()


def test_size_reserves_room_for_the_bold_checked_text(theme):
    button = ToggleButton("Photo information", "layout-sidebar-right")
    bold = QFont(button.font())
    bold.setWeight(QFont.Weight.DemiBold)
    extra = QFontMetrics(bold).horizontalAdvance(button.text()) - button.fontMetrics().horizontalAdvance(button.text())
    assert bold_text_extra(button) == max(0, extra)
    assert button.sizeHint().width() == QPushButton.sizeHint(button).width() + bold_text_extra(button)
    assert button.minimumSizeHint() == button.sizeHint()
    chip, action_chip = Chip("Photo information", checkable=True), Chip("Photo information")
    assert chip.sizeHint().width() == action_chip.sizeHint().width() + bold_text_extra(chip)
    assert chip.height() == CONTROL_HEIGHT_COMPACT and not chip.styleSheet()
    for widget in (button, chip, action_chip):
        widget.deleteLater()


def test_chip_group_is_exclusive_and_silent_on_programmatic_selection(theme):
    group = ChipGroup(columns=2)
    group.set_items([("16:9", "16:9"), ("3:2", "3:2"), ("1:1", "1:1")])
    host = _host(group, 240)
    changes = []
    group.current_changed.connect(changes.append)
    assert group.current() == ""

    group.set_current("3:2")
    assert group.current() == "3:2" and changes == []
    assert [chip.isChecked() for chip in group._chips.values()] == [False, True, False]
    with pytest.raises(KeyError):
        group.set_current("4:3")

    QTest.mouseClick(group._chips["1:1"], Qt.MouseButton.LeftButton)
    assert group.current() == "1:1" and changes == ["1:1"]
    QTest.mouseClick(group._chips["1:1"], Qt.MouseButton.LeftButton)
    assert changes == ["1:1"] and group._chips["1:1"].isChecked()

    first, second = group._chips["16:9"], group._chips["3:2"]
    assert first.y() == second.y() and first.width() == second.width()
    assert group._chips["1:1"].y() > first.y()

    group.set_items([("1:1", "1:1"), ("4:5", "4:5")])
    assert group.current() == "1:1" and group._chips["1:1"].isChecked()
    group.set_items([("4:5", "4:5")])
    assert group.current() == ""
    host.close()


def test_status_badge_draws_its_kind_in_the_theme_color(theme):
    badge = StatusBadge("success", "已安装")
    host = _host(badge)
    kinds = {"success": "success", "warning": "warning", "pending": "text_caption", "active": "accent"}
    for name in THEMES:
        theme.apply(name)
        for kind, role in kinds.items():
            badge.set_status(kind, "状态")
            QApplication.processEvents()
            image = badge.grab().toImage()
            colors = {image.pixelColor(x, 0).name() for x in range(10, badge.width() - 10)}
            assert colors == {_token(role)}, (name, kind)
    with_icon, without_icon = StatusBadge("warning", "文件损坏"), StatusBadge("pending", "文件损坏")
    assert with_icon.sizeHint().width() > without_icon.sizeHint().width()
    with pytest.raises(ValueError):
        badge.set_status("unknown", "x")
    assert badge.kind() == "active" and badge.text() == "状态"
    host.close()


def test_notice_bar_kinds_and_action(theme):
    bar = NoticeBar("warning", "已恢复未应用的调整", "放弃")
    assert not bar.styleSheet() and bar.property("kind") == "warning"
    host = _host(bar, 320)
    triggered = []
    bar.action_triggered.connect(lambda: triggered.append(True))
    for name in THEMES:
        theme.apply(name)
        QApplication.processEvents()
        assert _pixel(bar, bar.width() // 2, 2) == _token("warning_surface")
        icon_column = bar.grab().toImage()
        assert _close(_ink_in(icon_column, 12, 30, bar.height()), _token("warning"), tolerance=6)
    QTest.mouseClick(bar.action_button, Qt.MouseButton.LeftButton)
    assert triggered == [True]
    bar.set_action_text("")
    assert bar.action_button.isHidden()
    bar.set_text("新的说明")
    assert bar.text() == "新的说明"
    host.close()

    info = NoticeBar("info", "需要先下载模型")
    assert info.action_button.isHidden()
    host = _host(info, 320)
    assert _close(_pixel(info, info.width() // 2, 2), _over_surface("accent_soft"))
    host.close()
    with pytest.raises(ValueError):
        NoticeBar("danger", "x")


def _ink_in(image, left, right, height):
    """图标所在的一列里，离底色最远的像素。"""
    base = image.pixelColor(2, height // 2)
    pixels = [image.pixelColor(x, y) for x in range(left, right) for y in range(height)]
    return max(pixels, key=lambda color: sum(abs(a - b) for a, b in zip(color.getRgb()[:3], base.getRgb()[:3]))).name()


def test_tool_sidebar_shows_the_restore_notice(theme):
    sidebar = ToolSidebar()
    assert isinstance(sidebar._stash_banner, NoticeBar) and sidebar._stash_banner.kind() == "warning"
    assert "⚠" not in sidebar._stash_banner.text()
    assert not sidebar._title_label.styleSheet()
    assert sidebar._stash_banner.isHidden()
    discarded = []
    sidebar.discard_stash_requested.connect(lambda: discarded.append(True))
    sidebar.set_stash_banner_visible(True)
    assert not sidebar._stash_banner.isHidden()
    sidebar._stash_banner.action_button.click()
    assert discarded == [True]
    sidebar.retranslate_ui()
    assert sidebar._stash_banner.action_button.text()
    sidebar.close()
    sidebar.deleteLater()


def test_progress_bar_is_thin_without_text(theme):
    host = QWidget()
    thin, with_text = QProgressBar(host), QProgressBar(host)
    thin.setTextVisible(False)
    thin.setRange(0, 100)
    thin.setValue(50)
    thin.setGeometry(20, 20, 200, thin.sizeHint().height())
    with_text.setGeometry(20, 60, 200, with_text.sizeHint().height())
    host.resize(240, 120)
    host.show()
    QApplication.processEvents()
    assert thin.sizeHint().height() == 6 and with_text.sizeHint().height() >= 20
    for name in THEMES:
        theme.apply(name)
        QApplication.processEvents()
        assert _pixel(thin, 20, 3) == _token("accent")
        assert _pixel(thin, 180, 3) == _token("track")
    host.close()


def test_tooltip_palette_is_the_inverse_of_the_surface(theme):
    for name in THEMES:
        theme.apply(name)
        palette = QApplication.palette()
        assert palette.color(QPalette.ColorRole.ToolTipBase).name() == _token("text_primary")
        assert palette.color(QPalette.ColorRole.ToolTipText).name() == _token("surface")


def test_list_rows_paint_selection_and_keep_keyboard_navigation(theme):
    rows = GroupedOptionCard()
    rows.add_item("general", "常规", icon="settings")
    rows.add_item("display", "图片显示", "推荐", icon="photo")
    rows.add_item("tools", "工具")
    assert not hasattr(rows, "apply_theme")
    host = _host(rows, 220)
    first, second = rows._rows[0], rows._rows[1]
    changes = []
    rows.selection_changed.connect(lambda index, key: changes.append(key))
    for name in THEMES:
        theme.apply(name)
        QApplication.processEvents()
        image = rows.grab().toImage()
        assert _close(image.pixelColor(4, first.y() + first.height() // 2).name(), _over_surface("accent_soft"))
        assert image.pixelColor(4, second.y() + second.height() // 2).name() == _token("surface")
    second._hovered = True
    second.update()
    QApplication.processEvents()
    assert rows.grab().toImage().pixelColor(4, second.y() + second.height() // 2).name() == _token("control")
    second._hovered = False

    rows.setFocus()
    QTest.keyClick(rows, Qt.Key.Key_Down)
    assert rows.checked_key() == "display" and changes == ["display"]
    QTest.mouseClick(rows._rows[2], Qt.MouseButton.LeftButton)
    assert rows.checked_key() == "tools" and changes == ["display", "tools"]
    host.close()


def test_list_row_shows_a_focus_outline_on_the_selected_row(theme):
    rows = GroupedOptionCard()
    rows.add_item("a", "甲")
    rows.add_item("b", "乙")
    host = _host(rows, 200)
    host.activateWindow()
    rows.setFocus()
    QApplication.processEvents()
    if not rows.hasFocus():
        pytest.skip("离屏平台未给出键盘焦点")
    assert rows._rows[0]._focused and rows._rows[1]._focused
    row = rows._rows[0]
    assert _close(_pixel(rows, row.width() // 2, row.y()), _token("accent"))
    assert _close(_pixel(rows, row.width() // 2, rows._rows[1].y()), _token("surface"))
    rows.clearFocus()
    assert not rows._rows[0]._focused
    host.close()


def test_group_title_toggles_content_and_takes_header_actions(theme):
    card = CollapsibleCard("影调", is_expanded=True)
    body = QLabel("内容")
    card.add_widget(body)
    reset = AppleIconButton(themed_icon("refresh"), size=28)
    card.add_header_widget(reset)
    host = _host(card, 260)
    states = []
    card.toggled.connect(states.append)
    assert card.title() == "影调" and not card._header_btn.styleSheet()
    assert card._header_btn.focusPolicy() & Qt.FocusPolicy.TabFocus
    assert body.isVisible() and reset.isVisible()
    expanded_height = card.sizeHint().height()

    def arrow():
        return card._header_btn.icon().pixmap(16, 16).toImage()

    open_arrow = arrow()
    QTest.mouseClick(card._header_btn, Qt.MouseButton.LeftButton)
    assert states == [False] and not card.is_expanded() and not body.isVisible()
    assert card.sizeHint().height() < expanded_height
    assert arrow() != open_arrow
    assert reset.isVisible()

    card.set_expanded(True)
    assert states == [False, True] and body.isVisible() and arrow() == open_arrow
    card.set_title("色彩")
    assert card.title() == "色彩"

    QTest.mouseClick(reset, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    assert card.is_expanded()
    for name in THEMES:
        theme.apply(name)
        QApplication.processEvents()
        assert _pixel(card, card.width() // 2, card.height() - 1) == _token("hairline_solid")
        assert _pixel(card, card.width() - 2, card.height() - 4) == _token("surface")
    host.close()


def test_group_title_shows_an_ampersand_as_written(theme):
    card = CollapsibleCard("Tone & White Balance")
    assert card.title() == "Tone & White Balance"
    assert card._header_btn.text() == "Tone && White Balance"
    assert card._header_btn.shortcut().isEmpty()
    card.set_title("Presence & Saturation")
    assert card.title() == "Presence & Saturation" and card._header_btn.shortcut().isEmpty()
    card.deleteLater()


def _hover(widget):
    position = QPoint(4, 4)
    QApplication.sendEvent(widget, QHelpEvent(QEvent.Type.ToolTip, position, widget.mapToGlobal(position)))
    return QToolTip.text()


def test_tooltip_dims_the_shortcut_in_the_current_theme(theme):
    button = AppleIconButton(themed_icon("rotate-clockwise"))
    host = _host(button)
    set_tooltip(button, "顺时针旋转 <90°>", "R")
    assert button.toolTip() == "顺时针旋转 <90°>  R"
    seen = set()
    for name in THEMES:
        theme.apply(name)
        tokens = get_theme_tokens()
        dim = mix(tokens["text_primary"], tokens["surface"], 0.65)
        shown = _hover(button)
        assert "顺时针旋转 &lt;90°&gt;" in shown and f"<span style='color:{dim}'>R</span>" in shown
        assert dim not in (_token("text_primary"), _token("surface"))
        seen.add(dim)
    assert len(seen) == len(THEMES)

    set_tooltip(button, "旋转", "Ctrl+R")
    assert len(button.findChildren(QObject, options=Qt.FindChildOption.FindDirectChildrenOnly)) == 1
    assert "Ctrl+R" in _hover(button)
    set_tooltip(button, "旋转")
    assert button.toolTip() == "旋转" and _hover(button) == "旋转"
    QToolTip.hideText()
    host.close()


def test_tooltip_box_stays_compact(theme):
    button = AppleIconButton(themed_icon("rotate-clockwise"))
    host = _host(button)
    set_tooltip(button, "旋转", "R")
    _hover(button)
    tip = next(widget for widget in QApplication.allWidgets() if widget.inherits("QTipLabel") and widget.isVisible())
    assert tip.height() - tip.fontMetrics().height() <= 12
    QToolTip.hideText()
    host.close()


def test_chip_group_can_disable_single_items_with_a_reason(theme):
    group = ChipGroup(columns=3)
    group.set_items([("2x", "2×"), ("4x", "4×"), ("4k", "4K")])
    host = _host(group, 280)
    changes = []
    group.current_changed.connect(changes.append)
    assert all(group.is_item_enabled(key) for key in ("2x", "4x", "4k"))

    group.set_item_enabled("4x", False, "输出尺寸超过处理上限。")
    assert not group.is_item_enabled("4x") and group._chips["4x"].toolTip() == "输出尺寸超过处理上限。"
    QTest.mouseClick(group._chips["4x"], Qt.MouseButton.LeftButton)
    assert changes == [] and group.current() == ""
    QTest.mouseClick(group._chips["2x"], Qt.MouseButton.LeftButton)
    assert changes == ["2x"]

    # 重新启用时原因一并清掉
    group.set_item_enabled("4x", True, "不再适用")
    assert group.is_item_enabled("4x") and group._chips["4x"].toolTip() == ""
    QTest.mouseClick(group._chips["4x"], Qt.MouseButton.LeftButton)
    assert changes == ["2x", "4x"]
    host.close()


def test_toggle_section_shows_its_content_only_when_switched_on(theme):
    from src.ui.widgets.toggle_section import ToggleSection

    section = ToggleSection("降噪", "去掉高感光留下的噪点")
    body = QLabel("强度")
    section.add_widget(body)
    host = _host(section, 300, 160)
    toggles = []
    section.toggled_by_user.connect(toggles.append)
    assert section.title() == "降噪" and section.description() == "去掉高感光留下的噪点"
    assert not section.is_checked() and not section.is_content_visible() and body.isHidden() is False
    assert not body.isVisible() and not section.styleSheet()

    # 程序回填只改显示
    section.set_checked(True)
    QApplication.processEvents()
    assert section.is_checked() and section.is_content_visible() and body.isVisible() and toggles == []
    # 内容与标题左对齐，在开关右侧
    assert body.mapTo(section, QPoint(0, 0)).x() >= section.header.switch.geometry().right()

    # 用户点标题或开关才报告
    QTest.mouseClick(section.header, Qt.MouseButton.LeftButton)
    assert toggles == [False] and not section.is_checked() and not section.is_content_visible()
    section.header.switch.click()
    assert toggles == [False, True] and section.is_content_visible()

    # 保持展开：开关已关但内容仍显示，放开后收起
    section.set_content_held(True)
    section.set_checked(False)
    assert not section.is_checked() and section.is_content_visible() and toggles == [False, True]
    section.set_content_held(False)
    assert not section.is_content_visible()

    # 开关不可用时点标题没有反应，标题与说明仍照常显示
    section.set_switch_enabled(False)
    section.set_description("这张照片太大，现有模型无法放大")
    QTest.mouseClick(section.header, Qt.MouseButton.LeftButton)
    assert not section.is_switch_enabled() and not section.is_checked() and toggles == [False, True]
    assert section.header._title_label.isEnabled() and section.description().startswith("这张照片")
    host.close()


@pytest.mark.parametrize("name", THEMES)
def test_toggle_section_draws_a_hairline_below_itself(theme, name):
    from src.ui.widgets.toggle_section import ToggleSection

    theme.apply(name)
    section = ToggleSection("去模糊", "让虚掉的画面变清晰")
    host = _host(section, 300)
    image = section.grab().toImage()
    assert image.pixelColor(section.width() // 2, section.height() - 1).name() == _token("hairline_solid")
    host.close()
