"""窗口尺寸与状态：三档版式规则、右侧栏的拖动与浮层、宽屏与窄窗口的默认值，以及下次打开时恢复的状态。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtCore import QPoint, QSettings, QSize, Qt  # noqa: E402
from PyQt6.QtGui import QPalette  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QFrame, QWidget  # noqa: E402

from resources.styles.theme_variables import (CANVAS_TOOLBAR_HEIGHT, CANVAS_TOOLBAR_MARGIN, EDIT_SIDEBAR_WIDTH,  # noqa: E402
                                              EDIT_SIDEBAR_WIDTH_WIDE, FILMSTRIP_HEIGHT, FILMSTRIP_HEIGHT_COMPACT,
                                              FILMSTRIP_HEIGHT_WIDE, INFO_SIDEBAR_WIDTH, INFO_SIDEBAR_WIDTH_WIDE,
                                              SIDEBAR_MAX_WIDTH, SIDEBAR_MIN_WIDTH)
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from src.ui.widgets.side_dock import SideDock  # noqa: E402
from src.ui.widgets.top_bar import EDITOR, VIEWER  # noqa: E402
from src.ui.window_layout import PanelPreferences, WindowLayout, sidebar_width, window_layout  # noqa: E402

INSET = CANVAS_TOOLBAR_HEIGHT + 2 * CANVAS_TOOLBAR_MARGIN


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


def _open(tmp_path, size=(1600, 900)):
    from src.ui.main_window import MainWindow

    path = str(tmp_path / "photo.png")
    if not os.path.exists(path):
        Image.new("RGB", (400, 300), (60, 120, 180)).save(path)
    win = MainWindow()
    win.resize(*size)
    win.show()
    win.manager.open_file(path, load_siblings=True)
    win._refresh_all()
    assert _wait_until(lambda: win.viewer.has_image)
    QApplication.processEvents()
    return win


def _close(win):
    win.shutdown_previews()
    win.deleteLater()


def _close_fully(win):
    """走真实的关闭流程并等缓存释放：同一个数据目录接下来要交给下一个窗口，状态也在这时保存。"""
    win.close()
    assert _wait_until(lambda: win._caches_closed_for_exit and not win.isVisible(), 20000)
    win.deleteLater()


def _enter_editor(win):
    win.top_bar.workspace_requested.emit(EDITOR)
    assert _wait_until(lambda: win.studio_workspace is not None
                       and win.studio_workspace.session is not None
                       and win.studio_workspace.tool_sidebar.get_current_panel() is not None, 60000)
    QApplication.processEvents()
    return win.studio_workspace


def test_window_width_picks_one_of_three_layouts():
    standard = WindowLayout(False, INFO_SIDEBAR_WIDTH, EDIT_SIDEBAR_WIDTH, FILMSTRIP_HEIGHT)
    assert window_layout(1280) == standard == window_layout(1600) == window_layout(2559)
    assert window_layout(1279) == WindowLayout(True, INFO_SIDEBAR_WIDTH, EDIT_SIDEBAR_WIDTH, FILMSTRIP_HEIGHT_COMPACT)
    assert window_layout(800).floating_sidebar
    assert window_layout(2560) == window_layout(3440) == WindowLayout(
        False, INFO_SIDEBAR_WIDTH_WIDE, EDIT_SIDEBAR_WIDTH_WIDE, FILMSTRIP_HEIGHT_WIDE)
    assert (SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH) == (280, 420)


def test_sidebar_width_follows_the_drag_within_limits_and_never_clips_content():
    assert sidebar_width(0, 308) == 308
    assert sidebar_width(350, 308) == 350
    assert sidebar_width(200, 308) == SIDEBAR_MIN_WIDTH and sidebar_width(900, 308) == SIDEBAR_MAX_WIDTH
    # 内容需要更宽时以内容为准，哪怕超过可拖动的上限
    assert sidebar_width(0, 308, needed=356) == 356 and sidebar_width(300, 308, needed=356) == 356
    assert sidebar_width(0, 308, needed=460) == 460 and sidebar_width(900, 308, needed=460) == 460


def test_panel_preferences_round_trip_through_settings():
    settings = QSettings("OpenPictureStudio", "LayoutRoundTrip")
    assert PanelPreferences.load(settings) == PanelPreferences(False, 0, False, 0)
    PanelPreferences(True, 300, True, 400).save(settings)
    assert PanelPreferences.load(QSettings("OpenPictureStudio", "LayoutRoundTrip")) == PanelPreferences(True, 300, True, 400)


def test_side_dock_keeps_a_bar_on_the_hosts_right_edge_above_the_inset(theme):
    host, other = QWidget(), QWidget()
    host.resize(900, 600)
    host.show()
    bar = QFrame()
    bar.setMinimumWidth(280)
    dock = SideDock(bar, 70)
    dock.set_width(300)
    bar.show()
    dock.attach(host)
    QApplication.processEvents()
    assert bar.parentWidget() is host and not bar.isHidden() and bar.property("floating") is True
    assert bar.geometry().getRect() == (600, 0, 300, 530)

    host.resize(1000, 500)
    QApplication.processEvents()
    assert bar.geometry().getRect() == (700, 0, 300, 430)
    dock.set_width(320)
    assert bar.geometry().getRect() == (680, 0, 320, 430)

    # 收起后再显示仍在原位；交还后不再跟随
    bar.hide()
    host.resize(1100, 500)
    bar.show()
    QApplication.processEvents()
    assert bar.geometry().getRect() == (780, 0, 320, 430)
    dock.detach()
    assert dock.host() is None and bar.property("floating") is False
    host.resize(1200, 500)
    QApplication.processEvents()
    assert bar.geometry().x() == 780
    host.close()
    other.close()


def test_viewer_info_sidebar_can_be_dragged_and_remembers_width_and_state(theme, language, tmp_path):
    win = _open(tmp_path)
    try:
        bar, splitter = win.info_sidebar, win.viewer_splitter
        assert bar.parentWidget() is splitter and bar.isHidden() and not win._layout_rules.floating_sidebar
        assert (bar.minimumWidth(), bar.maximumWidth()) == (SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH)
        win.top_bar._info_toggle.click()
        QApplication.processEvents()
        assert not bar.isHidden() and bar.width() == INFO_SIDEBAR_WIDTH and win._panel_prefs.info_expanded

        # 拖动分隔条：宽度记为用户的选择，窗口再变大小时保持
        sizes = splitter.sizes()
        splitter.setSizes([sizes[0] - 60, sizes[1] + 60])
        splitter.splitterMoved.emit(sizes[0] - 60, 1)
        assert bar.width() == INFO_SIDEBAR_WIDTH + 60 and win._panel_prefs.info_width == INFO_SIDEBAR_WIDTH + 60
        win.resize(1700, 900)
        QApplication.processEvents()
        assert bar.width() == INFO_SIDEBAR_WIDTH + 60
        # 拖不出可拖动的范围
        splitter.setSizes([100, 1500])
        QApplication.processEvents()
        assert bar.width() == SIDEBAR_MAX_WIDTH
        splitter.setSizes([sizes[0] - 60, sizes[1] + 60])
    finally:
        _close_fully(win)

    again = _open(tmp_path)
    try:
        QApplication.processEvents()
        assert again._panel_prefs == PanelPreferences(True, INFO_SIDEBAR_WIDTH + 60, False, 0)
        assert not again.info_sidebar.isHidden() and again.top_bar._info_toggle.isChecked()
        assert again.info_sidebar.width() == INFO_SIDEBAR_WIDTH + 60
        # 用户收起后，下次也是收起的
        again.top_bar._info_toggle.click()
        assert again.info_sidebar.isHidden() and not again._panel_prefs.info_expanded
    finally:
        _close(again)


def test_narrow_window_floats_the_viewer_sidebar_over_the_canvas(theme, language, tmp_path):
    win = _open(tmp_path)
    try:
        bar, canvas = win.info_sidebar, win.scroll_area
        win.top_bar._info_toggle.click()
        QApplication.processEvents()
        assert not bar.isHidden() and win.thumbnail_panel.height() == FILMSTRIP_HEIGHT

        # 变窄：信息栏收起并改为浮层，胶片条用紧凑高度
        win.resize(1100, 700)
        QApplication.processEvents()
        assert win._layout_rules.floating_sidebar and bar.parentWidget() is canvas and bar.isHidden()
        assert not win.top_bar._info_toggle.isChecked() and win._panel_prefs.info_expanded
        assert _wait_until(lambda: win.thumbnail_panel.height() == FILMSTRIP_HEIGHT_COMPACT)
        width = canvas.width()

        # 点按钮展开：浮在画布右沿，不占画布的宽度，底部让出工具条
        win.top_bar._info_toggle.click()
        QApplication.processEvents()
        assert not bar.isHidden() and canvas.width() == width and win._panel_prefs.info_expanded
        assert bar.geometry().getRect() == (width - INFO_SIDEBAR_WIDTH, 0, INFO_SIDEBAR_WIDTH, canvas.height() - INSET)
        toolbar = win.canvas_toolbar
        assert toolbar.parentWidget() is canvas and not bar.geometry().intersects(toolbar.geometry())
        # 浮层是临时展开：收起不改下次要恢复的状态
        win.top_bar._info_toggle.click()
        assert bar.isHidden() and win._panel_prefs.info_expanded

        # 变宽：回到分隔条里，恢复变窄之前的展开状态
        win.resize(1600, 900)
        QApplication.processEvents()
        assert not win._layout_rules.floating_sidebar and bar.parentWidget() is win.viewer_splitter
        assert not bar.isHidden() and bar.width() == INFO_SIDEBAR_WIDTH
        assert bar.mapTo(win, QPoint(0, 0)).x() + bar.width() == win.width()
        assert _wait_until(lambda: win.thumbnail_panel.height() == FILMSTRIP_HEIGHT)
    finally:
        _close(win)


def test_editor_sidebars_dock_float_and_report_what_to_remember(theme, language, tmp_path):
    win = _open(tmp_path)
    try:
        studio = _enter_editor(win)
        right, info, splitter = studio.right_sidebar, studio.info_sidebar, studio.splitter
        assert right.parentWidget() is splitter and info.parentWidget() is splitter
        needed = studio.tool_sidebar.content_min_width()
        assert right.width() == max(EDIT_SIDEBAR_WIDTH, needed) and right.minimumWidth() == max(SIDEBAR_MIN_WIDTH, needed)

        # 拖动参数栏：记住宽度
        reports = []
        studio.sidebar_preferences_changed.connect(lambda *values: reports.append(values))
        sizes = splitter.sizes()
        target = min(right.maximumWidth(), right.width() + 40)
        splitter.setSizes([sizes[0] - (target - sizes[1]), target, 0])
        splitter.splitterMoved.emit(0, 1)
        assert right.width() == target and reports[-1] == (False, target, 0)
        assert win._panel_prefs.edit_width == target

        # 照片信息占用同一个位置，宽度与看图里的一致
        win.top_bar._info_toggle.click()
        QApplication.processEvents()
        assert right.isHidden() and not info.isHidden() and info.width() == INFO_SIDEBAR_WIDTH
        win.top_bar._info_toggle.click()
        QApplication.processEvents()
        assert not right.isHidden() and right.width() == target

        # 收起参数栏：记住收起
        studio._on_tool_sidebar_close()
        assert right.isHidden() and reports[-1] == (True, target, 0) and win._panel_prefs.edit_collapsed
        studio.tool_rail.button("color").click()
        assert not right.isHidden() and reports[-1] == (False, target, 0)

        # 变窄：两栏改为浮层并收起；点工具栏的入口才展开，不占画布的宽度
        count = len(reports)
        win.resize(1100, 700)
        QApplication.processEvents()
        canvas = studio.scroll_area
        assert right.parentWidget() is canvas and info.parentWidget() is canvas and right.isHidden()
        assert len(reports) == count and not win._panel_prefs.edit_collapsed
        width = canvas.width()
        studio.tool_rail.button("color").click()
        QApplication.processEvents()
        assert not right.isHidden() and canvas.width() == width
        assert right.geometry().right() == width - 1 and right.geometry().bottom() == canvas.height() - INSET - 1
        assert not right.geometry().intersects(win.canvas_toolbar.geometry())
        studio._on_tool_sidebar_close()
        assert right.isHidden() and len(reports) == count

        # 变宽：回到分隔条里，恢复变窄之前的状态与宽度
        win.resize(1600, 900)
        QApplication.processEvents()
        assert right.parentWidget() is splitter and info.parentWidget() is splitter
        assert not right.isHidden() and right.width() == target
        assert [splitter.widget(index) for index in range(3)][1:] == [right, info]
    finally:
        _close(win)


def test_wide_window_uses_the_larger_defaults_until_the_user_chooses(theme, language, tmp_path):
    win = _open(tmp_path, (2560, 1440))
    try:
        assert win._layout_rules == window_layout(2560)
        assert _wait_until(lambda: win.thumbnail_panel.height() == FILMSTRIP_HEIGHT_WIDE)
        win.top_bar._info_toggle.click()
        QApplication.processEvents()
        assert win.info_sidebar.width() == INFO_SIDEBAR_WIDTH_WIDE

        studio = _enter_editor(win)
        needed = studio.tool_sidebar.content_min_width()
        assert studio.right_sidebar.width() == max(EDIT_SIDEBAR_WIDTH_WIDE, needed)
        assert _wait_until(lambda: studio.thumbnail_panel.height() == FILMSTRIP_HEIGHT_WIDE)

        # 用户自己调过缩略图大小之后，窗口再变宽变窄也不改它
        win.top_bar.workspace_requested.emit(VIEWER)
        QApplication.processEvents()
        win.thumbnail_panel._on_thumb_zoom(1)
        chosen = win.thumbnail_panel._thumb_width
        win.resize(1600, 900)
        QApplication.processEvents()
        assert win._layout_rules == window_layout(1600) and win.thumbnail_panel._thumb_width == chosen
        assert win.info_sidebar.width() == INFO_SIDEBAR_WIDTH
    finally:
        _close(win)


def test_main_window_remembers_its_size(theme, language, tmp_path, qapp):
    from src.ui.main_window import MainWindow

    screen = qapp.primaryScreen().availableGeometry()
    size = QSize(min(900, screen.width()), min(640, screen.height() - 40))
    win = _open(tmp_path, size=(size.width(), size.height()))
    assert win.size() == size
    _close_fully(win)
    assert QSettings("OpenPictureStudio", "MainWindow").value("windowSize") == size

    again = MainWindow()
    try:
        assert again.size() == size
    finally:
        _close(again)


def test_window_geometry_and_maximized_state_are_restored(theme, language, tmp_path, qapp):
    from src.ui.base_window import BaseWindow

    screen = qapp.primaryScreen().availableGeometry()
    size = QSize(min(700, screen.width() - 40), min(520, screen.height() - 40))
    first = BaseWindow(settings_key="GeometryProbe", min_width=300, min_height=200)
    first.resize(size)
    first.move(screen.x() + 20, screen.y() + 30)
    first.show()
    QApplication.processEvents()
    position = first.pos()
    first.close()

    second = BaseWindow(settings_key="GeometryProbe", min_width=300, min_height=200)
    assert second.size() == size and second.pos() == position and not second.isMaximized()
    # 最大化时关闭：记下最大化，保存的仍是最大化之前的大小与位置
    second.show()
    second.showMaximized()
    QApplication.processEvents()
    second.close()
    settings = QSettings("OpenPictureStudio", "GeometryProbe")
    assert settings.value("windowMaximized", False, type=bool) and settings.value("windowSize") == size

    third = BaseWindow(settings_key="GeometryProbe", min_width=300, min_height=200)
    assert third.windowState() & Qt.WindowState.WindowMaximized
    third.close()

    # 上次的位置已经不在任何屏幕上：不恢复位置；大小不超过屏幕
    settings.setValue("windowMaximized", False)
    settings.setValue("windowPos", QPoint(screen.right() + 5000, screen.bottom() + 5000))
    settings.setValue("windowSize", QSize(screen.width() + 900, screen.height() + 900))
    fourth = BaseWindow(settings_key="GeometryProbe", min_width=300, min_height=200)
    assert fourth.pos() != QPoint(screen.right() + 5000, screen.bottom() + 5000)
    assert fourth.width() <= screen.width() and fourth.height() <= screen.height()
    fourth.close()


def test_startup_window_setting_decides_size_position_and_maximized(theme, language, tmp_path, qapp):
    from src.ui.base_window import STARTUP_LAST, STARTUP_MAXIMIZED, STARTUP_MODES, STARTUP_SIZES, BaseWindow

    def window():
        return BaseWindow(settings_key="StartupProbe", min_width=300, min_height=200)

    screen = qapp.primaryScreen().availableGeometry()
    saved = QSize(min(640, screen.width() - 40), min(480, screen.height() - 40))
    settings = QSettings("OpenPictureStudio", "StartupProbe")
    settings.setValue("windowSize", saved)
    settings.setValue("windowPos", QPoint(screen.x() + 10, screen.y() + 10))
    settings.setValue("windowMaximized", True)

    # 没有选过：上次的大小、位置与是否最大化
    first = window()
    assert first.startup_mode() == STARTUP_LAST and STARTUP_MODES[0] == STARTUP_LAST
    assert first.size() == saved and first.windowState() & Qt.WindowState.WindowMaximized
    with pytest.raises(ValueError):
        first.set_startup_mode("640x480")

    # 固定大小：居中，不超过屏幕，不管上次是否最大化
    for mode, expected in STARTUP_SIZES.items():
        assert first.set_startup_mode(mode)
        fixed = window()
        size = QSize(*expected).boundedTo(screen.size())
        assert fixed.startup_mode() == mode and fixed.size() == size
        assert not fixed.windowState() & Qt.WindowState.WindowMaximized
        assert fixed.pos() == screen.topLeft() + QPoint((screen.width() - size.width()) // 2,
                                                         (screen.height() - size.height()) // 2)
        fixed.close()

    # 最大化：还原后是上次的大小（上面几个窗口关闭时已经把各自的大小记成了上次的）
    settings.setValue("windowSize", saved)
    settings.setValue("windowMaximized", False)
    assert first.set_startup_mode(STARTUP_MAXIMIZED)
    maximized = window()
    assert maximized.windowState() & Qt.WindowState.WindowMaximized and maximized._normal_size == saved
    maximized.close()

    # 保存的值不认识时按上次的大小与位置
    settings.setValue("startupWindow", "800x600")
    assert window().startup_mode() == STARTUP_LAST
    first.close()


def test_position_read_before_the_window_is_shown_is_not_saved(theme, language, tmp_path, qapp):
    """真实窗口显示之前，读到的位置比实际高一个标题栏；这样的读数不能记成要保存的位置。"""
    from src.ui.base_window import BaseWindow

    class Probe(BaseWindow):
        def pos(self):
            actual = super().pos()
            return actual if self.isVisible() else actual - QPoint(0, 31)

    def window():
        return Probe(settings_key="StalePositionProbe", min_width=300, min_height=200)

    screen = qapp.primaryScreen().availableGeometry()
    position = QPoint(screen.x() + 40, screen.y() + 60)
    settings = QSettings("OpenPictureStudio", "StalePositionProbe")
    settings.setValue("windowPos", position)
    settings.setValue("windowSize", QSize(min(600, screen.width() - 80), min(400, screen.height() - 120)))

    for _ in range(3):
        win = window()
        win.show()
        QApplication.processEvents()
        assert QPoint(win.x(), win.y()) == position
        win.close()
        assert QSettings("OpenPictureStudio", "StalePositionProbe").value("windowPos") == position

    # 最大化时关闭用的是显示之前恢复出来的那一份，同样不能被显示之前的读数改掉
    win = window()
    win.show()
    win.showMaximized()
    QApplication.processEvents()
    win.close()
    assert QSettings("OpenPictureStudio", "StalePositionProbe").value("windowPos") == position


_POSITION_PROBE = r"""
import ctypes, json, os, sys
from ctypes import wintypes
from pathlib import Path
from PyQt6.QtCore import QPoint, QSettings, QSize, Qt
from PyQt6.QtWidgets import QApplication
from pytest import MonkeyPatch
from tests.support import isolate_paths

