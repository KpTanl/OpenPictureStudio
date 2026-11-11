from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from threading import Event
from concurrent.futures import CancelledError

from ..core.contracts import WorkflowGraph
from ..core.contracts.image_display import PreviewDisplaySpec
from .result_types import CoreResult, ErrorCode
from ..core.contracts.result import memory_error_key
from .image_source_service import path_key
from .studio_preview_cache import PreviewFrame, PreviewKey, preview_variant

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StudioLoadRequest:
    request_id: int
    path: str
    session_id: str | None = None
    preview_key: PreviewKey | None = None
    prefetch: bool = False
    cancellation: Event = field(default_factory=Event, compare=False)
    initial_workflow: WorkflowGraph = field(default_factory=WorkflowGraph)
    preview_workflow: WorkflowGraph | None = None
    max_dimension: int | None = 1600


@dataclass(frozen=True)
class StudioPreviewTarget:
    path: str
    workflow: WorkflowGraph
    preview_key: PreviewKey | None = None
    max_dimension: int | None = 1600


class StudioLoadQueue:
    """由调用线程管理，最多保留一个执行请求和一个最新待执行请求。"""

    def __init__(self):
        self.active = None
        self.pending = None
        self.generation = 0

    def submit(self, path, session_id=None, *, preview_key=None, prefetch=False,
               initial_workflow=None, preview_workflow=None, max_dimension=1600):
        path = os.path.normpath(path)
        initial_workflow = initial_workflow if initial_workflow is not None else WorkflowGraph()
        if (self.active is not None and not self.active.cancellation.is_set()
                and self.active.path == path and self.active.session_id == session_id
                and self.active.preview_key == preview_key and self.active.prefetch == prefetch
                and self.active.max_dimension == max_dimension
                and self.active.initial_workflow == initial_workflow
                and self.active.preview_workflow == preview_workflow):
            return None
        self.cancel()
        request = StudioLoadRequest(self.generation, path, session_id, preview_key, prefetch,
                                    initial_workflow=initial_workflow, preview_workflow=preview_workflow,
                                    max_dimension=max_dimension)
        if self.active is not None:
            self.pending = request
            return None
        self.active = request
        return request

    def finish(self, request):
        if self.active is not request:
            return None
        self.active, self.pending = self.pending, None
        return self.active

    def cancel(self):
        self.generation += 1
        if self.active is not None:
            self.active.cancellation.set()
        if self.pending is not None:
            self.pending.cancellation.set()
        self.pending = None


@dataclass
class PreparedStudioSource:
    core: object
    project: object
    owns_session: bool = False
    file_stamp: tuple[int, int] | None = None

    def close(self):
        if self.owns_session:
            self.core.close_session(self.project.session_id)
            self.owns_session = False


