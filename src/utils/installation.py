from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys

from .app_info import APP_NAME

APPLICATION_MUTEX = "Global\\OpenPictureStudioApp"
MAINTENANCE_MUTEX = "Global\\OpenPictureStudioMaintenance"
ADMISSION_MUTEX = "Global\\OpenPictureStudioAdmission"
PENDING_NAME = ".install-pending.txt"
_handles = []


def installation_root():
    return Path(sys.executable).absolute().parent if sys.platform == "win32" and getattr(sys, "frozen", False) else None


def check_storage_target(target):
    root = installation_root()
    if root is None:
        return
    left, right = (os.path.normcase(os.path.realpath(path)) for path in (root, target))
    try:
        common = os.path.commonpath((left, right))
    except ValueError:
        return
    if common in (left, right):
        raise ValueError("storage.error.installation")


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexExW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.CreateMutexExW.restype = wintypes.HANDLE
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return kernel


def _create_mutex(kernel, name):
    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;;0x00100001;;;WD)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        handle = kernel.CreateMutexExW(ctypes.byref(attributes), name, 0, 0x00100001)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle
    finally:
        kernel.LocalFree(descriptor)


def _maintenance_active(kernel):
    handle = kernel.OpenMutexW(0x00100000, False, MAINTENANCE_MUTEX)
    if handle:
        kernel.CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    if error == 5:
        return True
    if error != 2:
        raise ctypes.WinError(error)
    return False


@contextmanager
def _admission(kernel, name):
    handle = _create_mutex(kernel, name)
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, 5000)
        if result not in (0, 0x80):
            raise OSError("update.startup.failed")
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def _show_startup_error(key):
    message = {"update.startup.maintenance": "Installation or removal is in progress. Please try again later.",
               "update.startup.repair": "Installation is incomplete. Run the same installer again to repair it.",
               "update.startup.failed": "The installation status could not be checked. Please try again."}[key]
    try:
        import winreg
        language = "en"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"Software\\{APP_NAME}\\Language") as settings:
                language = winreg.QueryValueEx(settings, "current")[0]
        except FileNotFoundError:
            pass
        if language in ("en", "fr", "zh-CN"):
            root = Path(getattr(sys, "_MEIPASS", installation_root()))
            catalog = json.loads((root / "resources" / "i18n" / (language + ".json")).read_text(encoding="utf-8"))
            message = catalog.get(key, message)
    except (OSError, ValueError):
        pass
    ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)


def guard_startup():
    root = installation_root()
    if root is None:
        return True
    sys.dont_write_bytecode = True
    handles = []
    try:
        kernel = _kernel()
        with _admission(kernel, ADMISSION_MUTEX):
            if _maintenance_active(kernel):
                raise RuntimeError("update.startup.maintenance")
            if (root / PENDING_NAME).exists():
                raise RuntimeError("update.startup.repair")
            handles.append(_create_mutex(kernel, APPLICATION_MUTEX))
            if _maintenance_active(kernel):
                raise RuntimeError("update.startup.maintenance")
            _handles.extend(handles)
        return True
    except (OSError, RuntimeError) as error:
        for handle in handles:
            kernel.CloseHandle(handle)
        key = str(error) if str(error).startswith("update.startup.") else "update.startup.failed"
        _show_startup_error(key)
        return False
