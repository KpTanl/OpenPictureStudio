"""应用程序启动、双工作区集成与通用功能自动化测试。"""

from __future__ import annotations

import importlib
import os
import pkgutil
import subprocess
import sys
from pathlib import Path
from PIL import Image
import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.ui.main_window import MainWindow
from src.ui.theme import ThemeManager, set_theme_manager
from src.core.rating_store import get_rating_store
from tests.support import subprocess_setup, wait_until, wait_for_studio, wait_for_thumbnails


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        app = QApplication([])
    return app


@pytest.fixture
def sample_images(tmp_path):
    """生成包含多张不同尺寸测试图片的临时目录。"""
    paths = []
    for i in range(3):
        p = str(tmp_path / f"startup_test_{i}.png")
        img = Image.new("RGB", (400 + i * 50, 300 + i * 30), color=(100 + i * 40, 120, 160))
        img.save(p)
        paths.append(p)
    return paths


class TestApplicationStartup:
    """测试应用程序启动与初始状态。"""

    def test_cold_startup_default_state(self, qapp):
        """测试无图冷启动下的默认属性与组件就绪状态。"""
        win = MainWindow()

        # 验证默认几何属性推荐尺寸（1600x900）
        assert win.minimumWidth() >= 800
        assert win.minimumHeight() >= 600

        # 验证工作区初始处于看图工作区 (0)
        assert win._current_workspace_index == 0
        assert win.workspace_stack.currentIndex() == 0

        # 验证标题下拉工作区切换器处于 0
        if hasattr(win, "workspace_switcher") and win.workspace_switcher:
            assert win.workspace_switcher.currentIndex() == 0

        # 验证没有图片时照片信息栏收起、顶栏没有标题
        assert win.info_sidebar.isHidden()
        assert win.top_bar.title() == ("", "")

        # 验证底栏缩略图面板为空
        assert win.thumbnail_panel is not None
        assert win.thumbnail_panel.list_widget.count() == 0

        win.close()

    def test_startup_with_image_loaded(self, qapp, sample_images):
        """测试加载图片启动后的顶栏标题、照片信息与缩略图加载。"""
        win = MainWindow()
        win.show()
        first_img = sample_images[0]
        win.manager.open_file(first_img, load_siblings=True)
        win._refresh_all()

        # 验证当前图片路径
        assert win.manager.current_path == first_img

        # 验证顶栏标题与照片信息随后台读取更新
        wait_until(lambda: win.top_bar.title() == (os.path.basename(first_img), "400 × 300"),
                   "顶栏标题显示文件名与尺寸")
        assert win.info_sidebar.panel.image_path == first_img

        # 验证缩略图加载了同目录下所有兄弟图片
        wait_for_thumbnails(win.thumbnail_panel, sample_images, first_img,
                            version=win.manager.list_version)

        win.close()

    def test_real_subprocess_main_entry(self):
        """通过子进程以离屏模式运行主入口 main()，验证完整启动生命周期退出码为 0。"""
        code = (
            subprocess_setup()
            + "from tests.support import close_main_window\n"
            + "import os, sys\n"
            "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
            "from PySide6.QtCore import QTimer\n"
            "from src.ui.main_window import MainWindow\n"
            "orig_show = MainWindow.show\n"
            "def safe_show(self):\n"
            "    orig_show(self)\n"
            "    QTimer.singleShot(100, lambda: close_main_window(self))\n"
            "MainWindow.show = safe_show\n"
            "from src.main import main\n"
            "sys.exit(main())\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        assert res.returncode == 0, f"Startup failed with stderr:\n{res.stderr}"


class TestDualWorkspaceStartupAndSwitching:
    """测试启动后的双工作区无缝切换与安全防护。"""

    def test_no_image_guard_prevents_switching(self, qapp):
        """无图时尝试切换至修图工作区应被防护拦截，保持在看图工作区。"""
        win = MainWindow()
        assert win.manager.current_path is None

        # 尝试切换至修图工作区
        win._on_workspace_index_changed(1)

        assert win._current_workspace_index == 0
        assert win.workspace_stack.currentIndex() == 0
        if hasattr(win, "workspace_switcher") and win.workspace_switcher:
            assert win.workspace_switcher.currentIndex() == 0

        win.close()

    def test_dual_workspace_switching_roundtrip(self, qapp, sample_images):
        """打开图片后，可顺畅在看图模式与修图模式间往返切换，各工作区正常就绪。"""
        win = MainWindow()
        win.show()
        img_path = sample_images[0]
        win.manager.open_file(img_path, load_siblings=True)
        win._refresh_all()

        # 切换到工作室工作区
        studio = wait_for_studio(win, img_path)
        assert win._current_workspace_index == 1
        assert win.workspace_stack.currentIndex() == 1
        assert win.studio_workspace.session is not None
        assert studio.tool_rail.current() == "color"
        assert studio.tool_sidebar.get_current_panel().TOOL_ID == "color_adjust"

        # 切回看图工作区
        win._on_workspace_index_changed(0)
        assert win._current_workspace_index == 0
        assert win.workspace_stack.currentIndex() == 0
        assert win.top_bar.title()[0] == os.path.basename(img_path)
        assert win.info_sidebar.panel.image_path == img_path

        win.close()


class TestStarRatingAndFilterStartupIntegration:
    """测试双工作区通用的星标评分、筛选与一键清除联动。"""

    def test_star_rating_sync_across_both_workspaces(self, qapp, sample_images):
        """验证在看图工作区评星后，修图工作区与持久化存储均同步更新。"""
        win = MainWindow()
        img_path = sample_images[0]
        win.manager.open_file(img_path, load_siblings=True)
        win._refresh_all()

        store = get_rating_store()

        # 在看图工作区的底部工具条设置 5 星
        win.canvas_toolbar.rating_edited.emit(5)
        assert store.get_rating(img_path) == 5
        assert win.canvas_toolbar.rating() == 5

        # 切换到工作室工作区，底部工具条仍显示 5 星
        win._on_workspace_index_changed(1)
        assert win.canvas_toolbar.rating() == 5

        # 在编辑工作区把评分改为 3 星
        win.canvas_toolbar.rating_edited.emit(3)
        assert store.get_rating(img_path) == 3

        # 切回看图工作区，验证底部工具条已同步为 3 星
        win._on_workspace_index_changed(0)
        assert win.canvas_toolbar.rating() == 3

        # 清除本用例产生的测试星标数据
        store.set_rating(img_path, 0)
        win.close()

    def test_star_filtering_and_clear_all_flow(self, qapp, sample_images, monkeypatch):
        """验证星级筛选下缩略图的显示/隐藏，以及一键清除星标的全局重置逻辑。"""
        win = MainWindow()
        win.show()
        win.manager.open_file(sample_images[0], load_siblings=True)
        win._refresh_all()
        wait_for_thumbnails(win.thumbnail_panel, sample_images, sample_images[0],
                            version=win.manager.list_version)

        store = get_rating_store()
        # 给三张测试图分别打分：0星、3星、5星
        store.set_rating(sample_images[0], 0)
        store.set_rating(sample_images[1], 3)
        store.set_rating(sample_images[2], 5)

        # 设置最低筛选星级为 4 星及以上 ("4+")
        win.set_rating_filter("4+")

        list_widget = win.thumbnail_panel.list_widget
        wait_until(lambda: not win.thumbnail_panel.has_pending_items,
                   "评分筛选后的胶片条完成构建")
        assert list_widget.count() == 3
        assert [list_widget.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(3) if not list_widget.item(index).isHidden()] == [sample_images[2]]

        # 模拟点击一键清除标星：拦截破坏性确认并点危险按钮
        from src.ui.widgets.message_dialog import MessageDialog
        monkeypatch.setattr(MessageDialog, "run", lambda self: "confirm")
        win._clear_all_ratings()

        # 验证全局标星已被清除
        assert store.get_rating(sample_images[0]) == 0
        assert store.get_rating(sample_images[1]) == 0
        assert store.get_rating(sample_images[2]) == 0

        # 重置筛选后所有项重新可见
        win.set_rating_filter("all")
        wait_until(lambda: not win.thumbnail_panel.has_pending_items and list_widget.count() == 3,
                   "清除筛选后完整列表恢复")
        all_visible = all(not list_widget.item(i).isHidden() for i in range(3))
        assert all_visible is True

        win.close()


class TestModuleIntegrityAndImportSafety:
    """自动化扫描遍历 UI 核心模块，杜绝未定义类型注解或模块导入丢失造成的启动崩溃。"""

    def test_all_ui_widgets_and_dialogs_import_cleanly(self):
        """扫描 src.ui 下的所有子模块，确保 import 成功且无 NameError/ImportError。"""
        ui_package = importlib.import_module("src.ui")
        pkg_path = ui_package.__path__

        failed_modules = []
        for _, modname, ispkg in pkgutil.walk_packages(pkg_path, prefix="src.ui."):
            try:
                importlib.import_module(modname)
            except Exception as exc:
                failed_modules.append((modname, str(exc)))

        assert not failed_modules, f"Failed to import the following modules: {failed_modules}"


class TestThemeStartupCoverage:
    """测试不同主题环境下的主窗口与关键组件初始化。"""

    @pytest.mark.parametrize("theme_name", [ThemeManager.THEME_DARK, ThemeManager.THEME_LIGHT])
    def test_startup_under_theme(self, qapp, theme_name):
        """测试在深色或浅色主题下主窗口能平稳创建且无样式异常。"""
        tm = ThemeManager()
        tm.apply(theme_name)
        set_theme_manager(tm)

        win = MainWindow()
        assert win is not None
        assert win.info_sidebar is not None
        assert win.top_bar is not None and win.canvas_toolbar is not None
        assert win.studio_workspace is None
        win.close()
