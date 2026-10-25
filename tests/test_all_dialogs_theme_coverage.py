"""全量对话框与画布多主题解耦、动态切换及渲染覆盖测试。"""

import pytest
from PIL import Image
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPaintEvent
from PyQt6.QtCore import QRect

from src.ui.theme import ThemeManager, set_theme_manager
from src.ui.dialogs.compress_dialog import ImageProcessingDialog
from src.ui.dialogs.enhancement_dialog import EnhancementDialog
from src.ui.dialogs.pdf_dialog import PDFDialog
from src.ui.dialogs.settings_dialog import SettingsDialog
from src.ui.dialogs.wallpaper_dialog import WallpaperDialog
from src.ui.widgets.curves_widget import CurvesWidget
from src.ui.widgets.histogram_widget import HistogramWidget
from tests.support import wait_until


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def sample_image_file(tmp_path):
    img = Image.new("RGB", (200, 200), color=(120, 160, 200))
    file_path = str(tmp_path / "sample.jpg")
    img.save(file_path)
    return file_path


def test_dialogs_theme_lifecycle(qapp, sample_image_file):
    """测试对话框在不同主题下的实例化、动态切换与资源释放。"""
    mgr = ThemeManager()
    set_theme_manager(mgr)

    dialog_instances = []

    # 1. 在浅色模式下实例化所有对话框
    mgr.apply("light")

    dialog_factories = [
        lambda: ImageProcessingDialog(sample_image_file),
        lambda: EnhancementDialog([sample_image_file]),
        lambda: PDFDialog([sample_image_file]),
        lambda: SettingsDialog(),
        lambda: WallpaperDialog(sample_image_file),
    ]

    for factory in dialog_factories:
        dlg = factory()
        dialog_instances.append(dlg)

    assert len(dialog_instances) == len(dialog_factories) == 5

    # 2. 动态切换为 Dark 主题
    mgr.apply("dark")
    assert qapp.property("ops.theme") == "dark"

    # 3. 动态切换为 Beige 主题
    mgr.apply("beige")
    assert qapp.property("ops.theme") == "beige"

    # 4. 再次切换回 Light 主题并验证无深色硬编码污染
    mgr.apply("light")
    for dlg in dialog_instances:
        sheet = dlg.styleSheet()
        assert "#1b2129" not in sheet

    # 5. 关闭所有对话框并验证订阅注销
    for dlg in dialog_instances:
        dlg.close()
        if hasattr(dlg, "_theme_subscribed"):
            # 图片格式处理对话框在后台读取结束后才真正关闭
            wait_until(lambda dlg=dlg: not dlg._theme_subscribed,
                       lambda dlg=dlg: f"{type(dlg).__name__} 关闭后注销主题订阅")

    # 恢复默认 dark
    mgr.apply("dark")


def test_canvas_and_widget_painters_under_all_themes(qapp):
    """测试各类画布及自定义控件在不同主题下的 QPainter 渲染与色板绑定。"""
    mgr = ThemeManager()
    set_theme_manager(mgr)

    curves_w = CurvesWidget()
    hist_w = HistogramWidget()

    widgets = [curves_w, hist_w]

    for w in widgets:
        w.resize(200, 200)

    for theme_name in ("light", "dark", "beige"):
        mgr.apply(theme_name)

        # 触发每个画布的实际重绘事件
        for w in widgets:
            event = QPaintEvent(QRect(0, 0, 200, 200))
            w.paintEvent(event)

    # 恢复默认 dark
    mgr.apply("dark")
