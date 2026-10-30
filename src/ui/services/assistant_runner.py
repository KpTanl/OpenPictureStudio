from __future__ import annotations

import asyncio
import copy
import logging
import sqlite3
from concurrent.futures import CancelledError
from pathlib import Path
from io import BytesIO
from threading import Event, Lock, Thread

from PySide6.QtCore import QObject, QRunnable, Qt, Signal, Slot

from ...application.assistant_conversations import AssistantConversationService
from ...application.assistant_models import IMAGE_SOURCES, context_matches
from ...application.assistant_actions import AssistantActionService
from ...application.photo_analysis import (ANALYSIS_EDGE, ANALYSIS_WORKING_BYTES, analyzed_photo, encode_evidence,
                                           encode_jpeg, reference_thumbnail)
from ...application.assistant_context import safe_parameters
from ...application.assistant_observation import PhotoObservationService, frame_descriptor, geometry_identity
from ...application.assistant_images import ImageMemory, ImageObservationError, jpeg_information
from ...core.contracts.workflow import WorkflowGraph
from ...core.contracts.image_display import NonRawDisplayMode, RawDisplayMode
from ...core.constants import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)


class _MaterialJob(QRunnable):
    """在后台生成这一轮发给 AI 的照片素材：照片预览、照片分析与人脸有无。"""

    def __init__(self, core, context, request, face_detector, loop, future, cancelled):
        super().__init__()
        self.core, self.context, self.request = core, context, request
        self.face_detector = face_detector
        self.loop, self.future, self.cancelled = loop, future, cancelled
        self.render_info = {}

    def run(self):
        data, error = None, None
        owned = []
        try:
            state = self.core.get_session(self.context.session_id)
            if not state.success or not context_matches(self.context, state.data) or self.cancelled.is_set():
                raise ValueError("AI_PHOTO_CHANGED")
            source = self.request.get("image_source")
            data = {"images": [], "analysis": None, "faces": None, "regions": ()}
            current = None
            photo = None
            if source:
                edge, quality = IMAGE_SOURCES[source]
                photo = self._render(WorkflowGraph() if source == "original" else self.context.workflow,
                                     max(edge, ANALYSIS_EDGE) if source == "current" else edge)
                owned.append(photo)
                current = photo if source == "current" else None
            if self.request.get("analysis") or self.request.get("faces") or self.request.get("baseline"):
                if current is None:
                    current = self._render(self.context.workflow, ANALYSIS_EDGE)
                    owned.append(current)
                if self.request.get("faces") and self.face_detector is not None:
                    regions = self._faces(current)
                    if isinstance(regions, (list, tuple)):
                        data["regions"], data["faces"] = tuple(regions), bool(regions)
                    else:
                        data["faces"] = regions
                    if self.cancelled.is_set():
                        raise ValueError("AI_CANCELLED")
                if self.request.get("analysis"):
                    sources = self.core.image_sources
                    with sources.resources.working_memory(ANALYSIS_WORKING_BYTES, self.cancelled.is_set), \
                            sources.compute.borrow(self.cancelled.is_set, threads=1):
                        data["analysis"] = analyzed_photo(current, data["regions"],
                                                          identity=safe_parameters(self.context.identity()))
            if photo is not None:
                data["images"] = (encode_evidence(photo, data["regions"]) if source == "current"
                                  else [encode_jpeg(photo, edge, quality)])
            if self.request.get("baseline"):
                data["baseline"] = (data["images"] if source == "current"
                                    else encode_evidence(current, data["regions"]))
            frames = {}
            for name, workflow in (("current", self.context.workflow), ("original", WorkflowGraph())):
                info = self.render_info.get(workflow.workflow_hash())
                if info:
                    frames[name] = frame_descriptor(self.context, workflow, name, info["full_size"], info["geometry"],
                        run_id=self.request.get("run_id", ""), goal_version=self.request.get("goal_version", 0),
                        regions=data["regions"] if name == "current" else ())
            data["frames"] = list(frames.values())
            if source:
                data["image_info"] = [{**jpeg_information(image), "source": source,
                    "frame_id": frames[source]["frame_id"], "quality": "preview",
                    "region": "whole" if index == 0 else "face"} for index, image in enumerate(data["images"])]
            state = self.core.get_session(self.context.session_id)
            if self.cancelled.is_set() or not state.success or not context_matches(self.context, state.data):
                raise ValueError("AI_PHOTO_CHANGED")
        except Exception as exc:
            if not isinstance(exc, ValueError):
                logger.exception("生成助手照片素材失败")
            data, error = None, exc if isinstance(exc, ValueError) else ValueError("AI_IMAGE_UNAVAILABLE")
        finally:
            for image in owned:
                image.close()
        if not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._finish, data, error)
            except RuntimeError:
                pass

    def _faces(self, image):
        """人脸有无只决定要不要开放美颜；检测本身出错时按“不确定”处理，不影响照片与分析的发送。"""
        try:
            from ...application.beauty_analysis import query_face_regions
            return query_face_regions(self.face_detector, image, dict(self.context.beauty), self.cancelled.is_set,
                                      photo=self.context)
        except Exception:
            logger.exception("助手的人脸检测失败，本轮按未知处理")
            return None

    def _render(self, workflow, edge):
        result = self.core.preview(self.context.session_id, workflow, update_draft=False,
                                   max_dimension=edge, is_cancelled=self.cancelled.is_set)
        if not result.success:
            raise ValueError(result.error.code)
        self.render_info[workflow.workflow_hash()] = {
            "full_size": result.data.full_size or result.data.image.size,
            "geometry": geometry_identity(workflow, self.core.tools.descriptors())}
        return result.data.image

    def _finish(self, data, error):
        if not self.future.done():
            if error:
                self.future.set_exception(error)
            else:
                self.future.set_result(data)


