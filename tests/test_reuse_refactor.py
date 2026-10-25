"""Regression tests for the OPS/OPS reuse refactor (Phase A+B)."""

from __future__ import annotations

import os
import tempfile

import pytest
from PIL import Image


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication([])
    return app


@pytest.fixture
def sample_image():
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    Image.new("RGB", (32, 24), color=(90, 120, 150)).save(tmp.name)
    tmp.close()
    yield tmp.name
    if os.path.exists(tmp.name):
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def test_registry_resolves_legacy_tool_id_alias(qapp, sample_image):
    """studio 的 remove_bg 与 registry 的 bg_remove 统一到同一工厂。"""
    from src.ui.panels.registry import create_tool_panel, resolve_tool_id
    from src.ui.panels.bg_remove_panel import BGRemovePanel

    assert resolve_tool_id("remove_bg") == "bg_remove"
    assert resolve_tool_id("color_adjust") == "color_adjust"

    panel = create_tool_panel("remove_bg", sample_image)
    assert isinstance(panel, BGRemovePanel)
    panel.cleanup()


def test_apply_service_is_shared(qapp, sample_image):
    """OPS 与 OPS 复用同一个 ApplyService 实例，而不是反向依赖主窗口。"""
    from src.ui.main_window import MainWindow

    win = MainWindow()
    try:
        from tests.support import wait_for_studio
        win.show()
        win.manager.open_file(sample_image)
        win._refresh_all()
        wait_for_studio(win, sample_image)
        assert win.studio_workspace._apply_service is win._apply_service
        # 默认扩展名/透明通道处理由服务统一提供
        rgba = Image.new("RGBA", (8, 8))
        assert win._apply_service.default_ext(rgba, "x.jpg") == ".png"
    finally:
        win.close()
