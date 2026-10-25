import logging

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QObject, Qt, pyqtSignal

logger = logging.getLogger(__name__)


class UiThreadRelay(QObject):
    """把后台线程的回调转到界面线程执行。

    随 QApplication 存续：后台线程只接触这一个对象，不直接向会被销毁的界面对象发信号。
    """

    _posted = pyqtSignal(object)
    _NAME = "ops.uiThreadRelay"

    def __init__(self, application):
        super().__init__(application)
        self.setObjectName(self._NAME)
        self._posted.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @classmethod
    def shared(cls) -> "UiThreadRelay":
        """取得随应用存续的实例；须在界面线程调用。"""
        application = QCoreApplication.instance()
        if application is None:
            raise RuntimeError("UiThreadRelay requires a QApplication")
        relay = application.findChild(cls, cls._NAME, Qt.FindChildOption.FindDirectChildrenOnly)
        return relay if relay is not None else cls(application)

    def post(self, callback) -> None:
        """安排 callback 在界面线程执行；可在任意线程调用。"""
        if not sip.isdeleted(self):
            self._posted.emit(callback)

    def _run(self, callback) -> None:
        try:
            callback()
        except Exception:
            logger.exception("界面线程回调失败")
