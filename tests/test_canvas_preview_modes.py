from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from PyQt6.QtCore import QRectF, QSettings, QSize, QTimer
from PyQt6.QtGui import QColor, QFontDatabase, QImage, QPainter
from PyQt6.QtWidgets import QScrollArea
from PyQt6 import sip

from src.application.image_resources import ImageResourcePolicy, ImageResourcePool
from src.application.image_source_service import SourceIdentity
from src.application.studio_preload import StudioPreloadSettings
from src.application.studio_preview_queue import StudioPreviewContext, StudioPreviewQueue, StudioPreviewRequest
from src.application.tool_runtime import ToolRuntime
from src.core.contracts import WorkflowGraph
from src.core.contracts.result import CoreResult, ErrorCode
from src.ui.image_viewer import ImageViewer
from src.ui.services.canvas_scale_runner import CanvasScaleRunner, ScaledDisplay
from tests.support import wait_for_studio, wait_for_thumbnails, wait_until
from src.utils.app_info import APP_NAME
from src.i18n import TranslationService, get_translation_service, set_translation_service


@pytest.fixture
def canvas_fonts(qapp):
    previous = get_translation_service()
    set_translation_service(TranslationService(default_language="en"))
    fonts = []
    for name in ("segoeui.ttf", "msyh.ttc"):
        path = Path("C:/Windows/Fonts") / name
        if path.is_file():
            fonts.append(QFontDatabase.addApplicationFont(str(path)))
    yield
    set_translation_service(previous)
    for font in fonts:
        if font >= 0:
            QFontDatabase.removeApplicationFont(font)


@pytest.mark.parametrize("mode", StudioPreloadSettings.CANVAS_MODES)
def test_canvas_mode_persists_without_changing_cache_limits(tmp_path, mode):
    settings = QSettings(str(tmp_path / "preview.ini"), QSettings.Format.IniFormat)
    value = StudioPreloadSettings(canvas_preview_mode=mode, memory_mib=128, before=7)
    value.save(settings)
    assert StudioPreloadSettings.load(settings) == value
    settings.setValue("canvas_preview_mode", "unsupported")
    assert StudioPreloadSettings.load(settings) == replace(value, canvas_preview_mode="auto")


def test_switching_canvas_mode_rejects_an_active_old_result():
    identity = SourceIdentity("image.png", "0" * 64, 1, 1)
    context = StudioPreviewContext("session", identity, 0, None)
    queue = StudioPreviewQueue()
    old = queue.submit(StudioPreviewRequest(1, context, None, None, (4000, 3000)))
    assert queue.take() is old
    new = queue.submit(StudioPreviewRequest(2, replace(context, preview_mode="full"), None, None, (4000, 3000)))
    assert old.is_cancelled() and not queue.accepts(old) and queue.accepts(new)
    queue.finish(old)
    assert queue.take() is new


def test_full_fallback_reports_the_successful_input_resolution():
    small = SimpleNamespace(pixels=np.zeros((40, 60, 3), np.float32), is_proxy=True)
    full = SimpleNamespace(pixels=np.zeros((120, 180, 3), np.float32), is_proxy=False)
    calls = []

    def execute(source, workflow, **options):
        calls.append(source)
        return (CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, details={"requires_full_input": True})
                if source.is_proxy else CoreResult.ok(source))

    @contextmanager
    def borrow(cancelled):
        yield SimpleNamespace(value=full)

    runtime = ToolRuntime.__new__(ToolRuntime)
    runtime._resource_budget = None
    snapshot = SimpleNamespace(is_cancelled=lambda: False, executor=SimpleNamespace(execute=execute))
    result = runtime.execute_preview(snapshot, small, WorkflowGraph(), full_source=borrow, describe=True)
    assert result.success and result.data.input_size == (180, 120)
    assert result.data.input_is_proxy is False and result.data.source is full
    assert calls == [small, full]


def _image(color, size=(1600, 1000)):
    image = QImage(*size, QImage.Format.Format_RGBA8888)
    image.fill(QColor(color))
    return image


def _viewer(qapp, runner):
    area = QScrollArea()
    viewer = ImageViewer(canvas_scale_runner=runner)
    area.setWidget(viewer)
    area.resize(600, 400)
    area.show()
    qapp.processEvents()
    return area, viewer


