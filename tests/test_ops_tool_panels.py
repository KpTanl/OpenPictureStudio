"""工具面板的构造、真实渲染、预览与当前 Studio 入口回归。"""

from __future__ import annotations

import os

import pytest
from PIL import Image
from PySide6.QtCore import Qt

from src.ui.services.ui_thread_relay import UiThreadRelay
from src.core.tools.enhancement import ENHANCE_PARAMETERS, execution_plan
from src.ui.panels.registry import PANEL_TYPES, TOOL_ENTRIES
from tests.support import (
    wait_for_model_discovery, wait_for_panel_source, wait_for_studio,
    wait_for_thumbnails, wait_until,
)


@pytest.fixture
def sample_image(tmp_path):
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (160, 120), color=(120, 160, 200)).save(path)
    return path


def prepare_panel(panel_factory, tool_id, path):
    panel = panel_factory(tool_id, path)
    if tool_id == "bg_remove":
        panel.restore_params({**panel._params, "mode": "color", "target_color": [120, 160, 200]})
    wait_for_panel_source(panel, path)
    return panel


@pytest.mark.parametrize("tool_id", sorted(PANEL_TYPES))
def test_panel_constructs_and_resets(panel_factory, sample_image, tool_id):
    panel = panel_factory(tool_id, sample_image)
    assert panel.image_path == sample_image and panel.TOOL_ID == tool_id
    panel.reset()


@pytest.mark.parametrize(
    "tool_id", ["color_adjust", "beauty", "denoise", "ai_enhance", "bg_remove", "id_photo", "crop", "shape_crop"],
)
def test_panel_apply_returns_image(panel_factory, sample_image, tool_id):
    panel = prepare_panel(panel_factory, tool_id, sample_image)
    result = panel.apply()
    assert isinstance(result, Image.Image), f"{tool_id}.apply() returned {result!r}"
    try:
        assert result.width > 0 and result.height > 0
        if tool_id not in ("id_photo", "shape_crop"):
            assert result.size == (160, 120)
        if tool_id == "shape_crop":
            assert result.size == (112, 84) and result.mode == "RGBA"
            assert result.getchannel("A").getextrema() == (0, 255)
        if tool_id == "bg_remove":
            assert result.mode == "RGBA" and result.getchannel("A").getextrema() == (0, 0)
    finally:
        result.close()


def test_ai_enhance_config_uses_valid_kwargs(panel_factory, sample_image):
    panel = prepare_panel(panel_factory, "ai_enhance", sample_image)
    params = panel.parameters
    assert ENHANCE_PARAMETERS.normalize(params, 3).success
    assert params["target"] == "original" and params["fit"] == "contain"
    for target, size in (("original", (160, 120)), ("2x", (320, 240)), ("4x", (640, 480)), ("4k", (2880, 2160))):
        checked = ENHANCE_PARAMETERS.normalize({**params, "target": target}, 3)
        assert checked.success
        assert execution_plan((160, 120), checked.data, {}).output_size == size
    assert not ENHANCE_PARAMETERS.normalize({**params, "target": "bogus"}, 3).success


def test_ai_enhance_apply_produces_result(panel_factory, sample_image):
    panel = prepare_panel(panel_factory, "ai_enhance", sample_image)
    result = panel.apply()
    assert isinstance(result, Image.Image) and result.size == (160, 120)
    result.close()


def test_denoise_panel_has_run_button(panel_factory, sample_image):
    panel = panel_factory("denoise", sample_image)
    assert panel._run_btn.text()
    emitted = []
    panel.params_changed.connect(emitted.append)
    panel._run_btn.click()
    assert emitted and emitted[-1] == panel._params


@pytest.mark.parametrize("tool_id", ["color_adjust", "beauty", "ai_enhance", "bg_remove", "denoise", "id_photo"])
def test_panel_preview_emits(panel_factory, sample_image, tool_id):
    panel = prepare_panel(panel_factory, tool_id, sample_image)
    panel._preview_timer.stop()
    panel.cancel_preview()
    received = []
    panel.preview_ready.connect(received.append)
    if tool_id == "ai_enhance":
        panel._request_preview()
    else:
        panel._do_update_preview()
    wait_until(lambda: bool(received), f"{tool_id} 的预览结果")
    assert not received[-1].isNull()


