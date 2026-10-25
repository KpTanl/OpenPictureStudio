"""新版本提醒：关闭自动检查时撤销尚未联网的自动查询。"""

from threading import Event
import time

import pytest

from src.application import version_check
from src.application.version_check import UpdateState, VersionCheckService
from src.ui.services import version_check_runner
from src.ui.services.version_check_runner import VersionCheckRunner
from tests.support import wait_until


class MemoryStore:
    values = {}

    def value(self, key, default):
        return self.values.get(key, default)

    def set_value(self, key, value):
        self.values[key] = value

    def sync(self):
        pass

    def ok(self):
        return True


class BlockingService:
    """自动查询停在“等待偏好锁”，直到放行或被取消。"""

    def __init__(self):
        self.waiting, self.release, self.queried = Event(), Event(), Event()
        self.checks, self.saved = [], []

    def load(self, is_cancelled):
        return UpdateState()

    def check(self, manual, is_cancelled):
        self.checks.append(manual)
        self.waiting.set()
        while not self.release.is_set():
            if is_cancelled():
                raise InterruptedError
            time.sleep(0.005)
        self.queried.set()
        return UpdateState(enabled=not self.saved or self.saved[-1], status="current")

    def set_enabled(self, enabled, is_cancelled):
        self.saved.append(enabled)
        return UpdateState(enabled=enabled)


@pytest.fixture
def runner(monkeypatch):
    service = BlockingService()
    monkeypatch.setattr(version_check_runner, "VersionCheckService", lambda *args: service)
    runner = VersionCheckRunner()
    wait_until(lambda: not runner._operation, "读取更新偏好")
    yield runner, service
    service.release.set()
    runner.shutdown()


class Redirect:
    code = 302

    def __init__(self, location):
        self.headers = {"Location": location}

    def close(self):
        pass


@pytest.mark.parametrize(("location", "expected"), [
    ("https://github.com/KpTanl/OpenPictureStudio/releases/tag/v0.16.0", "0.16.0"),
    ("https://github.com/KpTanl/OpenPictureStudio-archive/releases/tag/v0.16.0", "update.error.response"),
    ("https://example.com/KpTanl/OpenPictureStudio/releases/tag/v0.16.0", "update.error.response"),
    ("https://github.com/KpTanl/OpenPictureStudio/releases/tag/v0.16.0-beta", "update.error.version"),
])
def test_latest_release_redirect_is_read_from_the_public_repository(monkeypatch, location, expected):
    opener = type("Opener", (), {"open": lambda self, request, timeout: Redirect(location)})()
    monkeypatch.setattr(version_check, "build_opener", lambda *handlers: opener)
    assert version_check.DOWNLOAD_URL == "https://github.com/KpTanl/OpenPictureStudio/releases/latest"
    if expected.startswith("update.error."):
        with pytest.raises(ValueError, match=expected.replace(".", r"\.")):
            version_check.query_version(lambda: False)
    else:
        assert version_check.query_version(lambda: False) == expected


def test_cancelled_automatic_check_never_queries(tmp_path, monkeypatch):
    queries = []
    monkeypatch.setattr(version_check, "query_version", lambda is_cancelled: queries.append(1) or "9.9.9")
    service = VersionCheckService(MemoryStore, tmp_path / "update-check.lock")
    with pytest.raises(InterruptedError):
        service.check(False, lambda: True)
    assert not queries


def test_disabling_cancels_waiting_automatic_check(runner):
    runner, service = runner
    runner._automatic()
    assert service.waiting.wait(5)
    runner.set_enabled(False)
    wait_until(lambda: service.saved == [False] and not runner._operation, "保存关闭状态")
    assert not service.queried.is_set()
    assert service.checks == [False]
    assert runner.state.enabled is False and runner.state.status == "idle" and not runner.state.error


def test_manual_request_runs_after_cancelled_automatic_check(runner):
    runner, service = runner
    runner._automatic()
    assert service.waiting.wait(5)
    runner.check(manual=True)
    runner.set_enabled(False)
    wait_until(lambda: service.saved == [False], "保存关闭状态")
    service.release.set()
    wait_until(lambda: service.queried.is_set() and not runner._operation, "手动检查")
    assert service.checks == [False, True]
    assert runner.state.enabled is False and runner.state.status == "current"