def test_scaled_comparison_pair_is_reused_and_original_pixels_survive_zoom(qapp):
    resources = ImageResourcePool(ImageResourcePolicy())
    runner = CanvasScaleRunner(resources)
    area, viewer = _viewer(qapp, runner)
    first, second = _image("#ff3300"), _image("#0033ff")
    viewer.set_display_pair(first, QSize(1600, 1000), second, (1600, 1000))
    viewer.set_zoom_percent(20)
    wait_until(lambda: len(viewer._scaled_pixmaps) == 2, "对比两边缩小完成")
    retained = viewer._scaled_display
    assert viewer._pixmap.size() == QSize(1600, 1000)
    assert {p.size() for p in viewer._scaled_pixmaps.values()} == {QSize(320, 200)}
    for _ in range(3):
        viewer.set_display_pair(second, QSize(1600, 1000), first, (1600, 1000))
        viewer.set_display_pair(first, QSize(1600, 1000), second, (1600, 1000))
    assert viewer._scaled_display is retained
    viewer.set_zoom_percent(100)
    assert not viewer._scaled_pixmaps and viewer._pixmap.size() == QSize(1600, 1000)
    assert viewer._pixmap.toImage().pixelColor(800, 500) == QColor("#ff3300")
    viewer.shutdown()
    runner.shutdown()
    area.close()
    assert resources._reserved == 0
    resources.close()


def test_canvas_scaling_keeps_only_active_and_latest_pending(qapp, monkeypatch):
    resources = ImageResourcePool(ImageResourcePolicy())
    runner = CanvasScaleRunner(resources)
    started, release = Event(), Event()
    original = runner._run
    seen, delivered = [], []

    def blocked(job):
        seen.append(job.key)
        if job.key == ("first",):
            started.set()
            release.wait(5)
        original(job)

    monkeypatch.setattr(runner, "_run", blocked)
    runner.ready.connect(lambda owner, key, packet: (delivered.append(key), packet.close() if packet else None))
    image = _image("#223344", (600, 400))
    entries = ((1, image, QSize(120, 80)),)
    owner = object()
    try:
        assert runner.submit(owner, ("first",), entries)
        assert started.wait(2)
        assert runner.submit(owner, ("discarded",), entries)
        pending = runner._pending
        assert runner.submit(owner, ("last",), entries)
        assert pending.images == () and pending.input_memory is None
        release.set()
        wait_until(lambda: runner._active is None, "只完成最新缩小任务")
        assert seen == [("first",), ("last",)] and delivered == [("last",)]
        assert resources._reserved == 0
    finally:
        release.set()
        runner.shutdown()
        resources.close()


def test_high_dpi_scale_uses_device_pixels_and_releases_on_widget_destruction(qapp, monkeypatch):
    resources = ImageResourcePool(ImageResourcePolicy())
    runner = CanvasScaleRunner(resources)
    area, viewer = _viewer(qapp, runner)
    monkeypatch.setattr(viewer, "devicePixelRatioF", lambda: 1.5)
    viewer.set_qimage(_image("#557799"), reference_size=QSize(1600, 1000))
    viewer.set_zoom_percent(20)
    wait_until(lambda: bool(viewer._scaled_pixmaps), "高 DPI 显示位图")
    pixmap = next(iter(viewer._scaled_pixmaps.values()))
    assert pixmap.size() == QSize(480, 300) and pixmap.devicePixelRatio() == 1.5
    assert resources._reserved > 0
    sip.delete(viewer)
    assert resources._reserved == 0
    runner.shutdown()
    area.close()
    resources.close()


def test_late_scaled_result_cannot_replace_new_source(qapp):
    resources = ImageResourcePool(ImageResourcePolicy())
    runner = CanvasScaleRunner(resources)
    area, viewer = _viewer(qapp, runner)
    viewer.set_qimage(_image("#ff0000"), reference_size=QSize(1600, 1000))
    viewer.set_zoom_percent(20)
    old_key, _ = viewer._scale_spec()
    viewer.set_qimage(_image("#0000ff"), preserve_zoom=True, reference_size=QSize(1600, 1000))
    packet = ScaledDisplay({1: _image("#ff0000", (320, 200))}, resources.try_hold_memory(320 * 200 * 12))
    runner.ready.emit(viewer._scale_owner, old_key, packet)
    assert packet.memory is None and not packet.images
    assert viewer._pixmap.toImage().pixelColor(100, 100) == QColor("#0000ff")
    assert not viewer._scaled_pixmaps
    viewer.shutdown()
    runner.shutdown()
    area.close()
    resources.close()


def test_scaling_budget_failure_keeps_realtime_fallback(qapp):
    resources = ImageResourcePool(ImageResourcePolicy(memory_bytes=8 * 1024 ** 2))
    runner = CanvasScaleRunner(resources)
    image = _image("#556677", (1000, 1000))
    assert not runner.submit(object(), ("over-budget",), ((1, image, QSize(400, 400)),))
    assert runner._active is None and resources._reserved == 0
    runner.shutdown()
    resources.close()


