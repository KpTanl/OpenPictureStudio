
import os
import logging
import sys

from .app_info import APP_NAME
from ..core.constants import SUPPORTED_EXTENSIONS
from .resource_paths import resolve_app_icon_path

PROGID_PREFIX = APP_NAME


logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import winreg


def get_app_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" "%1"'
    script_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "main.py")
    )
    return f'"{sys.executable}" "{script_path}" "%1"'


def register_extension(ext: str) -> bool:
    if not _IS_WINDOWS:
        return False

    app_command = get_app_command()
    icon_value = None
    if getattr(sys, "frozen", False):
        icon_value = f'"{sys.executable}",0'
    else:
        icon_path = resolve_app_icon_path()
        if icon_path.exists():
            icon_value = f'"{icon_path}",0'

    progid = f"{PROGID_PREFIX}{ext}"
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{progid}\\shell\\open\\command",
            0,
            winreg.KEY_WRITE,
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, app_command)

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{progid}",
            0,
            winreg.KEY_WRITE,
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"{APP_NAME} File")

        if icon_value:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                f"Software\\Classes\\{progid}\\DefaultIcon",
                0,
                winreg.KEY_WRITE,
            ) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, icon_value)

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\{ext}\\OpenWithProgids",
            0,
            winreg.KEY_WRITE,
        ) as key:
            winreg.SetValueEx(key, progid, 0, winreg.REG_NONE, b"")

        _notify_shell_change()
        return True
    except OSError as exc:
        logger.warning("Failed to register file association for %s: %s", ext, exc)
        return False


def unregister_extension(ext: str) -> bool:
    if not _IS_WINDOWS:
        return False

    progid = f"{PROGID_PREFIX}{ext}"
    _delete_key_recursive(
        winreg.HKEY_CURRENT_USER, f"Software\\Classes\\{progid}"
    )
    try:
        openwith_path = f"Software\\Classes\\{ext}\\OpenWithProgids"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, openwith_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, progid)
    except OSError as exc:
        logger.debug("Failed to remove OpenWithProgids entry for %s: %s", ext, exc)

    _notify_shell_change()
    return True


def register_file_associations() -> dict[str, bool]:
    if not _IS_WINDOWS:
        return {}

    results = {}
    for ext in SUPPORTED_EXTENSIONS:
        results[ext] = register_extension(ext)
    return results


def unregister_file_associations() -> dict[str, bool]:
    if not _IS_WINDOWS:
        return {}

    results = {}
    for ext in SUPPORTED_EXTENSIONS:
        results[ext] = unregister_extension(ext)
    return results


def get_registration_status() -> dict[str, bool]:
    if not _IS_WINDOWS:
        return {ext: False for ext in SUPPORTED_EXTENSIONS}

    results = {}
    for ext in SUPPORTED_EXTENSIONS:
        progid = f"{PROGID_PREFIX}{ext}"
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                f"Software\\Classes\\{progid}\\shell\\open\\command",
            ) as key:
                winreg.QueryValueEx(key, "")
            results[ext] = True
        except OSError as exc:
            logger.debug("Registration lookup failed for %s: %s", ext, exc)
            results[ext] = False
    return results


def open_system_default_apps():
    try:
        os.startfile("ms-settings:defaultapps")
    except Exception:
        logger.exception("Failed to open system default apps settings")


def is_windows() -> bool:
    return _IS_WINDOWS


def _delete_key_recursive(hive, path):
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_ALL_ACCESS) as key:
            subkeys = []
            i = 0
            while True:
                try:
                    subkeys.append(winreg.EnumKey(key, i))
                    i += 1
                except OSError:
                    break
            for subkey in subkeys:
                _delete_key_recursive(hive, f"{path}\\{subkey}")
        winreg.DeleteKey(hive, path)
    except OSError as exc:
        logger.debug("Registry key cleanup skipped for %s: %s", path, exc)


def _notify_shell_change():
    try:
        import ctypes

        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
    except Exception:
        logger.exception("Failed to notify shell change")

    try:
        import subprocess

        # 使用非阻塞 Popen 异步触发，避免阻塞 UI 线程与滑块动画
        subprocess.Popen(
            ["ie4uinit.exe", "-show"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        logger.debug("Icon cache refresh command is unavailable", exc_info=True)