import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 工作目录的回收线程池随模块导入创建、伴随整个进程；先于各测试导入，免得被当作某项测试的线程池在收尾时关闭
import src.application.storage_workspaces  # noqa: E402,F401


@pytest.fixture(autouse=True)
def isolated_app_state(tmp_path, monkeypatch, qapp):
    from PyQt6 import sip
    from PyQt6.QtCore import QCoreApplication, QEvent
    from src.core import rating_store
    from tests.support import (
        close_main_window, close_studio_workspace, finish_executors,
        isolate_paths, track_executors, wait_until,
    )

    isolate_paths(tmp_path / "app", monkeypatch)
    executors = track_executors(monkeypatch)
    monkeypatch.setenv("OPS_TEST_ROOT", str(tmp_path / "subprocess"))
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.setattr(rating_store, "_rating_store", None)
    # 主窗口启动时会向系统登记常驻内存下限；测试进程不登记，要测登记本身的用例直接调用原函数
    monkeypatch.setattr("src.application.system_profile.set_working_set_minimum", lambda size: True)
    theme = sys.modules.get("src.ui.theme")
    language = sys.modules.get("src.i18n.translation_service")
    previous_theme = theme.get_theme_manager() if theme else None
    theme_name = theme._ACTIVE_THEME_NAME if theme else "dark"
    previous_language = language.get_translation_service() if language else None
    language_code = previous_language.get_language() if previous_language else None
    if previous_theme is not None:
        monkeypatch.setattr(previous_theme, "_current", previous_theme._current)
        monkeypatch.setattr(previous_theme, "_canvas_background", previous_theme._canvas_background)
    app = QCoreApplication.instance()
    widgets_module = sys.modules.get("PyQt6.QtWidgets")
    widget_app = app if widgets_module and isinstance(app, widgets_module.QApplication) else None
    old_widgets = set(widget_app.allWidgets()) if widget_app else set()
    palette = widget_app.palette() if widget_app else None
    style = widget_app.styleSheet() if widget_app else ""
    theme_property = app.property("ops.theme") if app else None
    errors = []

    def cleanup(operation):
        try:
            operation()
        except Exception as error:
            errors.append(error)

    try:
        yield
    finally:
        app = QCoreApplication.instance()
        widgets_module = sys.modules.get("PyQt6.QtWidgets")
        widget_app = app if widgets_module and isinstance(app, widgets_module.QApplication) else None
        widgets = [widget for widget in widget_app.allWidgets() if widget not in old_widgets] if widget_app else []
        main_module = sys.modules.get("src.ui.main_window")
        studio_module = sys.modules.get("src.ui.studio_workspace")
        panel_module = sys.modules.get("src.ui.panels.base_tool_panel")
        main_windows = [widget for widget in widgets if not sip.isdeleted(widget)
                        and main_module and isinstance(widget, main_module.MainWindow)]
        studios = [widget for widget in widgets if not sip.isdeleted(widget)
                   and studio_module and isinstance(widget, studio_module.StudioWorkspace)]
        attached_studios = {getattr(window, "studio_workspace", None) for window in main_windows}
        studio_panels = {panel for studio in studios
                         for panel in getattr(studio, "_tool_panels", {}).values()}
        for window in main_windows:
            cleanup(lambda window=window: close_main_window(window, delete=False))
        for studio in studios:
            if not sip.isdeleted(studio) and studio not in attached_studios:
                cleanup(lambda studio=studio: close_studio_workspace(studio))
        for widget in widgets:
            if (not sip.isdeleted(widget) and widget not in studio_panels
                    and panel_module and isinstance(widget, panel_module.BaseToolPanel)):
                cleanup(widget.cleanup)
        from PyQt6.QtCore import QThreadPool
        pool = QThreadPool.globalInstance()
        cleanup(lambda: wait_until(
            lambda: pool.activeThreadCount() == 0, "测试 Qt 任务结束"))
        for widget in widgets:
            if not sip.isdeleted(widget) and widget.parent() is None:
                widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if app is not None:
            app.processEvents()
        cleanup(lambda: finish_executors(executors))
        theme = sys.modules.get("src.ui.theme")
        language = sys.modules.get("src.i18n.translation_service")
        if theme is not None:
            cleanup(lambda: theme.set_theme_manager(previous_theme))
            cleanup(lambda: theme._set_active_theme(theme_name))
        if language is not None:
            if previous_language is not None:
                cleanup(lambda: previous_language.set_language(language_code))
            cleanup(lambda: language.set_translation_service(previous_language))
        if widget_app is not None:
            cleanup(lambda: widget_app.setPalette(palette if palette is not None else widget_app.style().standardPalette()))
            cleanup(lambda: widget_app.setStyleSheet(style))
        if app is not None:
            cleanup(lambda: app.setProperty("ops.theme", theme_property))
        if errors:
            details = "\n".join(f"{type(error).__name__}: {error}" for error in errors)
            raise RuntimeError(f"测试资源清理失败：\n{details}") from errors[0]


@pytest.fixture
def window_factory(isolated_app_state, qapp, request):
    from src.ui.main_window import MainWindow
    from tests.support import close_main_window

    def create(**kwargs):
        window = MainWindow(**kwargs)
        request.addfinalizer(lambda: close_main_window(window))
        return window

    return create


@pytest.fixture
def image_sources():
    from src.application.image_source_service import ImageSourceService
    sources = ImageSourceService()
    try:
        yield sources
    finally:
        sources.close()


@pytest.fixture
def tool_services(image_sources):
    from src.application.panel_render_service import PanelRenderService
    from src.application.tool_runtime import ToolRuntime
    runtime = ToolRuntime(compute_budget=image_sources.compute,
                          resource_budget=image_sources.resources,
                          execution_threads=image_sources.policy.operator_threads)
    try:
        yield {"image_source_service": image_sources, "render_service": PanelRenderService(runtime)}
    finally:
        runtime.close()


@pytest.fixture
def panel_factory(tool_services, request):
    from PyQt6.QtCore import QCoreApplication, QEvent
    from src.ui.panels.registry import create_tool_panel

    def create(tool_id, path, **kwargs):
        panel = create_tool_panel(tool_id, path, services=tool_services, **kwargs)
        assert panel is not None, tool_id

        def close():
            getattr(panel, "close_panel", panel.cleanup)()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        request.addfinalizer(close)
        return panel

    return create


@pytest.fixture
def available_neural_models(monkeypatch):
    from pathlib import Path
    from src.utils import model_paths
    root = Path(__file__).parent / "models"
    if not (root / "deblurring_nafnet_2025may.onnx").is_file():
        pytest.skip("真实画质增强验收需本地固定 NAFNet Deblur 权重 deblurring_nafnet_2025may.onnx")
    monkeypatch.setattr(model_paths, "get_legacy_model_roots", lambda: (root,))
