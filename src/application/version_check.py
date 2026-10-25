from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import http.client
import logging
import math
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..utils.app_info import APP_VERSION
from .storage_paths import file_lock

logger = logging.getLogger(__name__)
DOWNLOAD_URL = "https://github.com/KpTanl/OpenPictureStudio/releases/latest"
CHECK_INTERVAL = 24 * 60 * 60
_VERSION = re.compile(r"[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}")


def version_parts(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError("update.error.version")
    return tuple(int(part) for part in value.split("."))


def newer(value, current=APP_VERSION):
    return version_parts(value) > version_parts(current)


@dataclass(frozen=True)
class UpdateState:
    enabled: bool = True
    status: str = "idle"
    version: str = ""
    checked_at: float = 0
    error: str = ""
    warning: str = ""
    cached: bool = False
    busy: bool = False
    saving: bool = False
    notification: bool = False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def query_version(is_cancelled):
    if is_cancelled():
        raise InterruptedError
    request = Request(DOWNLOAD_URL, headers={"User-Agent": f"OpenPictureStudio/{APP_VERSION}"})
    response = None
    try:
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=10)
        except HTTPError as error:
            response = error
        if is_cancelled():
            raise InterruptedError
        if response.code == 404:
            raise ValueError("update.error.unavailable")
        if response.code != 302:
            raise ValueError("update.error.response")
        location = response.headers.get("Location", "")
        if len(location) > 512:
            raise ValueError("update.error.response")
        url = urlsplit(location)
        prefix = "/KpTanl/OpenPictureStudio/releases/tag/v"
        if (url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment
                or not url.path.startswith(prefix)):
            raise ValueError("update.error.response")
        version = url.path[len(prefix):]
        version_parts(version)
        return version
    except URLError as error:
        reason = "certificate" if isinstance(error.reason, ssl.SSLError) else "network"
        raise ValueError(f"update.error.{reason}") from error
    except ValueError as error:
        if str(error).startswith("update.error."):
            raise
        raise ValueError("update.error.response") from error
    except (OSError, http.client.HTTPException) as error:
        raise ValueError("update.error.network") from error
    finally:
        if response is not None:
            response.close()


class VersionCheckService:
    def __init__(self, store_factory, lock_path):
        self._store_factory = store_factory
        self._lock_path = lock_path

    @contextmanager
    def _store(self, is_cancelled):
        deadline = time.monotonic() + 5

        def cancelled():
            if is_cancelled():
                raise InterruptedError
            if time.monotonic() >= deadline:
                raise ValueError("update.error.settings")
            return False

        with file_lock(self._lock_path, blocking=True, is_cancelled=cancelled):
            cancelled()
            store = self._store_factory()
            store.sync()
            if not store.ok():
                raise ValueError("update.error.settings")
            yield store

    @staticmethod
    def _read(store):
        enabled = str(store.value("enabled", "true")).lower() in ("true", "1")
        version = str(store.value("latest_version", ""))
        try:
            version_parts(version)
            checked_at = float(store.value("last_success_utc", 0))
            if not math.isfinite(checked_at) or checked_at < 0 or checked_at > time.time():
                raise ValueError
        except (ValueError, TypeError):
            version, checked_at = "", 0
        return UpdateState(enabled=enabled, version=version, checked_at=checked_at, cached=bool(version))

    @staticmethod
    def _save(store, values):
        for key, value in values.items():
            store.set_value(key, value)
        store.sync()
        if not store.ok():
            raise ValueError("update.error.settings")

    def load(self, is_cancelled):
        with self._store(is_cancelled) as store:
            return self._read(store)

    def set_enabled(self, enabled, is_cancelled):
        with self._store(is_cancelled) as store:
            self._save(store, {"enabled": enabled})
            return self._read(store)

    def check(self, manual, is_cancelled):
        from dataclasses import replace

        warning = ""
        attempted = time.time()
        state = UpdateState()
        try:
            with self._store(is_cancelled) as store:
                state = self._read(store)
                if not manual and not state.enabled:
                    return state
                try:
                    previous = float(store.value("last_attempt_utc", 0))
                    if not math.isfinite(previous) or previous < 0 or previous > attempted:
                        raise ValueError
                except (ValueError, TypeError):
                    self._save(store, {"last_attempt_utc": attempted})
                    if not manual:
                        return state
                    previous = 0
                if not manual and attempted - previous < CHECK_INTERVAL:
                    return state
                self._save(store, {"last_attempt_utc": attempted})
        except InterruptedError:
            raise
        except (OSError, ValueError):
            logger.warning("更新查询偏好读取或保存失败", exc_info=True)
            if not manual:
                return replace(state, warning="update.error.settings")
            warning = "update.error.settings"
        if is_cancelled():
            raise InterruptedError
        try:
            version = query_version(is_cancelled)
        except ValueError as error:
            logger.info("版本查询失败：%s", error)
            return replace(state, status="failed", error=str(error), warning=warning)
        checked_at = time.time()
        try:
            with self._store(is_cancelled) as store:
                state = self._read(store)
                self._save(store, {"latest_version": version, "last_success_utc": checked_at})
        except InterruptedError:
            raise
        except (OSError, ValueError):
            logger.warning("更新查询结果保存失败", exc_info=True)
            warning = "update.error.settings"
        return UpdateState(enabled=state.enabled, status="available" if newer(version) else "current",
                           version=version, checked_at=checked_at, warning=warning)
