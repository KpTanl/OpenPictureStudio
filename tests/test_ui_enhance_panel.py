"""画质增强面板：按放大、降噪、去模糊三件事分组，各组的状态、模型未就绪时的限制与“将执行”的说明。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QPalette  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.core.tools.enhancement import (ENHANCE_PARAMETERS, ENHANCE_START_STRENGTHS, TargetOutcome,  # noqa: E402
                                        target_outcomes)
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.panels.registry import create_tool_panel  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from tests.support import wait_for_panel_source, wait_for_model_discovery  # noqa: E402

GROUPS = ("super_resolution", "denoise", "deblur")


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
def panel(theme, language, tmp_path):
    """没有安装任何模型时的面板，源图 160×120。"""
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (160, 120), (120, 160, 200)).save(path)
    widget = create_tool_panel("ai_enhance", path)
    widget.resize(360, 900)
    widget.show()
    wait_for_panel_source(widget, path)
    wait_for_model_discovery(widget.models)
    changes = []
    widget.params_changed.connect(changes.append)
    yield widget, changes
    widget.close_panel()
    widget.deleteLater()


def _install(widget, monkeypatch, groups=GROUPS):
    from dataclasses import replace
    query = widget._query.state

    def state(params, size):
        snapshot = query(params, size)
        return replace(snapshot, features=tuple(
            replace(feature, readiness="ready" if feature.feature_id in groups else "model_missing")
            for feature in snapshot.features))

    monkeypatch.setattr(widget._query, "state", state)
    widget._availability()


def _toggle(widget, group):
    QTest.mouseClick(widget._sections[group].header.switch, Qt.MouseButton.LeftButton)


def test_panel_groups_the_three_jobs_and_explains_missing_models(panel):
    widget, changes = panel
    assert list(widget._sections) == list(GROUPS)
    assert [widget._sections[group].title() for group in GROUPS] == ["放大", "降噪", "去模糊"]
    assert [widget._target_chips._chips[key].text() for key in ("2x", "4x", "4k")] == ["2×", "4×", "4K"]
    assert [widget._rows[key].label() for key in ("luminance", "chroma", "deblur")] == \
        [widget._tr("enhance." + key) for key in ("luminance", "chroma", "deblur")]
    assert all(not section.is_checked() and not section.is_content_visible()
               for section in widget._sections.values())
    assert widget._footer.summary.text() == widget._tr("enhance.summary.none")
    assert not widget._footer.primary_button.isEnabled() and widget._footer.status.isHidden()
    assert widget._fit_row.isHidden() and changes == []
    _toggle(widget, "denoise")
    assert widget._sections["denoise"].is_checked() and widget._sections["denoise"].is_content_visible()
    assert widget._selectors["denoise"].gap.isVisible() and widget._details["denoise"].isHidden()
    assert changes[-1]["luminance"] == 40 and changes[-1]["chroma"] == 30
    assert not widget.models_ready and not widget._footer.primary_button.isEnabled()
    assert not widget._footer.status.isHidden()


def test_upscale_choice_drives_output_size_fit_and_summary(panel, monkeypatch):
    widget, changes = panel
    _install(widget, monkeypatch)
    assert all(not section.is_checked() for section in widget._sections.values())
    assert all(widget._selectors[group].readiness() == "ready" for group in GROUPS)
    assert widget._output_info.isHidden()
    _toggle(widget, "super_resolution")
    assert changes[-1]["target"] == "2x" and len(changes) == 1
    assert widget._sections["super_resolution"].is_checked()
    assert widget._details["super_resolution"].isVisible()
    assert not widget._selectors["super_resolution"].gap.isVisible()
    assert widget._output_info.text() == "160 × 120 → 320 × 240"
    assert widget._footer.summary.text() == "将执行：放大 2×"
    assert widget._fit_row.isHidden() and widget._footer.primary_button.isEnabled()
    QTest.mouseClick(widget._target_chips._chips["4k"], Qt.MouseButton.LeftButton)
    assert changes[-1]["target"] == "4k" and not widget._fit_row.isHidden()
    assert widget._footer.summary.text() == "将执行：放大 4K"
    assert widget._output_info.text() == "160 × 120 → 2880 × 2160"
    _toggle(widget, "super_resolution")
    assert changes[-1]["target"] == "original"
    assert not widget._sections["super_resolution"].is_content_visible()
    assert widget._footer.summary.text() == widget._tr("enhance.summary.none")
    assert not widget._footer.primary_button.isEnabled() and widget._footer.status.isHidden()


def test_strength_rows_edit_parameters_and_restoring_stays_silent(panel, monkeypatch):
    widget, changes = panel
    _install(widget, monkeypatch)
    _toggle(widget, "denoise")
    widget._rows["luminance"].slider.edit_value(45)
    assert changes[-1]["luminance"] == 45 and widget._rows["luminance"].value_edit.text() == "45 %"
    assert widget._sections["denoise"].is_checked() and widget._footer.summary.text() == "将执行：降噪"
    _toggle(widget, "deblur")
    widget._rows["deblur"].slider.edit_value(25)
    assert changes[-1]["deblur"] == 25 and widget._sections["deblur"].is_checked()
    assert widget._footer.summary.text() == "将执行：降噪、去模糊 25 %"
    widget._rows["luminance"].reset()
    assert changes[-1]["luminance"] == 0 and widget._sections["denoise"].is_checked()
    widget._rows["chroma"].reset()
    assert not widget._sections["denoise"].is_checked()
    assert widget._footer.summary.text() == "将执行：去模糊 25 %"
    count = len(changes)
    requested = []
    widget.preview_requested.connect(lambda: requested.append(True))
    widget.restore_params({**widget.parameters, "target": "4x", "chroma": 30, "deblur": 0})
    assert len(changes) == count and requested == []
    assert widget._target_chips.current() == "4x" and widget._rows["chroma"].value() == 30
    assert widget._rows["deblur"].value() == 0 and not widget._sections["deblur"].is_checked()
    assert widget._footer.summary.text() == "将执行：放大 4×、降噪"


def test_missing_model_blocks_new_work_but_lets_existing_strength_be_lowered(panel, monkeypatch):
    widget, changes = panel
    _install(widget, monkeypatch, groups=("super_resolution", "deblur"))
    ready = []
    widget.availability_changed.connect(lambda: ready.append(widget.models_ready))
    widget.restore_params({**widget.parameters, "luminance": 35})
    assert widget._sections["denoise"].is_checked() and widget._sections["denoise"].is_content_visible()
    assert widget._selectors["denoise"].gap.isVisible() and widget._details["denoise"].isHidden()
    assert ready == [False] and not widget._footer.primary_button.isEnabled()
    assert not widget._footer.status.isHidden() and "降噪" in widget._footer.status.text()
    assert changes == []
    _toggle(widget, "denoise")
    assert changes[-1]["luminance"] == 0 and changes[-1]["chroma"] == 0
    assert ready == [False, True] and not widget._sections["denoise"].is_checked()
    assert widget._footer.status.isHidden()


def test_panel_retranslates_in_place(panel, language, monkeypatch):
    widget, changes = panel
    _install(widget, monkeypatch)
    _toggle(widget, "super_resolution")
    count = len(changes)
    language.set_language("en")
    QApplication.processEvents()
    assert [widget._sections[group].title() for group in GROUPS] == \
        [language.translate("enhance.group." + group) for group in GROUPS]
    assert widget._step_select.text() == language.translate("enhance.step.select")
    assert widget._footer.title.text() == language.translate("enhance.step.render")
    assert widget._footer.primary_button.text() == language.translate("preview.render")
    assert widget._footer.summary.text() == language.translate(
        "preview.summary", steps=language.translate("enhance.summary.upscale", target="2×"))
    assert widget._target_chips.current() == "2x" and len(changes) == count


def test_target_outcomes_tell_which_sizes_a_photo_can_take():
    from types import SimpleNamespace
    params = ENHANCE_PARAMETERS.defaults(3)
    model = {"super_resolution": SimpleNamespace(scale=4, tile=256, overlap=32)}

    def banded(width, height, factor):
        """分带放大的估算：输出每像素 24 字节，两个行带各每像素 48 字节，源图缓冲的 4 倍。"""
        return width * factor * height * factor * 24 + 192 * 4 * width * 4 * 2 * 48 + width * height * 12 * 4

    assert target_outcomes((3000, 2000), params, model) == (
        TargetOutcome("2x", (6000, 4000), True), TargetOutcome("4x", (12000, 8000), True),
        TargetOutcome("4k", (3240, 2160), True))
    # 输出多大没有固定上限：不给内存上限时都可以，4K 对这张是缩小
    assert target_outcomes((4800, 2700), params, model) == (
        TargetOutcome("2x", (9600, 5400), True),
        TargetOutcome("4x", (19200, 10800), True),
        TargetOutcome("4k", (3840, 2160), False))
    # 所需内存超过上限的目标做不了，并带上估算的用量；缩小不受影响。2× 与 4× 分带处理，用量按输出算
    gib = 1024 ** 3
    limited = target_outcomes((4800, 2700), params, model, 4 * gib)
    assert limited == (
        TargetOutcome("2x", (9600, 5400), True),
        TargetOutcome("4x", None, False, "error.enhance.upscale_source"),
        TargetOutcome("4k", (3840, 2160), False))
    assert limited[0].memory_bytes == banded(4800, 2700, 2) < 4 * gib < limited[1].memory_bytes == banded(4800, 2700, 4)
    assert limited[2].memory_bytes == 0
    assert all(item.error for item in target_outcomes((4800, 2700), params, model, 2 * gib) if item.target != "4k")
    # 4K 对小图是放大，缩放比不是整数，仍要先得到完整的 4 倍结果，用量按它算
    small = target_outcomes((3000, 2000), params, model, 4 * gib)
    assert small == (
        TargetOutcome("2x", (6000, 4000), True), TargetOutcome("4x", (12000, 8000), True),
        TargetOutcome("4k", None, False, "error.enhance.upscale_source"))
    assert small[2].memory_bytes == 3000 * 2000 * (16 * 48 + 12 * 4)
    assert all(not item.error for item in target_outcomes((3000, 2000), params, model, 8 * gib))
    assert ENHANCE_START_STRENGTHS == {"denoise": {"luminance": 40, "chroma": 30}, "deblur": {"deblur": 50}}


def test_panel_names_the_two_steps_and_says_what_each_job_does(panel, language):
    widget, _changes = panel
    assert widget._step_select.text() == "1  选择要做的处理" and widget._footer.title.text() == "2  渲染预览"
    assert [widget._sections[group].description() for group in GROUPS] == [
        "把照片变大，细节由模型补出来", "去掉高感光留下的噪点", "让虚掉的画面变清晰"]
    assert widget._footer.summary.text() == "先在上面打开至少一种处理"
    assert widget._footer.primary_button.text() == "渲染预览" and widget._footer.cancel_button.isHidden()
    assert not widget._info_card.is_expanded()
    # 模型的管理不在每一行常驻
    assert all(not hasattr(selector, "manage") for selector in widget._selectors.values())


def test_group_dragged_to_zero_closes_after_the_drag_ends(panel, monkeypatch):
    widget, changes = panel
    _install(widget, monkeypatch)
    _toggle(widget, "deblur")
    slider = widget._rows["deblur"].slider
    slider.setSliderDown(True)
    slider.edit_value(0)
    assert changes[-1]["deblur"] == 0 and not widget._sections["deblur"].is_checked()
    assert widget._sections["deblur"].is_content_visible()
    slider.setSliderDown(False)
    assert not widget._sections["deblur"].is_content_visible()
    assert widget._footer.summary.text() == "先在上面打开至少一种处理"


def test_photo_too_large_to_enlarge_says_so_before_any_click(theme, language, tmp_path, monkeypatch):
    path = str(tmp_path / "large.png")
    Image.new("RGB", (4800, 2700), (120, 160, 200)).save(path)
    widget = create_tool_panel("ai_enhance", path)
    widget.resize(360, 900)
    widget.show()
    try:
        wait_for_panel_source(widget, path)
        wait_for_model_discovery(widget.models)
        changes = []
        widget.params_changed.connect(changes.append)
        # 面板用的上限就是工具运行时公布的内存上限；运行中把它调到 2 GiB，让两个放大目标都做不了
        assert widget._state.memory_limit == widget._render_service.runtime.memory_limit == 4 * 1024 ** 3
        widget._image_source_service.update_memory(memory_bytes=2 * 1024 ** 3, memory_seat_bytes=2 * 1024 ** 3)
        widget.on_memory_budget_changed()
        assert widget._state.memory_limit == 2 * 1024 ** 3 and changes == []
        _install(widget, monkeypatch)
        section = widget._sections["super_resolution"]
        assert not section.is_switch_enabled() and not section.is_checked()
        assert section.description() == "这张照片 4800 × 2700 太大，这台电脑的内存不足以放大"
        QTest.mouseClick(section.header, Qt.MouseButton.LeftButton)
        assert changes == [] and not section.is_checked()
        # 其余两项照常可用
        _toggle(widget, "deblur")
        assert widget._footer.summary.text() == "将执行：去模糊 50 %" and widget._footer.primary_button.isEnabled()

        # 已经存着“4K”的参数照常执行，并如实写成缩小；另外两个目标仍不可选
        widget.restore_params({**widget.parameters, "target": "4k", "deblur": 0})
        assert section.is_checked() and section.is_switch_enabled()
        assert section.description() == language.translate("enhance.desc.super_resolution")
        assert widget._target_chips.is_item_enabled("4k")
        assert not widget._target_chips.is_item_enabled("2x") and not widget._target_chips.is_item_enabled("4x")
        assert widget._target_chips._chips["2x"].toolTip() == language.translate("error.enhance.upscale_source")
        # 做不了的目标把所需内存与这台电脑的上限写出来
        def need(factor):
            size = 4800 * factor * 2700 * factor * 24 + 192 * 4 * 4800 * 4 * 2 * 48 + 4800 * 2700 * 12 * 4
            return f"{size / 1024 ** 3:.1f}"

        assert widget._memory_info.isVisible()
        assert widget._memory_info.text() == \
            f"2× 约需 {need(2)} GB、4× 约需 {need(4)} GB 内存，超过这台电脑可用于处理的 2.0 GB"
        assert widget._output_info.text() == "4800 × 2700 → 3840 × 2160（缩小）"
        assert widget._footer.summary.text() == "将执行：缩小到 3840 × 2160"
    finally:
        widget.close_panel()
        widget.deleteLater()


def test_missing_model_gives_an_install_entry_next_to_the_reason(panel, monkeypatch):
    widget, _changes = panel
    opened = []
    widget.model_manager_requested.connect(lambda capability, token: opened.append(capability))
    _toggle(widget, "denoise")
    selector = widget._selectors["denoise"]
    note, install = selector.reason, selector.install
    assert note.text() == "需要先安装这个模型" and not install.isHidden() and install.text() == "安装模型…"
    assert widget._footer.status.text() == "“降噪”还不能用：需要下载模型"
    install.click()
    assert opened == ["denoise"]
    # 装好后原位换成强度，底部可以渲染
    _install(widget, monkeypatch, groups=("denoise",))
    assert selector.gap.isHidden() and not widget._details["denoise"].isHidden()
    assert widget._footer.status.isHidden() and widget._footer.primary_button.isEnabled()


def test_render_button_follows_the_preview_state(panel, monkeypatch):
    widget, _changes = panel
    _install(widget, monkeypatch)
    widget.external_preview = True
    requests, cancels = [], []
    widget.preview_requested.connect(lambda: requests.append(True))
    widget.cancel_requested.connect(lambda: cancels.append(True))
    _toggle(widget, "deblur")
    cancels.clear()

    # 点击只发出请求；任务开始由宿主报告
    widget._footer.primary_button.click()
    assert requests == [True] and widget._footer.summary.text() == "将执行：去模糊 50 %"
    first = widget.preview_ticket
    widget.set_preview_state("processing", first)
    assert widget._footer.summary.text() == "正在渲染：去模糊 50 %…"
    assert not widget._footer.primary_button.isEnabled() and widget._footer.primary_button.text() == "渲染中…"
    assert not widget._footer.cancel_button.isHidden() and widget._footer.cancel_button.isEnabled()

    widget.set_preview_state("updated", first)
    assert widget._footer.summary.text() == "预览已是最新：去模糊 50 %"
    assert not widget._footer.primary_button.isEnabled() and widget._footer.primary_button.text() == "已渲染"
    assert widget._footer.cancel_button.isHidden()

    # 改了参数就回到可以渲染，凭据随之更换，旧凭据的报告不再处理
    widget._rows["deblur"].slider.edit_value(30)
    assert widget._footer.summary.text() == "将执行：去模糊 30 %" and widget._footer.primary_button.isEnabled()
    assert widget._footer.primary_button.text() == "渲染预览" and cancels == [True]
    assert widget.preview_ticket != first
    widget.set_preview_state("updated", first)
    widget.set_preview_state("stopped", first)
    assert widget._footer.summary.text() == "将执行：去模糊 30 %" and widget._footer.status.isHidden()

    # 取消：先显示“正在取消”，宿主报告任务停下后才是“已取消”
    second = widget.preview_ticket
    widget.set_preview_state("processing", second)
    widget._footer.cancel_button.click()
    assert cancels == [True, True] and widget._footer.summary.text() == "正在取消：去模糊 30 %…"
    assert not widget._footer.cancel_button.isHidden() and not widget._footer.cancel_button.isEnabled()
    assert not widget._footer.primary_button.isEnabled()
    widget.set_preview_state("stopped", second)
    assert widget._footer.cancel_button.isHidden() and widget._footer.primary_button.isEnabled()
    assert widget._footer.status.text() == widget._tr("preview.cancelled") and not widget._footer.status.isHidden()
    # 已经停下后再来一次 stopped 不改变状态
    widget.set_preview_state("stopped", second)
    assert widget._footer.status.text() == widget._tr("preview.cancelled")

    # 不是用户取消的中断回到尚未预览，不显示“已取消”
    widget.set_preview_state("processing", second)
    widget.set_preview_state("stopped", second)
    assert widget._footer.summary.text() == "将执行：去模糊 30 %" and widget._footer.status.isHidden()
    assert widget._footer.primary_button.isEnabled()

    widget.set_preview_state("failed", second)
    assert widget._footer.status.text() == widget._tr("preview.failed") and widget._footer.status.objectName() == "statusError"
    assert widget._footer.primary_button.isEnabled()

    # 回填参数也会更换凭据
    widget.restore_params(widget.parameters)
    assert widget.preview_ticket != second and widget._footer.status.isHidden()


def test_panel_hosting_its_own_preview_sets_the_state_itself(panel, monkeypatch):
    widget, _changes = panel
    _install(widget, monkeypatch)
    started = []
    monkeypatch.setattr(widget, "preview_panel_image", lambda: started.append(True))
    cancelled = []
    monkeypatch.setattr(widget, "cancel_preview", lambda: cancelled.append(True))
    _toggle(widget, "deblur")
    cancelled.clear()
    assert not widget.external_preview
    widget._footer.primary_button.click()
    assert started == [True] and widget._footer.summary.text() == "正在渲染：去模糊 50 %…"
    widget._footer.cancel_button.click()
    assert cancelled == [True] and widget._footer.status.text() == widget._tr("preview.cancelled")
    assert widget._footer.primary_button.isEnabled()


def test_reset_turns_every_job_off(panel, monkeypatch):
    widget, changes = panel
    _install(widget, monkeypatch)
    for group in GROUPS:
        _toggle(widget, group)
    assert all(widget._sections[group].is_checked() for group in GROUPS)
    widget.reset()
    assert changes[-1] == widget._default_model_params()
    assert not any(widget._sections[group].is_checked() or widget._sections[group].is_content_visible()
                   for group in GROUPS)


def test_footer_can_be_handed_to_the_host(panel):
    widget, _changes = panel
    footer = widget._footer
    assert widget._root_layout.indexOf(footer) >= 0
    assert widget.take_footer() is footer and widget._root_layout.indexOf(footer) < 0
    assert widget._footer.primary_button.parentWidget() is footer and widget._footer.summary.parentWidget() is footer


def test_state_query_reuses_readiness_until_models_or_selection_change(panel, monkeypatch):
    widget, _changes = panel
    query, params = widget._query, widget.parameters
    calls = []
    overview = query._readiness._resources.overview
    monkeypatch.setattr(query._readiness._resources, "overview", lambda context: calls.append(context) or overview(context))
    query.refresh()

    state = query.state(params, (160, 120))
    assert len(calls) == 1 and not state.active and state.ready and state.can_enlarge
    assert [(item.feature_id, item.active, item.readiness) for item in state.features] == [
        ("denoise", False, "model_missing"), ("deblur", False, "model_missing"),
        ("super_resolution", False, "model_missing")]
    assert state.current_target is None and state.blocked_feature is None

    # 强度不影响资源是否就绪：不重新查询
    busy = query.state({**params, "luminance": 30, "target": "2x"}, (160, 120))
    assert len(calls) == 1 and busy.active and not busy.ready
    assert busy.blocked_feature.feature_id == "denoise" and busy.current_target.output_size == (320, 240)

    query.state({**params, "denoise_model": "another@1"}, (160, 120))
    assert len(calls) == 2
    query.refresh()
    query.state({**params, "denoise_model": "another@1"}, (160, 120))
    assert len(calls) == 3
    # 还不知道源图尺寸时没有目标可判断
    assert query.state(params, (0, 0)).targets == ()
