from __future__ import annotations

import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QThreadPool


def wait_until(predicate, description, *, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        QCoreApplication.processEvents()
        if predicate():
            return
        if time.monotonic() >= deadline:
            details = description() if callable(description) else description
            raise AssertionError(f"等待超时：{details}")
        time.sleep(0.005)


def add_assistant_model(runner, events, model_id="fixture"):
    """给助手加一个免认证的通用连接和一个模型，返回连接 ID。

    通用连接新建时默认要密钥；用例的请求由替换掉的请求服务回答，不连网络，所以像用户那样改成免认证。"""
    runner.command("add_connection", provider="generic")
    wait_until(lambda: any(kind == "configuration_added" for kind, _ in events), "添加服务")
    connection = next(value for kind, value in events if kind == "configuration_added")["connection_id"]
    runner.command("configuration", connection={
        "id": connection, "name": "API", "endpoint": "http://localhost:8080/v1", "provider": "generic",
        "protocol": "chat_completions", "authentication": "none"}, models=[])
    runner.command("add_model", connection_id=connection, model_id=model_id)
    return connection


def track_executors(monkeypatch):
    executors = []
    original_init = ThreadPoolExecutor.__init__

    def initialize(executor, *args, **kwargs):
        original_init(executor, *args, **kwargs)
        executors.append(executor)

    monkeypatch.setattr(ThreadPoolExecutor, "__init__", initialize)
    return executors


def finish_executors(executors):
    executors = tuple(dict.fromkeys(executors))
    for executor in executors:
        executor.shutdown(wait=False)
    wait_until(
        lambda: not any(thread.is_alive() for executor in executors for thread in executor._threads),
        lambda: "后台线程结束：" + ", ".join(
            thread.name for executor in executors for thread in executor._threads if thread.is_alive()),
    )


def _executor_pools(owners):
    return [pool for owner in owners if owner is not None
            for name in ("_pool", "_quick_pool", "_cleanup")
            if isinstance(pool := getattr(owner, name, None), ThreadPoolExecutor)]


def wait_for_viewer(window, path):
    normalized = os.path.normcase(os.path.normpath(str(path)))
    wait_until(
        lambda: window.manager.current_path is not None
        and os.path.normcase(os.path.normpath(window.manager.current_path)) == normalized
        and window.viewer.display_snapshot(str(path)) is not None,
        lambda: f"看图终态 {path}，状态：{window.viewer.text()}",
    )


def wait_for_thumbnails(panel, paths, selected_path, *, version):
    paths = [str(path) for path in paths]
    wait_until(
        lambda: panel.isVisible() and panel.list_version == version
        and panel.get_all_paths() == paths and not panel.has_pending_items
        and panel.get_selected_paths() == [str(selected_path)],
        lambda: f"胶片条列表版本 {version} 与选中图片 {selected_path}，"
        f"实际版本 {panel.list_version}，路径 {panel.get_all_paths()}，"
        f"选中 {panel.get_selected_paths()}，可见 {panel.isVisible()}，"
        f"待构建 {panel.has_pending_items}",
    )


def wait_for_panel_source(panel, path, *, identity=None):
    normalized = os.path.normcase(os.path.normpath(str(path)))
    wait_until(
        lambda: panel.image_path is not None
        and os.path.normcase(os.path.normpath(panel.image_path)) == normalized
        and panel.source_bundle is not None and panel._source_job_id is None
        and not panel._needs_reload and not panel.parameters_pending
        and (identity is None or panel.source_bundle.identity == identity)
        and panel._image_source_service.matches_file(panel.source_bundle.identity),
        lambda: f"工具 {panel.TOOL_ID} 的源图与参数 {path}，"
        f"任务：{panel._source_job_id}，待重载：{panel._needs_reload}，"
        f"参数准备中：{panel.parameters_pending}",
    )


def wait_for_model_discovery(models):
    completed = models._pool.submit(lambda: None)
    wait_until(completed.done, "模型目录发现及校验完成")
    completed.result()
    QCoreApplication.processEvents()


def wait_for_studio(window, path, tool_id="color_adjust"):
    normalized = os.path.normcase(os.path.normpath(str(path)))
    studio = window.studio_workspace
    if window._current_workspace_index != 1 or studio is None:
        window.switch_to_studio(tool_id)
    elif studio.active_tool_id != tool_id:
        studio.switch_tool(tool_id)

    def prepared():
        if window._studio_init_error is not None:
            raise AssertionError(f"Studio 初始化失败：{window._studio_init_error}")
        studio = window.studio_workspace
        if studio is None:
            return False
        if studio.session is None:
            if studio._load_failed:
                raise AssertionError(f"Studio 源准备失败：{studio._notice_load.text()}")
            return False
        identity = studio.core.source_identity(studio.session.core_id)
        panel = studio.tool_sidebar.get_current_panel()
        return (
            os.path.normcase(os.path.normpath(studio.session.image_path)) == normalized
            and not studio._image_loading and studio.active_tool_id == tool_id
            and identity is not None and os.path.normcase(os.path.normpath(identity.path)) == normalized
            and studio.core.image_sources.matches_file(identity)
            and panel is not None and panel.TOOL_ID == tool_id
            and studio._panel_sessions.get(tool_id) is studio.session
            and panel._source_identity == identity
        )

    wait_until(prepared, f"Studio 源会话 {path}")
    studio = window.studio_workspace
    session = studio.session
    generation = studio._image_load_generation
    wait_for_panel_source(studio.tool_sidebar.get_current_panel(), path,
                          identity=studio.core.source_identity(session.core_id))
    assert studio.session is session and studio._image_load_generation == generation, "Studio 源准备期间请求已替换"
    return studio


def close_studio_workspace(studio):
    if sip.isdeleted(studio):
        return
    wait_until(lambda: not studio._commit_busy, "Studio 提交结束")
    studio.assistant_panel.invalidate()
    if studio._source_load_cancel is not None:
        studio._source_load_cancel()
    studio.shutdown_preview()
    studio._models_runner.close()
    studio.viewer.shutdown()
    studio.thumbnail_panel.shutdown()
    preview_runner = studio._panel_services["preview_runner"]
    for panel in studio._tool_panels.values():
        panel.cleanup()
    preview_runner.shutdown()
    pools = _executor_pools([studio._project_runner, studio._histogram_runner,
                             studio._original_runner, preview_runner, studio.viewer._load_runner,
                             studio.thumbnail_panel._load_runner])
    wait_until(lambda: studio._thread_pool.activeThreadCount() == 0
               and studio._output_pool.activeThreadCount() == 0, "Studio 渲染与输出任务结束")
    finish_executors(pools)
    studio.release_preview_source()
    for session in studio.edit_sessions.sessions():
        studio.edit_sessions.close(session.image_path)
        assert not studio.core.get_session(session.core_id).success
    studio.session = None
    studio.core.tools.close()
    finish_executors(_executor_pools([studio.core.models, studio.core.tools._executors]))


def close_main_window(window, *, delete=True):
    if sip.isdeleted(window):
        return
    studio = window.studio_workspace
    if not window._caches_closed_for_exit and studio is not None:
        wait_until(lambda: not studio._commit_busy, "Studio 提交结束")
        with patch.object(studio, "has_pending_edits", return_value=False):
            window.close()
            wait_until(lambda: window._caches_closed_for_exit, "主窗口草稿及缓存关闭")
    elif not window._caches_closed_for_exit:
        window.close()
        wait_until(lambda: window._caches_closed_for_exit, "主窗口草稿及缓存关闭")
    window.shutdown_previews()
    wait_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0, "Qt 全局任务结束")
    studio = window.studio_workspace
    if studio is not None:
        close_studio_workspace(studio)
    window._runtime_service.close()
    finish_executors(_executor_pools([window._browse_runner, window._info_runner,
                                     window._draft_runner, window._studio_source_runner,
                                     window._runtime_service, window.viewer._load_runner,
                                     window.thumbnail_panel._load_runner]))
    if delete:
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def isolate_paths(root, monkeypatch):
    from PyQt6.QtCore import QSettings, QStandardPaths
    from src.utils import model_paths

    root = Path(root)
    original_settings_init = QSettings.__init__
    original_location = QStandardPaths.writableLocation

    def settings_init(settings, *args, **kwargs):
        if len(args) == 2 and all(isinstance(arg, str) for arg in args):
            organization, application = args
            target = root / "settings" / organization / f"{application}.ini"
            target.parent.mkdir(parents=True, exist_ok=True)
            original_settings_init(settings, str(target), QSettings.Format.IniFormat)
            settings.setFallbacksEnabled(False)
        else:
            original_settings_init(settings, *args, **kwargs)

    # 与正式环境一样，应用资料目录就是环境变量所指目录下的 OpenPictureStudio/OpenPictureStudio
    locations = {
        QStandardPaths.StandardLocation.AppDataLocation: "appdata/OpenPictureStudio/OpenPictureStudio",
        QStandardPaths.StandardLocation.AppLocalDataLocation: "localappdata/OpenPictureStudio/OpenPictureStudio",
        QStandardPaths.StandardLocation.AppConfigLocation: "config",
        QStandardPaths.StandardLocation.CacheLocation: "cache",
    }

    def writable_location(location):
        if location not in locations:
            return original_location(location)
        target = root / locations[location]
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def model_root(*, create=True):
        target = root / "models"
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    # 资料根目录、存储设置与工作目录按环境变量和系统临时目录定位，同样指到隔离目录，测试不碰真实资料
    for name, folder in (("LOCALAPPDATA", "localappdata"), ("APPDATA", "appdata")):
        (root / folder).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(root / folder))
    (root / "temp").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(root / "temp"))
    monkeypatch.setattr(QSettings, "__init__", settings_init)
    monkeypatch.setattr(QStandardPaths, "writableLocation", writable_location)
    monkeypatch.setattr(model_paths, "get_project_model_root", model_root)
    monkeypatch.setattr(model_paths, "get_runtime_root", lambda: root / "runtimes")
    for module_name in ("src.core.ai_enhancer", "src.core.denoiser"):
        module = sys.modules.get(module_name)
        if module is not None:
            monkeypatch.setattr(module, "get_project_model_root", model_root)


def subprocess_setup():
    return (
        "import os, pytest\n"
        "from tests.support import isolate_paths\n"
        "test_patch = pytest.MonkeyPatch()\n"
        "isolate_paths(os.environ['OPS_TEST_ROOT'], test_patch)\n"
        "from src.ui.main_window import MainWindow\n"
    )