_settings_isolation = MonkeyPatch()
isolate_paths(Path(os.environ["OPS_TEST_ROOT"]), _settings_isolation)

from src.ui.base_window import BaseWindow
from src.ui.window_chrome import CLIENT

app = QApplication(sys.argv[:1])


class Window(BaseWindow):
    def __init__(self):
        super().__init__(settings_key="GeometryProbe", min_width=300, min_height=200)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(0.0)
        self.merged = self.install_window_chrome(lambda point: CLIENT, lambda hovered, pressed: None)


def saved():
    position = QSettings("OpenPictureStudio", "GeometryProbe").value("windowPos")
    return [position.x(), position.y()]


settings = QSettings("OpenPictureStudio", "GeometryProbe")
settings.setValue("windowPos", QPoint(200, 150))
settings.setValue("windowSize", QSize(700, 500))
settings.sync()
user32 = ctypes.windll.user32
origins, merged = [], []
for index in range(4):
    win = Window()
    win.show()
    for _ in range(5):
        app.processEvents()
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(wintypes.HWND(int(win.winId())), ctypes.byref(origin))
    origins.append([origin.x, origin.y])
    merged.append(win.merged)
    if index == 3:
        win.showMaximized()
        for _ in range(5):
            app.processEvents()
    win.close()
    app.processEvents()
    win.deleteLater()
    app.processEvents()
print(json.dumps({"merged": merged, "origins": origins, "saved": saved(),
                  "maximized": QSettings("OpenPictureStudio", "GeometryProbe").value("windowMaximized", type=bool)}))
sys.stdout.flush()
os._exit(0)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="窗口外框只在 Windows 上接管")
def test_real_window_reopens_where_it_was_closed():
    """在真实窗口平台上把顶栏兼作标题栏的窗口连开四次（透明、不抢焦点）：每次都在上次关闭的位置，不逐次上移。"""
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "QT_QPA_PLATFORM": "windows", "PYTHONPATH": str(root)}
    completed = subprocess.run([sys.executable, "-c", _POSITION_PROBE], cwd=str(root), env=environment,
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["merged"] == [True] * 4
    assert report["origins"] == [[200, 150]] * 4
    # 最后一次是最大化着关的：记下最大化，位置仍是最大化之前的
    assert report["saved"] == [200, 150] and report["maximized"]
