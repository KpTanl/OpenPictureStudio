"""Tests for StudioWorkspace and Dual Workspace Switching."""

from __future__ import annotations

import os
from PIL import Image
import pytest

from PyQt6.QtWidgets import QApplication

from src.ui.main_window import MainWindow
from tests.support import wait_for_studio, wait_for_thumbnails, wait_until


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        app = QApplication([])
    return app


@pytest.fixture
def sample_image(tmp_path):
    path = str(tmp_path / "photo.png")
    img = Image.new("RGB", (400, 300), color=(120, 180, 240))
    img.save(path)
    return path


@pytest.fixture
def editor_window(window_factory, sample_image):
    win = window_factory()
    win.show()
    win.manager.open_file(sample_image)
    win._refresh_all()
    wait_for_thumbnails(win.thumbnail_panel, [sample_image], sample_image, version=win.manager.list_version)
    wait_for_studio(win, sample_image)
    return win


class TestStudioWorkspace:
    """测试独立修图工作区容器与生命周期。"""

    def test_studio_workspace_lifecycle(self, qapp, sample_image, editor_window):
        win = editor_window
        studio = win.studio_workspace

        # 装载图片
        assert studio.session is not None
        assert studio.session.source_size == (400, 300)
        assert studio.is_dirty() is False

        # 切换工具
        studio.switch_tool("beauty")
        assert studio._active_tool_id == "beauty"

        # 模拟参数变动
        wait_for_studio(win, sample_image)
        panel = studio.tool_sidebar.get_current_panel()
        panel.notify_params_changed({**panel._params, "exposure_amt": 20})
        assert studio.is_dirty() is True
        assert "color_adjust" in studio.modified_tools()
        assert "color" in studio.tool_rail.modified()

        # 对比原图按键模拟
        studio.start_compare()
        studio.stop_compare()

        # 重置全部
        studio.reset_all()
        assert studio.is_dirty() is False


    def test_undo_after_param_change(self, qapp, sample_image, editor_window):
        """参数改动必须进入会话历史，公布的撤销/重做状态随之可用。"""
        win = editor_window
        studio = win.studio_workspace
        baseline = dict(studio.session.color_params)
        expected = {**studio.tool_sidebar.get_current_panel()._params, "exposure_amt": 20}

        assert studio.session.can_undo() is False
        assert studio.action_state().can_undo is False

        studio._on_panel_params_changed("color_adjust", expected)
        assert studio.session.can_undo() is True
        assert studio.action_state().can_undo is True

        studio.undo()
        assert studio.session.color_params == baseline
        assert studio.action_state().can_redo is True

        studio.redo()
        assert studio.session.color_params == expected


    def test_sidebar_close_stays_in_studio_and_can_reopen(self, qapp, sample_image, editor_window):
        """关闭右侧栏只折叠侧栏，不退出 studio；再次选工具可重新展开。"""
        win = editor_window
        assert win._current_workspace_index == 1

        studio = win.studio_workspace
        assert studio.right_sidebar.isHidden() is False

        studio.tool_sidebar._close_btn.click()
        assert win._current_workspace_index == 1  # 没有降级回 OPS
        assert studio.right_sidebar.isHidden() is True

        studio.switch_tool("color_adjust")
        assert studio.right_sidebar.isHidden() is False

    def test_filmstrip_syncs_with_viewer(self, qapp, sample_image, editor_window):
        """studio 底部胶片条与看图缩略图保持同一列表/选中状态。"""
        win = editor_window

        view_panel = win.thumbnail_panel
        studio_panel = win.studio_workspace.thumbnail_panel

        wait_for_thumbnails(studio_panel, [sample_image], sample_image, version=win.manager.list_version)
        assert studio_panel.get_all_paths() == view_panel.get_all_paths() == [sample_image]

        win._select_image_in_panels(sample_image)
        assert sample_image in studio_panel.get_selected_paths()
        assert sample_image in view_panel.get_selected_paths()


    def test_studio_apply_overwrite_writes_back(self, qapp, sample_image, editor_window):
        """覆盖原图：写回原路径并发出 applied 信号。"""
        from src.ui.dialogs.apply_target_dialog import ApplyTarget

        win = editor_window
        studio = win.studio_workspace
        panel = studio.tool_sidebar.get_current_panel()
        panel.notify_params_changed({**panel._params, "exposure_amt": 20})
        assert studio.is_dirty() is True

        applied = []
        studio.applied.connect(applied.append)
        win._apply_service.ask_target = lambda: ApplyTarget.OVERWRITE
        studio.apply_changes()

        wait_until(lambda: applied == [sample_image] and not studio._commit_busy, "编辑结果覆盖原图")
        assert os.path.isfile(sample_image)


class TestDualWorkspaceSwitching:
    """测试主窗口顶部双工作区切换机制。"""

    def test_no_image_studio_protection(self, qapp):
        win = MainWindow()
        win.manager.current_path = None

        # 尝试切到修图区
        win._on_workspace_index_changed(1)
        # 应停留在看图工作区 0
        assert win._current_workspace_index == 0
        assert win.workspace_stack.currentIndex() == 0
        win.close()

    def test_with_image_studio_switching_and_back(self, qapp, sample_image, editor_window):
        win = editor_window

        # 切换到修图工作区
        win._on_workspace_index_changed(1)
        wait_for_studio(win, sample_image)
        assert win._current_workspace_index == 1
        assert win.workspace_stack.currentIndex() == 1
        assert win.studio_workspace.session is not None

        # 从修图工作区切回看图工作区
        win._on_workspace_index_changed(0)
        assert win._current_workspace_index == 0
        assert win.workspace_stack.currentIndex() == 0


    def test_switch_to_studio_from_menu_trigger(self, qapp, sample_image, editor_window):
        win = editor_window

        # 模拟菜单点击人像美颜
        win._open_beauty()
        wait_for_studio(win, sample_image, "beauty")
        assert win._current_workspace_index == 1
        assert win.studio_workspace._active_tool_id == "beauty"

        # 模拟菜单点击调色
        win._open_color_adjust()
        wait_for_studio(win, sample_image)
        assert win._current_workspace_index == 1
        assert win.studio_workspace._active_tool_id == "color_adjust"


def test_entering_the_editor_does_not_probe_face_detection_on_the_interface_thread(window_factory, sample_image,
                                                                                  monkeypatch):
    """进入编辑、刷新 AI 助手上下文时不在界面线程导入人脸检测库、哈希模型文件：冷启动时导入要好几秒，调色面板会一直出不来。"""
    import threading
    from src.core.tools import beauty_backend
    probed = []
    original = beauty_backend.available_backends
    monkeypatch.setattr(beauty_backend, "available_backends",
                        lambda: probed.append(threading.current_thread() is threading.main_thread()) or original())
    win = window_factory()
    win.show()
    win.manager.open_file(sample_image)
    win._refresh_all()
    wait_for_thumbnails(win.thumbnail_panel, [sample_image], sample_image, version=win.manager.list_version)
    wait_for_studio(win, sample_image)
    context = win.studio_workspace.assistant_context()
    assert context is not None and context.tools["beauty"] in ("", "resources")
    assert not any(probed)
