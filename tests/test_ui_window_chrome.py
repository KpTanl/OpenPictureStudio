"""顶栏兼作标题栏：窗口外框的规则、窗口按钮、顶栏里哪里可以拖动，以及在真实窗口平台上系统给出的命中结果。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from PyQt6.QtGui import QAction, QPalette  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from resources.styles.theme_variables import TOP_BAR_HEIGHT  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens, to_qcolor  # noqa: E402
from src.ui.widgets.top_bar import EDITOR, TopBar, TopBarActions  # noqa: E402
from src.ui.widgets.window_controls import BUTTON_WIDTH, CLOSE, MAXIMIZE, RESTORE, WindowControls  # noqa: E402
from src.ui.window_chrome import CAPTION, CLIENT, WindowChrome, client_top, hit_code  # noqa: E402
from src.ui import window_chrome  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
THEMES = ("dark", "light", "beige")
HT_CAPTION, HT_MAXBUTTON, HT_TOP = 2, 9, 12


@pytest.fixture
def theme(qapp):
    previous = qapp.styleSheet(), qapp.property("ops.theme"), QPalette(qapp.palette())
    manager = ThemeManager()
    manager.apply("dark")
    yield manager
    qapp.setStyleSheet(previous[0])
    qapp.setProperty("ops.theme", previous[1])
    qapp.setPalette(previous[2])


def test_client_area_starts_at_the_window_top_except_when_maximized():
    assert client_top(200, 8, maximized=False) == 200
    # 最大化时窗口比屏幕大出一圈边框，客户区让开这一圈，正好从屏幕上沿开始
    assert client_top(-8, 8, maximized=True) == 0


def test_hit_rules_keep_a_resize_strip_and_hand_the_maximize_button_to_the_system():
    assert hit_code(CAPTION, 3, 8, maximized=False) == HT_TOP
    assert hit_code(CLIENT, 7, 8, maximized=False) == HT_TOP
    assert hit_code(CAPTION, 8, 8, maximized=False) == HT_CAPTION
    assert hit_code(CLIENT, 8, 8, maximized=False) is None
    # 最大化时没有缩放边，顶上那几个像素照常可以拖动或点按钮
    assert hit_code(CAPTION, 0, 8, maximized=True) == HT_CAPTION
    assert hit_code(CLIENT, 0, 8, maximized=True) is None
    # 最大化按钮整个交给系统，悬停时才有分屏布局
    assert hit_code(window_chrome.MAXIMIZE, 2, 8, maximized=False) == HT_MAXBUTTON
    assert hit_code(window_chrome.MAXIMIZE, 20, 8, maximized=True) == HT_MAXBUTTON


def test_chrome_is_not_installed_on_a_window_without_a_system_frame(qapp):
    host = QWidget()
    chrome = WindowChrome(host, lambda point: CLIENT, lambda hovered, pressed: None, lambda: None)
    # 离屏平台没有系统窗口：不接管，消息也不处理
    assert not chrome.install() and not chrome.is_installed()
    assert chrome.native_event(None) == (False, 0)
    host.close()


def _controls(translate=None):
    controls = WindowControls(translate or (lambda key, default: default))
    controls.resize(3 * BUTTON_WIDTH, TOP_BAR_HEIGHT)
    controls.show()
    QApplication.processEvents()
    return controls


def test_window_controls_report_requests_and_do_not_take_focus(theme):
    controls = _controls()
    seen = []
    controls.minimize_requested.connect(lambda: seen.append("minimize"))
    controls.maximize_requested.connect(lambda: seen.append("maximize"))
    controls.close_requested.connect(lambda: seen.append("close"))
    buttons = (controls._minimize, controls._maximize, controls._close)
    assert [button.geometry().getRect() for button in buttons] == [
        (index * BUTTON_WIDTH, 0, BUTTON_WIDTH, TOP_BAR_HEIGHT) for index in range(3)]
    for button in buttons:
        assert button.focusPolicy() == Qt.FocusPolicy.NoFocus
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert seen == ["minimize", "maximize", "close"]
    assert [button.accessibleName() for button in buttons] == ["Minimize", "Maximize", "Close"]
    controls.close()


def test_maximize_button_follows_the_window_state_and_the_state_given_from_outside(theme):
    texts = {"window.maximize": "最大化", "window.restore": "向下还原"}
    controls = _controls(lambda key, default: texts.get(key, default))
    button = controls._maximize
    assert button.kind() == MAXIMIZE and button.toolTip() == "最大化" and not controls.is_maximized()
    controls.set_maximized(True)
    assert button.kind() == RESTORE and button.toolTip() == "向下还原" and controls.is_maximized()
    controls.set_maximized(False)
    assert button.kind() == MAXIMIZE

    tokens = get_theme_tokens()
    corner = QPoint(2, 2)
    assert controls.grab().toImage().pixelColor(button.geometry().topLeft() + corner) != to_qcolor(tokens["control"])
    controls.set_maximize_state(True, False)
    assert button.state() == (True, False)
    assert controls.grab().toImage().pixelColor(button.geometry().topLeft() + corner) == to_qcolor(tokens["control"])
    controls.set_maximize_state(True, True)
    assert controls.grab().toImage().pixelColor(button.geometry().topLeft() + corner) == to_qcolor(tokens["track"])
    controls.set_maximize_state(False, False)
    assert button.state() == (False, False)
    controls.close()


@pytest.mark.parametrize("name", THEMES)
def test_close_button_turns_red_while_pressed(theme, name):
    theme.apply(name)
    tokens = get_theme_tokens()
    controls = _controls()
    button = controls._close
    assert button.kind() == CLOSE
    corner = button.geometry().topLeft() + QPoint(2, 2)
    QTest.mousePress(button, Qt.MouseButton.LeftButton)
    assert controls.grab().toImage().pixelColor(corner) == to_qcolor(tokens["danger_hover"])
    QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
    controls.close()


def _bar(width=1100):
    host = QWidget()
    actions = {name: QAction(name, host) for name in ("open", "save", "undo", "export", "more")}
    bar = TopBar(TopBarActions(
        open_menu=(actions["open"],),
        editor_buttons=((actions["save"], "device-floppy"), None, (actions["undo"], "arrow-back-up")),
        export_default=(actions["export"], actions["export"]),
        export_menus=((actions["export"],), (actions["export"],)),
        more_menu=(actions["more"],),
    ), lambda key, default: default, host)
    bar.setGeometry(0, 0, width, TOP_BAR_HEIGHT)
    host.resize(width, TOP_BAR_HEIGHT + 20)
    host.show()
    QApplication.processEvents()
    return host, bar


def _center(bar, widget):
    return widget.mapTo(bar, widget.rect().center())


def test_top_bar_shows_window_controls_only_when_it_doubles_as_the_title_bar(theme):
    host, bar = _bar()
    controls = bar._window_controls
    assert not bar.has_window_controls() and controls.isHidden()
    # 不兼作标题栏时整条顶栏都是普通控件，系统标题栏负责拖动
    assert bar.region_at(QPoint(bar._left.geometry().right() + 40, 20)) == CLIENT
    right_before = bar._right.geometry().right()

    bar.set_window_controls_visible(True)
    QApplication.processEvents()
    assert bar.has_window_controls() and controls.isVisible()
    # 贴着右上角，占满顶栏的高度
    assert controls.geometry().right() == bar.width() - 1 and controls.geometry().top() == 0
    assert controls.width() == 3 * BUTTON_WIDTH and controls.height() >= TOP_BAR_HEIGHT - 1
    assert bar._right.geometry().right() < controls.geometry().left() and right_before > bar._right.geometry().right()

    seen = []
    bar.minimize_requested.connect(lambda: seen.append("minimize"))
    bar.maximize_requested.connect(lambda: seen.append("maximize"))
    bar.close_requested.connect(lambda: seen.append("close"))
    for button in (controls._minimize, controls._maximize, controls._close):
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert seen == ["minimize", "maximize", "close"]
    bar.set_window_maximized(True)
    assert controls.is_maximized()

    bar.set_window_controls_visible(False)
    QApplication.processEvents()
    assert controls.isHidden() and bar._right.geometry().right() == right_before
    host.close()


def test_top_bar_tells_draggable_space_from_controls(theme):
    host, bar = _bar()
    bar.set_window_controls_visible(True)
    bar.set_title("a-photo-with-a-fairly-long-name.jpg", "6000 × 4000")
    QApplication.processEvents()
    controls = bar._window_controls
    # 空白、标志、分隔线与标题文字都可以拖动窗口
    assert bar.region_at(QPoint(bar._left.geometry().right() + 6, 24)) == CAPTION
    assert bar.region_at(_center(bar, bar._logo)) == CAPTION
    assert bar.region_at(_center(bar, bar._title)) == CAPTION
    assert bar.region_at(QPoint(4, 24)) == CAPTION
    # 控件照常响应
    for widget in (bar._switch, bar._open_button, bar._info_toggle, bar._export_button, bar._export_options,
                   bar._more_button, controls._minimize, controls._close):
        assert bar.region_at(_center(bar, widget)) == CLIENT, widget
    assert bar.region_at(_center(bar, controls._maximize)) == window_chrome.MAXIMIZE
    assert bar.region_at(QPoint(10, TOP_BAR_HEIGHT + 5)) == CLIENT

    # 编辑工作区的按钮同样是控件；提交进行中操作暂停，窗口按钮与拖动不受影响
    bar.set_workspace(EDITOR)
    QApplication.processEvents()
    for widget in bar._editor_widgets:
        expected = CAPTION if widget.objectName() == "topBarSeparator" else CLIENT
        assert bar.region_at(_center(bar, widget)) == expected
    bar.set_actions_enabled(False)
    assert not bar._left.isEnabled() and not bar._right.isEnabled() and bar.isEnabled()
    assert controls.isEnabled() and controls._close.isEnabled()
    assert bar.region_at(QPoint(bar._left.geometry().right() + 6, 24)) == CAPTION
    bar.set_actions_enabled(True)
    assert bar._left.isEnabled() and bar._right.isEnabled()
    host.close()


def test_main_window_keeps_the_system_title_bar_where_the_frame_cannot_be_taken_over(theme):
    from src.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.show()
        QApplication.processEvents()
        # 离屏平台没有系统窗口：不接管，顶栏没有窗口按钮，也不显示程序名称
        assert window._chrome is None and not window.top_bar.has_window_controls()
        assert window.top_bar.title() == ("", "")
        assert window.top_bar.region_at(QPoint(400, 20)) == CLIENT
        assert window._window_region_at(QPoint(400, 20)) == CLIENT
    finally:
        window.shutdown_previews()
        window.deleteLater()


_PROBE = r"""
import ctypes, json, sys
from ctypes import wintypes
from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtWidgets import QApplication, QMainWindow
from src.ui.window_chrome import CAPTION, CLIENT, MAXIMIZE, WindowChrome

