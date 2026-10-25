from __future__ import annotations

import ctypes
from uuid import UUID

CLSCTX_LOCAL_SERVER = 0x4


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value):
        return cls.from_buffer_copy(UUID(value).bytes_le)


class WindowsDesktopWallpaper:
    """在同一线程内借用和释放 IDesktopWallpaper。"""

    def __init__(self):
        self._instance = ctypes.c_void_p()
        self._initialized = False

    def __enter__(self):
        self._ole32 = ctypes.WinDLL("ole32")
        self._ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._ole32.CoInitializeEx.restype = ctypes.c_int32
        self._ole32.CoUninitialize.argtypes = []
        self._ole32.CoUninitialize.restype = None
        self._ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        self._ole32.CoCreateInstance.restype = ctypes.c_int32
        self._ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self._ole32.CoTaskMemFree.restype = None

        result = self._ole32.CoInitializeEx(None, 2)
        self._initialized = result >= 0
        if (result & 0xFFFFFFFF) != 0x80010106:
            self._check(result, "CoInitializeEx")
        try:
            clsid = _GUID.from_string("C2CF3110-460E-4FC1-B9D0-8A1C0C9CC4BD")
            iid = _GUID.from_string("B92B56A9-8B55-4E14-9A89-0199BBB6F93B")
            self._check(self._ole32.CoCreateInstance(
                ctypes.byref(clsid), None, CLSCTX_LOCAL_SERVER, ctypes.byref(iid),
                ctypes.byref(self._instance),
            ), "CoCreateInstance")
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        try:
            if self._instance:
                self._method(2, restype=ctypes.c_uint32)(self._instance)
        finally:
            self._instance = ctypes.c_void_p()
            if self._initialized:
                self._ole32.CoUninitialize()
                self._initialized = False

    @staticmethod
    def _check(result, operation):
        if result < 0:
            raise OSError(f"{operation}: HRESULT 0x{result & 0xFFFFFFFF:08X}")

    def _method(self, index, *argtypes, restype=ctypes.c_int32):
        vtable = ctypes.cast(
            self._instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])

    def _string(self, index, argtypes, *args):
        value = ctypes.c_void_p()
        try:
            result = self._method(index, *argtypes, ctypes.POINTER(ctypes.c_void_p))(
                self._instance, *args, ctypes.byref(value)
            )
            self._check(result, "IDesktopWallpaper")
            return ctypes.wstring_at(value.value) if value.value else ""
        finally:
            self._ole32.CoTaskMemFree(value)

    def get_wallpapers(self):
        count = ctypes.c_uint32()
        self._check(self._method(6, ctypes.POINTER(ctypes.c_uint32))(
            self._instance, ctypes.byref(count)
        ), "GetMonitorDevicePathCount")
        wallpapers = []
        for index in range(count.value):
            monitor = self._string(5, (ctypes.c_uint32,), index)
            path = self._string(4, (ctypes.c_wchar_p,), monitor)
            wallpapers.append((monitor, path))
        return tuple(wallpapers)

    def get_position(self):
        position = ctypes.c_int32()
        self._check(self._method(11, ctypes.POINTER(ctypes.c_int32))(
            self._instance, ctypes.byref(position)
        ), "GetPosition")
        return position.value

    def set_position(self, position):
        self._check(self._method(10, ctypes.c_int32)(
            self._instance, int(position)
        ), "SetPosition")

    def set_wallpaper(self, path, monitor=None):
        self._check(self._method(3, ctypes.c_wchar_p, ctypes.c_wchar_p)(
            self._instance, monitor, str(path)
        ), "SetWallpaper")
