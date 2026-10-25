from __future__ import annotations

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from ...application.output_writer import OperationCancellation
from ...application.result_types import CoreResult


class _ExportSignals(QObject):
    finished = pyqtSignal(int, object)


class ExportTask(QRunnable):
    def __init__(self, task_id, core, session_id, state_version, request, *, apply=False):
        super().__init__()
        self.task_id = task_id
        self.core = core
        self.session_id = session_id
        self.state_version = state_version
        self.request = request
        self.apply = apply
        self.cancellation = OperationCancellation()
        self.signals = _ExportSignals()

    def run(self):
        try:
            result = self.core.export(
                self.session_id, self.request, expected_state=self.state_version,
                cancellation=self.cancellation, allow_source_overwrite=self.apply,
            )
        except Exception as exc:
            result = CoreResult.from_exception(exc, message_key="error.export.failed")
        self.signals.finished.emit(self.task_id, result)
