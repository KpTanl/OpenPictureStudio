from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtCore import QSettings

from ..utils.app_info import APP_NAME
from .chat_completion import ModelSettings


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(data: bytes, *, decrypt: bool = False) -> bytes:
    if sys.platform != "win32":
        raise ValueError("AI_KEY_STORAGE_UNAVAILABLE")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _DataBlob(len(data), buffer)
    output = _DataBlob()
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError("AI_KEY_READ_FAILED" if decrypt else "AI_KEY_SAVE_FAILED")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel32.LocalFree(output.data)


class AssistantSettingsStore:
    def __init__(self):
        self.settings = QSettings(APP_NAME, "AIAssistant")
        self.error = None

    def load(self) -> ModelSettings:
        self.error = None
        key = ""
        encrypted = self.settings.value("api_key_protected", "")
        if encrypted:
            try:
                key = _protect(base64.b64decode(encrypted, validate=True), decrypt=True).decode("utf-8")
            except (ValueError, OSError):
                self.error = "AI_KEY_READ_FAILED"
        return ModelSettings(
            str(self.settings.value("endpoint", "")),
            str(self.settings.value("model", "")), key,
        )

    def save(self, configuration: ModelSettings) -> None:
        encrypted = ""
        if configuration.api_key:
            encrypted = base64.b64encode(_protect(configuration.api_key.encode("utf-8"))).decode("ascii")
        self.settings.setValue("endpoint", configuration.endpoint)
        self.settings.setValue("model", configuration.model)
        if encrypted:
            self.settings.setValue("api_key_protected", encrypted)
        else:
            self.settings.remove("api_key_protected")
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            raise ValueError("AI_KEY_SAVE_FAILED")
        self.error = None