def test_neural_preview_requires_user_request(
    window_factory, sample_image, available_neural_models,
):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    from PySide6.QtTest import QTest
    win = window_factory()
    win.show()
    win.manager.open_file(sample_image)
    win._refresh_all()
    studio = wait_for_studio(win, sample_image, "ai_enhance")
    panel = studio.tool_sidebar.get_current_panel()
    wait_for_model_discovery(panel.models)
    token = panel.parameters["deblur_model"]
    wait_until(lambda: any(item.descriptor.key.token == token and item.usable
                           for item in panel.models.list()), "固定去模糊模型就绪")
    panel.external_preview = True
    requested = []
    panel.preview_requested.connect(lambda: requested.append(panel.parameters))
    panel._rows["deblur"].slider.edit_value(20)
    assert requested == [] and panel._preview.state == "not_previewed"
    assert panel._footer.primary_button.isEnabled()
    panel._footer.primary_button.click()
    assert len(requested) == 1 and requested[0]["deblur"] == 20
    assert panel._preview.state == "processing"
    wait_until(lambda: panel._preview.state == "updated", "去模糊预览交付", timeout=30)
    wait_until(lambda: studio.action_state().can_compare, "画质增强原图对比就绪")
    rendered = studio.viewer._pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    QTest.mousePress(win.canvas_toolbar._compare_button, Qt.MouseButton.LeftButton)
    assert studio._comparing
    assert studio.viewer._pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888) == \
        studio._original_qimage.convertToFormat(QImage.Format.Format_RGBA8888)
    QTest.mouseRelease(win.canvas_toolbar._compare_button, Qt.MouseButton.LeftButton)
    assert not studio._comparing
    assert studio.viewer._pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888) == rendered


