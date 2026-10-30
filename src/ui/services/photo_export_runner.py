from __future__ import annotations

import copy
import logging
from concurrent.futures import CancelledError, ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from ...application.output_writer import OperationCancellation
from ...application.photo_exports import ExportItemCancellation
from ...application.result_types import CoreResult, ErrorCode

logger = logging.getLogger(__name__)


class PhotoExportRunner(QObject):
    changed = Signal(str)
    idle = Signal()
    loaded = Signal()
    preview_ready = Signal(object)
    preview_cleared = Signal()
    _delivered = Signal(object, object)
    _auxiliary = Signal(object, object)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-photo-export")
        self._active = None
        self._auxiliary_count = 0
        self._closed = False
        self._preparing = None
        self._preview_cancel = None
        self._preview_memory = None
        self._checks = set()
        self._delivered.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._auxiliary.connect(self._deliver_auxiliary, Qt.ConnectionType.QueuedConnection)
        self._submit_auxiliary(service.store.load, self._load)

    @property
    def busy(self):
        return self._preparing is not None or self._active is not None or bool(self._auxiliary_count) or any(
            item.status == "pending" for batch in self.service.batches.values() for item in batch.items)

    def _load(self, result):
        self.service.loaded = True
        if result.success:
            self.service.presets = result.data["presets"]
            records = {record["id"]: record for record in result.data["records"]}
            records.update({item.record["id"]: item.record for batch in self.service.batches.values()
                            for item in batch.items if item.record is not None})
            self.service.records = sorted(records.values(), key=lambda record: record["completed_at"], reverse=True)[:500]
            self.service.store_error = None
        else:
            self.service.store_error = result.error.to_dict()
        self.loaded.emit()
        self.changed.emit("")

    def reload(self):
        self._submit_auxiliary(self.service.store.load, self._load)

    def _submit_auxiliary(self, operation, callback, *, message_key="export_queue.store_failed"):
        if self._closed:
            raise ValueError("export_queue.expired")
        self._auxiliary_count += 1
        def execute():
            try:
                result = CoreResult.ok(operation())
            except (CancelledError, InterruptedError):
                result = CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            except Exception as error:
                logger.exception("导出记录操作失败")
                result = CoreResult.from_exception(error, message_key=message_key)
            if not self._closed:
                self._auxiliary.emit(callback, result)
            elif result.success and isinstance(result.data, dict) and result.data.get("memory") is not None:
                result.data["memory"].close()
        self._pool.submit(execute)

    @Slot(object, object)
    def _deliver_auxiliary(self, callback, result):
        self._auxiliary_count -= 1
        if not self._closed:
            callback(result)
            self._next()

    def enqueue(self, scope, range_kind, *, origin="manual"):
        if self._closed:
            raise ValueError("export_queue.expired")
        if self._preparing is not None:
            raise ValueError("batch.busy")
        from .background_operation import run_background
        from ...i18n.translation_service import tr
        self.cancel_preview()
        capture = self.service.capture(scope, range_kind, origin=origin)
        cancellation = self._preparing = OperationCancellation()
        batch = None
        try:
            batch = run_background(self.parent(), lambda token: self.service.prepare(capture, token.is_cancelled),
                                   label=tr("export_queue.preparing"), cancellation=cancellation)
            if self._closed or cancellation.is_cancelled():
                for item in batch.items:
                    self.service.release_item(item)
                raise ValueError("error.cancelled")
            identity = self.service.publish(batch)
        except CancelledError:
            raise ValueError("error.cancelled") from None
        finally:
            self._preparing = None
        self.changed.emit(identity)
        QTimer.singleShot(0, self, self._next)
        return self.service.summary(identity)

    def control(self, identity, action):
        self.service.control(identity, action)
        self.changed.emit(identity)
        QTimer.singleShot(0, self, self._next)
        return self.service.summary(identity)

    def _next(self):
        if self._closed or self._active is not None or self._auxiliary_count:
            return
        work = self.service.next_item()
        if work is None:
            if not self.busy:
                self.idle.emit()
            return
        batch, index = work
        item = batch.items[index]
        self._active = (batch.id, index)
        self.changed.emit(batch.id)
        self._pool.submit(self._execute, self._active, item.snapshot, item.target, batch.preset,
                          ExportItemCancellation(batch.cancellation))

    def _execute(self, identity, snapshot, target, preset, cancellation):
        record, error, record_state = None, None, "pending"
        prepared = snapshot
        try:
            if not prepared.render_json:
                prepared = self.service.core.prepare_export_snapshot(snapshot, is_cancelled=cancellation.is_cancelled)
            result = self.service.core.export_snapshot(prepared, target, preset, cancellation=cancellation)
            if result.success:
                record = self.service.record_for(prepared, target, preset, result.data)
                try:
                    self.service.store.record(record)
                    record_state = "saved"
                except Exception as write_error:
                    logger.exception("导出成功，完成记录保存失败")
                    error = CoreResult.from_exception(write_error, message_key="export_queue.store_failed").error.to_dict()
                    record_state = "failed"
            else:
                error = result.error.to_dict()
        except (CancelledError, InterruptedError):
            error = CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled").error.to_dict()
        except Exception as failure:
            logger.exception("固定快照导出失败")
            error = CoreResult.from_exception(failure, message_key=str(failure) if isinstance(failure, ValueError)
                                               else "error.export.failed").error.to_dict()
        if not self._closed:
            self._delivered.emit(identity, (prepared, record, error, record_state))

    @Slot(object, object)
    def _deliver(self, identity, result):
        if self._closed or self._active != identity:
            return
        batch = self.service.complete(identity, result)
        self._active = None
        self.changed.emit(batch.id)
        QTimer.singleShot(0, self, self._next)

    def save_preset(self, name, preset, callback):
        def saved(result):
            if result.success:
                self.service.presets[name.strip()] = preset.to_dict()
            callback(result)
            self.changed.emit("")
        self._submit_auxiliary(lambda: self.service.store.save_preset(name, preset), saved)

    def verify(self, record, callback, *, repair=False, is_cancelled=lambda: False):
        record = copy.deepcopy(record)
        token = OperationCancellation()
        self._checks.add(token)
        cancelled = lambda: token.is_cancelled() or is_cancelled()
        def check():
            state = self.service.store.verify(record, is_cancelled=cancelled)
            try:
                contract = self.service.core.export_render_contract(record["source_path"])
                contract_state = "current" if contract == record["render_contract"] else "render_changed"
            except (OSError, ValueError):
                contract_state = "render_unverified"
            if state == "verified" and repair:
                if cancelled():
                    raise CancelledError()
                self.service.store.record(record)
            return {"verification": state, "contract": contract_state, "record_state": "saved" if repair and state == "verified" else None}
        def delivered(result):
            self._checks.discard(token)
            if result.success:
                self.service.update_record(record["id"], result.data["verification"],
                                           saved=bool(result.data["record_state"]))
                self.changed.emit("")
            callback(result)
        self._submit_auxiliary(check, delivered, message_key="export_queue.verify_failed")

    def preview(self, identity, index):
        batch = self.service.get(identity)
        item = batch.items[index]
        if item.snapshot is None or item.status == "exporting":
            raise ValueError("batch.busy")
        self.cancel_preview()
        cancellation = self._preview_cancel = OperationCancellation()
        snapshot, preset = item.snapshot, batch.preset
        def prepare():
            with self.service.core.borrow_export_snapshot(snapshot) as retained:
                prepared = retained if retained.render_json else self.service.core.prepare_export_snapshot(retained, is_cancelled=cancellation.is_cancelled)
                result = self.service.core.preview_export_snapshot(prepared, preset, cancellation=cancellation)
            if not result.success:
                return {"snapshot": prepared, "result": result, "memory": None}
            frame, size, memory = result.data
            return {"snapshot": prepared, "result": CoreResult.ok((frame, size)), "memory": memory}
        def delivered(result):
            if result.success:
                memory = result.data["memory"]
                if (self._preview_cancel is not cancellation or cancellation.is_cancelled()
                        or self.service.batches.get(identity) is not batch):
                    if memory is not None:
                        memory.close()
                    return
                self.service.update_snapshot(batch, item, result.data["snapshot"])
                self._preview_memory = memory
                self.preview_ready.emit(result.data["result"])
            elif self._preview_cancel is cancellation and self.service.batches.get(identity) is batch:
                self.preview_ready.emit(result)
        self._submit_auxiliary(prepare, delivered, message_key="error.preview.failed")

    def cancel_preview(self):
        if self._preview_cancel is not None:
            self._preview_cancel.cancel()
        self._preview_cancel = None
        self.preview_cleared.emit()
        if self._preview_memory is not None:
            self._preview_memory.close()
            self._preview_memory = None

    def cancel_all(self):
        self.cancel_preview()
        if self._preparing is not None:
            self._preparing.cancel()
        for token in self._checks:
            token.cancel()
        for identity in tuple(self.service.batches):
            if identity not in self.service.batches:
                continue
            self.service.control(identity, "cancel")
        self.changed.emit("")
        QTimer.singleShot(0, self, self._next)

    def shutdown(self):
        if self._closed:
            return
        self.cancel_all()
        self._closed = True
        self.service.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
