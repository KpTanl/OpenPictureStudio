"""编辑栈与历史列表：只显示交给它的内容，用户操作发出请求，程序回填不发出；参数变化写成的文字与面板一致。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QPalette  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.application.edit_changes import ParameterChange, StageChange  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.edit_descriptions import changes_summary, parameter_name, stage_title, step_text, value_text  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.ui.widgets.edit_stack import EditStackView, StackRow  # noqa: E402
from src.ui.widgets.history_list import HistoryList, HistoryStep  # noqa: E402
from src.ui.widgets.history_panel import HistoryPanel  # noqa: E402
from src.ui.widgets.tool_sidebar import ToolSidebar  # noqa: E402

THEMES = ("dark", "light", "beige")
STEPS = (HistoryStep("r0", "打开 DSC_6674.NEF", "18:42"), HistoryStep("r1", "调色 · 曝光 +0.35", "18:45"),
         HistoryStep("r2", "调色 · 阴影 +32", "18:47", "current"), HistoryStep("r3", "调色 · 色温 +22", "已撤销", "undone"))
ROWS = (StackRow("color_adjust", "调色", "4 项调整", "adjustments-horizontal", current=True),
        StackRow("ai_enhance", "画质增强", "亮度降噪 30 %", "sparkles"),
        StackRow("crop", "裁切", "1 项调整", "crop"))


@pytest.fixture
def theme(qapp):
    previous = qapp.styleSheet(), qapp.property("ops.theme"), QPalette(qapp.palette())
    manager = ThemeManager()
    manager.apply("dark")
    yield manager
    qapp.setStyleSheet(previous[0])
    qapp.setProperty("ops.theme", previous[1])
    qapp.setPalette(previous[2])


@pytest.fixture
def language(qapp):
    previous = get_translation_service()
    service = TranslationService(default_language="zh-CN", fallback_language="en")
    set_translation_service(service)
    yield service
    set_translation_service(previous)


def _shown(widget, width=300, height=None):
    widget.resize(width, height or max(widget.sizeHint().height(), 40))
    widget.show()
    QApplication.processEvents()
    return widget


def _recorded(signal):
    received = []
    signal.connect(received.append)
    return received


def _row_center(view: HistoryList, row: int) -> QPoint:
    return view._row_rect(row).center()


def _token(key):
    return QColor(get_theme_tokens()[key]).name()


# ---------------------------------------------------------------------------
# 历史列表
# ---------------------------------------------------------------------------
def test_history_list_shows_the_newest_step_on_top_and_reports_clicks(theme):
    view = HistoryList()
    requested = _recorded(view.step_requested)
    view.set_steps(STEPS)
    _shown(view)
    assert view.steps() == STEPS and not requested
    assert view.height() == view.sizeHint().height() == 4 * view._row_rect(0).height()
    # 第 0 行是最新的一步（已撤销的那一步），当前这一步在第 1 行
    assert view._step_at(0).key == "r3" and view.current_rect() == view._row_rect(1)

    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=_row_center(view, 3))
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=_row_center(view, 0))
    assert requested == ["r0", "r3"]
    # 当前这一步点了不发出；按下后移到别的行再松开也不发出
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=_row_center(view, 1))
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=_row_center(view, 2))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=_row_center(view, 3))
    assert requested == ["r0", "r3"]
    view.close()


def test_history_list_keyboard_moves_a_cursor_and_activates_on_return(theme):
    view = HistoryList()
    requested = _recorded(view.step_requested)
    view.set_steps(STEPS)
    _shown(view)
    view.setFocus()
    assert view._cursor == 1
    # 方向键只移动光标，回车或空格才回到那一步
    QTest.keyClick(view, Qt.Key.Key_Down)
    QTest.keyClick(view, Qt.Key.Key_Down)
    assert view._cursor == 3 and not requested
    QTest.keyClick(view, Qt.Key.Key_Return)
    QTest.keyClick(view, Qt.Key.Key_Down)
    assert view._cursor == 3 and requested == ["r0"]
    for _ in range(5):
        QTest.keyClick(view, Qt.Key.Key_Up)
    QTest.keyClick(view, Qt.Key.Key_Space)
    assert view._cursor == 0 and requested == ["r0", "r3"]
    # 重新回填后光标回到当前这一步
    view.set_steps(STEPS[:3])
    assert view._cursor == 0 and view.current_rect() == view._row_rect(0)
    view.close()


@pytest.mark.parametrize("name", THEMES)
def test_history_list_marks_the_current_step_and_dims_undone_ones(theme, name):
    theme.apply(name)
    view = HistoryList()
    view.set_steps(STEPS)
    _shown(view)
    image = view.grab().toImage()
    current, undone, past = (view._row_rect(row) for row in (1, 0, 2))
    # 当前这一步垫着底色，其余行是面板本身的颜色
    assert image.pixelColor(current.right() - 30, current.top() + 4).name() == _token("control")
    assert image.pixelColor(past.right() - 30, past.top() + 4).name() != _token("control")
    dot_x = view._row_rect(0).left() + 12 + 4
    assert image.pixelColor(dot_x, current.center().y()).name() == _token("accent")
    # 已撤销的一步是空心圆点
    assert image.pixelColor(dot_x, undone.center().y()).name() == _token("surface")
    view.close()


def test_history_step_rejects_an_unknown_state():
    with pytest.raises(ValueError):
        HistoryStep("r", "text", state="redo")


def test_history_panel_switches_between_the_list_and_an_empty_state(theme):
    panel = HistoryPanel()
    panel.set_texts("历史", "点任意一步回到当时的状态；撤销和重做在顶栏。", "还没有历史", "打开一张照片开始编辑。")
    requested = _recorded(panel.step_requested)
    _shown(panel, 308, 500)
    assert panel.is_empty() and panel._hint.isHidden()
    panel.set_steps(STEPS)
    QApplication.processEvents()
    assert not panel.is_empty() and panel.steps() == STEPS and panel._hint.isVisible()
    QTest.mouseClick(panel.list, Qt.MouseButton.LeftButton, pos=_row_center(panel.list, 3))
    assert requested == ["r0"]
    panel.set_steps(())
    assert panel.is_empty() and panel._hint.isHidden()
    panel.close()


def test_history_panel_scrolls_the_current_step_into_view(theme):
    panel = HistoryPanel()
    panel.set_texts("历史", "说明", "还没有历史")
    _shown(panel, 308, 260)
    steps = [HistoryStep(f"r{index}", f"第 {index} 步", state="current" if index == 2 else "undone" if index > 2 else "past")
             for index in range(40)]
    panel.set_steps(steps)
    for _ in range(5):
        QApplication.processEvents()
    viewport = panel._scroll.viewport()
    current = panel.list.current_rect()
    top = panel.list.mapTo(viewport, current.topLeft()).y()
    assert 0 <= top and top + current.height() <= viewport.height()
    assert panel._scroll.verticalScrollBar().value() > 0
    panel.close()


# ---------------------------------------------------------------------------
# 编辑栈
# ---------------------------------------------------------------------------
def test_edit_stack_lists_rows_and_reports_requests(theme):
    view = EditStackView()
    view.set_texts("编辑栈", "还没有调整。", "重置这一步")
    stages, resets = _recorded(view.stage_requested), _recorded(view.reset_requested)
    view.set_rows(ROWS)
    _shown(view, 308)
    assert view.rows() == ROWS and not stages and not resets
    assert view._empty.isHidden()
    widgets = view.row_widgets()
    assert [widget.y() for widget in widgets] == sorted(widget.y() for widget in widgets)

    QTest.mouseClick(widgets[2], Qt.MouseButton.LeftButton, pos=QPoint(40, 10))
    QTest.mouseClick(widgets[1].reset_button, Qt.MouseButton.LeftButton)
    assert stages == ["crop"] and resets == ["ai_enhance"]
    # 行可以用键盘聚焦，回车等同点击
    widgets[0].setFocus()
    QTest.keyClick(widgets[0], Qt.Key.Key_Return)
    assert stages == ["crop", "color_adjust"]
    assert all(widget.reset_button.toolTip() == "重置这一步" for widget in widgets)
    view.close()


def test_edit_stack_updates_rows_in_place_and_shows_a_hint_when_empty(theme):
    view = EditStackView()
    view.set_texts("编辑栈", "还没有调整。", "重置这一步")
    _shown(view, 308)
    assert not view.rows() and view._empty.isVisible() and view._empty.text() == "还没有调整。"
    view.set_rows(ROWS)
    first = view.row_widgets()
    # 行数不变时沿用原来的行
    changed = (ROWS[0], StackRow("ai_enhance", "画质增强", "亮度降噪 45 %", "sparkles", current=True), ROWS[2])
    view.set_rows(changed)
    assert view.row_widgets() == first and view.rows() == changed
    view.set_rows((StackRow("crop", "裁切", resettable=False),))
    QApplication.processEvents()
    assert len(view.row_widgets()) == 1 and view.row_widgets()[0] is first[0]
    assert view.row_widgets()[0].reset_button.isHidden()
    # 收起后各行不显示，标题还在
    view.set_expanded(False)
    assert not view.is_expanded() and not view.row_widgets()[0].isVisible()
    view.close()


@pytest.mark.parametrize("name", THEMES)
def test_edit_stack_marks_the_row_of_the_tool_being_edited(theme, name):
    theme.apply(name)
    view = EditStackView()
    view.set_texts("编辑栈", "", "重置")
    view.set_rows(ROWS)
    _shown(view, 308)
    current, other = view.row_widgets()[0], view.row_widgets()[1]
    assert current.grab().toImage().pixelColor(120, 6).name() == _token("control")
    assert other.grab().toImage().pixelColor(120, 6).name() != _token("control")
    view.close()


def test_tool_sidebar_hosts_the_edit_stack_below_the_panels(theme, language):
    sidebar = ToolSidebar()
    tools, resets = _recorded(sidebar.tool_requested), _recorded(sidebar.stage_reset_requested)
    sidebar.set_edit_stack(ROWS)
    _shown(sidebar, 308, 700)
    stack = sidebar.edit_stack
    assert stack.rows() == ROWS and not tools and not resets
    assert stack._card.title() == language.translate("edit_stack.title")
    # 在滚动视口下面、底部操作区上面
    assert stack.y() >= sidebar._scroll_area.y() + sidebar._scroll_area.height()
    QTest.mouseClick(stack.row_widgets()[2], Qt.MouseButton.LeftButton, pos=QPoint(40, 10))
    QTest.mouseClick(stack.row_widgets()[0].reset_button, Qt.MouseButton.LeftButton)
    assert tools == ["crop"] and resets == ["color_adjust"]
    language.set_language("en")
    assert stack._card.title() == language.translate("edit_stack.title") == "Edit stack"
    sidebar.close()


# ---------------------------------------------------------------------------
# 把参数的变化写成文字
# ---------------------------------------------------------------------------
def test_descriptions_take_names_and_formats_from_the_tool_panels(language):
    tr = language.translate
    assert stage_title("color_adjust", tr) == "调色实验室" and stage_title("crop", tr) == "裁切"
    assert stage_title("plugin.unknown", tr) == "plugin.unknown"
    assert parameter_name("color_adjust", "shadows", tr) == "阴影"
    assert parameter_name("color_adjust", "color_mixer", tr) == "色彩混合"
    assert parameter_name("beauty", "skin_smooth", tr) == language.translate("beauty.skin.smooth")
    assert parameter_name("color_adjust", "unknown", tr) == "" and parameter_name("plugin.unknown", "x", tr) == ""
    # 读数的写法与面板一致
    assert value_text("color_adjust", "exposure_amt", 35) == "+0.35"
    assert value_text("color_adjust", "shadows", 32.0) == "+32" and value_text("color_adjust", "shadows", 0) == "0"
    assert value_text("ai_enhance", "luminance", 30) == "30 %" and value_text("lasso", "feather", 4) == "4 px"
    assert value_text("color_adjust", "color_mixer", {"red": {"hue": 3}}) == ""
    assert value_text("color_adjust", "look", "ops_base") == "" and value_text("beauty", "skin_smooth", True) == ""


def test_summary_names_a_single_change_and_counts_several(language):
    tr = language.translate
    assert changes_summary("color_adjust", (), tr) == ""
    assert changes_summary("color_adjust", (ParameterChange("shadows", 0, 32),), tr) == "阴影 +32"
    assert changes_summary("color_adjust", (ParameterChange("curves", None, {"rgb": []}),), tr) == "色调曲线"
    assert changes_summary("color_adjust", (ParameterChange("masks", None, [{}]),), tr) == "1 项调整"
    several = (ParameterChange("shadows", 0, 32), ParameterChange("highlights", 0, -20), ParameterChange("tint", 0, 4))
    assert changes_summary("color_adjust", several, tr) == "3 项调整"


def test_nested_changes_name_the_channel_color_and_component(language):
    tr = language.translate
    assert parameter_name("color_adjust", "curves.rgb", tr) == "色调曲线 · RGB"
    assert parameter_name("color_adjust", "curves.g", tr) == "色调曲线 · 绿"
    assert parameter_name("color_adjust", "color_mixer.orange.sat", tr) == "色彩混合 · 橙 · 饱和度"
    assert parameter_name("color_adjust", "color_mixer.teal.sat", tr) == ""
    assert value_text("color_adjust", "color_mixer.orange.sat", -12) == "-12"
    assert value_text("color_adjust", "curves.rgb", [[0, 8], [255, 250]]) == ""
    assert changes_summary("color_adjust", (ParameterChange("color_mixer.orange.sat", 0, -12),), tr) \
        == "色彩混合 · 橙 · 饱和度 -12"
    assert changes_summary("color_adjust", (ParameterChange("curves.r", None, [[0, 20], [255, 235]]),), tr) \
        == "色调曲线 · 红"
    language.set_language("en")
    assert parameter_name("color_adjust", "color_mixer.orange.sat", tr) == "Color Mixer · Orange · Saturation"
    language.set_language("fr")
    assert parameter_name("color_adjust", "curves.b", tr).count(" · ") == 1


def test_step_text_describes_what_a_history_step_changed(language):
    tr = language.translate
    assert step_text((), tr) == ""
    changed = StageChange("color_adjust", "changed", (ParameterChange("shadows", 0, 32),))
    assert step_text((changed,), tr) == "调色实验室 · 阴影 +32"
    assert step_text((StageChange("crop", "removed", (ParameterChange("crop_rect", [0, 0, 1, 1], None),)),), tr) \
        == "裁切 · 已重置"
    # 以默认参数加入的一步只写名称
    assert step_text((StageChange("bg_remove", "added", ()),), tr) == "移除背景"
    assert step_text((changed, StageChange("crop", "added", ())), tr) == "调色实验室、裁切"
    language.set_language("en")
    assert step_text((changed,), tr) == "Color Lab · Shadows +32"