app = QApplication(sys.argv[:1])
events = []


class Probe(QMainWindow):
    chrome = None

    def nativeEvent(self, event_type, message):
        if self.chrome is not None:
            handled, result = self.chrome.native_event(message)
            if handled:
                return True, result
        return False, 0


def region_at(point):
    if QRect(0, 0, 200, 48).contains(point):
        return CAPTION
    if QRect(200, 0, 46, 48).contains(point):
        return MAXIMIZE
    return CLIENT


win = Probe()
win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
win.resize(640, 400)
win.chrome = WindowChrome(win, region_at, lambda hovered, pressed: events.append([hovered, pressed]),
                          lambda: events.append("clicked"))
installed = win.chrome.install()
win.resize(640, 400)
win.show()
app.processEvents()
user32 = ctypes.windll.user32
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
hwnd = int(win.winId())
window, client, origin = wintypes.RECT(), wintypes.RECT(), wintypes.POINT(0, 0)
user32.GetWindowRect(hwnd, ctypes.byref(window))
user32.GetClientRect(hwnd, ctypes.byref(client))
user32.ClientToScreen(hwnd, ctypes.byref(origin))
ratio = win.devicePixelRatioF()
frame = win.chrome._frame(win.chrome._user32)


def lparam(x, y):
    return ((origin.y + y) & 0xFFFF) << 16 | ((origin.x + x) & 0xFFFF)


