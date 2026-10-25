from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from threading import Event
from concurrent.futures import CancelledError

from PIL import Image

from ..core.contracts import WorkflowGraph
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


@dataclass(frozen=True)
class StudioPreviewTarget:
    path: str
    workflow: WorkflowGraph
    preview_key: PreviewKey | None = None


class StudioLoadQueue:
    """由调用线程管理，最多保留一个执行请求和一个最新待执行请求。"""

    def __init__(self):
        self.active = None
        self.pending = None
        self.generation = 0

    def submit(self, path, session_id=None, *, preview_key=None, prefetch=False,
               initial_workflow=None, preview_workflow=None):
        path = os.path.normpath(path)
        initial_workflow = initial_workflow if initial_workflow is not None else WorkflowGraph()
        if (self.active is not None and not self.active.cancellation.is_set()
                and self.active.path == path and self.active.session_id == session_id
                and self.active.preview_key == preview_key and self.active.prefetch == prefetch
                and self.active.initial_workflow == initial_workflow
                and self.active.preview_workflow == preview_workflow):
            return None
        self.cancel()
        request = StudioLoadRequest(self.generation, path, session_id, preview_key, prefetch,
                                    initial_workflow=initial_workflow, preview_workflow=preview_workflow)
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
    image: Image.Image | None = None
    owns_session: bool = False
    file_stamp: tuple[int, int] | None = None
    proxy_lease: object = None

    def close(self):
        if self.proxy_lease is not None:
            self.proxy_lease.close()
            self.proxy_lease = None
        if self.image is not None:
            self.image.close()
            self.image = None
        if self.owns_session:
            self.core.close_session(self.project.session_id)
            self.owns_session = False


class StudioSourceService:
    def __init__(self, core):
        self.core = core

    def prepare_preview(self, request, cache):
        cancelled = request.cancellation.is_set
        try:
            if cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            snapshot = self.core.tools.snapshot(is_cancelled=cancelled)
            key = PreviewKey.capture(request.path, preview_variant(request.initial_workflow, snapshot.generation))
            sources = self.core.image_sources
            if cache.contains(key):
                with sources.borrow_linear(request.path, max_side=1600, is_cancelled=cancelled,
                                           priority=2, preloading=True):
                    pass
                logger.debug("OPS prefetch cache hit: request=%s image=%s",
                             request.request_id, os.path.basename(request.path))
                return CoreResult.ok()
            logger.debug("OPS prefetch decoding: request=%s image=%s",
                         request.request_id, os.path.basename(request.path))
            minimum_threads = snapshot.required_threads(request.initial_workflow)
            with sources.borrow_linear(request.path, max_side=1600, is_cancelled=cancelled,
                                       priority=2, preloading=True) as lease:
                reference_size = lease.value.orig_size
                source_bytes = lease.value.pixels.nbytes + (lease.value.alpha.nbytes if lease.value.alpha is not None else 0)
                with sources.resources.working_memory(source_bytes * 4, cancelled), \
                        sources.compute.borrow(cancelled, priority=2, minimum_threads=minimum_threads):
                    rendered = snapshot.executor.execute(lease.value, request.initial_workflow, is_cancelled=cancelled)
                    if not rendered.success:
                        return rendered
                    from ..core.photo_engine import _pil_from
                    image = _pil_from(rendered.data)
            try:
                if cancelled() or snapshot.is_cancelled() or not key.matches_file():
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                rgba = image.convert("RGBA")
                try:
                    cache.put(key, PreviewFrame(rgba.tobytes(), rgba.size, reference_size),
                              is_cancelled=lambda: cancelled() or snapshot.is_cancelled() or not key.matches_file())
                finally:
                    rgba.close()
            finally:
                image.close()
            return CoreResult.ok()
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as error:
            return CoreResult.from_exception(error, code=ErrorCode.DECODE_FAILED,
                                             message_key=memory_error_key(error))
        except Exception as error:
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
            workflow = request.preview_workflow if request.preview_workflow is not None else project.draft_workflow
            preview = self.core.preview(project.session_id, workflow,
                                        update_draft=False, max_dimension=1600, is_cancelled=cancelled)
            if not preview.success:
                return preview
            prepared.image = preview.data.image
            from .image_source_service import SourceMetadata
            identity = self.core.source_identity(project.session_id)
            contract = project.active_source
            spec = self.core.source_preview(project.session_id)
            with self.core.image_sources.borrow_panel_snapshot(identity,
                    image=prepared.image if not workflow.nodes else None,
                    half_size=spec.half_size,
                    metadata=SourceMetadata((contract.width, contract.height), contract.source_format,
                                            contract.color_pipeline), is_cancelled=cancelled):
                pass
            if cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            current = self.core.get_session(project.session_id)
            if not current.success or current.data.state_version != project.state_version:
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            after = os.stat(request.path)
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
            prepared.file_stamp = (after.st_mtime_ns, after.st_size)
            prepared.proxy_lease = self.core.image_sources.borrow_linear(request.path,
                identity=identity, max_side=1600, half_size=spec.half_size, is_cancelled=cancelled)
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
