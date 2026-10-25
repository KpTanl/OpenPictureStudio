from __future__ import annotations

import logging
from collections import deque
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal

from ...application.output_writer import OperationCancellation
from ...application.result_types import CoreResult, ErrorCode

logger = logging.getLogger(__name__)


class BatchItemCancellation:
    def __init__(self, is_cancelled):
        self._operation = OperationCancellation()
        self._is_cancelled = is_cancelled

    def is_cancelled(self):
        return self._is_cancelled() or self._operation.is_cancelled()

    def mark_published(self):
        self._operation.mark_published()

    @contextmanager
    def publication(self):
        with self._operation.publication() as active:
            yield active and not self._is_cancelled()


class PhotoBatchRunner(QObject):
    changed = pyqtSignal(str)
    item_changed = pyqtSignal(str)
    finished = pyqtSignal(str)
    idle = pyqtSignal()
    display_ready = pyqtSignal(object, object)
    _prepared = pyqtSignal(object, object)
    _display_prepared = pyqtSignal(object, object)
    _photo_prepared = pyqtSignal(object, object)
    _rendered = pyqtSignal(object, object)

    def __init__(self, service, drafts, parent=None):
        super().__init__(parent)
        self.service, self.drafts = service, drafts
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-photo-batch")
        self._jobs = {}
        self._queue = deque()
        self._active = None
        self._closed = False
        self._display_generation = 0
        self._display_memory = None
        self._prepared.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._display_prepared.connect(self._deliver_display, Qt.ConnectionType.QueuedConnection)
        self._photo_prepared.connect(self._deliver_photo, Qt.ConnectionType.QueuedConnection)
        self._rendered.connect(self._deliver_rendered, Qt.ConnectionType.QueuedConnection)

    @property
    def busy(self):
        return bool(self._jobs)

    def start(self, batch_id, *, action="apply", callback=None, is_cancelled=lambda: False):
        if self._closed:
            raise ValueError("batch.expired")
        if batch_id in self._jobs:
            if self._jobs[batch_id]["action"] != action:
                raise ValueError("batch.busy")
            if callback is not None:
                self._jobs[batch_id]["callbacks"].append(callback)
            return
        batch = self.service.get(batch_id)
        if batch.expired:
            raise ValueError("batch.expired")
        job = {"batch": batch_id, "action": action, "cancelled": Event(), "index": 0,
               "callbacks": [callback] if callback is not None else []}
        job["is_cancelled"] = lambda: job["cancelled"].is_set() or is_cancelled()
        batch.active, batch.save_state, batch.save_error = True, "pending", None
        batch.started = True
        self._jobs[batch_id] = job
        self._queue.append(job)
        self.changed.emit(batch_id)
        self._next_job()

    def _next_job(self):
        if self._active is None and self._queue and not self._closed:
            self._active = self._queue.popleft()
            QTimer.singleShot(0, self._next_item)

    def _next_item(self):
        job = self._active
        if job is None or self._closed:
            return
        batch = self.service.get(job["batch"])
        if job["is_cancelled"]():
            job["cancelled"].set()
        if not self.drafts.loaded and not job["cancelled"].is_set():
            if self.drafts.load_error is not None:
                for item in batch.items:
                    if not item.after_revision:
                        item.status, item.error = "failed", self.drafts.load_error.to_dict()
                self._finish(job, CoreResult(success=False, error=self.drafts.load_error))
                return
            QTimer.singleShot(100, self._next_item)
            return
        if job["cancelled"].is_set() or job["index"] >= len(batch.items):
            if job["action"] == "apply":
                for item in batch.items[job["index"]:]:
                    if not item.after_revision and item.status not in ("success", "unchanged", "skipped"):
                        item.status = "cancelled"
            self._save(job)
            return
        index = job["index"]
        item = batch.items[index]
        if item.status == "skipped":
            job["index"] += 1
            QTimer.singleShot(0, self._next_item)
            return
        if job["action"] != "apply":
            try:
                self.service.restore_item(batch.id, index, job["action"] == "redo", BatchItemCancellation(job["is_cancelled"]))
                self.item_changed.emit(item.path)
            except Exception as error:
                logger.exception("批次历史恢复失败")
                item.error = CoreResult.from_exception(error, message_key="batch.failed").error.to_dict()
            job["index"] += 1
            self.changed.emit(batch.id)
            QTimer.singleShot(0, self._next_item)
        elif item.after_revision or item.status in ("success", "unchanged"):
            self.service.retry_draft(batch.id, index)
            job["index"] += 1
            QTimer.singleShot(0, self._next_item)
        else:
            item.status, item.error = "preparing", None
            path = item.path
            self.changed.emit(batch.id)
            self._pool.submit(self._prepare, job, path)

    def _prepare(self, job, path):
        try:
            with self.service.core.image_sources.compute.borrow(job["is_cancelled"], priority=2, threads=1):
                result = self.service.core.bind_source(path, is_cancelled=job["is_cancelled"])
        except CancelledError:
            result = CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except Exception as error:
            logger.exception("批次来源准备失败")
            result = CoreResult.from_exception(error, message_key="batch.failed")
        if not self._closed:
            self._prepared.emit(job, result)
        elif result.success:
            self.service.core.release_source(result.data.source_id)

    def _deliver(self, job, result):
        if self._closed or self._active is not job:
            if result.success:
                self.service.core.release_source(result.data.source_id)
            return
        batch = self.service.get(job["batch"])
        item = batch.items[job["index"]]
        try:
            if job["is_cancelled"]():
                item.status = "cancelled"
            elif not result.success:
                item.status = "cancelled" if result.error.code == "CANCELLED" else "failed"
                item.error = result.error.to_dict()
            else:
                cancellation = BatchItemCancellation(job["is_cancelled"])
                if batch.operation == "calibration" and self.service.prepare_item(batch.id, job["index"], result.data, cancellation):
                    self._pool.submit(self._render_calibration, job, job["index"])
                    return
                if batch.operation != "calibration":
                    self.service.apply_item(batch.id, job["index"], result.data, cancellation)
                if item.after_revision:
                    self.item_changed.emit(item.path)
        except Exception as error:
            logger.exception("批次参数提交失败")
            if not item.after_revision:
                item.status = "failed"
            else:
                batch.save_state = "failed"
            item.error = CoreResult.from_exception(error, message_key="batch.failed").error.to_dict()
        finally:
            if result.success:
                self.service.core.release_source(result.data.source_id)
        job["index"] += 1
        self.changed.emit(batch.id)
        QTimer.singleShot(0, self._next_item)

    def _render_calibration(self, job, index):
        batch, agent = self.service.get(job["batch"]), self.service.agent
        item = batch.items[index]
        candidate = None
        cancelled = lambda: job["is_cancelled"]() or not self.service.calibration_guard(batch.id)
        try:
            if job["is_cancelled"]():
                raise CancelledError()
            if (not self.service.calibration_guard(batch.id) or self.service.core.tools.generation != batch.tool_generation
                    or self.service.core.export_render_contract(item.path) != item.render_contract):
                raise ValueError("calibration.expired")
            self.service.calibration_preflight(batch.id, cancelled)
            result = agent.propose(item.session_id, item.candidate, model_version="photo-calibration-v1",
                                   expected_state=item.before_version,
                                   audit_context={"batch": batch.id, "source_hash": item.source.file_hash})
            if result.success:
                candidate = result.data["candidate_id"]
                result = agent.preview(candidate, is_cancelled=cancelled)
                if result.success:
                    result.data.close()
                    self.service.calibration_preflight(batch.id, cancelled)
                    if self.service.core.export_render_contract(item.path) != item.render_contract:
                        raise ValueError("calibration.expired")
                    result = agent.commit(candidate, confirmed=True, is_cancelled=cancelled,
                        label=("ai.batch." if batch.origin == "agent" else "batch.") + "calibration")
        except (CancelledError, InterruptedError):
            result = (CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled") if job["is_cancelled"]()
                      else CoreResult.fail("STALE_CANDIDATE", "calibration.expired"))
        except Exception as error:
            logger.exception("照片校准提交失败")
            key = str(error) if isinstance(error, ValueError) else "batch.failed"
            code = {"calibration.expired": "STALE_CANDIDATE", "error.source.changed": ErrorCode.SOURCE_CHANGED
                    }.get(key, ErrorCode.INTERNAL_ERROR)
            result = CoreResult.from_exception(error, code=code, message_key=key)
        finally:
            if candidate is not None:
                agent.discard(candidate)
        if (not result.success and result.error.code == "CANCELLED" and not job["is_cancelled"]()
                and not self.service.calibration_guard(batch.id)):
            result = CoreResult.fail("STALE_CANDIDATE", "calibration.expired")
        if not self._closed:
            self._rendered.emit(job, result)

    def _deliver_rendered(self, job, result):
        if self._closed or self._active is not job:
            return
        batch = self.service.get(job["batch"])
        index = job["index"]
        item = batch.items[index]
        try:
            revision = (result.data.revision.revision_id if result.success
                        else result.error.details.get("revision_id") if result.error.details.get("operation_applied") else None)
            if revision:
                self.service.complete_item(batch.id, index, revision)
                if not result.success:
                    item.error = result.error.to_dict()
                self.item_changed.emit(item.path)
            else:
                self.service.fail_result(item, result)
        except Exception as error:
            logger.exception("照片校准回执失败")
            item.error = CoreResult.from_exception(error, message_key="batch.failed").error.to_dict()
            if item.after_revision:
                batch.save_state = "failed"
            else:
                item.status = "failed"
        job["index"] += 1
        self.changed.emit(batch.id)
        QTimer.singleShot(0, self._next_item)

    def _save(self, job):
        batch = self.service.get(job["batch"])
        versions = [item.draft_version for item in batch.items if item.draft_version is not None]
        if batch.save_state == "failed":
            self._finish(job, CoreResult.fail(ErrorCode.ARTIFACT_WRITE_FAILED, "studio.draft_save_failed"))
        elif versions:
            batch.save_state = "saving"
            self.changed.emit(batch.id)
            self.drafts.wait_for_version(max(versions), lambda result: self._finish(job, result))
        else:
            batch.save_state = "not_needed"
            self._finish(job, CoreResult.ok())

    def _finish(self, job, result):
        if self._jobs.get(job["batch"]) is not job:
            return
        batch = self.service.get(job["batch"])
        batch.active = False
        batch.save_state = ("not_needed" if batch.save_state == "not_needed" else "saved") if result.success else "failed"
        batch.save_error = None if result.success else result.error.to_dict()
        self._jobs.pop(batch.id, None)
        self._active = None
        self.changed.emit(batch.id)
        self.finished.emit(batch.id)
        for callback in job["callbacks"]:
            try:
                callback(self.service.summary(batch.id))
            except Exception:
                logger.exception("批次结果交付失败")
        if not self._jobs:
            self.idle.emit()
        self._next_job()

    def cancel(self, batch_id=None):
        for identity, job in self._jobs.items():
            if batch_id is None or identity == batch_id:
                job["cancelled"].set()

    def cancel_display(self):
        self._display_generation += 1
        memory, self._display_memory = self._display_memory, None
        if memory is not None:
            memory.__exit__(None, None, None)

    def prepare_photo(self, reference, callback, is_cancelled):
        session = self.service.photo_session(reference)
        request = (reference, session.core_id, session.state_version, callback, is_cancelled)
        self._pool.submit(self._prepare_photo, request)

    def _prepare_photo(self, request):
        reference, session_id, version, callback, is_cancelled = request
        try:
            result = self.service.core.ensure_source_ready(session_id, expected_state=version,
                is_cancelled=lambda: self._closed or is_cancelled())
        except Exception as error:
            logger.exception("批次照片准备失败")
            result = CoreResult.from_exception(error, message_key="batch.failed")
        if not self._closed:
            self._photo_prepared.emit(request, result)

    def _deliver_photo(self, request, result):
        reference, session_id, version, callback, is_cancelled = request
        state = self.service.core.get_session(session_id)
        if is_cancelled() or not state.success or state.data.state_version != version:
            result = CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        callback(result)

    def refresh_display(self, path, session_id):
        self.cancel_display()
        generation = self._display_generation
        self._pool.submit(self._prepare_display, generation, path, session_id)

    def _prepare_display(self, generation, path, session_id):
        core = self.service.core
        cancelled = lambda: self._closed or generation != self._display_generation
        memory, result = None, None
        try:
            ready = core.ensure_source_ready(session_id, is_cancelled=cancelled)
            if not ready.success:
                if not cancelled():
                    self._display_prepared.emit((generation, path, session_id, None), (ready, None))
            else:
                state = ready.data
                size = core.source_size(session_id)
                memory = core.image_sources.resources.working_memory(size[0] * size[1] * 8, cancelled)
                memory.__enter__()
                result = core.preview(session_id, state.draft_workflow, update_draft=False,
                    expected_state=state.state_version, display=True, is_cancelled=cancelled)
                if not cancelled():
                    self._display_prepared.emit((generation, path, session_id, state.state_version), (result, memory))
                    memory = None
                    result = None
        except Exception as error:
            logger.exception("批次当前照片刷新失败")
            if not cancelled():
                self._display_prepared.emit((generation, path, session_id, None),
                    (CoreResult.from_exception(error, message_key="batch.failed"), None))
        finally:
            if result is not None and result.success:
                result.data.close()
            if memory is not None:
                memory.__exit__(None, None, None)

    def _deliver_display(self, request, packet):
        result, memory = packet
        generation, path, session_id, version = request
        state = self.service.core.get_session(session_id)
        if (self._closed or generation != self._display_generation or not state.success
                or version is not None and state.data.state_version != version):
            if result.success:
                result.data.close()
            if memory is not None:
                memory.__exit__(None, None, None)
            return
        self._display_memory = memory
        if not result.success and memory is not None:
            memory.__exit__(None, None, None)
            self._display_memory = None
        self.display_ready.emit(request, result)

    def shutdown(self):
        self.cancel()
        self.cancel_display()
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
