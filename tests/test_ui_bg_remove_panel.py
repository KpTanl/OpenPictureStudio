"""移除背景面板：方式、模型只在 AI 抠图时出现、颜色抠图的取色与容差、边缘羽化与背景；另含色块组件。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPalette  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QColorDialog, QLabel, QWidget  # noqa: E402

from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens, to_qcolor  # noqa: E402
from src.ui.widgets.color_swatch import ColorSwatch  # noqa: E402
from src.ui.widgets.parameter_row import ParameterRow  # noqa: E402
from tests.support import wait_for_model_discovery, wait_for_panel_source  # noqa: E402


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
def panel(theme, language, panel_factory, tmp_path):
    """没有安装抠图模型时的移除背景面板。"""
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (160, 120), (120, 160, 200)).save(path)
    widget = panel_factory("bg_remove", path)
    widget.resize(308, 700)
    widget.show()
    wait_for_panel_source(widget, path)
    wait_for_model_discovery(widget.models)
    widget._models_changed()
    changes = []
    widget.params_changed.connect(changes.append)
    return widget, changes


def _select_mode(widget, index):
    control = widget._mode_seg
    x = control.width() * (2 * index + 1) // 4
    QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=control.rect().center().__class__(x, control.height() // 2))


def test_swatch_paints_its_color_with_a_themed_border(theme):
    swatch = ColorSwatch()
    swatch.set_color((38, 160, 90))
    swatch.resize(swatch.sizeHint())
    clicks = []
    swatch.clicked.connect(lambda: clicks.append(True))
    image = QImage(swatch.size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    swatch.render(image)
    center = image.pixelColor(swatch.width() // 2, swatch.height() // 2)
    assert (center.red(), center.green(), center.blue()) == (38, 160, 90)
    border = image.pixelColor(swatch.width() // 2, 1)
    expected = to_qcolor(get_theme_tokens()["hairline_solid"])
    assert abs(border.red() - expected.red()) < 40 and border != QColor(38, 160, 90)
    # 回填颜色不算点击
    swatch.set_color([1, 2, 3])
    assert swatch.color() == (1, 2, 3) and clicks == []
    QTest.mouseClick(swatch, Qt.MouseButton.LeftButton)
    assert clicks == [True]
    assert swatch.focusPolicy() == Qt.FocusPolicy.StrongFocus and not swatch.styleSheet()
    swatch.deleteLater()


def test_panel_shows_only_what_the_chosen_method_needs(panel):
    widget, changes = panel
    assert [card.title() for card in (widget._mode_card, widget._edge_card, widget._bg_card)] == ["方式", "边缘", "背景"]
    assert widget.parameters["mode"] == "ai" and widget._mode_seg.currentText() == "AI 智能抠图"
    # AI 抠图：模型选择在方式一组里，缺模型时说明原因；看不到取色与容差
    selector = widget.model_selector
    assert selector.parentWidget() is widget._ai_box and selector.isVisible()
    assert selector.gap.isVisible() and selector.install.isVisible()
    assert widget._color_box.isHidden() and not widget.models_ready
    _select_mode(widget, 1)
    assert changes[-1]["mode"] == "color" and widget._mode_seg.currentText() == "颜色抠图"
    assert widget._ai_box.isHidden() and widget._color_box.isVisible() and widget.models_ready
    assert widget._color_label.text() == "要去掉的颜色" and widget._color_name.text() == "RGB 255 255 255"
    # 边缘羽化与背景两种方式都在
    assert widget._edge_row.isVisible() and widget._bg_chips.isVisible()
    count = len(changes)
    widget.restore_params({**widget.parameters, "mode": "ai"})
    assert widget._ai_box.isVisible() and widget._color_box.isHidden() and len(changes) == count
    assert selector.gap.isVisible() and not widget.models_ready


def test_color_key_controls_commit_and_backfill_silently(panel, monkeypatch):
    widget, changes = panel
    _select_mode(widget, 1)
    assert isinstance(widget._tolerance_row, ParameterRow) and widget._tolerance_row.label() == "容差"
    assert (widget._tolerance_row.slider.minimum(), widget._tolerance_row.slider.maximum(),
            widget._tolerance_row.value()) == (0, 100, 30)
    widget._tolerance_row.slider.edit_value(55)
    assert changes[-1]["tolerance"] == 55
    assert widget._edge_row.label() == "边缘羽化" and widget._edge_row.value_edit.text() == "2 px"
    widget._edge_row.slider.edit_value(6)
    assert changes[-1]["edge_radius"] == 6 and widget._edge_row.value_edit.text() == "6 px"

    monkeypatch.setattr(QColorDialog, "getColor", lambda *args, **kwargs: QColor(10, 200, 30))
    QTest.mouseClick(widget._color_swatch, Qt.MouseButton.LeftButton)
    assert changes[-1]["target_color"] == [10, 200, 30]
    assert widget._color_swatch.color() == (10, 200, 30) and widget._color_name.text() == "RGB 10 200 30"
    # 取消取色不改参数
    count = len(changes)
    monkeypatch.setattr(QColorDialog, "getColor", lambda *args, **kwargs: QColor())
    QTest.mouseClick(widget._color_swatch, Qt.MouseButton.LeftButton)
    assert len(changes) == count

    assert [widget._bg_chips._chips[key].text() for key in ("transparent", "white", "black")] == ["透明", "白色", "黑色"]
    assert widget._bg_chips.current() == "transparent"
    QTest.mouseClick(widget._bg_chips._chips["white"], Qt.MouseButton.LeftButton)
    assert changes[-1]["bg_type"] == "white"

    # 程序回填不发布
    count = len(changes)
    widget.restore_params({**widget.parameters, "mode": "color", "tolerance": 12, "edge_radius": 0,
                           "target_color": [1, 2, 3], "bg_type": "black"})
    assert len(changes) == count
    assert widget._tolerance_row.value() == 12 and widget._edge_row.value() == 0
    assert widget._color_swatch.color() == (1, 2, 3) and widget._bg_chips.current() == "black"
    widget.reset()
    assert widget._mode_seg.currentIndex() == 0 and widget._bg_chips.current() == "transparent"
    assert widget._tolerance_row.value() == 30 and widget._edge_row.value() == 2


def test_panel_follows_a_language_switch_without_inline_styles(panel, language):
    widget, changes = panel
    _select_mode(widget, 1)
    count = len(changes)
    language.set_language("en")
    widget.retranslate_ui()
    assert widget._mode_card.title() == language.translate("bg_remove.mode")
    assert widget._mode_seg.currentText() == language.translate("bg_remove.mode.color")
    assert widget._tolerance_row.label() == language.translate("bg_remove.tolerance")
    assert widget._bg_chips._chips["white"].text() == language.translate("bg_remove.bg.white")
    texts = [label.text() for label in widget.findChildren(QLabel) if label.text()]
    assert not any(text.startswith("bg_remove.") for text in texts)
    assert len(changes) == count
    assert not any(child.styleSheet() for child in [widget, *widget.findChildren(QWidget)])


def test_switching_back_to_ai_in_the_editor_shows_the_install_entry(theme, language, tmp_path, window_factory):
    """编辑工作区里缺模型时也能切回 AI 抠图，切回后看得到原因与安装入口。"""
    from tests.support import wait_for_studio, wait_for_thumbnails, wait_until
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (160, 120), (120, 160, 200)).save(path)
    window = window_factory()
    window.show()
    window.manager.open_file(path)
    window._refresh_all()
    wait_for_thumbnails(window.thumbnail_panel, [path], path, version=window.manager.list_version)
    studio = wait_for_studio(window, path, "bg_remove")
    widget = studio.tool_sidebar.get_current_panel()
    wait_for_model_discovery(widget.models)
    widget._models_changed()
    _select_mode(widget, 1)
    wait_until(lambda: studio.session.tool_params.get("bg_remove", {}).get("mode") == "color", "切到颜色抠图")
    assert widget._ai_box.isHidden() and studio.action_state().can_apply
    _select_mode(widget, 0)
    wait_until(lambda: studio.session.tool_params.get("bg_remove", {}).get("mode") == "ai", "切回 AI 抠图")
    assert widget.parameters["mode"] == "ai" and widget._ai_box.isVisible()
    assert widget.model_selector.gap.isVisible() and widget.model_selector.install.isVisible()
    assert not studio.action_state().can_apply


def test_render_button_starts_the_cutout_even_when_nothing_was_changed(panel, language):
    """底部的“渲染预览”：没改过参数时点它，也把当前参数记入本次编辑再请求渲染；改参数不自动渲染。"""
    widget, changes = panel
    footer = widget._footer
    widget.external_preview = True
    requests, cancels = [], []
    widget.preview_requested.connect(lambda: requests.append(len(changes)))
    widget.cancel_requested.connect(lambda: cancels.append(True))
    # AI 抠图缺模型：写出原因，不能点
    assert footer.summary.text() == "将执行：AI 智能抠图" and not footer.primary_button.isEnabled()
    assert footer.status.text() == language.translate(
        "preview.blocked", group="AI 智能抠图", reason=language.translate("tool_manager.state.model_missing"))

    _select_mode(widget, 1)
    assert footer.summary.text() == "将执行：颜色抠图" and footer.status.isHidden()
    assert footer.primary_button.isEnabled() and footer.primary_button.text() == "渲染预览"
    # 改参数只发布参数，不请求渲染；正在跑的预览作废
    assert requests == [] and cancels == [True] and changes[-1]["mode"] == "color"

    count, ticket = len(changes), widget.preview_ticket
    footer.primary_button.click()
    # 参数没变也发布一次，发布之后才请求渲染
    assert len(changes) == count + 1 and changes[-1] == changes[-2] and requests == [count + 1]
    assert widget.preview_ticket != ticket
    widget.set_preview_state("processing", widget.preview_ticket)
    assert footer.primary_button.text() == "渲染中…" and footer.cancel_button.isVisible()
    widget.set_preview_state("updated", widget.preview_ticket)
    assert footer.primary_button.text() == "已渲染" and not footer.primary_button.isEnabled()

    # 再改参数：预览过期，按钮重新可点；旧凭据的报告不处理
    stale = widget.preview_ticket
    widget._tolerance_row.slider.edit_value(40)
    assert requests == [count + 1] and footer.primary_button.isEnabled()
    widget.set_preview_state("updated", stale)
    assert footer.primary_button.text() == "渲染预览"
    # 回填同样使预览过期，并且不触发渲染
    widget.set_preview_state("updated", widget.preview_ticket)
    widget.restore_params({**widget.parameters, "tolerance": 12})
    assert footer.primary_button.isEnabled() and requests == [count + 1]


def test_panel_renders_by_itself_when_it_has_no_host(panel):
    """面板自己做宿主时（不在编辑工作区里），点“渲染预览”由它自己渲染。"""
    from tests.support import wait_until
    widget, _ = panel
    _select_mode(widget, 1)
    previews = []
    widget.preview_ready.connect(previews.append)
    QApplication.processEvents()
    assert previews == []
    widget._footer.primary_button.click()
    assert widget._preview.state == "processing"
    wait_until(lambda: widget._preview.state == "updated", "颜色抠图预览")
    assert len(previews) == 1 and not previews[0].isNull()
    assert widget._footer.primary_button.text() == "已渲染"


def test_editor_pins_the_render_button_and_only_reports_tools_that_are_in_the_edit(
        theme, language, tmp_path, window_factory):
    """编辑工作区里：按钮固定在侧栏底部；工具还没进入本次编辑时进入它不算“已渲染”；点了才开始，重置后回到还没开始。"""
    from tests.support import wait_for_studio, wait_for_thumbnails, wait_until
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (160, 120), (255, 255, 255)).save(path)
    window = window_factory()
    window.show()
    window.manager.open_file(path)
    window._refresh_all()
    wait_for_thumbnails(window.thumbnail_panel, [path], path, version=window.manager.list_version)
    studio = wait_for_studio(window, path, "bg_remove")
    widget = studio.tool_sidebar.get_current_panel()
    footer = widget._footer
    assert footer.parent() is studio.tool_sidebar._footer_host and footer.isVisible()

    def idle():
        return studio._preview_queue.active is None and studio._preview_queue.pending is None

    def center_alpha():
        shown = studio.viewer._pixmap.toImage()
        return shown.pixelColor(shown.width() // 2, shown.height() // 2).alpha()

    wait_until(idle, "进入工具时的渲染结束")
    assert widget._preview.state == "not_previewed" and "bg_remove" not in studio.modified_tools()

    _select_mode(widget, 1)
    wait_until(lambda: studio.session.tool_params.get("bg_remove", {}).get("mode") == "color", "切到颜色抠图")
    wait_until(idle, "没有渲染在跑")
    # 改了参数：已记入本次编辑，但没有渲染
    assert widget._preview.state == "not_previewed" and footer.primary_button.isEnabled()
    assert center_alpha() == 255

    footer.primary_button.click()
    wait_until(lambda: widget._preview.state == "updated", "颜色抠图渲染完成")
    assert center_alpha() == 0
    assert footer.primary_button.text() == "已渲染" and "bg_remove" in studio.modified_tools()

    studio.reset_current_tool()
    wait_until(idle, "重置后的渲染结束")
    assert "bg_remove" not in studio.modified_tools() and widget._preview.state == "not_previewed"
    wait_until(lambda: center_alpha() == 255, "画面回到没有抠图")
