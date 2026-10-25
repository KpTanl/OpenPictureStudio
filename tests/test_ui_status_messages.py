"""状态栏撤销后的消息去向：一句话的消息走轻提示，需要操作的状态用带“重试”的提示条。"""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtCore import QPoint  # noqa: E402
from PyQt6.QtGui import QColor, QPalette  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QStatusBar, QWidget  # noqa: E402

from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.ui.widgets.toast import Toast  # noqa: E402
from src.ui.widgets.top_bar import EDITOR, VIEWER  # noqa: E402

THEMES = ("dark", "light", "beige")


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


def _wait_until(predicate, timeout_ms=5000):
    for _ in range(timeout_ms // 5):
        QApplication.processEvents()
        if predicate():
            return True
        QTest.qWait(5)
    return False


@pytest.fixture
def window(theme, language, tmp_path):
    from src.ui.main_window import MainWindow

    path = str(tmp_path / "photo.png")
    Image.new("RGB", (400, 300), (60, 120, 180)).save(path)
    win = MainWindow()
    win.resize(1280, 800)
    win.show()
    win.manager.open_file(path, load_siblings=True)
    win._refresh_all()
    assert _wait_until(lambda: win.viewer.has_image)
    yield win, path
    win.shutdown_previews()
    win.deleteLater()


def test_toast_without_a_duration_stays_until_replaced_or_dismissed(theme):
    host = QWidget()
    host.resize(400, 200)
    host.show()
    toast = Toast(host)
    toast.show_message("第一条", 20)
    toast.show_message("正在关闭…", 0)
    QTest.qWait(80)
    assert not toast.isHidden() and toast.text() == "正在关闭…"
    toast.show_message("第三条", 20)
    QTest.qWait(80)
    assert toast.isHidden()
    toast.show_message("正在关闭…", 0)
    toast.dismiss()
    assert toast.isHidden()
    host.close()


def test_main_window_has_no_status_bar_and_reports_through_the_toast(window, monkeypatch):
    win, _path = window
    assert win.findChild(QStatusBar) is None and not hasattr(win, "_update_statusbar")
    assert win._notices.isHidden()

    monkeypatch.setattr(win, "_start_transform_save", lambda *args: None)
    win._rotate_90()
    assert not win.toast.isHidden() and "300 × 400" in win.toast.text()
    win._on_transform_saved(SimpleNamespace(
        target_path=SimpleNamespace(parent="missing"), generation=win._save_generation,
        source_revision=win.manager.current_revision, source_path=win.manager.current_path), "磁盘已满")
    assert "磁盘已满" in win.toast.text() and not win.toast.isHidden()
    assert win.findChild(QStatusBar) is None


def test_viewer_load_status_shows_a_notice_with_retry(window, monkeypatch):
    win, _path = window
    retries = []
    monkeypatch.setattr(win.viewer, "retry_load", lambda: retries.append(True))
    win._notice_viewer_load.action_triggered.disconnect()
    win._notice_viewer_load.action_triggered.connect(win.viewer.retry_load)
    bar = win._notice_viewer_load

    win.viewer.load_status_changed.emit("error.image.memory_budget", True)
    QApplication.processEvents()
    assert not win._notices.isHidden() and not bar.isHidden() and win._notice_draft.isHidden()
    assert bar.text() == win._tr("error.image.memory_budget")
    assert not bar.action_button.isHidden() and bar.action_button.text() == win._tr("common.retry")
    # 提示条在顶栏下方、画布上方，占满窗口宽度
    assert win._notices.mapTo(win, QPoint(0, 0)).y() == win.top_bar.height()
    assert win._notices.width() == win.width()
    assert win.workspace_stack.mapTo(win, QPoint(0, 0)).y() == win.top_bar.height() + win._notices.height()
    bar.action_button.click()
    assert retries == [True]

    # 不能重试的状态只有说明
    win.viewer.load_status_changed.emit("error.source.decode_failed", False)
    assert not bar.isHidden() and bar.action_button.isHidden()

    win.viewer.load_status_changed.emit("", False)
    assert bar.isHidden() and win._notices.isHidden()


def test_viewer_load_notice_belongs_to_the_viewer_workspace(window):
    win, _path = window
    win.viewer.load_status_changed.emit("viewer.memory_camera_preview", True)
    assert not win._notice_viewer_load.isHidden()
    # 进入编辑时看图画布清空，提示条随之收起；编辑期间看图的状态不显示
    win.top_bar.workspace_requested.emit(EDITOR)
    assert win._notice_viewer_load.isHidden() and win._notices.isHidden()
    win.viewer.load_status_changed.emit("viewer.memory_camera_preview", True)
    assert win._notice_viewer_load.isHidden()
    win.top_bar.workspace_requested.emit(VIEWER)
    win.viewer.load_status_changed.emit("viewer.memory_camera_preview", True)
    assert not win._notice_viewer_load.isHidden()


def test_draft_failure_shows_a_notice_until_saved(window, language, monkeypatch):
    win, _path = window
    retries = []
    monkeypatch.setattr(win._draft_runner, "retry", lambda: retries.append(True))
    win._notice_draft.action_triggered.disconnect()
    win._notice_draft.action_triggered.connect(win._draft_runner.retry)
    bar = win._notice_draft

    win._on_draft_failed(SimpleNamespace(message_key="studio.draft_save_failed"))
    assert not win._notices.isHidden() and not bar.isHidden() and win._notice_viewer_load.isHidden()
    assert bar.text() == language.translate("studio.draft_save_failed")
    assert bar.action_button.text() == language.translate("common.retry")
    bar.action_button.click()
    assert retries == [True]

    # 切换工作区不影响它；切换语言时原位改文字
    win.top_bar.workspace_requested.emit(EDITOR)
    assert not bar.isHidden()
    win.top_bar.workspace_requested.emit(VIEWER)
    language.set_language("en")
    QApplication.processEvents()
    assert bar.text() == language.translate("studio.draft_save_failed") and bar.action_button.text() == "Retry"

    win._on_draft_saved()
    assert bar.isHidden() and win._notices.isHidden()


@pytest.mark.parametrize("name", THEMES)
def test_window_notices_take_their_colors_from_the_theme(window, theme, name):
    win, _path = window
    theme.apply(name)
    win.viewer.load_status_changed.emit("error.image.memory_budget", True)
    QApplication.processEvents()
    tokens = get_theme_tokens()
    strip, bar = win._notices, win._notice_viewer_load
    assert not strip.styleSheet() and not bar.styleSheet()
    image = strip.grab().toImage()
    assert image.pixelColor(2, 2).name() == QColor(tokens["surface"]).name()
    assert image.pixelColor(strip.width() // 2, strip.height() - 1).name() == QColor(tokens["hairline_solid"]).name()
    assert bar.grab().toImage().pixelColor(bar.width() // 2, 3).name() != QColor(tokens["surface"]).name()


def test_studio_reports_through_the_window_toast_and_its_own_notices(window, language):
    win, path = window
    win.top_bar.workspace_requested.emit(EDITOR)
    assert _wait_until(lambda: win.studio_workspace is not None
                       and win.studio_workspace.session is not None
                       and win.studio_workspace.tool_sidebar.get_current_panel() is not None, 60000)
    studio = win.studio_workspace
    QApplication.processEvents()
    assert win.findChild(QStatusBar) is None

    # 编辑工作区只请求轻提示，由主窗口显示在当前画布上
    studio._toast("图片已导出", 4000)
    assert win.toast.text() == "图片已导出" and not win.toast.isHidden()
    assert win.toast.parent() is studio.scroll_area

    # 当前工具的面板报告的消息同样走轻提示；不在前台的面板不显示
    studio._on_panel_status("color_adjust", "参数无效")
    assert win.toast.text() == "参数无效"
    studio._on_panel_status("crop", "不在前台")
    assert win.toast.text() == "参数无效"

    # 加载失败：右侧栏顶部的提示条带“重试”，重试重新装载这张图片
    requests = []
    studio._source_load_request = requests.append
    studio.show_image_load_error(SimpleNamespace(message_key="error.source.not_found"))
    bar = studio._notice_load
    assert not studio._notices.isHidden() and not bar.isHidden()
    assert bar.text() == language.translate("error.source.not_found")
    assert bar.action_button.text() == language.translate("common.retry") and bar.action_button.isEnabled()
    language.set_language("en")
    QApplication.processEvents()
    assert bar.text() == language.translate("error.source.not_found") and bar.action_button.text() == "Retry"
    # 进入过编辑后切换语言，顶栏、工具栏与当前面板同样原位改文字
    assert win.top_bar._export_button.text() == "Export" and win.top_bar._info_toggle.text() == "Photo Info"
    assert studio.tool_rail.button("color").text() == language.translate("rail.color")
    assert win.window_actions.action("undo").text() == language.translate("studio.undo")
    panel = studio._tool_panels["color_adjust"]
    assert panel._preset_label.text() == language.translate("color_adjust.presets")
    language.set_language("zh-CN")
    bar.action_button.click()
    assert requests == [os.path.normpath(path)]
    studio.prepare_image_load(None)
    assert bar.isHidden() and studio._notices.isHidden()

    # 等待完整编辑源：一条没有操作的说明，就绪或换图后收起
    studio._awaiting_full_source = True
    studio._update_actions_state()
    waiting = studio._notice_source
    assert not waiting.isHidden() and waiting.text() == language.translate("studio.full_source_loading")
    assert waiting.action_button.isHidden() and waiting.kind() == "info"
    studio.prepare_image_load(None)
    assert waiting.isHidden() and studio._notices.isHidden()


def test_studio_confirms_manual_preview_states_and_keeps_own_gaps_out_of_the_top_notice(window, language):
    win, _path = window
    win.top_bar.workspace_requested.emit(EDITOR)
    assert _wait_until(lambda: win.studio_workspace is not None
                       and win.studio_workspace.session is not None
                       and win.studio_workspace.tool_sidebar.get_current_panel() is not None, 60000)
    studio = win.studio_workspace
    studio.switch_tool("ai_enhance")
    assert _wait_until(lambda: studio.active_tool_id == "ai_enhance"
                       and studio.tool_sidebar.get_current_panel() is studio._tool_panels.get("ai_enhance")
                       and studio._preview_queue.active is None and studio._preview_queue.pending is None, 30000)
    panel = studio._tool_panels["ai_enhance"]
    queue = studio._preview_queue
    assert panel.external_preview and panel._footer.parentWidget() is studio.tool_sidebar._footer_host

    # 工具进入本次编辑之后宿主才向面板报告；这里先记入一份全为不处理的参数
    panel.notify_params_changed(panel.parameters)
    assert "ai_enhance" in studio.modified_tools()

    # 入队即报告开始；请求完成并被接受后报告结局
    studio._schedule_pipeline_render()
    assert panel._preview.state == "processing" and queue.pending is not None
    assert studio._manual_preview_tickets == {queue.pending.generation: panel.preview_ticket}
    assert _wait_until(lambda: queue.active is None and queue.pending is None, 30000)
    assert panel._preview.state == "updated" and studio._manual_preview_tickets == {}

    # 还没开始的请求被取消：没有完成回调，立即确认
    studio._schedule_pipeline_render()
    studio._render_timer.stop()
    panel._cancel_processing()
    assert queue.pending is None and panel._preview.state == "cancelled"

    # 正在跑的请求被取消：等它退出后才确认
    studio._schedule_pipeline_render()
    studio._render_timer.stop()
    running = queue.take()
    panel._cancel_processing()
    assert queue.active is running and running.is_cancelled() and panel._preview.state == "cancelling"
    studio._on_pipeline_render_finished(running, None, None, None)
    assert queue.active is None and panel._preview.state == "cancelled"
    assert studio._manual_preview_tickets == {}

    # 宿主自己中断（不是用户取消）：回到尚未预览
    studio._schedule_pipeline_render()
    studio._render_timer.stop()
    studio.stop_preview()
    assert panel._preview.state == "not_previewed"

    # 参数变了以后，旧请求的结局不再影响面板
    studio._schedule_pipeline_render()
    studio._render_timer.stop()
    stale = queue.take()
    panel.restore_params(panel.parameters)
    studio._on_pipeline_render_finished(stale, None, None, None)
    assert panel._preview.state == "not_previewed"
    assert _wait_until(lambda: queue.active is None and queue.pending is None, 30000)

    # 当前工具自己缺模型：面板里说明，顶部的提示条不重复；换到别的工具后它才是项目一级的事
    gap = {"code": "TOOL_RESOURCE_MISSING", "message_key": "error.tool.resource_missing",
           "details": {"tool_id": "ai_enhance", "node_id": "n1"}}
    other = {"code": "TOOL_RESOURCE_MISSING", "message_key": "error.tool.resource_missing",
             "details": {"tool_id": "beauty", "node_id": "n2"}}
    studio.session.issues = [gap]
    studio._update_actions_state()
    assert studio._notice_resources.isHidden()
    studio.session.issues = [gap, other]
    studio._update_actions_state()
    assert not studio._notice_resources.isHidden()
    assert "beauty" in studio._notice_resources.text() and "ai_enhance" not in studio._notice_resources.text()
    studio.sidebar_tabs.setCurrentIndex(1)
    studio._update_actions_state()
    assert "ai_enhance" in studio._notice_resources.text()
    studio.sidebar_tabs.setCurrentIndex(0)
    studio.session.issues = []
    studio._update_actions_state()
    assert studio._notice_resources.isHidden()
