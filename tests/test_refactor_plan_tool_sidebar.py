"""工具参数容器、覆盖层及双工作区编辑流程回归。"""

from __future__ import annotations

import os
from pathlib import Path
from PIL import Image
import pytest
from PySide6.QtCore import Qt, QPointF, QRect, QPoint
from PySide6.QtGui import QMouseEvent, QImage
from PySide6.QtWidgets import QApplication, QFileDialog

from src.ui.services.ui_thread_relay import UiThreadRelay
from src.core.edit_state import EditStateStore
from src.ui.dialogs.apply_target_dialog import ApplyTarget
from src.ui.widgets.tool_sidebar import ToolSidebar
from src.ui.panels import BaseToolPanel, GridCropPanel
from src.ui.panels.registry import TOOL_ENTRIES
from tests.support import wait_for_studio, wait_for_thumbnails, wait_for_panel_source, wait_until
from src.ui.overlays import (
    CropOverlay,
    GridCropOverlay,
    ShapeCropOverlay,
    LassoOverlay,
)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        app = QApplication([])
    return app


@pytest.fixture
def sample_image(tmp_path):
    path = tmp_path / "photo.png"
    Image.new("RGB", (400, 300), (120, 160, 200)).save(path)
    return str(path)


class TestEditStateStore:
    """测试 EditStateStore 双层暂存系统（当前图片未应用暂存 + 跨图片记忆）。"""

    def test_initial_state(self):
        store = EditStateStore()
        assert store.has_pending("/fake/path.png") is False
        assert store.has_tool_stash("/fake/path.png", "color_adjust") is False
        assert store.get_tool_stash("/fake/path.png", "color_adjust") is None
        assert store.get_last_used("color_adjust") is None

    def test_stash_and_has_pending(self):
        store = EditStateStore()
        path = "/fake/img1.png"
        store.stash(path, "color_adjust", {"temperature": 25, "exposure": 10})

        assert store.has_pending(path) is True
        assert store.has_tool_stash(path, "color_adjust") is True
        assert store.get_tool_stash(path, "color_adjust") == {"temperature": 25, "exposure": 10}

        # 检查未暂存的工具
        assert store.has_tool_stash(path, "beauty") is False
        assert store.get_tool_stash(path, "beauty") is None

    def test_effective_params_fallback(self):
        store = EditStateStore()
        path1 = "/fake/img1.png"
        path2 = "/fake/img2.png"
        defaults = {"temperature": 0, "tint": 0}

        # 1. 初始返回 defaults
        eff = store.get_effective_params(path1, "color_adjust", defaults)
        assert eff == defaults

        # 2. 暂存优先
        store.stash(path1, "color_adjust", {"temperature": 50, "tint": 10})
        eff1 = store.get_effective_params(path1, "color_adjust", defaults)
        assert eff1 == {"temperature": 50, "tint": 10}

        # 3. 另一张图无暂存，fallback 到 defaults (尚未 commit 产生 last_used)
        eff2 = store.get_effective_params(path2, "color_adjust", defaults)
        assert eff2 == defaults

        # 4. commit 后更新 last_used，跨图继承
        store.commit(path1, "/fake/img1_saved.png", "color_adjust", {"temperature": 50, "tint": 10})
        assert store.has_pending(path1) is False
        assert store.get_last_used("color_adjust") == {"temperature": 50, "tint": 10}

        eff2_after_commit = store.get_effective_params(path2, "color_adjust", defaults)
        assert eff2_after_commit == {"temperature": 50, "tint": 10}

    def test_discard_and_clear_path(self):
        store = EditStateStore()
        path = "/fake/img.png"
        store.stash(path, "crop", {"ratio": "16:9"})
        store.stash(path, "color_adjust", {"exposure": 20})

        assert store.has_tool_stash(path, "crop") is True
        assert store.has_tool_stash(path, "color_adjust") is True

        # 单工具丢弃
        store.discard(path, "crop")
        assert store.has_tool_stash(path, "crop") is False
        assert store.has_tool_stash(path, "color_adjust") is True
        assert store.has_pending(path) is True

        # 全路径清理
        store.clear_path(path)
        assert store.has_pending(path) is False
        assert store.has_tool_stash(path, "color_adjust") is False


