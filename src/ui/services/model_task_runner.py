import weakref

from PyQt6 import sip
from PyQt6.QtCore import QObject, pyqtSignal

from .ui_thread_relay import UiThreadRelay


class _Delivery:
    """交给后台服务的订阅者：只把通知转投到界面线程，不在后台线程接触界面对象。"""

    def __init__(self, runner, relay):
        self._runner = weakref.ref(runner)
        self._relay = relay
        self._closed = False
        self.unsubscribe = None

    def __call__(self, _key=None) -> None:
        """服务状态变化时在任意线程调用。"""
        if not self._closed:
            self._relay.post(self._deliver)

    def _deliver(self) -> None:
        """在界面线程执行：接收方仍然存在才发出信号。"""
        runner = self._runner()
        if self._closed or runner is None or sip.isdeleted(runner):
            self.close()
            return
        runner.changed.emit()

    def close(self) -> None:
        self._closed = True
        unsubscribe, self.unsubscribe = self.unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()


class ModelTaskRunner(QObject):
    """把模型、运行环境等后台服务的状态变化转成界面线程上的 changed 信号。"""

    changed = pyqtSignal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self._delivery = _Delivery(self, UiThreadRelay.shared())
        self._delivery.unsubscribe = service.subscribe(self._delivery)
        self.destroyed.connect(self._delivery.close)

    def close(self):
        self._delivery.close()
