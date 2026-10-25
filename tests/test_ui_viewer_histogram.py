"""看图的直方图：应用层入口、后台运行器、照片信息栏里的显示与请求，以及主窗口的接入。"""

import os
import threading
from concurrent.futures import CancelledError
from contextlib import contextmanager

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.application.display_histogram import (HISTOGRAM_SAMPLE_PIXELS, DisplayHistogramRequest,  # noqa: E402
                                               DisplayHistogramService, display_histogram)
from src.application.image_resources import ComputeBudget  # noqa: E402
from src.core.contracts.image_display import RawDisplayMode  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.services.display_histogram_runner import DisplayHistogramRunner, _sample_frame  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from src.ui.widgets.photo_info_panel import PhotoInfoSidebar  # noqa: E402
from tests.support import wait_for_viewer, wait_until  # noqa: E402

STAMP = (1, 2, 3)


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


def _solid(width, height, color, image_format=QImage.Format.Format_ARGB32_Premultiplied):
    image = QImage(width, height, image_format)
    image.fill(QColor(*color))
    return image


def test_display_histogram_counts_an_rgba_frame():
    rgba = bytes((10, 20, 30, 255)) * 6 + bytes((200, 100, 50, 255)) * 2
    histogram = display_histogram((4, 2), rgba)
    assert histogram["r"][10] == 6 and histogram["r"][200] == 2
    assert histogram["g"][20] == 6 and histogram["b"][50] == 2
    assert sum(histogram["luma"]) == 8


def test_service_reads_the_frame_inside_a_background_compute_lease():
    events = []

    class Budget:
        @contextmanager
        def borrow(self, is_cancelled, priority=0, *, threads=None):
            events.append(("borrow", priority, threads))
            yield
            events.append("released")

    def read_frame():
        events.append("read")
        return (1, 1), bytes((5, 6, 7, 255))

    histogram = DisplayHistogramService(compute_budget=Budget()).histogram(read_frame)
    assert events == [("borrow", 1, 1), "read", "released"] and histogram["r"][5] == 1

    # 已经取消的请求不读画面
    real = ComputeBudget(1)
    try:
        with pytest.raises(CancelledError):
            DisplayHistogramService(compute_budget=real).histogram(read_frame, is_cancelled=lambda: True)
    finally:
        real.close()
    assert events.count("read") == 1


def test_frames_are_sampled_down_before_they_leave_qt(qapp):
    size, rgba = _sample_frame(_solid(1000, 800, (40, 80, 120)))
    assert size[0] * size[1] <= HISTOGRAM_SAMPLE_PIXELS and len(rgba) == size[0] * size[1] * 4
    assert size == (500, 400) and rgba[:4] == bytes((40, 80, 120, 255))
    # 小画面不缩；预乘的半透明像素还原成直通透明度
    translucent = QImage(8, 4, QImage.Format.Format_ARGB32_Premultiplied)
    translucent.fill(QColor(200, 100, 50, 128))
    size, rgba = _sample_frame(translucent)
    assert size == (8, 4) and rgba[3] == 128
    assert all(abs(value - expected) <= 2 for value, expected in zip(rgba[:3], (200, 100, 50)))


class _BlockingService:
    """每次统计都等放行；记录统计过哪些画面。"""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.frames = []

    def histogram(self, read_frame, *, is_cancelled):
        self.entered.set()
        assert self.release.wait(10)
        if is_cancelled():
            raise CancelledError()
        size, rgba = read_frame()
        self.frames.append(tuple(rgba[:3]))
        return {"r": [0] * 256, "g": [0] * 256, "b": [0] * 256, "luma": [0] * 256, "tag": tuple(rgba[:3])}


def test_runner_only_delivers_the_latest_request(qapp):
    service = _BlockingService()
    runner = DisplayHistogramRunner(service)
    started, ready = [], []
    runner.started.connect(started.append)
    runner.ready.connect(lambda request, histogram: ready.append((request, histogram["tag"])))
    try:
        runner.request("a.png", STAMP, RawDisplayMode.OPS, _solid(4, 4, (1, 1, 1)))
        assert service.entered.wait(10)
        # 第一张还在统计时换了两次图：只有最后一张的结果交付
        runner.request("b.png", STAMP, RawDisplayMode.OPS, _solid(4, 4, (2, 2, 2)))
        runner.request("c.png", STAMP, RawDisplayMode.ORIGINAL, _solid(4, 4, (3, 3, 3)))
        assert [request.path for request in started] == ["a.png", "b.png", "c.png"]
        assert [request.generation for request in started] == [1, 2, 3]
        service.release.set()
        wait_until(lambda: ready, "最新一次直方图交付")
        QApplication.processEvents()
        assert len(ready) == 1 and ready[0][1] == (3, 3, 3)
        assert ready[0][0] == DisplayHistogramRequest(3, "c.png", STAMP, RawDisplayMode.ORIGINAL)
        assert (2, 2, 2) not in service.frames

        # 取消之后不再交付
        service.release.clear()
        service.entered.clear()
        runner.request("d.png", STAMP, RawDisplayMode.OPS, _solid(4, 4, (4, 4, 4)))
        assert service.entered.wait(10)
        runner.cancel()
        service.release.set()
        wait_until(lambda: not runner._queue.active, "取消的统计收尾")
        assert len(ready) == 1
    finally:
        service.release.set()
        runner.shutdown()


