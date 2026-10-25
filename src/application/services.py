"""Application services and the image-engine adapter.

This module wraps the existing ``photo_engine`` as a stable capability
(``ImageEngineAdapter``) without changing any algorithm or processing order.
It also provides the service objects that the Core API orchestrates:

    PreviewService  -> cancellable, low-resolution, no revision
    CommitService   -> full quality, creates a revision + persisted artifact
    ExportService   -> reads a rendered result and performs external writes

The module is intentionally Qt-free.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from concurrent.futures import CancelledError
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Optional

from PIL import Image

from ..core.contracts import ArtifactRef, RenderSource, SourceBinding, RevisionRef, WorkflowGraph
from ..core.contracts.artifact import LINEAR_ARTIFACT
from ..core.mixer_sampling import MixerSampling, MixerSamplingRequest
from ..core.output_formats import output_format, validate_quality
from ..core.photo_engine import (
    RenderMeta,
    RenderSource as EngineRenderSource,
    apply_chain_v2,
    display_rgba8,
    load_render_pil,
    load_render_source,
    render_source,
    save_render_source,
    _pil_from,
)
from ..core.workflow import WorkflowExecutor
from ..core.artifact_store import ArtifactStore
from ..utils.app_info import APP_VERSION
from .result_types import CoreError, CoreResult, ErrorCode
from ..core.contracts.result import memory_error_key

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray
    from .image_resources import MemoryHold
    from .image_source_service import SourceIdentity, SourcePreviewSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------
class CommitPolicy(str, Enum):
    QUICK_EDIT = "quick_edit"
    PROJECT = "project"
    GENERATIVE = "generative"


@dataclass
class ProjectSession:
    """Runtime editing context. Rebuildable; never the persistent source of truth."""

    session_id: str
    document_id: str
    active_source: RenderSource | SourceBinding
    draft_workflow: WorkflowGraph = field(default_factory=WorkflowGraph)
    current_revision: Optional[RevisionRef] = None
    preview_state: dict[str, Any] = field(default_factory=dict)
    ui_context: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    state_version: int = 0


@dataclass(frozen=True)
class DisplayFrame:
    """显示用 RGBA8 帧。rgba 是只读字节缓冲，长度为 宽 × 高 × 4；size 为 (宽, 高)。"""
    rgba: bytes | memoryview
    size: tuple[int, int]

    @classmethod
    def from_array(cls, rgba):
        height, width = rgba.shape[:2]
        return cls(memoryview(rgba).cast("B"), (width, height))

    @classmethod
    def from_image(cls, image: Image.Image):
        if image.mode == "RGBA":
            return cls(image.tobytes(), image.size)
        with image.convert("RGBA") as rgba:
            return cls(rgba.tobytes(), rgba.size)


class DetailDisplay:
    """完整显示帧及其两份 Qt 显示副本的预算，随接收方释放。"""
    def __init__(self, resources, size, render, is_cancelled):
        self._resources = resources
        self._key = (object(), "studio-detail")
        self.source = None
        size_bytes = size[0] * size[1] * 12
        self._lease = resources.acquire(self._key, "preview", size_bytes,
            lambda cancelled: render(), lambda frame: frame.size[0] * frame.size[1] * 12, is_cancelled)

    @property
    def frame(self):
        return self._lease.value

    def take_source(self):
        source, self.source = self.source, None
        return source

    def close(self):
        source = self.take_source()
        if source is not None:
            source.close()
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.close()
            self._resources.discard(self._key)


@dataclass
class SamplingHandle:
    channels: NDArray[np.uint8] | None
    request: MixerSamplingRequest
    memory: MemoryHold | None
    backend: str = ""
    source_identity: SourceIdentity | None = None
    source_preview: SourcePreviewSpec | None = None
    workflow_hash: str = ""
    tool_generation: int = 0
    state_version: int = 0

    @property
    def size(self):
        if self.channels is None:
            return (0, 0)
        return (self.channels.shape[1], self.channels.shape[0])

    def channel_at(self, x: float, y: float) -> int | None:
        if self.channels is None or not (0 <= x < 1 and 0 <= y < 1):
            return None
        width, height = self.size
        channel = int(self.channels[min(height - 1, int(y * height)), min(width - 1, int(x * width))])
        return channel if channel < 8 else None

    def close(self):
        self.channels = None
        memory, self.memory = self.memory, None
        if memory is not None:
            memory.close()

    def __del__(self):
        self.close()


@dataclass
class PreviewResult:
    """image 与 display 二选一：请求显示输出时 image 为 None，display 为显示帧。"""
    image: Image.Image | None
    source_id: str
    workflow_hash: str
    width: int
    height: int
    generation: int = 0
    histogram: dict | None = None
    duration_ms: float = 0.0
    display: DisplayFrame | None = None
    sampling: SamplingHandle | None = None
    detail: DetailDisplay | None = None
    full_size: tuple[int, int] | None = None
    detail_error: CoreError | None = None
    effect_status: tuple = ()
    input_size: tuple[int, int] | None = None
    input_is_proxy: bool | None = None

    def take_detail(self):
        detail, self.detail = self.detail, None
        return detail

    def take_sampling(self):
        sampling, self.sampling = self.sampling, None
        return sampling

    def close(self):
        image, self.image = self.image, None
        self.display = None
        self.histogram = None
        if image is not None:
            image.close()
        sampling = self.take_sampling()
        if sampling is not None:
            sampling.close()
        detail = self.take_detail()
        if detail is not None:
            detail.close()


@dataclass
class CommitResult:
    revision: RevisionRef
    workflow_hash: str = ""
    artifact: ArtifactRef | None = None


@dataclass(frozen=True)
class ExportRequest:
    target_path: str
    ext: str | None = None
    quality: int = 95
    overwrite: bool = False


@dataclass
class ExportResult:
    path: str
    media_type: str
    bytes_written: int = 0
    size: tuple[int, int] | None = None
    product_hash: str = ""


# ---------------------------------------------------------------------------
# Engine adapter
# ---------------------------------------------------------------------------
class ImageEngineAdapter:
    """Formal wrapper around the existing PhotoEngine.

    It exposes the engine as an object capability and converts engine results
    into Core contracts. Algorithms and processing order stay untouched.
    """

    ENGINE_VERSION = APP_VERSION

    def load_engine_source(
        self, path: str, *, for_preview: bool = False, threads: int = 1
    ) -> EngineRenderSource:
        return load_render_source(path, for_preview=for_preview, threads=threads)

    def render_source(
        self, source: EngineRenderSource, params: dict[str, Any], *, threads=None,
        cancelled=lambda: False, sampling=None,
    ) -> EngineRenderSource:
        return render_source(source, params, threads=threads, cancelled=cancelled, sampling=sampling)

    def render_pil(
        self,
        image: Image.Image,
        params: dict[str, Any],
        *,
        meta: RenderMeta | None = None,
    ) -> Image.Image:
        return apply_chain_v2(image, params, meta=meta)

    def source_to_pil(self, source: EngineRenderSource, *, parallelism=1) -> Image.Image:
        return _pil_from(source, parallelism=parallelism)

    def source_to_display(self, source: EngineRenderSource, *, parallelism=1) -> DisplayFrame:
        return DisplayFrame.from_array(display_rgba8(source, parallelism=parallelism))

    def save_source(
        self,
        source: EngineRenderSource,
        target_path: str,
        *,
        ext: str | None = None,
        quality: int = 95,
    ) -> None:
        save_render_source(source, target_path, ext=ext, quality=quality)

    def to_contract_source(
        self,
        engine_source: EngineRenderSource,
        *,
        file_hash: str = "",
        is_raw: bool = False,
    ) -> RenderSource:
        width, height = engine_source.orig_size
        source_id = make_source_id(engine_source.source_path, file_hash)
        return RenderSource(
            source_id=source_id,
            path=engine_source.source_path,
            file_hash=file_hash,
            width=width,
            height=height,
            source_format=engine_source.source_format,
            color_pipeline=engine_source.color_pipeline,
            note=engine_source.note,
            is_raw=is_raw,
        )


class LegacyEngineAdapter:
    """Compatibility shim so legacy Dialogs can keep calling the old logic.

    It re-exposes the historical module-level functions and the engine's pixel
    ``RenderSource`` under one object. New code should use ``ImageEngineAdapter``
    and the Core API instead.
    """

    RenderSource = EngineRenderSource

    def load_render_source(
        self, path: str, *, for_preview: bool = False, threads: int = 1
    ) -> EngineRenderSource:
        return load_render_source(path, for_preview=for_preview, threads=threads)

    def load_render_pil(
        self, path: str, *, engine: str = "pro", for_preview: bool = False
    ) -> tuple[Image.Image, RenderMeta]:
        return load_render_pil(path, engine=engine, for_preview=for_preview)

    def apply_chain_v2(
        self,
        image: Image.Image,
        params: dict[str, Any],
        copy_if_unchanged: bool = True,
        *,
        meta: RenderMeta | None = None,
    ) -> Image.Image:
        return apply_chain_v2(image, params, copy_if_unchanged=copy_if_unchanged, meta=meta)

    def render_source(
        self, source: EngineRenderSource, params: dict[str, Any], *, threads=None,
        cancelled=lambda: False,
    ) -> EngineRenderSource:
        return render_source(source, params, threads=threads, cancelled=cancelled)

    def save_render_source(
        self,
        source: EngineRenderSource,
        target_path: str,
        *,
        ext: str | None = None,
        quality: int = 95,
    ) -> None:
        save_render_source(source, target_path, ext=ext, quality=quality)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _workflow_executor(engine=None, runtime=None) -> WorkflowExecutor:
    from .tool_runtime import ToolRuntime
    return (runtime or ToolRuntime(engine)).snapshot().executor


def make_source_id(path: str, file_hash: str) -> str:
    seed = f"{os.path.normpath(path)}|{file_hash}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def hash_file(path: str, *, chunk_size: int = 1 << 20, is_cancelled=lambda: False) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            if is_cancelled():
                raise CancelledError()
            digest.update(chunk)
    if is_cancelled():
        raise CancelledError()
    return digest.hexdigest()


def validate_workflow(workflow, runtime=None):
    from .tool_runtime import ToolRuntime
    return (runtime or ToolRuntime()).validate(workflow)


def _compute_histogram(image: Image.Image) -> dict | None:
    try:
        from ..core.color_adjuster import ColorAdjuster

        return ColorAdjuster(engine="pro").compute_histogram(image)
    except Exception:
        logger.exception("Failed to compute histogram")
        return None


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
class PreviewService:
    """Cancellable, low-resolution preview rendering. Never creates a revision."""

    def __init__(self, engine: ImageEngineAdapter | None = None, *, runtime=None, display_parallelism=1):
        self._engine = engine or ImageEngineAdapter()
        self._display_parallelism = display_parallelism
        from .tool_runtime import ToolRuntime
        self.runtime = runtime or ToolRuntime(self._engine)

    def render(
        self,
        engine_source: EngineRenderSource,
        workflow: WorkflowGraph,
        *,
        generation: int = 0,
        compute_histogram: bool = False,
        is_cancelled: Callable[[], bool] | None = None,
        display: bool = False,
        max_dimension: int | None = None,
        parallelism: int | None = None,
        sampling_request: MixerSamplingRequest | None = None,
        resources=None,
        keep_full_display: bool = False,
        full_source=None,
        temporary_workflow=None,
    ) -> CoreResult[PreviewResult]:
        """显示预览可同时保留完整帧；缩小帧继续供交互、统计和采样使用。"""
        prepare_started = time.perf_counter()
        from ..core.workflow.validator import validate
        snapshot = self.runtime.snapshot()
        validation = validate(workflow, snapshot.registry)
        if not validation.success:
            err = validation.error
            assert err is not None
            return CoreResult.fail(err.code, err.message_key, details=err.details)

        started = time.perf_counter()
        parallelism = self._display_parallelism if parallelism is None else parallelism
        cancelled = lambda: snapshot.is_cancelled() or bool(is_cancelled and is_cancelled())
        sampling, map_memory, image, handle, detail = None, None, None, None, None
        detail_error = None
        retained = False
        try:
            if sampling_request is not None:
                if resources is None:
                    return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.preview.invalid")
                sampling = MixerSampling(sampling_request,
                    reserve_memory=lambda size: resources.hold_memory(size, cancelled),
                    working_memory=lambda size: resources.working_memory(size, cancelled))
            execution_options = {"sampling": sampling} if sampling is not None else {}
            executed = self.runtime.execute_preview(snapshot, engine_source, workflow, is_cancelled=cancelled,
                full_source=full_source, temporary_workflow=temporary_workflow, resources=resources,
                describe=True, **execution_options)
            executed_at = time.perf_counter()
            if not executed.success:
                return executed  # type: ignore[return-value]
            rendered = executed.data.source
            if snapshot.is_cancelled() or (is_cancelled is not None and is_cancelled()):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")

            height, width = rendered.pixels.shape[:2]
            if engine_source.is_proxy:
                full_size = rendered.orig_size
            else:
                full_size = (width, height)
            image, frame = None, None
            if display and keep_full_display and (max_dimension is None or max(width, height) > max_dimension):
                try:
                    detail = DetailDisplay(resources, (width, height),
                        lambda: self._engine.source_to_display(rendered, parallelism=parallelism), cancelled)
                except MemoryError as error:
                    if max_dimension is None:
                        raise
                    detail_error = CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(error)).error
            if detail is not None:
                frame = detail.frame
                if max_dimension is not None and max(frame.size) > max_dimension:
                    with Image.frombuffer("RGBA", frame.size, frame.rgba, "raw", "RGBA", 0, 1) as reduced:
                        reduced.thumbnail((max_dimension, max_dimension))
                        frame = DisplayFrame.from_image(reduced)
                width, height = frame.size
            elif display and (max_dimension is None or max(rendered.pixels.shape[:2]) <= max_dimension):
                frame = self._engine.source_to_display(rendered, parallelism=parallelism)
                width, height = frame.size
            else:
                image = self._engine.source_to_pil(rendered, parallelism=parallelism)
                width, height = image.size
            if sampling is not None and image is not None:
                if max_dimension is not None:
                    image.thumbnail((max_dimension, max_dimension))
                width, height = image.size
                if display:
                    frame = DisplayFrame.from_image(image)
                    image.close()
                    image = None
            handle = None
            if sampling is not None:
                map_memory = resources.hold_memory(width * height, cancelled)
                channels = sampling.to_channel_map((width, height))
                handle = SamplingHandle(channels, sampling_request, map_memory, sampling.backend,
                                        workflow_hash=workflow.workflow_hash(), tool_generation=snapshot.generation)
                map_memory = None
                if cancelled():
                    handle.close()
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            displayed_at = time.perf_counter()
            duration_ms = (displayed_at - started) * 1000.0
            result = PreviewResult(
                image=image,
                source_id=engine_source.source_path,
                workflow_hash=workflow.workflow_hash(),
                width=width,
                height=height,
                generation=generation,
                histogram=_compute_histogram(image) if compute_histogram and image is not None else None,
                duration_ms=duration_ms,
                display=frame,
                sampling=handle,
                detail=detail,
                full_size=full_size,
                detail_error=detail_error,
                effect_status=rendered.effect_status,
                input_size=executed.data.input_size,
                input_is_proxy=executed.data.input_is_proxy,
            )
            logger.debug("OPS 渲染分段 workflow=%s input=%sx%s output=%sx%s prepare_ms=%.1f execute_ms=%.1f display_ms=%.1f",
                         result.workflow_hash[:12], engine_source.pixels.shape[1], engine_source.pixels.shape[0],
                         width, height, (started - prepare_started) * 1000,
                         (executed_at - started) * 1000, (displayed_at - executed_at) * 1000)
            retained = True
            return CoreResult.ok(result)
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as memory_error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(memory_error))
        except Exception as exc:  # noqa: BLE001
            logger.exception("PreviewService.render failed")
            return CoreResult.from_exception(
                exc, code=ErrorCode.DECODE_FAILED, message_key="error.preview.failed"
            )
        finally:
            if sampling is not None:
                sampling.close()
            if map_memory is not None:
                map_memory.close()
            if not retained and image is not None:
                image.close()
            if not retained and handle is not None:
                handle.close()
            if not retained and detail is not None:
                detail.close()


class CommitService:
    """Full-quality rendering that persists an artifact and creates a revision."""

    def __init__(
        self,
        engine: ImageEngineAdapter | None = None,
        artifact_dir: str | None = None,
        *, runtime=None,
    ):
        self._engine = engine or ImageEngineAdapter()
        from .storage_workspaces import ManagedWorkspace
        self._temporary_artifacts = ManagedWorkspace("artifacts") if artifact_dir is None else None
        artifact_dir = self._temporary_artifacts.name if self._temporary_artifacts else artifact_dir
        self._artifact_dir = artifact_dir
        from .tool_runtime import ToolRuntime
        self.runtime = runtime or ToolRuntime(self._engine)
        self._artifact_store = ArtifactStore(artifact_dir) if artifact_dir else None

    def commit(
        self,
        engine_source: EngineRenderSource,
        source: RenderSource,
        workflow: WorkflowGraph,
        *,
        policy: CommitPolicy = CommitPolicy.PROJECT,
        parent: RevisionRef | None = None,
        label: str = "",
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CoreResult[CommitResult]:
        from ..core.workflow.validator import validate
        snapshot = self.runtime.snapshot()
        validation = validate(workflow, snapshot.registry)
        if not validation.success:
            err = validation.error
            assert err is not None
            return CoreResult.fail(err.code, err.message_key, details=err.details)

        stored = None
        retained = False
        try:
            executed = snapshot.executor.execute(engine_source, workflow, is_cancelled=lambda: snapshot.is_cancelled() or bool(is_cancelled and is_cancelled()))
            if not executed.success:
                return executed  # type: ignore[return-value]
            rendered = executed.data
            import tempfile
            from ..core.render_codec import write_linear, read_linear

            estimated = 2 * (rendered.pixels.nbytes + (rendered.alpha.nbytes if rendered.alpha is not None else 0)) + 2 * 1024 ** 2
            budget = self._temporary_artifacts.reserve(estimated) if self._temporary_artifacts else nullcontext()
            with budget, tempfile.TemporaryDirectory(dir=self._artifact_dir, prefix="render-") as staging:
                path = os.path.join(staging, "render.tiff")
                write_linear(rendered, path)
                read_linear(path)
                if snapshot.is_cancelled() or (is_cancelled is not None and is_cancelled()):
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                stored = self._artifact_store.put_file(path, media_type="image/tiff",
                                                       kind=LINEAR_ARTIFACT, producer_node="render")

            artifact = ArtifactRef(
                hash=stored.hash,
                kind=LINEAR_ARTIFACT,
                media_type="image/tiff",
                producer_node="render",
                engine_version=self._engine.ENGINE_VERSION,
                source_hash=source.file_hash,
                color_context="linear_srgb",
                path=stored.path,
            )
            revision = RevisionRef(
                revision_id=uuid.uuid4().hex[:16],
                parent_id=parent.revision_id if parent else None,
                workflow_hash=workflow.workflow_hash(),
                artifact_refs=(artifact,),
                label=label or policy.value,
            )
            if snapshot.is_cancelled() or (is_cancelled is not None and is_cancelled()):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            retained = True
            return CoreResult.ok(
                CommitResult(
                    revision=revision,
                    workflow_hash=revision.workflow_hash,
                    artifact=artifact,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("CommitService.commit failed")
            return CoreResult.from_exception(
                exc,
                code=ErrorCode.ARTIFACT_WRITE_FAILED,
                message_key=str(exc) if str(exc).startswith("storage.") else "error.commit.failed",
            )
        finally:
            if stored is not None and not retained:
                self.discard(stored)

    def discard(self, artifact):
        if artifact is not None:
            try:
                self._artifact_store.discard_unpublished(artifact)
            except OSError:
                logger.exception("未能清理未发布的提交产物")



class ExportService:
    """统一渲染、历史产物和外部写入。"""

    def __init__(self, engine=None, *, runtime=None, writer=None):
        from .tool_runtime import ToolRuntime
        from .output_writer import OutputWriter
        self._engine = engine or ImageEngineAdapter()
        self.runtime = runtime or ToolRuntime(self._engine)
        self.writer = writer or OutputWriter()

    def export(self, engine_source, workflow, request, *, artifact=None,
               execution_allowed=True, is_cancelled=None, publication=None,
               reservation=None, on_published=None, preserve_alpha=False,
               runtime_snapshot=None, resize=None, resources=None, record_digest=False):
        from pathlib import Path
        from io import BytesIO
        checked = self.validate_request(request)
        if not checked.success:
            return checked
        ext, media_type = checked.data
        if reservation is None:
            with self.writer.reserve(request.target_path) as reserved:
                return self.export(engine_source, workflow, request, artifact=artifact,
                                   execution_allowed=execution_allowed, is_cancelled=is_cancelled,
                                   publication=publication, reservation=reserved,
                                   on_published=on_published, preserve_alpha=preserve_alpha,
                                   runtime_snapshot=runtime_snapshot, resize=resize, resources=resources,
                                   record_digest=record_digest)
        snapshot = runtime_snapshot or self.runtime.snapshot()

        def cancelled():
            return (snapshot.is_cancelled() or bool(is_cancelled and is_cancelled())
                    or not self.writer.is_current(reservation))

        @contextmanager
        def default_publication():
            with self.runtime.publication(snapshot) as current:
                yield current and not bool(is_cancelled and is_cancelled())

        resize_hold = None
        try:
            from ..core.render_codec import read_linear, validate_output
            if cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            rendered = None
            raw_png = None
            if artifact is not None:
                payload = Path(artifact.path).read_bytes()
                if hashlib.sha256(payload).hexdigest() != artifact.hash:
                    raise ValueError("历史产物校验失败")
                if artifact.kind == LINEAR_ARTIFACT:
                    rendered = read_linear(BytesIO(payload))
                elif not execution_allowed:
                    if ext != ".png" or artifact.media_type != "image/png":
                        return CoreResult.fail(ErrorCode.UNSUPPORTED_FORMAT, "error.tool.history_export_format")
                    raw_png = payload
                elif ext not in (".tif", ".tiff"):
                    from ..core.photo_engine import _src_from_pil
                    with Image.open(BytesIO(payload)) as image:
                        rendered = _src_from_pil(image)
                del payload
            if rendered is None and raw_png is None:
                if not execution_allowed:
                    return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.history_export_format")
                from ..core.workflow.validator import validate
                checked = validate(workflow, snapshot.registry)
                if not checked.success:
                    return checked
                executed = snapshot.executor.execute(engine_source, workflow, is_cancelled=cancelled)
                if not executed.success:
                    return executed
                rendered = executed.data
            if cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            if resize is not None and (resize.width is not None or resize.height is not None) and rendered is not None:
                from ..core.image_resize import apply_resize, plan_resize, resize_memory
                plan = plan_resize((rendered.pixels.shape[1], rendered.pixels.shape[0]), resize)
                changes_size = plan.content_size != plan.source_size or plan.crop_box is not None or any(plan.padding)
                if resources is not None and changes_size:
                    hold = resources.working_memory(resize_memory(plan) - plan.source_size[0] * plan.source_size[1] * 48, cancelled)
                    hold.__enter__()
                    resize_hold = hold
                rendered = apply_resize(rendered, plan)
            if raw_png is not None:
                with Image.open(BytesIO(raw_png)) as original:
                    if original.format != "PNG":
                        raise ValueError("历史产物格式不匹配")
                    size, has_alpha = original.size, "A" in original.getbands()
                def encode(path):
                    Path(path).write_bytes(raw_png)
            else:
                size = (rendered.pixels.shape[1], rendered.pixels.shape[0])
                has_alpha = rendered.alpha is not None
                if preserve_alpha and has_alpha and ext in (".jpg", ".jpeg", ".bmp"):
                    return CoreResult.fail(ErrorCode.UNSUPPORTED_FORMAT, "error.export.alpha_format")
                def encode(path):
                    self._engine.save_source(rendered, path, ext=ext, quality=request.quality)
            digest = ""
            def check_output(path):
                nonlocal digest
                validate_output(path, ext, size=size, has_alpha=has_alpha)
                if record_digest:
                    digest = hash_file(path, is_cancelled=cancelled)
            written = self.writer.write(
                reservation, ext=ext, overwrite=request.overwrite, encode=encode,
                validate=check_output,
                publication=publication or default_publication,
                on_published=on_published,
            )
            if not written.success:
                return written
            return CoreResult.ok(ExportResult(str(reservation[0]), media_type, written.data, size, digest))
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except ImportError as exc:
            return CoreResult.from_exception(exc, code=ErrorCode.UNSUPPORTED_FORMAT,
                                             message_key="error.export.codec_unavailable")
        except Exception as exc:
            logger.exception("ExportService.export failed")
            return CoreResult.from_exception(exc, code=ErrorCode.ARTIFACT_WRITE_FAILED,
                                             message_key="error.export.failed")
        finally:
            if resize_hold is not None:
                resize_hold.__exit__(None, None, None)

    @staticmethod
    def validate_request(request):
        from pathlib import Path
        if not isinstance(request.target_path, str) or not request.target_path:
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.export.no_target")
        if (request.ext is not None and not isinstance(request.ext, str)) or type(request.overwrite) is not bool:
            return CoreResult.fail(ErrorCode.PARAMETER_INVALID, "error.param.type")
        try:
            validate_quality(request.quality)
        except ValueError:
            return CoreResult.fail(ErrorCode.PARAMETER_INVALID, "error.param.range")
        ext = "." + (request.ext or Path(request.target_path).suffix).lower().lstrip(".")
        try:
            format_info = output_format(ext)
        except ValueError:
            return CoreResult.fail(ErrorCode.UNSUPPORTED_FORMAT, "error.export.format")
        return CoreResult.ok((ext, format_info.media_type))
