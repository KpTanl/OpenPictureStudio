from __future__ import annotations

import json
from threading import Event, Thread

from PySide6.QtCore import QEventLoop, QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from ..application.storage_paths import StorageSession, read_settings
from ..application.runtime_bootstrap import activate
from .model_paths import get_runtime_root
from .resource_paths import resolve_resource
from .app_info import APP_NAME


class _StartupSignals(QObject):
    progress = Signal(str)
    done = Signal()


def prepare_storage(app):
    language = QSettings(APP_NAME, "Language").value("current", "en")
    language = language if language in ("en", "fr", "zh-CN") else "en"
    catalog = json.loads(resolve_resource("i18n/" + language + ".json").read_text(encoding="utf-8"))
    cancelled = Event()
    signals = _StartupSignals()
    result, errors = [], []

    def work():
        session = None
        try:
            session = StorageSession(cancelled=cancelled.is_set, progress=signals.progress.emit)
            activation = activate(get_runtime_root())
            session.ready()
            result.append((session, activation))
        except Exception as error:
            if session is not None:
                session.close()
            errors.append(error)
        finally:
            signals.done.emit()

    try:
        settings = read_settings()
    except ValueError as error:
        QMessageBox.critical(None, catalog["storage.startup"], catalog.get(str(error), str(error)))
        raise
    if not settings["jobs"] and not settings["limits"].get("backup_days", 0):
        work()
    else:
        dialog = QProgressDialog(catalog["storage.startup_note"], catalog["common.cancel"], 0, 0)
        dialog.setWindowTitle(catalog["storage.startup"])
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.canceled.connect(cancelled.set)
        signals.progress.connect(lambda kind: dialog.setLabelText(catalog["storage.startup_category"].format(
            kind=catalog.get("storage.kind." + kind, kind))))
        loop = QEventLoop()
        signals.done.connect(loop.quit)
        worker = Thread(target=work, name="ops-storage-startup")
        quit_on_close = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        dialog.show()
        worker.start()
        loop.exec()
        worker.join()
        dialog.close()
        app.setQuitOnLastWindowClosed(quit_on_close)
    if errors:
        error = errors[0]
        QMessageBox.critical(None, catalog["storage.startup"], catalog.get(str(error), str(error)))
        raise error
    return result[0]
