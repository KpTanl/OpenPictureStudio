
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import logging
import sys
from threading import Event
import time

from PySide6.QtCore import QObject, QSettings, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from ...utils.app_info import APP_NAME
from ...application.storage_paths import bootstrap_root
from ...application.version_check import CHECK_INTERVAL, DOWNLOAD_URL, UpdateState, VersionCheckService, newer

logger = logging.getLogger(__name__)


class _SettingsStore:
    def __init__(self):
        self._settings = QSettings(APP_NAME, "Update")

    def value(self, key, default):
        return self._settings.value(key, default)

    def set_value(self, key, value):
        self._settings.setValue(key, value)

    def sync(self):
        self._settings.sync()

    def ok(self):
        return self._settings.status() == QSettings.Status.NoError


class VersionCheckRunner(QObject):
    changed = Signal(object)
    failed = Signal(str)
    _completed = Signal(str, object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = UpdateState()
        self._service = VersionCheckService(_SettingsStore, bootstrap_root() / "update-check.lock")
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-update")
        self._cancelled = Event()
        self._automatic_cancel = None
        self._closed = False
        self._operation = ""
        self._manual = False
        self._pending_manual = False
        self._pending_enabled = None
        self._dismissed = False
        self._confirmed_enabled = True
        self._automatic_due = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._automatic)
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._submit("load", lambda: self._service.load(self._cancelled.is_set))
        if getattr(sys, "frozen", False):
            self._timer.start(60000)

    def _submit(self, name, operation):
        self._operation = name
        self.state = replace(self.state, busy=name == "check", saving=name == "enabled")
        self.changed.emit(self.state)
        try:
            self._pool.submit(self._run, name, operation)
        except RuntimeError:
            self._deliver(name, None, "update.error.settings")

    def _run(self, name, operation):
        state, error = None, ""
        try:
            state = operation()
        except InterruptedError:
            if self._cancelled.is_set():
                return
            error = "interrupted"
        except Exception:
            logger.exception("更新任务失败：%s", name)
            error = "update.error.settings"
        if not self._cancelled.is_set():
            try:
                self._completed.emit(name, state, error)
            except RuntimeError:
                logger.debug("更新任务接收方已释放")

    def _automatic(self):
        if self._closed:
            return
        self._automatic_due = True
        if not self._operation and self.state.enabled:
            self.check()

    def check(self, manual=False):
        if self._closed:
            return
        if self._operation:
            if manual:
                if self._operation == "check":
                    self._manual = True
                else:
                    self._pending_manual = True
            return
        if not manual and (not self.state.enabled or not self._automatic_due):
            return
        self._automatic_due = False
        self._manual = manual
        # 关闭自动检查时撤销尚未联网的自动查询；手动检查只随退出取消
        cancel = None if manual else Event()
        self._automatic_cancel = cancel
        is_cancelled = self._cancelled.is_set if cancel is None else (
            lambda: self._cancelled.is_set() or cancel.is_set())
        self.state = replace(self.state, status="checking", error="", warning="")
        self._submit("check", lambda: self._service.check(manual, is_cancelled))

    def set_enabled(self, enabled):
        if self._closed:
            return
        self._pending_enabled = bool(enabled)
        if not enabled and self._automatic_cancel is not None:
            self._automatic_cancel.set()
        self.state = replace(self.state, enabled=bool(enabled), notification=False, saving=True)
        self.changed.emit(self.state)
        self._drain()

    def _drain(self):
        if self._closed or self._operation:
            return
        if self._pending_enabled is not None:
            enabled, self._pending_enabled = self._pending_enabled, None
            self._submit("enabled", lambda: self._service.set_enabled(enabled, lambda: False))
        elif self._pending_manual:
            self._pending_manual = False
            self.check(manual=True)
        elif self._automatic_due and self.state.enabled:
            self.check()

    @Slot(str, object, str)
    def _deliver(self, name, state, error):
        if self._closed:
            return
        self._operation = ""
        if name == "check":
            self._automatic_cancel = None
        interrupted = error == "interrupted"
        if interrupted:
            state = replace(self.state, enabled=self._confirmed_enabled, status="idle")
        elif state is None:
            state = replace(self.state, enabled=self._confirmed_enabled, error=error, status="failed")
        else:
            self._confirmed_enabled = state.enabled
        if name == "check" and self._manual and (interrupted or (state.cached and state.status != "failed")):
            self._pending_manual = True
        if self._pending_enabled is not None:
            state = replace(state, enabled=self._pending_enabled)
        eligible = (getattr(sys, "frozen", False) and not self._dismissed and state.enabled and bool(state.version)
                    and 0 <= time.time() - state.checked_at < CHECK_INTERVAL and newer(state.version))
        if name == "check" and state.status == "failed":
            eligible = False
        self.state = replace(state, busy=False, saving=self._pending_enabled is not None,
                             notification=eligible and (name != "check" or not self._manual))
        self.changed.emit(self.state)
        self._drain()

    def dismiss(self):
        self._dismissed = True
        self.state = replace(self.state, notification=False)
        self.changed.emit(self.state)

    def open_download(self):
        if not QDesktopServices.openUrl(QUrl(DOWNLOAD_URL)):
            self.failed.emit("update.error.browser")

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._cancelled.set()
        if self._pending_enabled is not None:
            enabled, self._pending_enabled = self._pending_enabled, None
            self._pool.submit(self._run, "enabled", lambda: self._service.set_enabled(enabled, lambda: False))
        self._pool.shutdown(wait=False, cancel_futures=False)