def hit(x, y):
    return user32.SendMessageW(hwnd, 0x0084, 0, lparam(round(x * ratio), round(y * ratio)))


hits = {"top": user32.SendMessageW(hwnd, 0x0084, 0, lparam(round(100 * ratio), frame - 1)),
        "caption": hit(100, 30), "maximize": hit(220, 30), "client_in_bar": hit(400, 30), "body": hit(100, 200)}
point = lparam(round(220 * ratio), round(30 * ratio))
user32.SendMessageW(hwnd, 0x00A0, 9, point)
user32.SendMessageW(hwnd, 0x00A1, 9, point)
user32.SendMessageW(hwnd, 0x00A2, 9, point)
user32.SendMessageW(hwnd, 0x00A2, 9, point)
user32.SendMessageW(hwnd, 0x0200, 0, point)
print(json.dumps({"installed": installed, "client_top_offset": origin.y - window.top,
                  "client_height": client.bottom, "window_height": window.bottom - window.top,
                  "frame": frame, "hits": hits, "events": events}))
sys.stdout.flush()
import os
os._exit(0)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="窗口外框只在 Windows 上接管")
def test_system_agrees_with_the_chrome_on_a_real_window():
    """在真实窗口平台上建一个不显示出来的窗口，直接向系统查询：客户区到了窗口顶端，各区域的命中与约定一致。"""
    environment = {**os.environ, "QT_QPA_PLATFORM": "windows", "PYTHONPATH": str(ROOT)}
    completed = subprocess.run([sys.executable, "-c", _PROBE], cwd=str(ROOT), env=environment,
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["installed"]
    # 客户区从窗口的上沿开始，只在下方留着系统看不见的缩放边
    assert report["client_top_offset"] == 0
    assert report["window_height"] - report["client_height"] == report["frame"]
    assert report["hits"] == {"top": HT_TOP, "caption": HT_CAPTION, "maximize": HT_MAXBUTTON,
                              "client_in_bar": 1, "body": 1}
    # 最大化按钮：悬停、按下、松开各报告一次，点击只算一次；鼠标回到客户区后悬停结束
    assert report["events"] == [[True, False], [True, True], [True, False], "clicked", [False, False]]