def test_shutdown_releases_a_scale_job_before_its_worker_starts(qapp):
    resources = ImageResourcePool(ImageResourcePolicy())
    runner = CanvasScaleRunner(resources)
    started, release = Event(), Event()

    def occupied():
        started.set()
        release.wait(5)

    runner._pool.submit(occupied)
    try:
        assert started.wait(2)
        assert runner.submit(object(), ("queued",), ((1, _image("#556677"), QSize(320, 200)),))
        assert resources._reserved > 0
        runner.shutdown()
        release.set()
        wait_until(lambda: resources._reserved == 0, "关闭时释放未启动的缩小任务")
    finally:
        release.set()
        resources.close()


def test_source_completion_can_prepare_only_a_small_comparison(qapp):
    from src.ui.services.studio_original_runner import StudioOriginalRunner

    resources = ImageResourcePool(ImageResourcePolicy())
    completed, packets = [], []
    identity = SourceIdentity("image.png", "0" * 64, 1, 1)

    def complete(session_id, source, **options):
        completed.append((session_id, source))
        return CoreResult.ok()

    sources = SimpleNamespace(resources=resources, dimensions=lambda path: (3200, 2000),
        load_preview=lambda path, **options: (Image.new("RGB", (1600, 1000)), (3200, 2000)))
    runner = StudioOriginalRunner(sources, complete_source=complete)
    runner.ready.connect(lambda key, image, error: packets.append((key, image, error)))
    try:
        runner.submit("session", identity, full=False)
        wait_until(lambda: bool(packets), "源接管后的小尺寸改前画面")
        key, image, error = packets[0]
        assert error is None and key == ("session", identity)
        assert completed == [("session", identity)]
        assert image.preview.size() == QSize(1600, 1000) and image.full is None
        assert image.full_memory is None
        image.close()
        assert resources._reserved == 0
    finally:
        runner.shutdown()
        resources.close()


@pytest.mark.parametrize("language", ("zh-CN", "en", "fr"))
def test_canvas_controls_translate_sizes_and_computation_path(qapp, canvas_fonts, language):
    from PyQt6.QtGui import QAction
    from src.application.studio_preview_cache import CanvasPreviewState
    from src.ui.dialogs.settings_dialog import SettingsDialog, SettingsValues
    from src.ui.widgets.canvas_toolbar import CanvasToolbar, CanvasToolbarActions, EDITOR

    service = get_translation_service()
    service.set_language(language)
    dialog = SettingsDialog(values=SettingsValues(preload=StudioPreloadSettings(canvas_preview_mode="full")))
    actions = [QAction(str(index), dialog) for index in range(9)]
    toolbar = CanvasToolbar(CanvasToolbarActions(*actions[:4], actions[4:7], actions[7:]), service.translate, dialog)
    try:
        assert dialog.canvas_mode.currentData() == "full"
        for index, mode in enumerate(StudioPreloadSettings.CANVAS_MODES):
            assert dialog.canvas_mode.itemText(index) == service.translate("canvas.preview.mode." + mode)
        toolbar.set_mode(EDITOR)
        toolbar.set_preview_state(CanvasPreviewState("full", "full", (6720, 4480), (6720, 4480), False))
        label = toolbar._preview_label
        assert label.accessibleName() == service.translate("canvas.preview.state.full") + " · 6720×4480"
        assert service.translate("canvas.preview.compute.full", width=6720, height=4480) in label.toolTip()
        assert "{width}" not in label.toolTip() and "{height}" not in label.toolTip()
        toolbar.set_preview_available_width(500)
        assert not label.text() and "6720×4480" in label.accessibleName()
    finally:
        dialog.close()
        sip.delete(dialog)