class TestToolSidebarWidget:
    """测试右侧常驻参数容器 ToolSidebar。"""

    def test_sidebar_creation_and_signals(self, qapp):
        sidebar = ToolSidebar()
        assert sidebar.isVisible() is False or sidebar.isHidden() is False

        # 测试标题净化与横幅
        sidebar.set_title("调色实验室(&L)...")
        assert sidebar._title_label.text() == "调色实验室"

        # 测试关闭按钮为右侧收起矢量图标
        assert not sidebar._close_btn.icon().isNull()
        assert sidebar._close_btn.width() == 28
        assert sidebar._close_btn.height() == 28

        sidebar.set_stash_banner_visible(True)
        assert sidebar._stash_banner.isHidden() is False
        sidebar.set_stash_banner_visible(False)
        assert sidebar._stash_banner.isHidden() is True

        # 测试信号连接
        received_signals = []
        sidebar.reset_requested.connect(lambda: received_signals.append("reset"))
        sidebar.discard_stash_requested.connect(lambda: received_signals.append("discard_stash"))
        sidebar.close_requested.connect(lambda: received_signals.append("close"))

        sidebar._reset_btn.click()
        sidebar._stash_banner.action_button.click()
        sidebar._close_btn.click()

        assert received_signals == ["reset", "discard_stash", "close"]
        # 应用、取消、撤销与重做不再由侧栏容器提供
        for name in ("_apply_btn", "_cancel_btn", "_undo_btn", "_redo_btn", "_action_bar"):
            assert not hasattr(sidebar, name), name


class TestOverlays:
    """测试 B 类画布覆盖层的归一化几何与交互契约。"""

    def test_crop_overlay(self):
        overlay = CropOverlay()
        assert overlay.norm_rect == [0.1, 0.1, 0.9, 0.9]
        overlay.set_aspect_ratio("1:1")
        assert overlay._aspect_ratio == 1.0

        img_rect = QRect(100, 100, 800, 600)
        px_rect = overlay.get_pixel_rect(img_rect)
        assert px_rect.width() > 0 and px_rect.height() > 0

    def test_grid_crop_overlay(self):
        overlay = GridCropOverlay()
        overlay.set_grid(3, 3)
        assert len(overlay.row_splits) == 2
        assert len(overlay.col_splits) == 2

        regions = overlay.get_grid_regions(300, 300)
        assert len(regions) == 9

    def test_shape_crop_overlay(self):
        overlay = ShapeCropOverlay()
        overlay.set_shape_type("circle")
        assert overlay.shape_type == "circle"
        overlay.set_corner_radius(25.0)
        assert overlay.corner_radius == 25.0

    def test_lasso_overlay(self):
        overlay = LassoOverlay()
        assert overlay.is_closed is False
        assert len(overlay.draft_points) == 0

        # 添加锚点
        overlay.add_point(0.1, 0.1)
        overlay.add_point(0.9, 0.1)
        overlay.add_point(0.5, 0.9)
        overlay.close_selection()
        assert overlay.is_closed is True

        regions = overlay.get_pixel_regions(100, 100)
        assert len(regions) == 1
        assert len(regions[0]["points"]) == 3

        overlay.clear_selection()
        assert overlay.is_closed is False
        assert len(overlay.draft_points) == 0


@pytest.fixture
def editor_window(window_factory, sample_image):
    win = window_factory()
    win.show()
    win.manager.open_file(sample_image)
    win._refresh_all()
    wait_for_thumbnails(win.thumbnail_panel, [sample_image], sample_image, version=win.manager.list_version)
    wait_for_studio(win, sample_image)
    return win


def record_editor_outputs(studio, monkeypatch):
    outputs = []
    start_output = studio._start_output

    def start(request, *, apply=False):
        generation = studio._output_generation
        start_output(request, apply=apply)
        task = studio._output_tasks.get(studio._output_generation)
        if task is not None and studio._output_generation != generation:
            relay = UiThreadRelay.shared()
            task.signals.finished.connect(lambda task_id, result: relay.post(lambda: outputs.append(result)),
                                          Qt.ConnectionType.DirectConnection)

    monkeypatch.setattr(studio, "_start_output", start)
    return outputs


