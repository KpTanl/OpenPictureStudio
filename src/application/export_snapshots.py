from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import CancelledError
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from threading import RLock

from PIL import Image, ImageOps

from ..core.contracts import WorkflowGraph
from ..core.contracts.result import CoreResult, ErrorCode, memory_error_key
from ..core.image_resize import ResizeOptions, apply_resize, plan_resize, resize_memory
from ..core.project.assets import validate_asset
from ..core.workflow.validator import validate
from .export_store import canonical
from .image_source_service import SourceIdentity
from .mask_assets import workflow_assets
from .output_writer import OperationCancellation
from .services import DisplayFrame, ExportRequest


@lru_cache(maxsize=1)
def _runtime_dependencies():
    # 进程内不变：已载入的软件包与原生模块
    from ..core import photo_engine
    versions = {"opencv-runtime": getattr(photo_engine.cv2, "__version__", None)}
    for package in ("numpy", "Pillow", "opencv-python", "tifffile", "rawpy", "mediapipe", "onnxruntime"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    native = getattr(photo_engine, "ops_accel", None)
    native_path = getattr(native, "__file__", None) if getattr(photo_engine, "_HAS_RUST_ACCEL", False) else None
    if native_path:
        from .services import hash_file
        versions["ops_accel"] = hash_file(native_path)
    return tuple(versions.items())


@dataclass(frozen=True, eq=False)
class ExportProtection:
    """固定输出的路径索引，发布目标须沿 OutputWriter 独占创建。"""

    paths: tuple[str, ...] = ()
    directories: tuple[str, ...] = ()
    path_keys: frozenset[str] | None = None
    directory_keys: frozenset[str] = frozenset()
    size_bytes: int = field(init=False)

    def __post_init__(self):
        groups = (self.paths, self.directories, self.path_keys, self.directory_keys)
        object.__setattr__(self, "size_bytes", sum(sys.getsizeof(group) + sum(sys.getsizeof(path)
            for path in group) for group in groups if group is not None) + 128)

    def prepare(self, is_cancelled=lambda: False):
        if self.path_keys is not None:
            return self
        def resolve(paths):
            keys = set()
            for path in paths:
                if is_cancelled():
                    raise CancelledError()
                keys.add(os.path.normcase(os.path.abspath(path)))
                keys.add(os.path.normcase(str(Path(path).resolve())))
            return frozenset(keys)
        return ExportProtection(self.paths, self.directories, resolve(self.paths), resolve(self.directories))

    def allows(self, target):
        key = os.path.normcase(os.path.abspath(target))
        return (key not in self.path_keys
                and not any(os.path.normcase(str(parent)) in self.directory_keys
                            for parent in (target, *target.parents)))


@dataclass(frozen=True)
class ExportSnapshot:
    id: str
    path: str
    source_hash: str
    stamp: tuple[int, int]
    workflow_json: str
    workflow_hash: str
    tool_generation: int
    protection: ExportProtection
    render_json: str = ""

    @property
    def workflow(self):
        import json
        return WorkflowGraph.from_dict(json.loads(self.workflow_json))

    @property
    def size_bytes(self):
        return sum(sys.getsizeof(value) for value in (
            self.path, self.workflow_json, self.render_json)) + 512


class FixedOutputService:
    def __init__(self, tools, sources, engine, exporter, target_guard):
        self.tools, self.sources, self.engine, self.exporter = tools, sources, engine, exporter
        self.target_guard = target_guard
        self._lock = RLock()
        self._snapshots = {}
        self._registrations = {}
        self._protections = {}
        self._metadata_bytes = 0
        self._live_protection = None

    def current_protection(self, captured, is_cancelled=lambda: False):
        with self._lock:
            previous = self._live_protection
            if (previous is not None and previous.paths == captured.paths
                    and previous.directories == captured.directories):
                return previous
        prepared = captured.prepare(is_cancelled)
        with self._lock:
            if not self._snapshots:
                raise CancelledError()
            previous = self._live_protection
            extra = prepared.size_bytes - (previous.size_bytes if previous is not None else 0)
            if self._metadata_bytes + extra > 64 * 1024 * 1024:
                raise ValueError("export_queue.snapshot_limit")
            self._live_protection = prepared
            self._metadata_bytes += extra
        return prepared

    def capture(self, path, workflow, *, source_hash="", protection=None, tool_generation=None, is_cancelled=lambda: False):
        if is_cancelled():
            raise CancelledError()
        target = Path(path).resolve()
        stat = target.stat()
        payload = workflow.canonical_json()
        generation = self.tools.generation if tool_generation is None else tool_generation
        protection = protection or ExportProtection()
        with self._lock:
            shared = next((value for value in self._protections
                           if value.paths == protection.paths and value.directories == protection.directories), None)
        protection = shared or protection.prepare(is_cancelled)
        if is_cancelled():
            raise CancelledError()
        snapshot = ExportSnapshot(uuid.uuid4().hex, str(target), source_hash, (stat.st_mtime_ns, stat.st_size),
            payload, workflow.workflow_hash(), generation, protection)
        return self.retain(snapshot)

    def retain(self, snapshot):
        with self._lock:
            current = self._snapshots.get(snapshot.id)
            if current is not None:
                if current != snapshot and not (not snapshot.render_json and
                        replace(current, source_hash=snapshot.source_hash, render_json="") == snapshot):
                    raise ValueError("export_queue.expired")
                self._registrations[snapshot.id] += 1
                return current
            protection = snapshot.protection
            shared = next((value for value in self._protections
                           if value.paths == protection.paths and value.directories == protection.directories), None)
            if shared is not None and protection is not shared:
                protection = shared
                snapshot = replace(snapshot, protection=shared)
            shared_size = protection.size_bytes if protection not in self._protections else 0
            if self._metadata_bytes + snapshot.size_bytes + shared_size > 64 * 1024 * 1024:
                raise ValueError("export_queue.snapshot_limit")
            self._snapshots[snapshot.id] = snapshot
            self._registrations[snapshot.id] = 1
            self._protections[protection] = self._protections.get(protection, 0) + 1
            self._metadata_bytes += snapshot.size_bytes + shared_size
            return snapshot

    def release(self, snapshot):
        with self._lock:
            count = self._registrations.get(snapshot.id, 0)
            if count > 1:
                self._registrations[snapshot.id] = count - 1
                return
            self._registrations.pop(snapshot.id, None)
            previous = self._snapshots.pop(snapshot.id, None)
            if previous is not None:
                self._metadata_bytes -= previous.size_bytes
                protection = previous.protection
                count = self._protections[protection] - 1
                if count:
                    self._protections[protection] = count
                else:
                    self._protections.pop(protection)
                    self._metadata_bytes -= protection.size_bytes
            if not self._snapshots and self._live_protection is not None:
                self._metadata_bytes -= self._live_protection.size_bytes
                self._live_protection = None

    @contextmanager
    def borrow(self, snapshot):
        retained = self.retain(snapshot)
        try:
            yield retained
        finally:
            self.release(retained)

    def reference_count(self, digests):
        with self._lock:
            graphs = tuple(snapshot.workflow for snapshot in self._snapshots.values())
        return sum(not self.tools.workflow_resource_digests(graph).isdisjoint(digests) for graph in graphs)

    def _current(self, snapshot):
        with self._lock:
            current = self._snapshots.get(snapshot.id) == snapshot
        return current and self.tools.is_current(snapshot.tool_generation)

    def render_contract(self, path):
        from ..core import photo_engine
        from ..utils.app_info import APP_VERSION
        decode = photo_engine.linear_proxy_decode_identity(path)
        if decode is None:
            raise ValueError("export_queue.decoder_unpinned")
        return canonical({"fixed_output": 1, "application": APP_VERSION, "decode": decode,
                          "dependencies": dict(_runtime_dependencies())})

    def prepare(self, snapshot, is_cancelled):
        with self._lock:
            current = self._snapshots.get(snapshot.id)
            if (current is not None and current.render_json and not snapshot.render_json
                    and (not snapshot.source_hash or snapshot.source_hash == current.source_hash)
                    and replace(current, source_hash=snapshot.source_hash, render_json="") == snapshot):
                snapshot = current
        if is_cancelled() or not self._current(snapshot):
            raise InterruptedError("error.cancelled")
        if snapshot.render_json:
            return snapshot
        identity = self.sources.identity(snapshot.path, is_cancelled=is_cancelled)
        if ((identity.mtime_ns, identity.size) != snapshot.stamp
                or snapshot.source_hash and identity.file_hash != snapshot.source_hash):
            raise ValueError("error.source.changed")
        updated = replace(snapshot, source_hash=identity.file_hash, render_json=self.render_contract(snapshot.path))
        runtime = self.tools.snapshot(require_resource_versions=True, is_cancelled=is_cancelled)
        checked = validate(updated.workflow, runtime.registry)
        if not checked.success:
            from ..core.contracts.result import CoreOperationError
            raise CoreOperationError(checked.error)
        for digest, asset in workflow_assets(updated.workflow).items():
            validate_asset(self.tools.mask_assets.path(digest), digest, asset["size"], is_cancelled)
        if not self._current(snapshot) or is_cancelled():
            raise InterruptedError("error.cancelled")
        with self._lock:
            current = self._snapshots.get(snapshot.id)
            if current == updated:
                return current
            if current != snapshot:
                raise InterruptedError("error.cancelled")
            extra = updated.size_bytes - current.size_bytes
            if self._metadata_bytes + extra > 64 * 1024 * 1024:
                raise ValueError("export_queue.snapshot_limit")
            self._snapshots[snapshot.id] = updated
            self._metadata_bytes += extra
        return updated

    def _target_allowed(self, snapshot, target):
        return (os.path.normcase(str(target)) != os.path.normcase(snapshot.path)
                and snapshot.protection.allows(target))

    @contextmanager
    def _render_source(self, snapshot, cancellation, *, max_side=None, hashed=False):
        """hashed 为真时沿用准备快照时的完整哈希，只复核文件标记；正式导出重新完整哈希。"""
        if not snapshot.render_json or self.render_contract(snapshot.path) != snapshot.render_json:
            raise ValueError("export_queue.render_changed")
        cancelled = lambda: cancellation.is_cancelled() or not self._current(snapshot)
        if hashed and snapshot.source_hash:
            identity = SourceIdentity(snapshot.path, snapshot.source_hash, *snapshot.stamp)
            if not self.sources.matches_file(identity):
                raise ValueError("error.source.changed")
        else:
            identity = self.sources.identity(snapshot.path, is_cancelled=cancelled)
            if identity.file_hash != snapshot.source_hash:
                raise ValueError("error.source.changed")
        runtime = self.tools.snapshot(require_resource_versions=True, is_cancelled=cancelled)
        checked = validate(snapshot.workflow, runtime.registry)
        if not checked.success:
            from ..core.contracts.result import CoreOperationError
            raise CoreOperationError(checked.error)
        with self.sources.borrow_linear(snapshot.path, identity=identity, max_side=max_side,
                                        is_cancelled=cancelled, priority=2) as lease:
            with self.sources.resources.working_memory(lease.value.pixels.nbytes * 4, cancelled):
                with self.sources.compute.borrow(cancelled, priority=2,
                        threads=max(self.sources.policy.operator_threads, runtime.required_threads(snapshot.workflow))):
                    if cancelled():
                        raise CancelledError()
                    yield lease.value, runtime, identity, cancelled

    def export(self, snapshot, target_path, preset, *, cancellation=None):
        cancellation = cancellation or OperationCancellation()
        try:
            planned = Path(os.path.abspath(target_path))
            with self.exporter.writer.reserve(target_path) as reservation:
                if reservation[0] != planned or not self._target_allowed(snapshot, reservation[0]):
                    return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.export.protected_target")
                with self.target_guard(reservation[0], is_cancelled=cancellation.is_cancelled) as allowed:
                    if not allowed:
                        return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.export.protected_target")
                with self._render_source(snapshot, cancellation) as (source, runtime, identity, cancelled):
                    @contextmanager
                    def publication():
                        if cancelled():
                            raise CancelledError()
                        if not self.sources.matches_file(identity):
                            raise ValueError("error.source.changed")
                        if (Path(target_path).resolve() != reservation[0]
                                or not self._target_allowed(snapshot, reservation[0])):
                            raise ValueError("error.export.protected_target")
                        with (self.target_guard(reservation[0], is_cancelled=cancelled) as allowed,
                              self.tools.publication(runtime) as available,
                              cancellation.publication() as active):
                            valid = available and active and not cancelled()
                            if valid and not allowed:
                                raise ValueError("error.export.protected_target")
                            yield valid
                    return self.exporter.export(source, snapshot.workflow,
                        ExportRequest(target_path, preset.format, preset.quality, False),
                        reservation=reservation, is_cancelled=cancelled, publication=publication,
                        on_published=cancellation.mark_published, runtime_snapshot=runtime,
                        resize=ResizeOptions(preset.width, preset.height, preset.mode), resources=self.sources.resources,
                        record_digest=True)
        except Exception as error:
            if isinstance(error, (CancelledError, InterruptedError)):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            if isinstance(error, MemoryError):
                return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(error))
            return CoreResult.from_exception(error, message_key=str(error) if isinstance(error, ValueError) else "error.export.failed")

    def preview(self, snapshot, preset, *, cancellation, max_dimension=1600, proxy=False, square=False):
        """proxy 为真且工作流全部支持代理、预设不缩放时，用 1600 线性代理渲染缩略图。"""
        memory = None
        try:
            if (max_dimension is not None and (type(max_dimension) is not int or max_dimension < 1)
                    or square and max_dimension is None):
                return CoreResult.fail(ErrorCode.PARAMETER_INVALID, "error.param.range")
            proxy = (proxy and preset.width is None and preset.height is None
                     and self.tools.supports_proxy(snapshot.workflow, canonical_inputs=True))
            with self._render_source(snapshot, cancellation, max_side=1600 if proxy else None,
                                     hashed=True) as (source, runtime, identity, cancelled):
                executed = self.tools.execute_preview(runtime, source, snapshot.workflow, is_cancelled=cancelled,
                    resources=self.sources.resources,
                    full_source=lambda abort: self.sources.borrow_linear(snapshot.path, identity=identity,
                                                                         is_cancelled=abort, priority=2))
                if not executed.success:
                    return executed
                rendered = executed.data
                plan = plan_resize((rendered.pixels.shape[1], rendered.pixels.shape[0]), ResizeOptions(preset.width, preset.height, preset.mode))
                changes_size = plan.content_size != plan.source_size or plan.crop_box is not None or any(plan.padding)
                extra = resize_memory(plan) - plan.source_size[0] * plan.source_size[1] * 48 if changes_size else 0
                with self.sources.resources.working_memory(extra, cancelled):
                    # 代理只用于不缩放的预设
                    resized = rendered if source.is_proxy else apply_resize(rendered, plan)
                    image = self.engine.source_to_pil(resized)
                    try:
                        if max_dimension is not None:
                            if square:
                                with self.sources.resources.working_memory(
                                        image.width * image.height * 4 + max_dimension ** 2 * 8, cancelled):
                                    fitted = ImageOps.fit(image, (max_dimension, max_dimension),
                                                          method=Image.Resampling.LANCZOS)
                                image.close()
                                image = fitted
                            else:
                                image.thumbnail((max_dimension, max_dimension))
                        if cancelled() or not self.sources.matches_file(identity):
                            raise InterruptedError("error.cancelled")
                        memory = self.sources.resources.hold_memory(image.width * image.height * 12, cancelled)
                        frame = DisplayFrame.from_image(image)
                        size = tuple(rendered.orig_size) if source.is_proxy else plan.output_size
                        result = CoreResult.ok((frame, size, memory))
                        memory = None
                        return result
                    finally:
                        image.close()
        except MemoryError as error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(error))
        except Exception as error:
            return CoreResult.from_exception(error, message_key="error.preview.failed")
        finally:
            if memory is not None:
                memory.close()
