from __future__ import annotations

import logging
from threading import Condition, get_ident

logger = logging.getLogger(__name__)


class ListenerRegistry:
    """线程安全的订阅者登记。

    ``notify`` 在调用方线程逐个回调，可来自任意线程。``unsubscribe`` 返回时，
    该订阅者既不在其他线程执行，之后也不会再被调用，订阅方随后可以释放自身资源。
    订阅者在自己的回调中取消订阅不等待。
    """

    def __init__(self, failure_message: str):
        self._failure_message = failure_message
        self._condition = Condition()
        self._listeners: dict = {}
        self._running: dict = {}

    def subscribe(self, listener):
        with self._condition:
            self._listeners[listener] = None
        return lambda: self.unsubscribe(listener)

    def unsubscribe(self, listener) -> None:
        current = get_ident()
        with self._condition:
            self._listeners.pop(listener, None)
            while any(thread != current for thread in self._running.get(listener, ())):
                self._condition.wait()

    def clear(self) -> None:
        with self._condition:
            self._listeners.clear()

    def notify(self, *args) -> None:
        current = get_ident()
        with self._condition:
            pending = tuple(self._listeners)
        for listener in pending:
            with self._condition:
                if listener not in self._listeners:
                    continue
                self._running.setdefault(listener, []).append(current)
            try:
                listener(*args)
            except Exception:
                logger.exception(self._failure_message)
            finally:
                with self._condition:
                    threads = self._running[listener]
                    threads.remove(current)
                    if not threads:
                        del self._running[listener]
                    self._condition.notify_all()