def apply_in_editor(win, target, monkeypatch):
    studio = win.studio_workspace
    errors = []
    monkeypatch.setattr(studio, "_show_core_error", errors.append)
    monkeypatch.setattr(win._apply_service, "ask_target", lambda: target)
    outputs = record_editor_outputs(studio, monkeypatch)
    studio.apply_changes()
    assert studio._commit_busy and studio._output_tasks
    wait_until(lambda: outputs and not studio._commit_busy and not studio._output_tasks,
               "编辑结果输出完成")
    assert outputs[-1].success, outputs[-1].error
    assert not errors and not studio._commit_busy
    for result in outputs[:-1]:
        assert not result.success and result.error.code == "UNSUPPORTED_FORMAT"
        assert result.error.message_key == "error.export.alpha_format"
    return outputs[-1].data.path


@pytest.fixture
def transparent_editor(window_factory, tmp_path):
    source = tmp_path / "photo.jpg"
    Image.new("RGB", (200, 200), (200, 100, 50)).save(source)
    win = window_factory()
    win.show()
    win.manager.open_file(str(source))
    win._refresh_all()
    studio = wait_for_studio(win, source, "shape_crop")
    panel = studio.tool_sidebar.get_current_panel()
    panel.overlay.norm_rect = [0.2, 0.2, 0.6, 0.6]
    panel.overlay.geometry_changed.emit()
    return win, source, source.read_bytes()


class TestMainWindowIntegration:
    def test_editor_tools_lifecycle(self, editor_window, sample_image):
        win = editor_window
        studio = win.studio_workspace
        for entry in TOOL_ENTRIES:
            for tool_id in entry.tool_ids:
                wait_for_studio(win, sample_image, tool_id)
                panel = studio.tool_sidebar.get_current_panel()
                assert isinstance(panel, BaseToolPanel) and panel.TOOL_ID == tool_id
                assert studio.tool_rail.current() == entry.entry_id
                assert studio.tool_sidebar.isVisible()
                assert studio.viewer._overlay is getattr(panel, "overlay", None)
                studio.tool_sidebar._close_btn.click()
                assert not studio.tool_sidebar.isVisible()
                assert studio.active_tool_id == tool_id
                studio.switch_tool(tool_id)
                assert studio.tool_sidebar.isVisible()

    def test_stash_restore_and_explicit_reset(self, editor_window, sample_image):
        win = editor_window
        studio = win.studio_workspace
        panel = studio.tool_sidebar.get_current_panel()
        panel.notify_params_changed({**panel._params, "temperature": 40})
        expected = dict(studio.session.color_params)
        drafts = win._edit_state_store.drafts
        assert drafts.get_draft(sample_image, "color_adjust") == expected
        studio.tool_sidebar._close_btn.click()
        assert studio.is_dirty() and drafts.get_draft(sample_image, "color_adjust") == expected
        win._on_workspace_index_changed(0)
        studio = wait_for_studio(win, sample_image)
        panel = studio.tool_sidebar.get_current_panel()
        assert panel._params == expected and studio.session.color_params == expected
        studio.tool_sidebar._reset_btn.click()
        assert not studio.is_dirty()
        assert drafts.get_draft(sample_image, "color_adjust") == panel._params
        assert panel._params["temperature"] == 0


