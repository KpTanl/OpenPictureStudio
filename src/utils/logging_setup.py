from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PyQt6.QtCore import QStandardPaths, QtMsgType, qInstallMessageHandler

from .app_info import APP_NAME


LOGGER_NAME = APP_NAME
LOG_FILE_NAME = "openpicturestudio.log"
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def configure_logging(app_name: str = APP_NAME) -> logging.Logger:
    root_logger = logging.getLogger()
    if getattr(root_logger, "_openpicturestudio_logging_configured", False):
        return logging.getLogger(LOGGER_NAME)

    log_dir = _resolve_log_dir(app_name)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_FILE_NAME

    formatter = logging.Formatter(LOG_FORMAT)
    from ..application.storage_paths import read_settings
    retention = read_settings()["limits"]

    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=retention.get("log_bytes", 2_000_000),
        backupCount=retention.get("log_count", 3),
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    root_logger._openpicturestudio_logging_configured = True

    logging.captureWarnings(True)
    _install_exception_hooks()
    _install_qt_message_handler()

    logger = logging.getLogger(LOGGER_NAME)
    logger.info("Logging initialized at %s", log_file)
    return logger


def _resolve_log_dir(app_name: str) -> Path:
    if app_name == APP_NAME:
        from .user_paths import application_root
        base_dir = str(application_root("APPDATA"))
    else:
        base_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    if not base_dir:
        base_dir = str(Path.home() / f".{app_name.lower()}")
    from ..application.storage_paths import storage_path
    return storage_path("logs", Path(base_dir) / "logs")


def _install_exception_hooks() -> None:
    if getattr(sys, "_openpicturestudio_exception_hooks", False):
        return

    previous_excepthook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_traceback):
        logging.getLogger(LOGGER_NAME).critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        previous_excepthook(exc_type, exc_value, exc_traceback)

    sys.excepthook = _excepthook

    if hasattr(threading, "excepthook"):
        previous_thread_hook = threading.excepthook

        def _thread_excepthook(args):
            logging.getLogger(LOGGER_NAME).critical(
                "Unhandled exception in thread %s",
                getattr(args.thread, "name", "unknown"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            previous_thread_hook(args)

        threading.excepthook = _thread_excepthook

    sys._openpicturestudio_exception_hooks = True


_QT_LOG_MAP = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}

_SUPPRESSED_QT_MESSAGES = {
    "QFont::setPointSize",
}


def _qt_message_handler(msg_type: QtMsgType, context, message: str) -> None:
    if any(message.startswith(prefix) for prefix in _SUPPRESSED_QT_MESSAGES):
        return
    level = _QT_LOG_MAP.get(msg_type, logging.WARNING)
    logger = logging.getLogger("Qt")
    logger.log(level, "%s", message)


def _install_qt_message_handler() -> None:
    if getattr(sys, "_openpicturestudio_qt_handler_installed", False):
        return
    qInstallMessageHandler(_qt_message_handler)
    sys._openpicturestudio_qt_handler_installed = True