class StudioSourceService:
    def __init__(self, core):
        self.core = core

    def prepare_preview(self, request, cache):
        from ..core.contracts.gpu import cpu_recovery
        cancelled = request.cancellation.is_set
        try:
            if not self.core.tools.cacheable_workflow(request.initial_workflow):
                return CoreResult.ok()
            if cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            snapshot = self.core.tools.snapshot(is_cancelled=cancelled)
            spec = PreviewDisplaySpec(request.max_dimension)
            backend = self.core.tools.preview_backend(request.path, request.initial_workflow, spec)
            key = (request.preview_key if cpu_recovery.get() is None else None) or PreviewKey.capture(request.path,
                preview_variant(request.initial_workflow, snapshot.generation, spec, backend), backend)
            if not key.variant.startswith(preview_variant(request.initial_workflow, snapshot.generation, spec, backend)):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            sources = self.core.image_sources
            if cache.contains(key):
                logger.debug("OPS prefetch cache hit: request=%s image=%s",
                             request.request_id, os.path.basename(request.path))
                return CoreResult.ok()
            logger.debug("OPS prefetch decoding: request=%s image=%s",
                         request.request_id, os.path.basename(request.path))
            minimum_threads = snapshot.required_threads(request.initial_workflow)
            identity = sources.identity(request.path, is_cancelled=cancelled)
            metadata = sources.describe(identity, is_cancelled=cancelled)
            limit = (snapshot.executor.preview_input_limit(metadata.orig_size, request.initial_workflow, spec)
                     if self.core.tools.supports_proxy(request.initial_workflow, canonical_inputs=True) else None)
            half_size = (not self.core.tools.uses_prepared_inputs(request.initial_workflow)
                         and sources.preview_half_size(identity, limit, is_cancelled=cancelled))
            with sources.borrow_deferred(request.path, identity=identity, max_side=limit, is_cancelled=cancelled,
                                       priority=2, preloading=True, half_size=half_size) as lease:
                reference_size = lease.value.orig_size
                from ..core.contracts.gpu import display_request
                with sources.compute.request_context(cancelled, priority=2, minimum_threads=minimum_threads), display_request(spec):
                    executed = self.core.tools.execute_preview(snapshot, lease.value, request.initial_workflow,
                        is_cancelled=cancelled, resources=sources.resources, describe=True,
                        full_source=lambda abort: sources.borrow_deferred(request.path, identity=identity,
                                                                        is_cancelled=abort, priority=2))
                    if not executed.success:
                        return executed
                    rendered = executed.data.source
                    source_size, source_is_proxy = executed.data.input_size, executed.data.input_is_proxy
                    from ..core.photo_engine import display_rgba8
                    from ..core.image_patches import display_stage_scope
                    with self.core.tools.execution.output_operation(rendered, "prefetch.display", cancelled,
                            threads=sources.policy.operator_threads, memory_bytes=rendered.pixels.nbytes), display_stage_scope(
                                self.core.tools.execution.results if self.core.tools.execution.memo_enabled else None):
                        rgba = display_rgba8(rendered, max_dimension=request.max_dimension,
                            parallelism=sources.policy.operator_threads, cancelled=cancelled)
            if cancelled() or snapshot.is_cancelled() or not key.matches_file():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            full_size = rendered.orig_size if source_is_proxy else (
                rendered.pixels.shape[1], rendered.pixels.shape[0])
            cache.put(key, PreviewFrame(memoryview(rgba).cast("B"), tuple(reversed(rgba.shape[:2])), reference_size,
                                       full_size, rendered.effect_status, source_size, source_is_proxy, spec, rendered.backend_identity),
                      is_cancelled=lambda: cancelled() or snapshot.is_cancelled() or not key.matches_file())
            sources.publish_source_cache(identity, is_cancelled=cancelled, preloading=True)
            return CoreResult.ok()
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as error:
            return CoreResult.from_exception(error, code=ErrorCode.DECODE_FAILED,
                                             message_key=memory_error_key(error))
        except Exception as error:
            from ..core.contracts.gpu import GpuExecutionError, cpu_recovery, recover_cpu
            if isinstance(error, GpuExecutionError) and cpu_recovery.get() is None and not cancelled():
                rendered = executed = None
                error.__traceback__ = error.__cause__ = error.__context__ = None
                with recover_cpu(error.code):
                    return self.prepare_preview(request, cache)
            return CoreResult.from_exception(error, code=ErrorCode.DECODE_FAILED,
                                             message_key="studio.prefetch_failed")

    def prepare(self, request, *, is_cancelled=None):
        cancelled = lambda: request.cancellation.is_set() or bool(is_cancelled and is_cancelled())
        try:
            identity = self.core.image_sources.identity(request.path, is_cancelled=cancelled)
            return self._prepare(request, cancelled, identity)
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as memory_error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(memory_error))
        except Exception as error:
            return CoreResult.from_exception(error, message_key="error.source.decode_failed")

    def _prepare(self, request, cancelled, identity):
        prepared = None
        opened_source = None
        transferred = False
        try:
            if cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            before = os.stat(request.path)
            if request.session_id is not None:
                current = self.core.get_session(request.session_id)
                if not current.success:
                    return current
                project = current.data
                ready = self.core.ensure_source_ready(project.session_id, expected_source=identity,
                    expected_state=project.state_version, is_cancelled=cancelled)
                if not ready.success:
                    return ready
                project = ready.data
                source = project.active_source
                if path_key(request.path) != path_key(source.path):
                    return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
                if identity.file_hash != source.file_hash:
                    return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
                prepared = PreparedStudioSource(self.core, project)
            else:
                opened = self.core.open_source(request.path, is_cancelled=cancelled, identity=identity,
                                               initial_proxy=True)
                if not opened.success:
                    return opened
                opened_source = opened.data
                if cancelled():
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                created = self.core.create_session(opened_source, initial_workflow=request.initial_workflow)
                if not created.success:
                    return created
                prepared = PreparedStudioSource(self.core, created.data, owns_session=True)
                project = created.data
            if cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            current = self.core.get_session(project.session_id)
            if not current.success or current.data.state_version != project.state_version:
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            after = os.stat(request.path)
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
            prepared.file_stamp = (after.st_mtime_ns, after.st_size)
            if request.session_id is not None:
                self.core.image_sources.record_source_open(identity)
            transferred = True
            return CoreResult.ok(prepared)
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as memory_error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(memory_error))
        except FileNotFoundError as error:
            return CoreResult.from_exception(error, code=ErrorCode.SOURCE_NOT_FOUND,
                                             message_key="error.source.not_found")
        except Exception as error:
            return CoreResult.from_exception(error, code=ErrorCode.DECODE_FAILED,
                                             message_key="error.source.decode_failed")
        finally:
            if not transferred:
                if prepared is not None:
                    prepared.close()
                if opened_source is not None:
                    self.core.release_source(opened_source.source_id)

    def validate_prepared(self, prepared):
        try:
            stat = os.stat(prepared.project.active_source.path)
        except OSError as error:
            return CoreResult.from_exception(error, code=ErrorCode.SOURCE_NOT_FOUND,
                                             message_key="error.source.not_found")
        if prepared.file_stamp != (stat.st_mtime_ns, stat.st_size):
            return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
        current = self.core.get_session(prepared.project.session_id)
        if not current.success or current.data.state_version != prepared.project.state_version:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        return CoreResult.ok()
