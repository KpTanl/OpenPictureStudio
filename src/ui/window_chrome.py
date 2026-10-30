"""把系统标题栏的位置交给界面自己画，窗口的边框行为仍由系统负责（仅 Windows）。

窗口保留系统的样式：拖边缩放、贴边分屏、阴影、圆角、最大化与系统菜单都还是系统的。这里只做三件事：
客户区向上扩到窗口顶端；顶端留一条缩放边；按界面给出的区域告诉系统哪里可以拖动、哪里是最大化按钮。
界面经 ``region_at`` 回答窗口里一点属于哪一种区域。最大化按钮上的鼠标消息归系统（这样悬停时才有分屏布局），
界面收不到，所以它的悬停与按下经 ``maximize_state_changed`` 告知，点击经 ``maximize_clicked`` 告知；
这里不改变窗口的状态。

其他平台上 ``install()`` 返回 False，窗口保持系统标题栏。
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Optional

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QWidget

logger = logging.getLogger(__name__)

# region_at 的返回值
CAPTION, MAXIMIZE, CLIENT = "caption", "maximize", "client"

_WM_NCCALCSIZE = 0x0083
_WM_NCHITTEST = 0x0084
_WM_NCMOUSEMOVE = 0x00A0
_WM_NCLBUTTONDOWN = 0x00A1
_WM_NCLBUTTONUP = 0x00A2
_WM_NCLBUTTONDBLCLK = 0x00A3
_WM_MOUSEMOVE = 0x0200
_WM_NCMOUSELEAVE = 0x02A2
_HTCLIENT, _HTCAPTION, _HTMAXBUTTON, _HTTOP = 1, 2, 9, 12
_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000
_SM_CYSIZEFRAME, _SM_CXPADDEDBORDER = 33, 92
_SWP_FRAME_CHANGED_ONLY = 0x0020 | 0x0002 | 0x0001 | 0x0004 | 0x0010  # 只重算边框，不动位置、大小、层次与激活
_TME_LEAVE, _TME_NONCLIENT = 0x0002, 0x0010


def client_top(window_top: int, frame: int, maximized: bool) -> int:
    """客户区的上沿：平时就是窗口的上沿；最大化时窗口比屏幕大出一圈边框，让开这一圈。"""
    return window_top + frame if maximized else window_top


def hit_code(region: str, y: int, frame: int, maximized: bool) -> Optional[int]:
    """窗口里一点的命中结果；None 表示按普通客户区交给界面。y 是相对客户区上沿的物理像素。"""
    if region == MAXIMIZE:
        return _HTMAXBUTTON
    if not maximized and 0 <= y < frame:
        return _HTTOP
    if region == CAPTION:
        return _HTCAPTION
    return None


class WindowChrome:
    def __init__(self, window: QWidget, region_at: Callable[[QPoint], str],
                 maximize_state_changed: Callable[[bool, bool], None], maximize_clicked: Callable[[], None]):
        self._window = window
        self._region_at = region_at
        self._maximize_state_changed = maximize_state_changed
        self._maximize_clicked = maximize_clicked
        self._user32 = None
        self._message_type = None
        self._hwnd = 0
        self._hovered = False
        self._pressed = False

    def is_installed(self) -> bool:
        return self._user32 is not None

    def install(self) -> bool:
        """让窗口按这里的规则重算边框。窗口的系统句柄建好之后调用一次；不支持时返回 False。"""
        if sys.platform != "win32" or self._user32 is not None:
            return self._user32 is not None
        try:
            from ctypes import wintypes
            hwnd = int(self._window.winId())
            user32 = _bind_user32()
            if not hwnd or not user32.GetWindowLongPtrW(hwnd, _GWL_STYLE) & _WS_CAPTION:
                return False
            self._user32, self._hwnd, self._message_type = user32, hwnd, wintypes.MSG
            if not user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, _SWP_FRAME_CHANGED_ONLY):
                self._user32 = None
                return False
            return True
        except Exception:
            logger.debug("窗口外框未能接管", exc_info=True)
            self._user32 = None
            return False

    def native_event(self, message) -> tuple[bool, int]:
        """窗口的 nativeEvent 把消息交到这里；返回 (是否已处理, 结果)。"""
        user32 = self._user32
        if user32 is None:
            return False, 0
        msg = self._message_type.from_address(int(message))
        if msg.hWnd != self._hwnd:
            return False, 0
        kind = msg.message
        if kind == _WM_NCCALCSIZE:
            return self._calculate_client(user32, msg)
        if kind == _WM_NCHITTEST:
            return self._hit_test(user32, msg)
        if kind == _WM_NCMOUSEMOVE:
            self._set_maximize_state(msg.wParam == _HTMAXBUTTON, self._pressed and msg.wParam == _HTMAXBUTTON)
            if msg.wParam == _HTMAXBUTTON:
                self._track_leave(user32)
            return False, 0
        if kind in (_WM_NCLBUTTONDOWN, _WM_NCLBUTTONDBLCLK) and msg.wParam == _HTMAXBUTTON:
            # 不交给系统：它会画出旧式的按钮并自己跟踪鼠标
            self._set_maximize_state(True, True)
            return True, 0
        if kind == _WM_NCLBUTTONUP and msg.wParam == _HTMAXBUTTON:
            pressed = self._pressed
            self._set_maximize_state(True, False)
            if pressed:
                self._maximize_clicked()
            return True, 0
        if kind in (_WM_NCMOUSELEAVE, _WM_MOUSEMOVE, _WM_NCLBUTTONUP):
            self._set_maximize_state(False, False)
        return False, 0

    def _has_caption(self, user32) -> bool:
        """全屏时窗口没有标题栏样式，这时不接管。"""
        return bool(user32.GetWindowLongPtrW(self._hwnd, _GWL_STYLE) & _WS_CAPTION)

    def _frame(self, user32) -> int:
        dpi = user32.GetDpiForWindow(self._hwnd)
        return user32.GetSystemMetricsForDpi(_SM_CYSIZEFRAME, dpi) + user32.GetSystemMetricsForDpi(
            _SM_CXPADDEDBORDER, dpi)

    def _calculate_client(self, user32, msg) -> tuple[bool, int]:
        if not msg.wParam or not self._has_caption(user32):
            return False, 0
        from ctypes import wintypes
        # wParam 非零时 lParam 指向 NCCALCSIZE_PARAMS，它的第一项就是窗口矩形，处理后改成客户区
        rect = wintypes.RECT.from_address(msg.lParam)
        window_top = rect.top
        user32.DefWindowProcW(msg.hWnd, msg.message, msg.wParam, msg.lParam)
        rect.top = client_top(window_top, self._frame(user32), bool(user32.IsZoomed(self._hwnd)))
        return True, 0

    def _hit_test(self, user32, msg) -> tuple[bool, int]:
        # 被模态对话框挡住时不接管：拖动、缩放与最大化都不应绕过对话框
        if not self._has_caption(user32) or QApplication.activeModalWidget() not in (None, self._window):
            return False, 0
        from ctypes import byref, c_short, wintypes
        point = wintypes.POINT(c_short(msg.lParam & 0xFFFF).value, c_short((msg.lParam >> 16) & 0xFFFF).value)
        if not user32.ScreenToClient(self._hwnd, byref(point)):
            return False, 0
        client = wintypes.RECT()
        user32.GetClientRect(self._hwnd, byref(client))
        if not (0 <= point.x < client.right and 0 <= point.y < client.bottom):
            return False, 0
        ratio = self._window.devicePixelRatioF() or 1.0
        region = self._region_at(QPoint(int(point.x / ratio), int(point.y / ratio)))
        code = hit_code(region, point.y, self._frame(user32), bool(user32.IsZoomed(self._hwnd)))
        return (False, 0) if code is None else (True, code)

    def _track_leave(self, user32) -> None:
        from ctypes import Structure, byref, sizeof, wintypes

        class _TrackMouseEvent(Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]

        request = _TrackMouseEvent(sizeof(_TrackMouseEvent), _TME_LEAVE | _TME_NONCLIENT, self._hwnd, 0)
        user32.TrackMouseEvent(byref(request))

    def _set_maximize_state(self, hovered: bool, pressed: bool) -> None:
        if (hovered, pressed) != (self._hovered, self._pressed):
            self._hovered, self._pressed = hovered, pressed
            self._maximize_state_changed(hovered, pressed)


def _bind_user32():
    """取 user32 并声明这里用到的函数的参数与返回类型；指针与句柄在 64 位下不能按默认的 int 传。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    result = ctypes.c_ssize_t
    user32.DefWindowProcW.restype = result
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetWindowLongPtrW.restype = result
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wintypes.UINT]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetSystemMetricsForDpi.restype = ctypes.c_int
    user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
    user32.ScreenToClient.restype = wintypes.BOOL
    user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.TrackMouseEvent.restype = wintypes.BOOL
    user32.TrackMouseEvent.argtypes = [ctypes.c_void_p]
    return user32