class TestStudioIntegration:
    """当前工具栏导航与连续编辑经过真实宿主、会话和输出。"""

    def _window(self, sample_image, window_factory):
        win = window_factory()
        win.show()
        win.manager.open_file(sample_image)
        win._refresh_all()
        wait_for_thumbnails(win.thumbnail_panel, [sample_image], sample_image, version=win.manager.list_version)
        wait_for_studio(win, sample_image)
        return win

    def test_every_editor_tool_activates_a_panel(self, sample_image, window_factory):
        win = self._window(sample_image, window_factory)
        studio = win.studio_workspace
        for entry in TOOL_ENTRIES:
            for tool_id in entry.tool_ids:
                wait_for_studio(win, sample_image, tool_id)
                panel = studio.tool_sidebar.get_current_panel()
                assert panel.TOOL_ID == tool_id and not studio.tool_sidebar.isHidden()
                assert studio.tool_rail.current() == entry.entry_id
                assert studio.action_state().can_apply == bool(getattr(panel, "models_ready", True))

    def test_studio_apply_color_adjust(self, sample_image, window_factory):
        from src.ui.dialogs.apply_target_dialog import ApplyTarget
        win = self._window(sample_image, window_factory)
        studio = win.studio_workspace
        panel = studio.tool_sidebar.get_current_panel()
        panel.notify_params_changed({**panel._params, "exposure_amt": 25})
        assert studio.is_dirty()
        applied = []
        studio.applied.connect(applied.append)
        win._apply_service.ask_target = lambda: ApplyTarget.OVERWRITE
        studio.apply_changes()
        wait_until(lambda: applied == [sample_image] and not studio._commit_busy, "调色结果写回")
        assert os.path.isfile(sample_image)

    @pytest.mark.parametrize(
        "tool_id,params",
        [("ai_enhance", {"deblur": 20}), ("bg_remove", {"mode": "color", "tolerance": 40}),
         ("id_photo", {"preset_size": "自定义", "custom_width": 120, "custom_height": 160})],
    )
    def test_one_shot_tool_marks_studio_dirty(self, sample_image, window_factory, tool_id, params):
        win = self._window(sample_image, window_factory)
        studio = wait_for_studio(win, sample_image, tool_id)
        assert not studio.is_dirty()
        panel = studio.tool_sidebar.get_current_panel()
        panel.notify_params_changed({**panel._params, **params})
        assert studio.is_dirty() and tool_id in studio.modified_tools()
        studio.reset_all()
        assert not studio.is_dirty()

    def test_one_shot_ai_enhance_apply_writes_back(
        self, sample_image, window_factory, available_neural_models, monkeypatch,
    ):
        from src.ui.dialogs.apply_target_dialog import ApplyTarget
        win = self._window(sample_image, window_factory)
        studio = wait_for_studio(win, sample_image, "ai_enhance")
        panel = studio.tool_sidebar.get_current_panel()
        wait_for_model_discovery(panel.models)
        token = panel.parameters["deblur_model"]
        wait_until(lambda: any(item.descriptor.key.token == token and item.usable
                               for item in panel.models.list()), "固定去模糊模型就绪")
        panel._rows["deblur"].slider.edit_value(20)
        wait_until(lambda: studio.action_state().can_apply, "画质增强允许应用")
        assert studio.is_dirty()
        applied = []
        errors = []
        monkeypatch.setattr(studio, "_show_core_error", errors.append)
        studio.applied.connect(applied.append)
        win._apply_service.ask_target = lambda: ApplyTarget.OVERWRITE
        studio.apply_changes()
        assert studio._commit_busy and studio._output_tasks
        outputs = []
        task = studio._output_tasks[studio._output_generation]
        relay = UiThreadRelay.shared()
        task.signals.finished.connect(lambda task_id, result: relay.post(lambda: outputs.append(result)),
                                      Qt.ConnectionType.DirectConnection)
        wait_until(lambda: outputs, "真实去模糊输出完成", timeout=30)
        assert outputs[-1].success, outputs[-1].error
        assert not errors, [result.error for result in errors]
        assert applied == [sample_image] and not studio._commit_busy
        with Image.open(sample_image) as result:
            assert result.size == (160, 120) and result.mode == "RGB"

    def test_pipeline_tool_after_one_shot_keeps_working(
        self, sample_image, window_factory, available_neural_models,
    ):
        from src.ui.dialogs.apply_target_dialog import ApplyTarget
        win = self._window(sample_image, window_factory)
        studio = wait_for_studio(win, sample_image, "ai_enhance")
        panel = studio.tool_sidebar.get_current_panel()
        wait_for_model_discovery(panel.models)
        token = panel.parameters["deblur_model"]
        wait_until(lambda: any(item.descriptor.key.token == token and item.usable
                               for item in panel.models.list()), "固定去模糊模型就绪")
        panel._rows["deblur"].slider.edit_value(20)
        studio = wait_for_studio(win, sample_image)
        panel = studio.tool_sidebar.get_current_panel()
        panel.notify_params_changed({**panel._params, "exposure_amt": 15})
        wait_until(lambda: studio.action_state().can_apply, "去模糊与调色允许应用")
        assert {"ai_enhance", "color_adjust"} <= studio.modified_tools()
        applied = []
        studio.applied.connect(applied.append)
        win._apply_service.ask_target = lambda: ApplyTarget.OVERWRITE
        studio.apply_changes()
        assert studio._commit_busy and studio._output_tasks
        outputs = []
        task = studio._output_tasks[studio._output_generation]
        relay = UiThreadRelay.shared()
        task.signals.finished.connect(lambda task_id, result: relay.post(lambda: outputs.append(result)),
                                      Qt.ConnectionType.DirectConnection)
        wait_until(lambda: outputs, "去模糊与调色连续输出", timeout=30)
        assert outputs[-1].success, outputs[-1].error
        assert applied == [sample_image] and not studio._commit_busy