class TestCoreStateAndWorkflowFixes:
    def test_apply_preserves_image_list_and_navigation(self, window_factory, tmp_path, monkeypatch):
        paths = []
        for index in range(3):
            path = tmp_path / f"photo-{index}.png"
            Image.new("RGB", (100, 100), (50 * index, 80, 100)).save(path)
            paths.append(str(path))
        win = window_factory()
        win.show()
        win.manager.open_files(paths, append=False)
        win.manager.select_image(paths[1])
        win._refresh_all()
        studio = wait_for_studio(win, paths[1])
        panel = studio.tool_sidebar.get_current_panel()
        panel.notify_params_changed({**panel._params, "temperature": 30})
        assert apply_in_editor(win, ApplyTarget.OVERWRITE, monkeypatch) == paths[1]
        assert win.manager.image_list == paths and win.manager.current_index == 1
        assert win.manager.has_prev() and win.manager.has_next()
        assert win.manager.prev_image() == paths[0]
        assert win.manager.next_image() == paths[1]
        assert win.manager.next_image() == paths[2]

    def test_transparent_cutout_saved_as_png_for_jpeg(self, transparent_editor, tmp_path, monkeypatch):
        win, source, original = transparent_editor
        output = str(tmp_path / "cutout.png")
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (output, "PNG"))
        assert apply_in_editor(win, ApplyTarget.OVERWRITE, monkeypatch) == output
        assert win.manager.current_path == output and source.read_bytes() == original
        with Image.open(output) as image:
            assert image.mode == "RGBA" and image.getchannel("A").getextrema() == (0, 255)

    def test_transparent_save_as_cancel_preserves_draft(self, transparent_editor, monkeypatch):
        win, source, original = transparent_editor
        studio = win.studio_workspace
        before = studio.core.get_session(studio.session.core_id).unwrap()
        errors, dialogs = [], []

        def cancel(*args, **kwargs):
            dialogs.append(args[2])
            return "", ""

        monkeypatch.setattr(QFileDialog, "getSaveFileName", cancel)
        monkeypatch.setattr(studio, "_show_core_error", errors.append)
        monkeypatch.setattr(win._apply_service, "ask_target", lambda: ApplyTarget.OVERWRITE)
        outputs = record_editor_outputs(studio, monkeypatch)
        studio.apply_changes()
        wait_until(lambda: outputs and not studio._output_tasks and not studio._commit_busy,
                   "取消透明图片另存为")
        assert len(outputs) == len(dialogs) == 1
        assert outputs[0].error.message_key == "error.export.alpha_format"
        assert dialogs[0].endswith("_edited.png") and not errors
        assert studio.core.get_session(studio.session.core_id).unwrap() == before
        assert studio.is_dirty() and win.manager.current_path == str(source)
        assert source.read_bytes() == original

    @pytest.mark.parametrize("change", ["image", "edit", "cancel"])
    def test_transparent_save_as_rejects_changed_context(
        self, transparent_editor, tmp_path, monkeypatch, change
    ):
        win, source, original = transparent_editor
        studio = win.studio_workspace
        session = studio.session
        before = studio.core.get_session(session.core_id).unwrap()
        output = tmp_path / "stale.png"
        other = tmp_path / "other.png"
        Image.new("RGB", (80, 60), "blue").save(other)
        win.manager.open_files([str(source), str(other)], append=False)
        errors, dialogs = [], []

        def choose(*args, **kwargs):
            dialogs.append(args[2])
            if change == "image":
                win._on_thumbnail_selected(str(other))
                wait_for_studio(win, other)
            elif change == "edit":
                panel = studio.tool_sidebar.get_current_panel()
                panel.overlay.norm_rect = [0.1, 0.1, 0.7, 0.7]
                panel.overlay.geometry_changed.emit()
                assert studio.core.get_session(session.core_id).unwrap().state_version != before.state_version
            else:
                studio.cancel_output()
            return str(output), "PNG"

        monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
        monkeypatch.setattr(studio, "_show_core_error", errors.append)
        monkeypatch.setattr(win._apply_service, "ask_target", lambda: ApplyTarget.OVERWRITE)
        outputs = record_editor_outputs(studio, monkeypatch)
        studio.apply_changes()
        wait_until(lambda: outputs and not studio._output_tasks and not studio._commit_busy,
                   "拒收已改变的另存为上下文")
        assert len(outputs) == len(dialogs) == 1
        assert outputs[0].error.message_key == "error.export.alpha_format"
        assert not errors and not output.exists() and source.read_bytes() == original
        assert win.manager.current_path == str(other if change == "image" else source)
        if change != "edit":
            assert studio.core.get_session(session.core_id).unwrap() == before

    def test_apply_save_as_creates_new_entry(self, editor_window, sample_image, tmp_path, monkeypatch):
        output = str(tmp_path / "saved_as.png")
        original = Path(sample_image).read_bytes()
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (output, "PNG"))
        panel = editor_window.studio_workspace.tool_sidebar.get_current_panel()
        panel.notify_params_changed({**panel._params, "temperature": 15})
        assert apply_in_editor(editor_window, ApplyTarget.SAVE_AS, monkeypatch) == output
        assert editor_window.manager.current_path == output
        assert editor_window.manager.image_list == [output]
        assert Path(sample_image).read_bytes() == original
        with Image.open(output) as image:
            assert image.size == (400, 300)

    def test_apply_cancel_keeps_original(self, editor_window, sample_image, monkeypatch):
        win = editor_window
        studio = win.studio_workspace
        panel = studio.tool_sidebar.get_current_panel()
        panel.notify_params_changed({**panel._params, "temperature": 15})
        original = Path(sample_image).read_bytes()
        expected = dict(studio.session.color_params)
        monkeypatch.setattr(win._apply_service, "ask_target", lambda: ApplyTarget.CANCEL)
        studio.apply_changes()
        assert not studio._output_tasks and not studio._commit_busy
        assert win.manager.current_path == sample_image and win.manager.image_list == [sample_image]
        assert Path(sample_image).read_bytes() == original
        assert studio.is_dirty() and studio.session.color_params == expected

    def test_cancel_cleans_dirty_params_and_reopen_clean(self, editor_window, sample_image):
        win = editor_window
        studio = win.studio_workspace
        panel = studio.tool_sidebar.get_current_panel()
        panel.notify_params_changed({**panel._params, "exposure_amt": 45})
        assert studio.is_dirty()
        studio.cancel_current_tool()
        assert not studio.is_dirty()
        assert win._edit_state_store.drafts.get_draft(sample_image, "color_adjust") == panel._params
        assert panel._params["exposure_amt"] == 0
        win._on_workspace_index_changed(0)
        studio = wait_for_studio(win, sample_image)
        assert not studio.is_dirty()
        assert studio.tool_sidebar.get_current_panel()._params["exposure_amt"] == 0

    @pytest.mark.parametrize("change", ["tool", "image", "workspace"])
    def test_stale_preview_rejected(self, editor_window, sample_image, tmp_path, change):
        win = editor_window
        studio = win.studio_workspace
        wait_until(lambda: studio._displayed_request is not None, "编辑初始预览")
        request, frame = studio._displayed_request, studio._displayed_frame
        if change == "tool":
            wait_for_studio(win, sample_image, "crop")
        elif change == "image":
            other = tmp_path / "other.png"
            Image.new("RGB", (80, 60), "blue").save(other)
            win.manager.open_files([sample_image, str(other)], append=False)
            win.manager.select_image(str(other))
            win._refresh_all()
            wait_for_studio(win, other)
        else:
            win._on_workspace_index_changed(0)
        assert not studio._accept_preview(request)
        generation = studio._displayed_generation
        image = QImage(50, 50, QImage.Format.Format_RGB888)
        image.fill(Qt.GlobalColor.magenta)
        studio._on_pipeline_render_finished(request, image, frame, None)
        assert studio._displayed_generation == generation

    def test_shape_crop_all_8_handles(self):
        """测试 5: 形状裁剪覆盖层全部 8 个方向手柄均能正确响应拖拽。"""
        overlay = ShapeCropOverlay()
        overlay.norm_rect = [0.2, 0.2, 0.4, 0.4]
        img_rect = QRect(0, 0, 1000, 1000)

        directions = ["nw", "n", "ne", "e", "se", "s", "sw", "w"]
        for handle in directions:
            overlay._drag_mode = handle
            overlay._drag_start_pos = QPoint(500, 500)
            overlay._drag_start_rect = [0.2, 0.2, 0.4, 0.4]

            move_event = QMouseEvent(
                QMouseEvent.Type.MouseMove,
                QPointF(550.0, 550.0),
                QPointF(550.0, 550.0),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )

            class DummyWidget:
                def update(self): pass
                def setCursor(self, c): pass
            move_event._widget = DummyWidget()
            assert overlay.handle_mouse_move(move_event, img_rect) is True

    def test_batch_denoise_trigger_on_multi_select(self, editor_window, sample_image, tmp_path, monkeypatch):
        import src.ui.main_window as main_module
        win = editor_window
        other = str(tmp_path / "other.png")
        Image.new("RGB", (80, 60), "blue").save(other)
        win.manager.open_files([sample_image, other], append=False)
        win.manager.select_image(sample_image)
        win._refresh_all()
        studio = wait_for_studio(win, sample_image)
        thumbnails = studio.thumbnail_panel
        wait_for_thumbnails(thumbnails, win.manager.image_list, sample_image, version=win.manager.list_version)
        for index in range(thumbnails.list_widget.count()):
            thumbnails.list_widget.item(index).setSelected(True)
        assert set(thumbnails.get_selected_paths()) == {sample_image, other}
        opened = []

        class Dialog:
            def __init__(self, paths, parent, **services):
                opened.append((tuple(paths), services))
            def exec(self):
                return 0
            def deleteLater(self):
                pass

        monkeypatch.setattr(main_module, "EnhancementDialog", Dialog)
        win._open_ai_enhance()
        assert len(opened) == 1 and set(opened[0][0]) == {sample_image, other}
        assert opened[0][1]["runtime"] is studio.core.tools
        assert studio.active_tool_id == "color_adjust"

    def test_grid_crop_panel_export_does_not_replace_main(self, editor_window, sample_image, monkeypatch):
        win = editor_window
        studio = wait_for_studio(win, sample_image, "grid_crop")
        panel = studio.tool_sidebar.get_current_panel()
        assert isinstance(panel, GridCropPanel)
        original = Path(sample_image).read_bytes()
        exported = []
        monkeypatch.setattr(panel, "export_tiles", lambda: exported.append(True))
        studio.apply_changes()
        assert exported == [True] and not studio._output_tasks
        assert win.manager.current_path == sample_image and win.manager.image_list == [sample_image]
        assert Path(sample_image).read_bytes() == original

    def test_beauty_panel_layout_and_workflow(self, panel_factory, sample_image):
        panel = panel_factory("beauty", sample_image)
        wait_for_panel_source(panel, sample_image)
        assert panel.PARAMETER_CONTRACT.version == 3 and panel.model_selector is not None
        assert list(panel._group_cards) == ["skin", "eye", "shape", "detail"]
        sliders = {"skin_smooth", "skin_brighten", "skin_rosy", "sharpen", "eye_enlarge", "eye_distance",
                   "face_slim", "nose_slim", "teeth_whiten", "spot_remove", "eye_bag_remove"}
        rows = panel._parameter_rows
        assert set(rows) == sliders
        assert all(not rows[key].label().endswith((":", "：")) for key in sliders)
        initial = dict(panel._params)
        assert all(initial[key] == 0 for key in sliders)
        # 人脸模型未安装时两个快捷操作不可用；以下按模型就绪验证面板自身的流程
        assert not panel._btn_auto_beauty.isEnabled() and not panel._btn_preset.isEnabled()
        panel._readiness.states = lambda params: {"effect": "ready", "analysis": "ready"}
        panel._model_status_changed()
        statuses = []
        panel.request_status.connect(statuses.append)
        panel._btn_auto_beauty.click()
        wait_until(lambda: statuses, "缺少人脸模型的自动美颜提示")
        assert panel._params == initial and not panel.can_undo()
        # 保留细节美颜的推荐预设先做人脸分析，没有可处理的脸时不写入参数
        statuses.clear()
        panel._btn_preset.click()
        wait_until(lambda: statuses, "保留细节美颜推荐预设的分析提示")
        assert panel._params == initial and not panel.can_undo()
        rows["skin_smooth"].slider.edit_value(80)
        assert panel._params["skin_smooth"] == 80 and rows["skin_smooth"].value_edit.text() == "80"
        panel.undo()
        assert panel._params == initial and panel.can_redo()
        panel.redo()
        assert panel._params["skin_smooth"] == 80
        rows["eye_enlarge"].value_edit.setText("35")
        rows["eye_enlarge"].value_edit.editingFinished.emit()
        assert rows["eye_enlarge"].value() == 35
        panel.reset()
        assert panel._params == initial
        panel.retranslate_ui()