@pytest.fixture
def sidebar(theme, language, tmp_path):
    bar = PhotoInfoSidebar(language.translate, histogram=True)
    bar.resize(280, 700)
    events = []
    bar.panel.histogram_requested.connect(lambda: events.append("requested"))
    bar.panel.histogram_stopped.connect(lambda: events.append("stopped"))
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (32, 24), (60, 120, 180)).save(path)
    yield bar, events, path
    bar.panel.cleanup()
    bar.deleteLater()


def _data(level):
    hist = [0] * 256
    hist[level] = 100
    return {"r": hist, "g": hist, "b": hist, "luma": hist}


def test_panel_asks_for_a_histogram_only_while_it_is_open_on_a_photo(sidebar, language):
    bar, events, path = sidebar
    panel = bar.panel
    assert panel._histogram is not None and not panel.histogram_wanted
    panel.set_active(True)
    panel.set_path(path)
    # 收起时不需要
    assert "requested" not in events and not panel.histogram_wanted
    panel.set_expanded(True)
    assert events[-1] == "requested" and panel.histogram_wanted
    panel.set_expanded(False)
    assert events[-1] == "stopped" and not panel.histogram_wanted
    panel.set_expanded(True)
    panel.set_active(False)
    assert events[-1] == "stopped"
    panel.set_active(True)
    assert events[-1] == "requested"
    # 没有直方图的信息栏（编辑工作区的那一个）不发请求
    plain = PhotoInfoSidebar(language.translate)
    asked = []
    plain.panel.histogram_requested.connect(lambda: asked.append(True))
    plain.panel.set_active(True)
    plain.panel.set_path(path)
    plain.panel.set_expanded(True)
    assert plain.panel._histogram is None and not asked
    plain.panel.cleanup()
    plain.deleteLater()


def test_panel_only_accepts_the_histogram_it_is_waiting_for(sidebar, tmp_path):
    bar, _, path = sidebar
    panel = bar.panel
    panel.set_active(True)
    panel.set_path(path)
    panel.set_expanded(True)
    request = DisplayHistogramRequest(1, path, STAMP, RawDisplayMode.OPS)
    stale = DisplayHistogramRequest(0, path, STAMP, RawDisplayMode.OPS)
    panel.set_histogram_request(request)
    panel.set_histogram(stale, _data(10))
    assert not panel._histogram.has_data()
    panel.set_histogram(request, _data(10))
    assert panel._histogram.has_data()

    # 画面换了：清空，原来那次请求的结果不再接收
    panel.clear_histogram()
    assert not panel._histogram.has_data()
    panel.set_histogram(request, _data(10))
    assert not panel._histogram.has_data()

    # 别的照片的请求不登记；换照片时清空
    other = str(tmp_path / "other.png")
    panel.set_histogram_request(DisplayHistogramRequest(2, other, STAMP, RawDisplayMode.OPS))
    panel.set_histogram(DisplayHistogramRequest(2, other, STAMP, RawDisplayMode.OPS), _data(20))
    assert not panel._histogram.has_data()
    panel.set_histogram_request(request)
    panel.set_histogram(request, _data(10))
    Image.new("RGB", (8, 8)).save(other)
    panel.set_path(other)
    assert not panel._histogram.has_data()


def test_main_window_fills_the_histogram_from_the_displayed_photo(theme, language, tmp_path, window_factory):
    first, second = str(tmp_path / "a.png"), str(tmp_path / "b.png")
    Image.new("RGB", (400, 300), (60, 120, 180)).save(first)
    Image.new("RGB", (300, 400), (200, 40, 40)).save(second)
    window = window_factory()
    window.resize(1280, 720)
    window.show()
    window.manager.open_file(first, load_siblings=True)
    window._refresh_all()
    wait_for_viewer(window, first)
    panel = window.info_sidebar.panel
    histogram = panel._histogram
    runner = window._histogram_runner
    # 信息栏收起时不统计
    QApplication.processEvents()
    assert not histogram.has_data() and runner._latest is None

    window.top_bar._info_toggle.click()
    wait_until(histogram.has_data, "展开后出现直方图")
    assert runner._latest.path == first and runner._latest.raw_display_mode == window.viewer._display_key.raw_display_mode
    # 纯色照片：红色通道的像素都落在同一格附近
    assert max(range(256), key=lambda index: histogram._layers["r"][index]) in range(58, 63)

    window._next_image()
    wait_for_viewer(window, second)
    wait_until(lambda: histogram.has_data() and runner._latest is not None and runner._latest.path == second,
               "换图后重新统计")
    wait_until(lambda: max(range(256), key=lambda index: histogram._layers["r"][index]) in range(198, 203),
               "直方图换成第二张照片的")

    window.top_bar._info_toggle.click()
    QApplication.processEvents()
    assert runner._latest is None and not panel.histogram_wanted
    # 进入编辑后看图的信息栏停用，不再请求
    window.top_bar._info_toggle.click()
    wait_until(histogram.has_data, "再次展开")
    window._on_workspace_index_changed(1)
    assert not panel.histogram_wanted and runner._latest is None
