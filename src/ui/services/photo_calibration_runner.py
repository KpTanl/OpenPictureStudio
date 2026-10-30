from __future__ import annotations

import logging
from concurrent.futures import CancelledError, ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ...application.output_writer import OperationCancellation
from ...application.result_types import CoreResult

logger = logging.getLogger(__name__)


class PhotoCalibrationRunner(QObject):
    changed = Signal()
    idle = Signal()
    _completed = Signal(object, object)

    def __init__(self, service, parent=None, *, before_capture=lambda: True):
        super().__init__(parent)
        self.service = service
        self.before_capture = before_capture
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-calibration")
        self._active = None
        self._closed = False
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    @property
    def busy(self):
        return self._active is not None

    def propose(self, group, operations, callback=None, *, is_cancelled=lambda: False):
        if self.busy or self._closed:
            raise ValueError("batch.busy")
        self.check_capture()
        identity = self.service.capture_plan(group, operations)
        self._start("plan", identity, lambda cancelled: self.service.prepare_plan(identity, cancelled), callback, is_cancelled)
        return identity

    def check_capture(self):
        if not self.before_capture():
            raise ValueError("batch.parameters_pending")

    def suggest(self, paths, references, callback, *, minutes=30, confirm_local=False, is_cancelled=lambda: False):
        if self.busy or self._closed:
            raise ValueError("batch.busy")
        self._start("groups", "", lambda cancelled: self.service.suggest_groups(paths, references, minutes=minutes,
                    confirm_local=confirm_local, is_cancelled=cancelled), callback, is_cancelled)

    def _start(self, kind, identity, execute, callback, external):
        request = {"kind": kind, "id": identity, "cancel": OperationCancellation(), "callback": callback}
        request["cancelled"] = lambda: request["cancel"].is_cancelled() or external()
        self._active = request
        self.service.busy = True
        self.changed.emit()
        self._pool.submit(self._work, request, execute)

    def _work(self, request, execute):
        try:
            result = CoreResult.ok(execute(request["cancelled"]))
        except (CancelledError, InterruptedError):
            result = CoreResult.fail("CANCELLED", "error.cancelled")
        except Exception as error:
            logger.exception("照片组校准准备失败")
            result = CoreResult.from_exception(error, message_key=str(error) if isinstance(error, ValueError) else "batch.failed")
        self._completed.emit(request, result)

    @Slot(object, object)
    def _deliver(self, request, result):
        if self._active is not request:
            return
        self._active = None
        self.service.busy = False
        if request["kind"] == "plan":
            plan = self.service.plans[request["id"]]
            if result.success:
                plan["rows"], plan["sample"] = result.data["rows"], result.data["sample"]
                current = not self._closed and not request["cancelled"]() and self.service.plan_current(plan)
                if not current:
                    for row in plan["rows"]:
                        if row["preview"] is not None:
                            self.service.release_snapshot(row["preview"])
                            row["preview"] = None
                    result = (CoreResult.fail("CANCELLED", "error.cancelled") if request["cancelled"]()
                              else CoreResult.fail("STALE_CANDIDATE", "calibration.expired"))
            if result.success:
                plan["state"] = "ready" if any(row["status"] == "ready" for row in plan["rows"]) else "skipped"
            else:
                plan["state"] = "cancelled" if request["cancelled"]() else "failed"
            plan["error"] = "" if result.success else result.error.message_key
            if plan["state"] == "skipped":
                plan["error"] = "calibration.no_suggestions"
            result = CoreResult.ok(self.service.summary(plan["id"])) if result.success else result
        elif request["cancelled"]():
            result = CoreResult.fail("CANCELLED", "error.cancelled")
        if self._closed:
            self.service.shutdown()
            return
        self.changed.emit()
        if request["callback"] is not None:
            try:
                request["callback"](result)
            except Exception:
                logger.exception("照片组校准回执失败")
        self.idle.emit()

    def cancel(self):
        if self._active is not None:
            self._active["cancel"].cancel()

    def shutdown(self):
        self.cancel()
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        if not self.busy:
            self.service.shutdown()
