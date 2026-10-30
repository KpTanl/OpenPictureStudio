"""自由裁切、形状裁切、套索与网格裁切面板：标签、参数行、选区状态与固定在底部的导出。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from src.ui.widgets.chip import ChipGroup  # noqa: E402
from src.ui.widgets.parameter_row import ParameterRow  # noqa: E402
from src.ui.widgets.tool_sidebar import ToolSidebar  # noqa: E402
from src.ui.widgets.value_editing import format_slider_value, parse_slider_value  # noqa: E402
from tests.support import wait_for_panel_source  # noqa: E402


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


@pytest.fixture
def open_panel(theme, language, panel_factory, tmp_path):
    """打开一张 600×400 的照片上的面板，返回面板与它发布过的参数。"""
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (600, 400), (120, 160, 200)).save(path)

    def create(tool_id):
        panel = panel_factory(tool_id, path)
        panel.resize(308, 700)
        panel.show()
        wait_for_panel_source(panel, path)
        changes = []
        panel.params_changed.connect(changes.append)
        return panel, changes

    return create


def _click(chips: ChipGroup, key: str) -> None:
    QTest.mouseClick(chips._chips[key], Qt.MouseButton.LeftButton)


def test_chip_group_can_be_backfilled_to_no_selection(qapp):
    chips = ChipGroup(2)
    chips.set_items([("a", "A"), ("b", "B")])
    emitted = []
    chips.current_changed.connect(emitted.append)
    chips.set_current("a")
    chips.set_current("")
    assert chips.current() == "" and not any(chip.isChecked() for chip in chips._chips.values())
    assert emitted == []
    # 全部不选中之后，点原先选中的那个也算一次选择
    _click(chips, "a")
    assert emitted == ["a"] and chips._chips["a"].isChecked() and not chips._chips["b"].isChecked()
    with pytest.raises(KeyError):
        chips.set_current("missing")
    chips.deleteLater()


def test_pixel_readout_formats_and_parses():
    assert format_slider_value("px", 12) == "12 px"
    assert parse_slider_value("px", "12 px", 0, 50) == 12
    assert parse_slider_value("px", "30PX", 0, 50) == 30
    assert parse_slider_value("px", "80", 0, 50) == 50
    assert parse_slider_value("px", "px", 0, 50) is None


def test_crop_panel_offers_ratios_as_chips_and_reports_the_size(open_panel):
    panel, changes = open_panel("crop")
    chips = panel._ratio_chips
    assert list(chips._chips) == ["free", "1:1", "4:3", "3:2", "16:9", "2:3", "9:16"]
    assert chips._chips["free"].text() == "自由" and chips.current() == "free"
    assert panel._ratio_card.title() == "比例" and panel._size_title.text() == "尺寸"
    assert panel._size_label.text() == "600 × 400" and changes == []
    _click(chips, "1:1")
    assert changes[-1]["aspect_ratio"] == 1.0 and panel._size_label.text() == "600 × 400 → 400 × 400"
    _click(chips, "16:9")
    assert abs(changes[-1]["aspect_ratio"] - 16 / 9) < 1e-9
    # 程序回填不发布；不在预设里的比例不选中任何标签
    count = len(changes)
    panel.restore_params({**panel.parameters, "aspect_ratio": 2.5})
    assert chips.current() == "" and len(changes) == count
    panel.restore_params(panel._default_params())
    assert chips.current() == "free" and panel._size_label.text() == "600 × 400"
    assert "Shift" not in panel._tip_label.text() and panel._tip_label.objectName() == "mutedText"
    assert not panel.styleSheet() and not panel._size_label.styleSheet()


def test_shape_panel_shows_the_radius_only_for_rounded_rectangles(open_panel):
    panel, changes = open_panel("shape_crop")
    chips = panel._shape_chips
    assert [chips._chips[key].text() for key in ("circle", "ellipse", "round_rect", "rect")] == \
        ["圆形", "椭圆", "圆角矩形", "矩形"]
    assert chips.current() == "circle" and panel._radius_row.isHidden()
    _click(chips, "round_rect")
    assert changes[-1]["shape"] == "round_rect" and panel._radius_row.isVisible()
    assert isinstance(panel._radius_row, ParameterRow) and panel._radius_row.label() == "圆角"
    assert (panel._radius_row.slider.minimum(), panel._radius_row.slider.maximum(), panel._radius_row.value()) == (0, 100, 20)
    panel._radius_row.slider.edit_value(45)
    assert changes[-1]["corner_radius"] == 45 and panel.overlay.corner_radius == 45
    # 双击名称恢复默认
    QTest.mouseDClick(panel._radius_row._label, Qt.MouseButton.LeftButton)
    assert changes[-1]["corner_radius"] == 20
    count = len(changes)
    panel.restore_params({**panel.parameters, "shape": "rect", "corner_radius": 60})
    assert chips.current() == "rect" and panel._radius_row.isHidden() and len(changes) == count
    assert panel._radius_row.value() == 60


def test_lasso_panel_says_what_is_selected_and_which_side_is_kept(open_panel):
    panel, changes = open_panel("lasso")
    assert panel._selection_card.title() == "选区" and panel._result_card.title() == "结果"
    assert panel._state_label.text() == "还没有选区" and not panel._clear_button.isEnabled()
    for point in ((0.2, 0.2), (0.6, 0.2), (0.4, 0.7)):
        panel.overlay.add_point(*point)
        panel.overlay.geometry_changed.emit()
    assert panel._state_label.text() == "正在画的轮廓有 3 个点" and panel._clear_button.isEnabled()
    panel.overlay.close_selection()
    assert panel._state_label.text() == "已圈出 1 个区域" and len(changes[-1]["regions"]) == 1
    panel.overlay.add_point(0.1, 0.1)
    panel.overlay.geometry_changed.emit()
    assert panel._state_label.text() == "已圈出 1 个区域，正在画的轮廓有 1 个点"

    assert panel._keep_segments.currentIndex() == 0 and changes[-1]["keep_inside"] is True
    QTest.mouseClick(panel._keep_segments, Qt.MouseButton.LeftButton,
                     pos=panel._keep_segments.rect().center() + panel._keep_segments.rect().center() / 2)
    assert changes[-1]["keep_inside"] is False and panel._keep_segments.currentText() == "去掉选区内"
    assert panel._feather_row.label() == "边缘羽化" and panel._feather_row.value_edit.text() == "0 px"
    panel._feather_row.slider.edit_value(12)
    assert changes[-1]["feather"] == 12 and panel._feather_row.value_edit.text() == "12 px"

    QTest.mouseClick(panel._clear_button, Qt.MouseButton.LeftButton)
    assert changes[-1]["regions"] == [] and changes[-1]["draft_points"] == []
    assert panel._state_label.text() == "还没有选区" and not panel._clear_button.isEnabled()
    # 清空选区不动另外两项
    assert changes[-1]["feather"] == 12 and changes[-1]["keep_inside"] is False
    count = len(changes)
    panel.restore_params(panel._default_params())
    assert panel._keep_segments.currentIndex() == 0 and panel._feather_row.value() == 0 and len(changes) == count


def test_grid_panel_marks_even_presets_and_keeps_the_export_at_the_bottom(open_panel):
    panel, changes = open_panel("grid_crop")
    chips = panel._preset_chips
    assert [chips._chips[key].text() for key in ("3x3", "2x2", "1x3", "3x1")] == ["3 × 3", "2 × 2", "1 × 3", "3 × 1"]
    assert chips.current() == "3x3" and panel._total_label.text() == "3 × 3，共 9 张"
    assert [label.text() for label in (panel._rows_label, panel._cols_label, panel._prefix_label, panel._fmt_label)] == \
        ["行数", "列数", "文件名前缀", "格式"]
    _click(chips, "2x2")
    assert (changes[-1]["rows"], changes[-1]["cols"]) == (2, 2) and panel._total_label.text() == "2 × 2，共 4 张"
    assert panel._rows_spin.value() == 2 and panel._cols_spin.value() == 2
    # 行列不在预设里，或分割线被拖动过，都不选中任何预设
    panel._cols_spin.setValue(4)
    panel._cols_spin.valueEdited.emit(4)
    assert chips.current() == "" and panel._total_label.text() == "2 × 4，共 8 张"
    _click(chips, "2x2")
    panel.overlay.h_lines = [0.3]
    panel.overlay.geometry_changed.emit()
    assert changes[-1]["h_lines"] == [0.3] and chips.current() == ""
    # 再点一次同一个预设，重新等分
    _click(chips, "2x2")
    assert changes[-1]["h_lines"] == [0.5] and chips.current() == "2x2"

    exported = []
    panel.export_tiles = lambda: exported.append(True)
    panel._export_btn.clicked.disconnect()
    panel._export_btn.clicked.connect(panel.export_tiles)
    assert panel._export_btn.text() == "导出切片…"
    sidebar = ToolSidebar()
    sidebar.register_panel("grid_crop", panel)
    sidebar.switch_to_panel("grid_crop")
    sidebar.resize(308, 600)
    sidebar.show()
    # 导出区交给侧栏，固定在滚动区之外
    assert panel._footer.parent() is sidebar._footer_host and panel._footer.isVisible()
    assert not panel.isAncestorOf(panel._export_btn)
    QTest.mouseClick(panel._export_btn, Qt.MouseButton.LeftButton)
    assert exported == [True]
    sidebar.remove_panel("grid_crop")
    sidebar.deleteLater()


@pytest.mark.parametrize("tool_id", ["crop", "shape_crop", "lasso", "grid_crop"])
def test_panels_follow_a_language_switch_without_inline_styles(open_panel, language, tool_id):
    from PySide6.QtWidgets import QLabel, QWidget
    panel, changes = open_panel(tool_id)
    before = [label.text() for label in panel.findChildren(QLabel) if label.text()]
    language.set_language("en")
    panel.retranslate_ui()
    after = [label.text() for label in panel.findChildren(QLabel) if label.text()]
    assert len(after) == len(before) and after != before
    # 文案里不留键名
    assert not any("." in text and " " not in text and "×" not in text for text in after), after
    assert changes == []
    assert not any(widget.styleSheet() for widget in [panel, *panel.findChildren(QWidget)])


def test_canvas_panels_describe_the_result_and_ask_the_host_to_show_it(open_panel, language):
    """三个画布工具底部都是“查看结果”：面板写明将得到什么、做不了的原因，只发出请求，状态由宿主告知。"""
    # 自由裁切：框还是整幅时没有结果可看
    crop, changes = open_panel("crop")
    control, footer = crop.result_view, crop.result_view.footer
    # 没有宿主时不显示这一块
    assert footer.isHidden() and not control.viewing
    asked = []
    control.requested.connect(asked.append)
    assert footer.primary_button.text() == "查看结果" and not footer.primary_button.isEnabled()
    assert footer.summary.text() == "还没有裁切" and footer.status.text() == "拖动边框或选一个比例后可以查看结果"
    assert control.toggle() is False and asked == []
    _click(crop._ratio_chips, "1:1")
    assert footer.summary.text() == "裁切为 400 × 400" and footer.status.text() == ""
    assert footer.primary_button.isEnabled()
    footer.primary_button.click()
    assert asked == [True] and not control.viewing
    # 宿主告知之后才换成“继续调整”；这时面板上的参数照常可改
    control.set_viewing(True)
    assert footer.primary_button.text() == "继续调整" and footer.summary.text() == "正在查看结果"
    _click(crop._ratio_chips, "16:9")
    assert footer.primary_button.text() == "继续调整" and len(changes) == 2
    footer.primary_button.click()
    assert asked == [True, False]
    control.set_viewing(False)
    assert footer.summary.text().startswith("裁切为 ") and footer.primary_button.text() == "查看结果"

    # 形状裁切：没改过参数时点它，先把当前参数发布一次
    shape, changes = open_panel("shape_crop")
    asked = []
    shape.result_view.requested.connect(lambda viewing: asked.append((viewing, len(changes))))
    footer = shape.result_view.footer
    assert footer.summary.text() == "形状：圆形" and footer.primary_button.isEnabled()
    footer.primary_button.click()
    assert asked == [(True, 1)] and changes[0] == shape.parameters
    _click(shape._shape_chips, "ellipse")
    assert footer.summary.text() == "形状：椭圆"
    shape.restore_params({**shape.parameters, "shape": "rect"})
    assert footer.summary.text() == "形状：矩形" and len(changes) == 2

    # 套索：没有可闭合的轮廓时不能看；有三个点以上时先闭合再看
    lasso, changes = open_panel("lasso")
    asked = []
    lasso.result_view.requested.connect(asked.append)
    footer = lasso.result_view.footer
    assert not footer.primary_button.isEnabled() and footer.status.text() == "先在照片上圈出一个区域"
    for point in ((0.2, 0.2), (0.6, 0.2), (0.6, 0.7)):
        lasso.overlay.add_point(*point)
        lasso.overlay.geometry_changed.emit()
    assert footer.summary.text() == "轮廓还没有闭合，查看结果时会先闭合" and footer.primary_button.isEnabled()
    footer.primary_button.click()
    assert asked == [True] and len(lasso.overlay.regions) == 1 and lasso.overlay.draft_points == []
    assert footer.summary.text() == "1 个区域，保留选区内"
    lasso._on_keep_activated(1)
    assert footer.summary.text() == "1 个区域，去掉选区内"

    language.set_language("en")
    for panel in (crop, shape, lasso):
        panel.retranslate_ui()
    assert crop.result_view.footer.primary_button.text() == "View Result"
    assert shape.result_view.footer.summary.text() == "Shape: Rectangle"
    assert lasso.result_view.footer.summary.text() == "1 region(s), Remove inside"


def test_lasso_overlay_keys_only_touch_the_outline_in_progress(open_panel):
    """退格删掉上一个点，Esc 放弃正在画的轮廓；已经闭合的区域不动，没有轮廓时按键不归它管。"""
    panel, changes = open_panel("lasso")
    overlay = panel.overlay
    assert overlay.handle_key_press(Qt.Key.Key_Backspace) is False and overlay.handle_key_press(Qt.Key.Key_Escape) is False
    for point in ((0.1, 0.1), (0.5, 0.1), (0.5, 0.5)):
        overlay.add_point(*point)
    assert overlay.close_selection() is True and overlay.close_selection() is False
    for point in ((0.6, 0.6), (0.8, 0.6), (0.8, 0.8)):
        overlay.add_point(*point)
    overlay.geometry_changed.emit()
    assert overlay.handle_key_press(Qt.Key.Key_Backspace) is True
    assert len(overlay.draft_points) == 2 and [list(p) for p in panel.parameters["draft_points"]] == [[0.6, 0.6], [0.8, 0.6]]
    assert panel._state_label.text() == "已圈出 1 个区域，正在画的轮廓有 2 个点"
    assert overlay.handle_key_press(Qt.Key.Key_Return) is False
    assert overlay.handle_key_press(Qt.Key.Key_Escape) is True
    assert overlay.draft_points == [] and len(overlay.regions) == 1 and panel.parameters["draft_points"] == []
    assert overlay.handle_key_press(Qt.Key.Key_Escape) is False
