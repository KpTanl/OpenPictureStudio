"""后台服务状态到界面线程的转接：信号只在界面线程发出，接收方销毁后不再被触碰。"""

import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.application.listeners import ListenerRegistry  # noqa: E402
from src.ui.services.model_task_runner import ModelTaskRunner  # noqa: E402
from src.ui.services.ui_thread_relay import UiThreadRelay  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Service:
    """与模型服务、运行环境服务相同的订阅接口。"""

    def __init__(self):
        self.registry = ListenerRegistry("订阅失败")

    def subscribe(self, listener):
        return self.registry.subscribe(listener)

    def notify_from_worker(self, key="model"):
        worker = threading.Thread(target=self.registry.notify, args=(key,))
        worker.start()
        worker.join(5)

    @property
    def subscribers(self):
        return len(self.registry._listeners)


def test_relay_is_shared_by_the_application(qapp):
    assert UiThreadRelay.shared() is UiThreadRelay.shared()
    assert UiThreadRelay.shared().parent() is qapp


def test_relay_runs_callbacks_on_the_ui_thread_and_survives_failures(qapp, caplog):
    relay, threads = UiThreadRelay.shared(), []

    def broken():
        raise RuntimeError("boom")

    worker = threading.Thread(target=lambda: (relay.post(broken),
                                              relay.post(lambda: threads.append(threading.get_ident()))))
    worker.start()
    worker.join(5)
    assert threads == []
    qapp.processEvents()
    assert threads == [threading.get_ident()]
    assert "界面线程回调失败" in caplog.text


def test_background_notification_arrives_on_the_ui_thread(qapp):
    service = _Service()
    runner, threads = ModelTaskRunner(service), []
    runner.changed.connect(lambda: threads.append(threading.get_ident()))
    service.notify_from_worker()
    assert threads == [], "后台线程里不应直接发出界面信号"
    qapp.processEvents()
    assert threads == [threading.get_ident()]
    runner.close()


def test_close_unsubscribes_and_stops_delivery(qapp):
    service = _Service()
    runner, calls = ModelTaskRunner(service), []
    runner.changed.connect(lambda: calls.append(1))
    assert service.subscribers == 1
    runner.close()
    runner.close()
    assert service.subscribers == 0
    service.notify_from_worker()
    qapp.processEvents()
    assert calls == []


def test_destroyed_runner_is_unsubscribed(qapp):
    service = _Service()
    runner = ModelTaskRunner(service)
    sip.delete(runner)
    assert service.subscribers == 0
    service.notify_from_worker()
    qapp.processEvents()


def test_notification_queued_before_destruction_is_dropped(qapp):
    """通知已排进界面线程队列、接收方随后被销毁：投递时应丢弃而不是访问已销毁对象。"""
    service = _Service()
    runner, calls = ModelTaskRunner(service), []
    runner.changed.connect(lambda: calls.append(1))
    service.notify_from_worker()
    sip.delete(runner)
    qapp.processEvents()
    assert calls == []


def test_each_runner_only_releases_its_own_subscription(qapp):
    service = _Service()
    first, second, calls = ModelTaskRunner(service), ModelTaskRunner(service), []
    second.changed.connect(lambda: calls.append("second"))
    first.close()
    assert service.subscribers == 1
    service.notify_from_worker()
    qapp.processEvents()
    assert calls == ["second"]
    second.close()