class _ActionJob(QRunnable):
    def __init__(self, operation, loop, future, *, failure_code="AI_REQUEST_FAILED"):
        super().__init__()
        self.operation, self.loop, self.future = operation, loop, future
        self.failure_code = failure_code

    def run(self):
        result, error = None, None
        try:
            result = self.operation()
        except Exception as exc:
            if not isinstance(exc, ValueError):
                logger.exception("助手后台操作失败")
            error = exc if isinstance(exc, ValueError) else ValueError(self.failure_code)
        if not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._finish, result, error)
            except RuntimeError:
                pass

    def _finish(self, result, error):
        if not self.future.done():
            if error:
                self.future.set_exception(error)
            else:
                self.future.set_result(result)


class AssistantRunner(QObject):
    event_received = Signal(str, object)
    changed = Signal(object)

    def __init__(self, directory: str, parent=None):
        super().__init__(parent)
        self.state = {"ready": False, "current": "", "conversation": None, "messages": [],
                      "conversations": [], "connections": [], "models": [], "busy": [], "default_model": ""}
        self._directory, self._core, self._pool = directory, None, None
        self._face_detector = None
        self._actions = None
        self._batch_bridge = None
        self._audit = None
        self._pending, self._lock = [], Lock()
        self._loop, self._service = None, None
        self._started, self._closed = Event(), False
        self._shutdown_future = None
        self.last_error = None
        self.event_received.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self._thread = Thread(target=self._run, name="ops-assistant", daemon=True)
        self._thread.start()

    def set_image_services(self, core, thread_pool, *, audit=None, face_detector=None, agent=None) -> None:
        self._core, self._pool = core, thread_pool
        self._audit = audit
        self._face_detector = face_detector
        if agent is not None:
            self._actions = AssistantActionService(core, agent)

    def set_batch_services(self, runner, capture, exports=None):
        from .assistant_batch_bridge import AssistantBatchBridge
        self._batch_bridge = AssistantBatchBridge(runner, capture, self, exports=exports)
        runner.changed.connect(lambda identity: self._project_batch_state())
        if exports is not None:
            exports.changed.connect(lambda identity: self._on_exports_changed())

    def _on_exports_changed(self):
        exports = self._batch_bridge.exports
        if self._service is not None:
            summaries = [exports.service.summary(identity, limit=0) for identity in exports.service.batches]
            self.command("exports_changed", summaries=summaries)
        self._project_batch_state()

    def set_browser(self, callback):
        if self._batch_bridge is not None:
            self._batch_bridge.set_browser(callback)

    def set_calibration_services(self, runner):
        if self._batch_bridge is not None:
            self._batch_bridge.calibrations = runner

    def set_library_services(self, runner):
        if self._batch_bridge is not None:
            self._batch_bridge.library = runner

    async def _batch(self, operation, values, context, *, is_cancelled, progress):
        if self._batch_bridge is None:
            raise ValueError("AI_TOOL_UNAVAILABLE")
        return await self._batch_bridge.perform(operation, values, context,
                                                 is_cancelled=is_cancelled, progress=progress)

    def _project_batch_state(self):
        if self._batch_bridge is None:
            return
        service = self._batch_bridge.runner.service
        available = set(service.batches())
        record = self.state.get("conversation") or {}
        for message in [*self.state.get("messages", ()), *record.get("messages", {}).values()]:
            run = message.get("run") or {}
            for index, receipt in enumerate(run.get("batch_receipts", ())):
                identity = receipt["batch"]
                run["batch_receipts"][index] = ({**service.summary(identity, limit=20), "available": True}
                    if identity in available else {**receipt, "available": False})
            exports = self._batch_bridge.exports
            for index, receipt in enumerate(run.get("export_receipts", ())):
                identity = receipt["export"]
                run["export_receipts"][index] = ({**exports.service.summary(identity, limit=0), "available": True}
                    if exports is not None and identity in exports.service.batches else {**receipt, "available": False})
        self.changed.emit(self.state)

    def batch_history(self, identity, message_id, batch_id, redo):
        async def perform():
            try:
                result = await self._batch("redo_batch" if redo else "undo_batch", {"batch": batch_id}, {},
                    is_cancelled=lambda: self._closed,
                    progress=lambda summary: self._service.command("batch_receipt", {
                        "identity": identity, "message_id": message_id, "summary": summary}))
                self._service.command("batch_receipt", {"identity": identity, "message_id": message_id, "summary": result})
            except ValueError as error:
                self._emit("error", {"code": "AI_TOOL_UNAVAILABLE", "operation": "batch_history", "reason": str(error)})
        if self._loop is not None and not self._closed:
            asyncio.run_coroutine_threadsafe(perform(), self._loop)

    def _emit(self, kind, value) -> None:
        self.event_received.emit(kind, value)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._service = AssistantConversationService(self._directory, self._emit, material_provider=self._materials,
                                                         action_provider=self._apply, reference_provider=self._reference,
                                                         variant_provider=self._variants,
                                                         fit_provider=self._fit,
                                                         style_provider=self._styles,
                                                         evaluation_provider=self._evaluation_image,
                                                         observation_provider=self._observe,
                                                         image_memory_provider=self._image_memory,
                                                         batch_provider=self._batch,
                                                         audit=self._record)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if not (isinstance(exc, ValueError) and str(exc).startswith("AI_")):
                logger.exception("打开助手数据失败")
            code = str(exc) if isinstance(exc, ValueError) else "AI_STORE_FAILED"
            self._emit("error", {"code": code, "operation": "load"})
        self._started.set()
        with self._lock:
            pending, self._pending = self._pending, []
        for name, values in pending:
            loop.call_soon(self._dispatch, name, values)
        loop.run_forever()
        loop.close()

    def _dispatch(self, name, values):
        if self._service:
            self._service.command(name, values)
        else:
            self._emit("error", {"code": "AI_STORE_FAILED", "operation": name})

    def _record(self, event, **fields):
        return self._audit(event, **fields) if self._audit else None

    def command(self, name: str, /, **values) -> None:
        if self._closed:
            return
        if name == "send":
            scope_kind = values.pop("scope_kind", "current")
            scope_revision = values.pop("scope_revision", 0)
            if self._batch_bridge is not None:
                try:
                    values["batch_context"] = self._batch_bridge.context_for_send(scope_kind)
                    values["batch_context"]["scope_revision"] = scope_revision
                except ValueError as error:
                    if scope_kind != "current":
                        self._emit("error", {"code": "AI_TOOL_UNAVAILABLE", "operation": "send", "reason": str(error)})
                        return
        values = copy.deepcopy(values)
        with self._lock:
            if not self._started.is_set():
                self._pending.append((name, values))
                return
        self._loop.call_soon_threadsafe(self._dispatch, name, values)

    async def _materials(self, context, request, cancel_event) -> dict:
        if self._core is None or self._pool is None:
            raise ValueError("AI_IMAGE_UNAVAILABLE")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        cancelled = Event()
        worker = _MaterialJob(self._core, context, request, self._face_detector, loop, future, cancelled)
        self._pool.start(worker)
        try:
            return await future
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            if cancel_event.is_set():
                cancelled.set()

    async def _apply(self, context, workflow, adjustments, **options):
        if self._actions is None or self._pool is None:
            raise ValueError("AI_IMAGE_UNAVAILABLE")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        actions = self._actions
        worker = _ActionJob(lambda: actions.apply(context, workflow, adjustments, **options), loop, future)
        self._pool.start(worker)
        return await asyncio.shield(future)

    async def _observe(self, request, *, is_cancelled, progress):
        if self._core is None or self._pool is None:
            raise ValueError("AI_IMAGE_UNAVAILABLE")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        observer = PhotoObservationService(self._core, self._face_detector)

        def perform():
            try:
                return observer.observe(request, is_cancelled=is_cancelled,
                                        progress=lambda stage: loop.call_soon_threadsafe(progress, stage))
            except MemoryError:
                raise ImageObservationError("AI_OBSERVATION_MEMORY") from None
            except CancelledError:
                raise ImageObservationError("AI_CANCELLED") from None

        self._pool.start(_ActionJob(perform, loop, future, failure_code="AI_IMAGE_UNAVAILABLE"))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.gather(future, return_exceptions=True)
            raise

    async def _image_memory(self, memory, size, *, is_cancelled):
        if memory is None and not size:
            return None
        if self._core is None or self._pool is None:
            raise ValueError("AI_IMAGE_UNAVAILABLE")
        memory = memory or ImageMemory(self._core.image_sources.resources)
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def reserve():
            try:
                memory.resize(size, is_cancelled)
                return memory
            except MemoryError:
                raise ImageObservationError("AI_OBSERVATION_MEMORY") from None
            except CancelledError:
                raise ImageObservationError("AI_CANCELLED") from None

        self._pool.start(_ActionJob(reserve, loop, future, failure_code="AI_OBSERVATION_MEMORY"))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.gather(future, return_exceptions=True)
            memory.close()
            raise

    async def _fit(self, context, builder, target, values, *, is_cancelled):
        if self._core is None or self._pool is None:
            raise ValueError("AI_IMAGE_UNAVAILABLE")
        from ...application.style_fit import ReferenceFitter

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        core, sources = self._core, self._core.image_sources

        def measure(workflow, edge):
            state = core.get_session(context.session_id)
            if is_cancelled() or not state.success or not context_matches(context, state.data):
                raise ValueError("AI_PHOTO_CHANGED")
            result = core.preview(context.session_id, workflow, update_draft=False,
                                  max_dimension=edge, is_cancelled=is_cancelled)
            if not result.success:
                raise ValueError(result.error.code)
            with result.data.image as image:
                with sources.resources.working_memory(ANALYSIS_WORKING_BYTES, is_cancelled), \
                        sources.compute.borrow(is_cancelled, threads=1):
                    analysis = analyzed_photo(image)
            state = core.get_session(context.session_id)
            if is_cancelled() or not state.success or not context_matches(context, state.data):
                raise ValueError("AI_PHOTO_CHANGED")
            return analysis

        def perform():
            options = {key: value for key, value in values.items() if key != "target"}
            result = ReferenceFitter(builder, measure, is_cancelled).fit(dict(context.color), target, **options)
            result.update(revision=context.revision_id, target="reference")
            return result

        self._pool.start(_ActionJob(perform, loop, future))
        return await asyncio.shield(future)

    async def _evaluation_image(self, context, revision, *, is_cancelled):
        if self._core is None or self._pool is None:
            raise ValueError("AI_IMAGE_UNAVAILABLE")
        core, sources = self._core, self._core.image_sources
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def valid():
            state = core.get_session(context.session_id)
            if is_cancelled() or not state.success or not context_matches(context, state.data):
                raise ValueError("AI_PHOTO_CHANGED")

        def perform():
            valid()
            workflow = core.revision_workflow(context.session_id, revision)
            if not workflow.success:
                raise ValueError("AI_EVALUATION_REVISION")
            preview = core.preview(context.session_id, workflow.data, update_draft=False,
                                   max_dimension=1536, is_cancelled=is_cancelled)
            if not preview.success:
                raise ValueError(preview.error.code)
            with preview.data.image as image:
                with sources.resources.working_memory(ANALYSIS_WORKING_BYTES, is_cancelled), \
                        sources.compute.borrow(is_cancelled, threads=1):
                    data = encode_jpeg(image, 1536, 90)
            valid()
            return {"image": data, "workflow_hash": workflow.data.workflow_hash()}

        self._pool.start(_ActionJob(perform, loop, future))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.gather(future, return_exceptions=True)
            raise

    async def _styles(self, operation, context, *, is_cancelled, builder=None, **options):
        if self._core is None or self._pool is None:
            raise ValueError("AI_IMAGE_UNAVAILABLE")
        from ...application.style_workflow import capture_style, analyze_style_frames, apply_style_suggestion
        from ...application.photo_analysis import analysis_summary
        from ..adapters.studio_adapter import workflow_stages

        core, sources = self._core, self._core.image_sources
        detector = self._face_detector
        measured_faces = False
        loop, future = asyncio.get_running_loop(), asyncio.get_running_loop().create_future()

        def render(workflow, edge):
            state = core.get_session(context.session_id)
            if is_cancelled() or not state.success or not context_matches(context, state.data):
                raise ValueError("AI_PHOTO_CHANGED")
            result = core.preview(context.session_id, workflow, update_draft=False, max_dimension=edge, is_cancelled=is_cancelled)
            if not result.success:
                raise ValueError(result.error.code)
            state = core.get_session(context.session_id)
            if is_cancelled() or not state.success or not context_matches(context, state.data):
                result.data.close()
                raise ValueError("AI_PHOTO_CHANGED")
            return result.data.image

        def measure(workflow, edge):
            nonlocal measured_faces
            with render(workflow, edge) as image:
                regions, face_count = (), None
                if detector is not None and not measured_faces:
                    try:
                        from ...application.beauty_analysis import query_face_regions
                        detected = query_face_regions(detector, image, dict(context.beauty), is_cancelled,
                                                      photo=context, workflow=workflow)
                        regions = detected if isinstance(detected, (list, tuple)) else ()
                        face_count = len(detected) if isinstance(detected, (list, tuple)) else None
                    except Exception:
                        logger.exception("风格样例的人脸区域检测失败")
                measured_faces = True
                with sources.resources.working_memory(ANALYSIS_WORKING_BYTES, is_cancelled), \
                        sources.compute.borrow(is_cancelled, threads=1):
                    analysis = analyzed_photo(image, regions)
                    if face_count is not None:
                        analysis["face_count"] = face_count
                    return analysis

        def perform():
            state = core.get_session(context.session_id)
            if is_cancelled() or not state.success or not context_matches(context, state.data):
                raise ValueError("AI_PHOTO_CHANGED")
            if operation == "history":
                timeline = core.history_timeline(context.session_id)
                if not timeline.success:
                    raise ValueError("AI_STYLE_HISTORY")
                return [{"id": revision.revision_id, "number": index + 1, "label": revision.label,
                         "workflow_hash": revision.workflow_hash} for index, revision in enumerate(timeline.data["revisions"])]
            if operation == "apply":
                result = apply_style_suggestion(context, options.pop("style"), build=builder, measure=measure,
                                                is_cancelled=is_cancelled, **options)
                if result.get("analysis"):
                    result["analysis"] = analysis_summary(result["analysis"])
                return result
            before = core.revision_workflow(context.session_id, options["before_revision"])
            after = core.revision_workflow(context.session_id, options["result_revision"])
            if not before.success or not after.success:
                raise ValueError("AI_STYLE_HISTORY")
            def parameters(workflow):
                stages = workflow_stages(workflow)
                return {"color": stages.get("color_adjust", {}), "beauty": stages.get("beauty", {})}
            def analyze(arrays):
                with sources.compute.borrow(is_cancelled, threads=1):
                    paired, statistics = analyze_style_frames(arrays)
                if detector is not None:
                    from PIL import Image
                    for array, stats in zip(arrays, statistics):
                        with Image.fromarray(array) as image:
                            try:
                                faces = detector(image, parameters(after.data)["beauty"], is_cancelled)
                                if isinstance(faces, (list, tuple)):
                                    stats["face_count"] = len(faces)
                            except Exception:
                                logger.exception("风格配对的人脸区域检测失败")
                return paired, statistics
            with sources.resources.working_memory(ANALYSIS_WORKING_BYTES * 3, is_cancelled):
                sample, assets = capture_style(parameters(before.data), parameters(after.data), after.data, builder, render, analyze)
            sample.update(before_revision=options["before_revision"], result_revision=options["result_revision"],
                          before_workflow_hash=before.data.workflow_hash(), result_workflow_hash=after.data.workflow_hash(),
                          result_workflow=after.data.to_dict())
            state = core.get_session(context.session_id)
            if is_cancelled() or not state.success or not context_matches(context, state.data):
                raise ValueError("AI_PHOTO_CHANGED")
            return {"sample": sample, "assets": assets}

        self._pool.start(_ActionJob(perform, loop, future))
        return await asyncio.shield(future)

    async def _variants(self, operation, context, **options):
        if self._actions is None or self._pool is None:
            raise ValueError("AI_IMAGE_UNAVAILABLE")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        cancelled = Event()
        actions, detector, sources = self._actions, self._face_detector, self._core.image_sources

        def perform():
            if operation == "generate":
                return actions.variants(context, detector=detector, **options)
            if operation == "adopt":
                state = self._core.get_session(context.session_id)
                if not state.success or not context_matches(context, state.data):
                    raise ValueError("AI_VARIANT_STALE")
                return actions.apply(context, **options)
            from PIL import Image
            images = {}
            with sources.resources.working_memory(ANALYSIS_WORKING_BYTES, cancelled.is_set), \
                    sources.compute.borrow(cancelled.is_set, threads=1):
                for label, raw in options["images"].items():
                    if cancelled.is_set():
                        raise ValueError("AI_CANCELLED")
                    with Image.open(BytesIO(raw)) as encoded, encoded.convert("RGBA") as image:
                        images[label] = {"size": image.size, "rgba": image.tobytes()}
            return images

        self._pool.start(_ActionJob(perform, loop, future))
        try:
            return await future if operation == "images" else await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _reference(self, path, context):
        if self._core is None or self._pool is None:
            raise ValueError("AI_IMAGE_UNAVAILABLE")
        loop = asyncio.get_running_loop()
        future, cancelled = loop.create_future(), Event()
        sources, detector = self._core.image_sources, self._face_detector

        def load():
            if Path(path).suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise ValueError("AI_REFERENCE_FORMAT")
            stamp = sources.file_stamp(path)
            image, _size = sources.viewer_image(path, raw_display_mode=RawDisplayMode.ORIGINAL,
                                               non_raw_display_mode=NonRawDisplayMode.ORIGINAL,
                                               expected_stamp=stamp, is_cancelled=cancelled.is_set)
            try:
                with sources.resources.working_memory(image.width * image.height * 4 + ANALYSIS_WORKING_BYTES,
                                                       cancelled.is_set):
                    with sources.compute.borrow(cancelled.is_set, threads=1):
                        image.thumbnail((ANALYSIS_EDGE, ANALYSIS_EDGE))
                    regions = ()
                    if detector is not None:
                        try:
                            detected = detector(image, dict(context.beauty) if context else {}, cancelled.is_set)
                            if isinstance(detected, (list, tuple)):
                                regions = detected
                        except Exception:
                            logger.exception("参考图的人脸检测失败，保留全图测量")
                    if cancelled.is_set():
                        raise ValueError("AI_CANCELLED")
                    with sources.compute.borrow(cancelled.is_set, threads=1):
                        analysis = analyzed_photo(image, regions, display="original")
                        return {"image": encode_jpeg(image, IMAGE_SOURCES["current"][0], IMAGE_SOURCES["current"][1]),
                                "thumbnail": reference_thumbnail(image), "analysis": analysis}
            finally:
                image.close()

        self._pool.start(_ActionJob(load, loop, future, failure_code="AI_REFERENCE_FAILED"))
        try:
            return await future
        except asyncio.CancelledError:
            cancelled.set()
            raise

    @Slot(str, object)
    def _receive(self, kind, value):
        if kind == "error":
            self.last_error = value
        if kind == "state":
            self.state = value
            if self._batch_bridge is not None:
                self._project_batch_state()
            else:
                self.changed.emit(value)
        elif kind == "delta":
            conversation = self.state.get("conversation")
            if conversation and value["conversation_id"] == conversation["id"]:
                message = conversation["messages"].get(value["message_id"])
                if message and message.get("request_id") == value["request_id"]:
                    message.update(text=value["text"], status=value["status"], reasoning_summary=value["summary"])
                for item in self.state.get("messages", ()):
                    if item["id"] == value["message_id"] and item.get("request_id") == value["request_id"]:
                        item.update(text=value["text"], status=value["status"], reasoning_summary=value["summary"])

    def begin_shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started.is_set():
            self._shutdown_future = asyncio.run_coroutine_threadsafe(self._close(), self._loop)

    async def _close(self):
        if self._service:
            await self._service.close()

    def shutdown(self) -> None:
        self.begin_shutdown()
        self._started.wait(10)
        if self._shutdown_future is None and self._loop and not self._loop.is_closed():
            self._shutdown_future = asyncio.run_coroutine_threadsafe(self._close(), self._loop)
        if self._shutdown_future:
            self._shutdown_future.result(timeout=15)
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
