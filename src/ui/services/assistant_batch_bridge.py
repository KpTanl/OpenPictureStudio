from __future__ import annotations

import asyncio
import copy
import logging
import os

from ...application.photo_browser import BROWSER_TOOLS
from ...application.photo_calibration import CALIBRATION_TOOLS
from ...application.photo_library import LIBRARY_TOOLS

from PySide6.QtCore import QObject, Qt, Signal, Slot

logger = logging.getLogger(__name__)


class AssistantBatchBridge(QObject):
    requested = Signal(object)

    def __init__(self, runner, capture, parent=None, *, exports=None):
        super().__init__(parent)
        self.runner, self.capture = runner, capture
        self.exports = exports
        self._browser = None
        self.calibrations = None
        self.library = None
        self.requested.connect(self._execute, Qt.ConnectionType.QueuedConnection)

    def set_browser(self, callback):
        self._browser = callback

    def context_for_send(self, scope_kind):
        context = self.capture()
        return {**context, "range_kind": "filtered" if scope_kind == "filtered" else "selected",
                "explicit": scope_kind != "current"}

    async def perform(self, operation, values, context, *, is_cancelled, progress):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request = {"operation": operation, "values": copy.deepcopy(values), "context": copy.deepcopy(context),
                   "loop": loop, "future": future, "cancelled": is_cancelled, "progress": progress}
        self.requested.emit(request)
        return await asyncio.shield(future)

    @staticmethod
    def _return(request, result=None, error=None):
        def deliver():
            if not request["future"].done():
                if error is not None:
                    request["future"].set_exception(error)
                else:
                    request["future"].set_result(result)
        if not request["loop"].is_closed():
            request["loop"].call_soon_threadsafe(deliver)

    @Slot(object)
    def _execute(self, request):
        try:
            if request["cancelled"]():
                raise ValueError("AI_CANCELLED")
            service = self.runner.service
            operation, values, context = request["operation"], request["values"], request["context"]
            if operation in LIBRARY_TOOLS:
                if self.library is None:
                    raise ValueError("AI_TOOL_UNAVAILABLE")
                def completed(value, error):
                    self._return(request, value, ValueError(error) if error else None)
                self.library.command(operation, values, completed, request["cancelled"], origin="agent")
                return
            if operation in CALIBRATION_TOOLS:
                self._execute_calibration(request)
                return
            if operation in BROWSER_TOOLS:
                if self._browser is None:
                    raise ValueError("AI_TOOL_UNAVAILABLE")
                if operation in ("set_photo_marks", "rate_photos") and self.library is not None:
                    browser = self.library.service.browser
                    paths = browser.resolve(values["scope"], values.get("photos", [browser.reference(p) for p in browser.selected]))
                    field = "mark" if operation == "set_photo_marks" else "rating"
                    def metadata_saved(_value, error):
                        self._return(request, self._browser("get_browser_context", {}), ValueError(error) if error else None)
                    self.library.browser_metadata(paths, {field: values[field]}, metadata_saved, request["cancelled"])
                    return
                self._return(request, self._browser(operation, values))
                return
            if operation in ("get_export_context", "enqueue_exports", "get_export_status", "control_exports", "get_export_history", "verify_export", "repair_export_record"):
                self._execute_export(request)
                return
            if operation == "get_batch_context":
                if not context:
                    raise ValueError("batch.scope_expired")
                result = service.context(context["scope"])
                result["sample_parameters"] = service.sample_values(context["scope"], offset=values.get("offset", 0),
                                                                       limit=values.get("limit", 20))
                result["recent_batches"] = [service.summary(identity, limit=0) for identity in service.batches()[-16:]]
                self._return(request, result)
                return
            if operation == "get_batch_status":
                self._return(request, service.summary(values["batch"], offset=values.get("offset", 0),
                                                       limit=values.get("limit", 20)))
                return
            if operation == "batch_photo_context":
                reference = values["photo"]
                def prepared(result):
                    if result.success:
                        try:
                            self._return(request, {"photo_context": self._photo_context(reference)})
                        except ValueError as error:
                            self._return(request, error=error)
                    else:
                        self._return(request, error=ValueError(result.error.code))
                self.runner.prepare_photo(reference, prepared, request["cancelled"])
                return
            if operation in ("sync_batch_adjustments", "adjust_batch_exposure"):
                if not context or values["scope"] != context["scope"]:
                    raise ValueError("batch.scope_expired")
                range_kind = values.get("range", context["range_kind"])
                if context.get("explicit") and range_kind != context["range_kind"]:
                    raise ValueError("batch.scope_expired")
                batch_id = service.create(values["scope"],
                    "sync" if operation == "sync_batch_adjustments" else "exposure",
                    fields=values.get("fields", ()), delta_ev=values.get("delta_ev", 0),
                    range_kind=range_kind, sample_snapshot=values.get("sample_snapshot"), origin="agent")
                service.preflight(batch_id)
                action = "apply"
            else:
                batch_id = values["batch"]
                service.get(batch_id)
                action = {"retry_batch": "apply", "undo_batch": "undo", "redo_batch": "redo"}[operation]
            loop = request["loop"]

            def update(identity):
                if identity == batch_id and not loop.is_closed():
                    result = service.summary(batch_id, limit=20)
                    loop.call_soon_threadsafe(request["progress"], result)

            def finished(result):
                self.runner.changed.disconnect(update)
                sample = context.get("sample_snapshot")
                if sample:
                    try:
                        result["_photo"] = self._photo_context(sample)
                    except ValueError:
                        result["sample_unavailable"] = True
                self._return(request, result)

            self.runner.changed.connect(update)
            try:
                self.runner.start(batch_id, action=action, callback=finished, is_cancelled=request["cancelled"])
            except ValueError:
                self.runner.changed.disconnect(update)
                raise
            update(batch_id)
        except (ValueError, KeyError) as error:
            self._return(request, error=ValueError(str(error)))
        except Exception:
            logger.exception("批量命令交付失败")
            self._return(request, error=ValueError("batch.failed"))

    def _execute_export(self, request):
        if self.exports is None:
            raise ValueError("AI_TOOL_UNAVAILABLE")
        service = self.exports.service
        operation, values, context = request["operation"], request["values"], request["context"]
        if operation == "get_export_context":
            result = service.context()
        elif operation == "get_export_status":
            result = service.summary(values["export"], offset=values.get("offset", 0), limit=values.get("limit", 20))
        elif operation == "control_exports":
            result = self.exports.control(values["export"], values["action"])
        elif operation == "get_export_history":
            offset, limit = values.get("offset", 0), values.get("limit", 20)
            rows = [{**record, "verification": "unchecked", "record_state": service.record_state(record)}
                    for record in service.records[offset:offset + limit]]
            result = {"records": rows, "offset": offset, "has_more": offset + limit < len(service.records),
                      "loaded": service.loaded, "store_error": service.store_error}
        elif operation in ("verify_export", "repair_export_record"):
            runtime = [item.record for batch in service.batches.values() for item in batch.items if item.record is not None]
            record = next((record for record in [*runtime, *service.records] if record["id"] == values["record"]), None)
            if record is None:
                raise ValueError("export_queue.expired")
            item = service.record_item(record["id"])
            repair = operation == "repair_export_record"
            if repair and (item is None or item.record_state != "failed"):
                raise ValueError("export_queue.expired")
            def verified(result):
                if result.success:
                    self._return(request, {"record": record["id"], **result.data})
                else:
                    self._return(request, error=ValueError(result.error.message_key))
            self.exports.verify(record, verified, repair=repair, is_cancelled=request["cancelled"])
            return
        else:
            if not context or values["scope"] != context["scope"]:
                raise ValueError("batch.scope_expired")
            kind = values.get("range", context["range_kind"] if context.get("explicit") else "current")
            if context.get("explicit") and kind != context["range_kind"]:
                raise ValueError("batch.scope_expired")
            result = self.exports.enqueue(values["scope"], kind, origin="agent")
        if result.get("export") and not request["loop"].is_closed():
            request["loop"].call_soon_threadsafe(request["progress"], copy.deepcopy(result))
        self._return(request, result)

    def _execute_calibration(self, request):
        if self.calibrations is None:
            raise ValueError("AI_TOOL_UNAVAILABLE")
        runner, values, operation = self.calibrations, request["values"], request["operation"]
        service, browser = runner.service, runner.service.browser
        if operation == "get_photo_groups":
            self._return(request, service.context())
        elif operation == "get_photo_calibration":
            self._return(request, service.summary(values["calibration"], offset=values.get("offset", 0), limit=values.get("limit", 50)))
        elif operation in ("create_photo_group", "update_photo_group"):
            runner.check_capture()
            paths = browser.resolve(values["scope"], values["photos"])
            sample = browser.resolve(values["scope"], [values["sample"]])[0]
            self._return(request, service.set_group(values["name"], paths, sample,
                identity=values.get("group") if operation == "update_photo_group" else None))
            runner.changed.emit()
        elif operation in ("suggest_photo_groups", "propose_photo_calibration"):
            def completed(result):
                if result.success:
                    self._return(request, result.data)
                else:
                    self._return(request, error=ValueError(result.error.message_key))
            if operation == "suggest_photo_groups":
                references = values.get("photos", [browser.reference(path) for path in browser.selected])
                paths = browser.resolve(values["scope"], references)
                runner.suggest(paths, {path: browser.reference(path) for path in paths}, completed,
                    minutes=values.get("minutes", 30), confirm_local=values.get("confirm_local_time", False),
                    is_cancelled=request["cancelled"])
            else:
                runner.propose(values["group"], values["operations"], completed, is_cancelled=request["cancelled"])
        else:
            runner.check_capture()
            identity = service.apply(values["calibration"], origin="agent")
            loop = request["loop"]
            def update(batch):
                if batch == identity and not loop.is_closed():
                    loop.call_soon_threadsafe(request["progress"], self.runner.service.summary(identity, limit=20))
            def finished(result):
                self.runner.changed.disconnect(update)
                self._return(request, result)
            self.runner.changed.connect(update)
            try:
                self.runner.start(identity, callback=finished, is_cancelled=request["cancelled"])
            except Exception:
                self.runner.changed.disconnect(update)
                raise
            update(identity)

    def _photo_context(self, reference):
        from ...application.assistant_models import AssistantContext
        from ...core.tools.color import COLOR_UI_PARAMETERS
        state, params, project_path = self.runner.service.photo_snapshot(reference)
        size = self.runner.service.core.source_size(state.session_id)
        if size is None:
            raise ValueError("batch.source_not_ready")
        source = state.active_source
        return AssistantContext(state.session_id, state.state_version,
            {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version), **params["color"]}, state.draft_workflow,
            "project:" + state.document_id if project_path else "source:" + source.file_hash,
            os.path.basename(source.path), state.document_id, source.file_hash, state.current_revision.revision_id,
            *size, beauty=params["beauty"], tools={"beauty": "resources"})
