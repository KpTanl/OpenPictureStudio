from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from ...application.output_writer import OperationCancellation
from ...core.contracts.result import CoreResult


class _UpgradeSignals(QObject):
    finished = pyqtSignal(object, object)


class EffectUpgradeTask(QRunnable):
    def __init__(self, core, session_id, state_version):
        super().__init__()
        self.core = core
        self.session_id = session_id
        self.state_version = state_version
        self.cancellation = OperationCancellation()
        self.signals = _UpgradeSignals()

    def run(self):
        try:
            result = self.core.upgrade_legacy_effects(
                self.session_id, expected_version=self.state_version, cancellation=self.cancellation)
        except Exception as error:
            result = CoreResult.from_exception(error, message_key="error.tool.incompatible")
        self.signals.finished.emit(self, result)