def test_smooth_reduction_avoids_aliasing_on_fine_detail(qapp):
    yy, xx = np.indices((1024, 1536))
    gray = np.where((xx // 2 + yy // 2) % 2, 240, 16).astype(np.uint8)
    pixels = np.repeat(gray[..., None], 3, axis=2)
    source = QImage(pixels.data, 1536, 1024, 1536 * 3, QImage.Format.Format_RGB888).copy()
    resources = ImageResourcePool(ImageResourcePolicy())
    runner = CanvasScaleRunner(resources)
    packets = []
    runner.ready.connect(lambda owner, key, packet: packets.append(packet))
    runner.submit(object(), ("fine-detail",), ((1, source, QSize(257, 171)),))
    wait_until(lambda: bool(packets), "细节图缩小")
    smooth = packets[0].images[1].convertToFormat(QImage.Format.Format_RGBA8888)
    realtime = QImage(257, 171, QImage.Format.Format_RGBA8888)
    painter = QPainter(realtime)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage(QRectF(0, 0, 257, 171), source, QRectF(0, 0, 1536, 1024))
    painter.end()

    def rgb(image):
        data = image.bits()
        data.setsize(image.sizeInBytes())
        return np.frombuffer(data, np.uint8).reshape(171, 257, 4)[..., :3].astype(np.int16)

    reference = np.asarray(Image.fromarray(pixels).resize((257, 171), Image.Resampling.LANCZOS)).astype(np.int16)
    assert np.abs(rgb(smooth) - reference).mean() < np.abs(rgb(realtime) - reference).mean()
    packets[0].close()
    runner.shutdown()
    resources.close()


def test_preview_and_full_modes_update_canvas_and_original_together(window_factory, tmp_path, canvas_fonts, monkeypatch):
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (2400, 1600), (120, 155, 185)).save(path)
    window = window_factory()
    window.show()
    window.resize(1440, 900)
    window.manager.open_file(path)
    window._refresh_all()
    wait_for_thumbnails(window.thumbnail_panel, [path], path, version=window.manager.list_version)
    wait_for_studio(window, path)
    studio = window.studio_workspace
    window._set_canvas_preview_mode("preview1600")
    wait_until(lambda: studio._displayed_request is not None
               and studio._displayed_request.context.preview_mode == "preview1600"
               and studio._original_display is not None, "1600 编辑与改前就绪")
    studio.viewer.set_zoom_percent(100)
    assert studio._displayed_frame.size == (1600, 1067)
    assert not studio._view_needs_detail()
    studio.start_compare()
    assert studio.canvas_preview_state().kind == "original_preview"
    assert studio.canvas_preview_state().size == (1600, 1067)
    studio.stop_compare()
    window._set_canvas_preview_mode("full")
    wait_until(lambda: studio._displayed_request is not None
               and studio._displayed_request.context.preview_mode == "full"
               and studio._original_display is not None and studio._original_display.full is not None,
               "完整编辑与改前就绪")
    assert studio._displayed_frame.size == (2400, 1600)
    assert len(studio._canvas_full_frames) == 1
    full_hold = studio._canvas_full_frames[0]
    assert full_hold.frame.size == (2400, 1600)
    assert studio.canvas_preview_state().kind == "full"
    assert "Full resolution" in window.canvas_toolbar._preview_label.accessibleName()
    assert studio.viewer.get_zoom_percent() == 100
    studio.start_compare()
    assert studio.canvas_preview_state().kind == "original"
    assert studio.canvas_preview_state().size == (2400, 1600)
    studio.stop_compare()
    assert studio.canvas_preview_state().kind == "full"
    window._set_canvas_preview_mode("auto")
    studio.viewer.set_zoom_percent(20)
    wait_until(lambda: studio._displayed_request is not None
               and studio._displayed_request.context.preview_mode == "auto", "自动模式预览")
    assert studio.canvas_preview_state().kind == "preview"
    assert not studio._canvas_full_frames and full_hold._lease is None
    studio.viewer.set_zoom_percent(100)
    wait_until(lambda: studio._detail_showing, "自动模式完整细节")
    previous = studio._detail_resources.image
    studio.viewer.set_zoom_percent(20)
    assert studio.canvas_preview_state().kind == "preview"
    studio.start_compare()
    assert studio.canvas_preview_state().kind == "original_preview"
    studio.stop_compare()
    studio.viewer.set_zoom_percent(100)
    assert studio._detail_showing and studio._detail_resources.image is previous
    old_request = studio._displayed_request
    studio._on_panel_params_changed("color_adjust", {**studio.session.color_params, "exposure_amt": 30})
    wait_until(lambda: studio._displayed_request is not None and studio._displayed_request is not old_request
               and studio._detail_showing, "新参数完整细节")
    assert studio._detail_resources.image is not previous

    from src.ui.widgets.apple_menu import AppleMenu

    def choose_preview():
        menu = next(menu for menu in window.canvas_toolbar.findChildren(AppleMenu) if menu.isVisible())
        menu.actions()[2].trigger()
        menu.close()

    QTimer.singleShot(0, choose_preview)
    window.canvas_toolbar._open_preview_menu()
    wait_until(lambda: studio._displayed_request is not None
               and studio._displayed_request.context.preview_mode == "preview1600", "状态标签切换模式")
    assert StudioPreloadSettings.load(QSettings(APP_NAME, "OPSPreload")).canvas_preview_mode == "preview1600"
    assert window._preload_settings.canvas_preview_mode == "preview1600"
    assert studio._original_display.full is None
    old_request = studio._displayed_request
    monkeypatch.setattr(studio.core.tools, "supports_proxy", lambda workflow, **options: False)
    studio._schedule_pipeline_render()
    wait_until(lambda: studio._displayed_request is not None and studio._displayed_request is not old_request
               and studio._displayed_frame.input_is_proxy is False
               and studio._original_display is not None and not studio._original_display.preview_proxy_input,
               "完整运算后缩小的改前对比")
    assert studio.canvas_preview_state().kind == "preview"
    assert studio.canvas_preview_state().input_size == (2400, 1600)
