"""订阅者登记：通知顺序、异常隔离，以及取消订阅后不再回调的保证。"""

import threading

from src.application.listeners import ListenerRegistry


def _registry():
    return ListenerRegistry("订阅失败")


def test_listeners_are_called_in_subscription_order_with_arguments():
    registry, calls = _registry(), []
    registry.subscribe(lambda key: calls.append(("first", key)))
    registry.subscribe(lambda key: calls.append(("second", key)))
    registry.notify("model")
    assert calls == [("first", "model"), ("second", "model")]


def test_unsubscribed_listener_is_not_called():
    registry, calls = _registry(), []
    unsubscribe = registry.subscribe(calls.append)
    unsubscribe()
    unsubscribe()
    registry.notify(1)
    assert calls == []


def test_failing_listener_does_not_block_the_others(caplog):
    registry, calls = _registry(), []

    def broken(_key):
        raise RuntimeError("boom")

    registry.subscribe(broken)
    registry.subscribe(calls.append)
    registry.notify(1)
    assert calls == [1]
    assert "订阅失败" in caplog.text


def test_listener_can_unsubscribe_itself_without_waiting():
    registry, calls = _registry(), []

    def once(key):
        calls.append(key)
        unsubscribe()

    unsubscribe = registry.subscribe(once)
    registry.notify(1)
    registry.notify(2)
    assert calls == [1]


def test_listener_removed_during_a_notification_pass_is_skipped():
    registry, calls = _registry(), []
    registry.subscribe(lambda key: remove_second())
    remove_second = registry.subscribe(lambda key: calls.append(key))
    registry.notify(1)
    assert calls == []


def test_clear_drops_all_listeners():
    registry, calls = _registry(), []
    registry.subscribe(calls.append)
    registry.clear()
    registry.notify(1)
    assert calls == []


def test_unsubscribe_waits_for_a_callback_running_on_another_thread():
    """取消订阅返回时，其他线程上的回调必须已经结束，订阅方才能安全释放自身。"""
    registry = _registry()
    entered, release, events = threading.Event(), threading.Event(), []

    def slow(_key):
        entered.set()
        release.wait(5)
        events.append("callback finished")

    unsubscribe = registry.subscribe(slow)
    worker = threading.Thread(target=registry.notify, args=(1,))
    worker.start()
    assert entered.wait(5)

    def stop():
        unsubscribe()
        events.append("unsubscribe returned")

    stopper = threading.Thread(target=stop)
    stopper.start()
    stopper.join(0.2)
    assert stopper.is_alive(), "回调仍在执行时取消订阅不应返回"
    release.set()
    stopper.join(5)
    worker.join(5)
    assert events == ["callback finished", "unsubscribe returned"]


def test_no_callback_starts_after_unsubscribe_returns():
    """后台线程持续通知时取消订阅：返回之后一次回调都不应再发生。"""
    registry = _registry()
    stop, state = threading.Event(), {"alive": True, "late": 0, "calls": 0}

    def listener(_key):
        state["calls"] += 1
        if not state["alive"]:
            state["late"] += 1

    def spam():
        while not stop.is_set():
            registry.notify(1)

    for _ in range(50):
        state["alive"] = True
        unsubscribe = registry.subscribe(listener)
        workers = [threading.Thread(target=spam) for _ in range(3)]
        stop.clear()
        for worker in workers:
            worker.start()
        while state["calls"] == 0:
            pass
        unsubscribe()
        state["alive"] = False
        stop.set()
        for worker in workers:
            worker.join(5)
        state["calls"] = 0
    assert state["late"] == 0
