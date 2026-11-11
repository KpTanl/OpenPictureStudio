from __future__ import annotations

from collections import OrderedDict, deque
from concurrent.futures import CancelledError
from contextlib import contextmanager
from dataclasses import dataclass, replace
from threading import Condition, Thread
from time import monotonic
import logging
import os
from weakref import WeakSet

from ..core.contracts.execution import CPU_BACKEND_IDENTITY, current_execution
from ..core.contracts.gpu import GpuArtifact, GpuResident, GpuExecutionError, cpu_recovery, display_target
from .image_compute_settings import ImageComputeSettings, MIB
from .image_resources import queue_order

logger = logging.getLogger(__name__)


class _ResidentCache(OrderedDict):
    def __init__(self):
        super().__init__()
        self.allocations = {}
        self.paths = {}
        self.nbytes = 0

    def __setitem__(self, key, value):
        self.pop(key, None)
        allocation = value.carrier.allocation_id
        size, users = self.allocations.get(allocation, (value.carrier.nbytes, 0))
        self.allocations[allocation] = size, users + 1
        if not users:
            self.nbytes += size
        super().__setitem__(key, value)
        self.paths.setdefault(key[0], OrderedDict())[key] = None

    def pop(self, key, default=None):
        value = super().pop(key, default)
        if value is not default:
            allocation = value.carrier.allocation_id
            size, users = self.allocations[allocation]
            if users == 1:
                del self.allocations[allocation]
                self.nbytes -= size
            else:
                self.allocations[allocation] = size, users - 1
            path = self.paths[key[0]]
            del path[key]
            if not path:
                del self.paths[key[0]]
        return value

    def move_to_end(self, key, last=True):
        super().move_to_end(key, last=last)
        self.paths[key[0]].move_to_end(key, last=last)

    def clear(self):
        super().clear()
        self.allocations.clear()
        self.paths.clear()
        self.nbytes = 0


@dataclass(frozen=True)
class GpuStatus:
    state: str = "idle"
    tier: str = "C"
    name: str = ""
    backend: str = "CPU"
    device: str = ""
    reason: str = "not_probed"
    revision: str = ""
    generation: int = 0
    budget_bytes: int = 0
    allocated_bytes: int = 0
    cache_bytes: int = 0
    uploaded_bytes: int = 0
    readback_bytes: int = 0
    cpu_ms: float = 0.0
    gpu_ms: float = 0.0

    @property
    def identity(self):
        return ("gpu", self.revision, self.device + ":" + str(self.generation)) if self.state == "ready" else CPU_BACKEND_IDENTITY


class GpuBackend:
    def __init__(self, compute, resources, *, stages, settings=None, profile_provider=None):
        self.compute, self.resources = compute, resources
        self._stages = stages
        self.settings = settings or ImageComputeSettings()
        self._profile_provider = profile_provider
        self._condition = Condition()
        self._status = GpuStatus()
        self._device = None
        self._worker = None
        self._tasks = deque()
        self._closed = False
        self._generation = 0
        self._busy = False
        self._waiting = {}
        self._cache = _ResidentCache()
        self._failed = OrderedDict()
        self._window = ()
        self._window_generation = 0
        self._scratch_holds = []
        self._scratch_bytes = 0
        self._model_bytes = 0
        self._device_models = WeakSet()
        self._face_qualifications = {}
        self._profile = None
        self._session_lost = False
        self._geometry_ready = False
        self._color_ready = False
        self._source_ready = frozenset()
        self._source_reasons = ()
        self._blocks_ready = False
        self._implementations = frozenset((("source", "1"), ("export", "1")))
        from .gpu_tool_routes import GpuToolRoutes
        self.routes = GpuToolRoutes(self)

    def _submit(self, operation, *args):
        with self._condition:
            if self._closed:
                return
            self._tasks.append((operation, args))
            if self._worker is None:
                self._worker = Thread(target=self._manage, name="ops-gpu", daemon=True)
                self._worker.start()
            self._condition.notify_all()

    def _manage(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._tasks)
                    if self._closed:
                        return
                    operation, args = self._tasks.popleft()
                try:
                    operation(*args)
                except CancelledError:
                    pass
                except Exception:
                    logger.exception("GPU 后台任务失败")
        finally:
            self._retire()

    def tool_capable(self, implementation, settings):
        if implementation[0] == "color_adjust":
            from .gpu_color import capable
            return self._color_ready and capable(implementation, settings)
        from .gpu_geometry import capable
        return self._geometry_ready and capable(implementation, settings)

    def execute_tool(self, reference, source, settings, upstream_identity, cancelled, assets=None, context=None):
        if self.routes.supported(reference):
            return self.routes.execute(reference, source, settings, context, cancelled)
        if reference.tool_id == "color_adjust":
            from .gpu_color import GpuColor
            return GpuColor(self, assets).execute(reference, source, settings, upstream_identity, cancelled)
        from .gpu_geometry import GpuGeometry
        return GpuGeometry(self).execute(reference, source, settings, upstream_identity, cancelled)

    def resize(self, source, size, box, cancelled):
        from .gpu_geometry import GpuGeometry
        return GpuGeometry(self).resize(source, size, (source.gpu_image.resident.identity, size, box), cancelled, box)

    def face_capable(self, identity):
        required = {"single", "small", "multiple", "edge", "alpha", "crop", "rotation", "no_face"}
        with self._condition:
            return identity[-1] == self._status.identity and required <= self._face_qualifications.get(identity,set())

    def record_face_qualification(self, identity, case, passed):
        with self._condition:
            if identity[-1] != self._status.identity:
                raise GpuExecutionError("device_changed", "Face qualification belongs to another device")
            if passed:
                self._face_qualifications.setdefault(identity,set()).add(case)
            else:
                self._face_qualifications.pop(identity,None)
                self.routes.invalidate(identity)

    def capabilities(self):
        with self._condition:
            info = self._device.info() if self._device is not None else {}
            return {"backend_identity":self._status.identity,"geometry":self._geometry_ready,
                "source_inputs": tuple(sorted(self._source_ready)), "source_reasons": self._source_reasons,
                "source_blocks": self._blocks_ready,
                "color":self._color_ready,"face_analysis":tuple(identity for identity in self._face_qualifications
                    if self.face_capable(identity)), "tools": self.routes.pending(),
                "kernels": {key: info.get(key) for key in ("shader_compiler", "compiler_reason", "kernel_compilation_ms",
                    "kernel_first_submission_ms", "fxc_compilation_ms")}}

    def reset_models(self):
        with self._condition:
            if self._closed:
                return
            self._generation += 1
            self._face_qualifications.clear()
            self.routes.invalidate()
            self._cache.clear()
            self._failed.clear()
            self._status = GpuStatus(generation=self._generation)
            self._condition.notify_all()
        self._submit(self._restart)

    def configure(self, settings):
        with self._condition:
            if self._closed or settings == self.settings:
                return
            previous, self.settings = self.settings, settings
            self._generation += 1
            self._cache.clear()
            self._failed.clear()
            self._face_qualifications.clear()
            self.routes.invalidate()
            if self._device is None and not self._session_lost:
                self._status = GpuStatus(generation=self._generation)
            self._condition.notify_all()
        if settings.engine == "cpu":
            self._submit(self._retire)
        elif previous.engine == "cpu":
            self._submit(self._restart)
        else:
            self.start()
        self._submit(self._apply_limits, self._generation)

    def _apply_limits(self, generation):
        cancelled = lambda: self._closed or generation != self._generation
        with self.borrow(cancelled):
            if self._device is not None:
                budget = self._budget(self._device.info())
                self._trim(budget)
                self._device.configure(budget, self._idle_limit(budget))
                if self.settings.cache == "off":
                    self._device.trim()
                    self._release_scratch()

    def _restart(self):
        self._retire()
        self.start()

    def _retire(self):
        with self._condition:
            while self._busy:
                self._condition.wait(0.05)
            device, self._device = self._device, None
            self._geometry_ready = False
            self._color_ready = False
            self._source_ready = frozenset()
            self._source_reasons = ()
            self._blocks_ready = False
            self._cache.clear()
            if self._closed:
                state, reason = "disabled", "closed"
            elif self.settings.engine == "cpu":
                state, reason = "disabled", "cpu_selected"
            elif self._session_lost:
                state, reason = "lost", "device_lost"
            else:
                state, reason = "idle", "not_probed"
            self._status = GpuStatus(state=state, reason=reason, generation=self._generation)
        self.routes.clear()
        if device is not None:
            for model in tuple(self._device_models):
                model.close()
            device.close()
        self._release_scratch()

    def start(self):
        with self._condition:
            if self._closed or self._session_lost or self.settings.engine == "cpu" or self._status.state in ("probing", "ready", "lost", "unavailable"):
                return
            self._status = GpuStatus(state="probing", reason="probing", generation=self._generation)
            generation = self._generation
        self._submit(self._probe, generation)

    def _probe(self, generation):
        device = None
        cancelled = lambda: self._closed or generation != self._generation
        try:
            if cancelled():
                raise CancelledError()
            import ops_accel
            from .system_profile import detect_profile
            profile = self._profile_provider() if self._profile_provider is not None else detect_profile()
            self._profile = profile
            adapters = [item for item in ops_accel.gpu_adapters() if item["hardware"]]
            for item in adapters:
                known = next((adapter for adapter in profile.adapters if adapter.luid == item["luid"]), None)
                item["dedicated_bytes"] = max(item["dedicated_bytes"], known.dedicated_bytes if known else 0)
            adapters.sort(key=lambda item: (item["kind"] == "DiscreteGpu", item["dedicated_bytes"]), reverse=True)
            if not adapters:
                raise GpuExecutionError("no_adapter", "No hardware D3D12 adapter")
            candidate = adapters[0]
            available = candidate["process_budget"] or candidate["dedicated_bytes"]
            if not available:
                raise GpuExecutionError("budget_unavailable", "GPU process budget is unavailable")
            capacity = (min(profile.memory_bytes // 4, self.resources.policy.memory_bytes)
                        if candidate["kind"] == "IntegratedGpu" else candidate["dedicated_bytes"] or available)
            limit = min(capacity * 3 // 4, 8 * 1024 ** 3)
            budget = min(limit, int(available * 0.75))
            with self.borrow(cancelled, priority=2), self.host_memory(384 * MIB, cancelled):
                device = ops_accel.GpuDevice(candidate["index"], candidate["luid"], generation, budget)
                if cancelled():
                    raise CancelledError()
                qualification = device.qualify(min(8, self.compute.snapshot().thread_capacity),
                    lambda: self.compute.borrow(cancelled,
                        priority=2, threads=min(8, self.compute.snapshot().thread_capacity)))
                geometry_ready, geometry_reason = False, "device_qualification"
                color_ready, color_reason = False, "device_qualification"
                source_ready, source_reasons = frozenset(), ()
                blocks_ready, blocks_reason = False, "device_qualification"
                if qualification["max_lsb"] <= 1:
                    from .gpu_qualification import qualify_geometry, qualify_color
                    geometry_ready, geometry_reason = self._qualify_tool(device,qualify_geometry,cancelled)
                    color_ready, color_reason = self._qualify_tool(device,qualify_color,cancelled)
                    from .gpu_source_input import qualify_source
                    source_ready, source_reasons = qualify_source(device, self.compute, cancelled)
                    from .gpu_source_input import qualify_blocks
                    blocks_ready, blocks_reason = self._qualify_tool(device, qualify_blocks, cancelled)
            if not qualification["max_lsb"] <= 1:
                raise GpuExecutionError("self_check", "GPU qualification did not pass")
            device.trim()
            with self._condition:
                if self._closed or generation != self._generation or self.settings.engine == "cpu":
                    raise CancelledError()
                self._device = device
                self._geometry_ready = geometry_ready
                self._color_ready = color_ready
                self._source_ready, self._source_reasons = source_ready, source_reasons
                self._blocks_ready = blocks_ready
                info = device.info()
                compiler = info.get("shader_compiler", "fxc")
                self._status = GpuStatus("ready", "A" if candidate["kind"] == "DiscreteGpu" else "B",
                    candidate["name"], "D3D12 / "+compiler, candidate["luid"], "", ops_accel.GPU_REVISION+":"+compiler,
                    generation, limit, cpu_ms=qualification["cpu_ms"], gpu_ms=qualification["gpu_ms"])
                device = None
                self._condition.notify_all()
                logger.info("GPU 几何资格 ready=%s reason=%s", geometry_ready, geometry_reason)
                logger.info("GPU 调色资格 ready=%s reason=%s", color_ready, color_reason)
                logger.info("GPU 源输入资格 ready=%s reasons=%s", tuple(sorted(source_ready)), source_reasons)
                logger.info("GPU 源分块资格 ready=%s reason=%s", blocks_ready, blocks_reason)
                logger.info("GPU 内核 compiler=%s compile_ms=%s first_submission_ms=%s fxc_ms=%s reason=%s",
                    compiler, info.get("kernel_compilation_ms"), info.get("kernel_first_submission_ms"),
                    info.get("fxc_compilation_ms"), info.get("compiler_reason"))
        except Exception as reason:
            with self._condition:
                if generation == self._generation and not self._closed:
                    code = reason.code if isinstance(reason, GpuExecutionError) else str(reason.args[0]) if reason.args else "unavailable"
                    self._session_lost = code == "device_lost"
                    self._status = GpuStatus(state="idle" if isinstance(reason, MemoryError) else
                        "lost" if self._session_lost else "unavailable", reason=code, generation=generation)
            logger.info("GPU 探测结束 reason=%s", reason)
        finally:
            if device is not None:
                device.close()

    def _qualify_tool(self, device, qualify, cancelled):
        try:
            return qualify(device,self.compute,cancelled)
        except (CancelledError,InterruptedError):
            raise CancelledError() from None
        except Exception as reason:
            if device.info()["lost"]:
                raise
            device.trim()
            return False,str(reason)

    def snapshot(self):
        with self._condition:
            status, device = self._status, self._device
            if device is None:
                return status
            info = device.info()
            if info["lost"]:
                self._lost()
                status = self._status
            return replace(status, allocated_bytes=info["allocated_bytes"] + self._model_bytes, cache_bytes=self._cache.nbytes,
                uploaded_bytes=info["uploaded_bytes"], readback_bytes=info["readback_bytes"], budget_bytes=self._budget(info))

    @staticmethod
    def _path(path):
        try:
            stat = os.stat(path)
            return os.path.normcase(os.path.abspath(path)), stat.st_mtime_ns, stat.st_size
        except OSError:
            return path, 0, 0

    def family(self, path, workflow, spec):
        return self._path(path), workflow, spec.max_dimension, self._generation

    def hint(self, path, workflow, spec):
        if cpu_recovery.get() is not None or self.settings.engine == "cpu":
            return CPU_BACKEND_IDENTITY
        self.start()
        with self._condition:
            family = self.family(path, workflow.workflow_hash(), spec)
            return self._status.identity if self._failed_reason(family) is None else CPU_BACKEND_IDENTITY

    def select(self, source, graph, target, requirements, order=None, *, workflow_identity=None):
        self.start()
        spec = display_target.get()
        with self._condition:
            status = self._status
            if cpu_recovery.get() is not None:
                return CPU_BACKEND_IDENTITY, "cpu_recovery"
            if self.settings.engine == "cpu" or status.state != "ready":
                return CPU_BACKEND_IDENTITY, "cpu_selected" if self.settings.engine == "cpu" else status.reason
            if spec is None:
                return CPU_BACKEND_IDENTITY, "output_adapter_missing"
            if source.is_proxy and any(item.implementation[0] in ("ai_enhance", "id_photo")
                    or item.implementation[0] == "bg_remove" and any(dep[:2] == ("model", "model") for dep in item.dependencies)
                    for item in requirements):
                return CPU_BACKEND_IDENTITY, "model_requires_full_input"
            if not source.content_identity:
                return CPU_BACKEND_IDENTITY, "content_identity_missing"
            from .gpu_geometry import IMPLEMENTATIONS, target_plan
            from .gpu_color import IMPLEMENTATIONS as COLOR_IMPLEMENTATIONS, workflow_cost, working_memory
            registered = (self._implementations | (IMPLEMENTATIONS if self._geometry_ready else frozenset())
                          | (COLOR_IMPLEMENTATIONS if self._color_ready else frozenset()))
            if (source.patches or any("gpu" not in item.engines or not (
                    item.implementation in registered and all(dependency == ("input", "color_adjust.float") for dependency in item.dependencies)
                    or self.routes.supports(item.implementation, item.dependencies))
                    for item in requirements)):
                return CPU_BACKEND_IDENTITY, "stage_capability_missing"
            if (target.colorspace != "linear_srgb" or target.dtype != "float32" or target.white_point != "D65"
                    or target.gamut != "srgb" or target.alpha_mode != "straight"):
                return CPU_BACKEND_IDENTITY, "target_format_missing"
            family = self.family(source.source_path, workflow_identity or graph.workflow_hash(), spec)
            failure = self._failed_reason(family)
            if failure is not None:
                return CPU_BACKEND_IDENTITY, failure
            width, height = target.size
            try:
                steps = {}
                output_size, largest = target_plan(source, graph, order, steps)
                color_cost = workflow_cost(graph,largest)
            except ValueError:
                return CPU_BACKEND_IDENTITY, "target_limit"
            final_w, final_h = output_size
            out_w, out_h = spec.size_for(final_w, final_h)
            from ..core.contracts.gpu import ImageOutputSpec
            formal = isinstance(spec, ImageOutputSpec)
            if formal:
                largest = max(largest, out_w*out_h, out_w*final_h)
            pixels = width * height
            peak = max(pixels * 32, largest * (96 if formal else 64), working_memory(graph, steps))
            peak += out_w * out_h * (32 if formal else 8)
            tool_peak, tool_cost = self.routes.estimate(requirements, graph, steps)
            peak = max(peak, tool_peak)
            info = self._device.info()
            if info["lost"]:
                self._lost()
                return CPU_BACKEND_IDENTITY, "device_lost"
            estimate = status.gpu_ms * largest * max(1, len(requirements), color_cost/8, tool_cost) / 1600000
            if (largest * 16 > min(info["max_binding_size"], info["max_buffer_size"])
                    or not formal and ((final_w + out_w - 1) // out_w + 1) * ((final_h + out_h - 1) // out_h + 1) > 4096
                    or max(out_w, out_h) > 32768
                    or (out_w + 7) // 8 > info["max_workgroups"] or (out_h + 7) // 8 > info["max_workgroups"]):
                return CPU_BACKEND_IDENTITY, "target_limit"
            if status.tier == "B" and estimate > 500:
                return CPU_BACKEND_IDENTITY, "submission_limit"
            budget = self._budget(info)
            if peak > budget and self._cache:
                self._cache.clear()
                self._device.trim()
                budget = self._budget(self._device.info())
            if peak > budget:
                logger.debug("GPU 预算不足 target=%s working_bytes=%s budget_bytes=%s", target.size, peak, budget)
                return CPU_BACKEND_IDENTITY, "target_budget"
            return status.identity, "gpu_stage"

    def output(self, source, cancelled, resize=None):
        from ..core.image_resize import plan_resize
        from .gpu_enhancement import GpuEnhancement
        if source.gpu_image is None or resize is None or resize.width is None and resize.height is None:
            return source
        plan = plan_resize(source.pixels.shape[1::-1], resize)
        return GpuEnhancement(self, {}, None, cancelled).layout(source, source.gpu_image, plan, alpha=source.alpha is not None)

    def source_capable(self, source):
        spec = source.input_spec
        if spec is None:
            return False
        with self._condition:
            if self._status.state != "ready" or self.settings.engine == "cpu" or spec.qualification not in self._source_ready:
                return False
            from .gpu_source_input import source_peak
            width, height = spec.size
            out_width, out_height = source.pixels.shape[1::-1]
            info = self._device.info()
            return (not info["lost"] and width * height <= 100_000_000 and max(width, height) <= 32768
                and max(spec.nbytes, out_width * height * 16) <= min(info["max_binding_size"], info["max_buffer_size"])
                and 6 * max(width / out_width, height / out_height) <= 4096
                and source_peak(spec, (out_width, out_height)) <= self._budget(info))

    def prepare_source(self, source_input, metadata, context, cancelled):
        from .gpu_source_input import prepare
        return prepare(self, source_input, metadata, context, cancelled)

    def cached_source(self, metadata, context, cancelled):
        from .gpu_source_input import cached_source
        return cached_source(self, metadata, context, cancelled)

    @contextmanager
    def host_memory(self, size, cancelled):
        if cancelled():
            raise CancelledError()
        hold = self.resources.try_working_memory(size)
        if hold is None:
            raise MemoryError("GPU host memory is unavailable")
        try:
            yield hold
        finally:
            hold.close()

    def mask_asset(self, component, source, assets, cancelled):
        from ..core.photo_engine import RenderSource
        from ..core.contracts.execution import current_execution
        with assets.borrow(component["asset"], component["size"], cancelled) as weights:
            import numpy as np
            with self.resources.working_memory(weights.size*12,cancelled):
                with self.compute.borrow(cancelled, threads=1):
                    pixels = np.broadcast_to(weights[..., None], (*weights.shape, 3)).copy()
                    pixels.setflags(write=False)
                mask = RenderSource(pixels, None, "linear32", source.source_path, False,
                    tuple(component["size"]), "linear_srgb", content_identity=("mask-gray16", component["asset"]))
                context = current_execution.get()
                if context is None:
                    raise GpuExecutionError("mask_context", "GPU mask upload requires an execution context")
                return self.upload(mask, replace(context, content_identity=mask.content_identity,
                    target=replace(context.target, size=tuple(component["size"])), is_cancelled=cancelled))

    def interop_probe(self, directml_path=None, cancelled=lambda: False):
        with self.borrow(cancelled):
            if self._device is None:
                raise GpuExecutionError("unavailable", "GPU device is unavailable")
            with self.host_memory(4 * MIB, cancelled):
                try:
                    return self._device.interop_probe(directml_path)
                finally:
                    self._device.trim()
                    self._release_scratch()

    def _budget(self, info):
        accounted = info["allocated_bytes"] + self._model_bytes
        dynamic = max(0, info["process_budget"] * 9 // 10 - max(0, info["process_usage"] - accounted))
        return max(0, min(self._status.budget_bytes, dynamic) - self._model_bytes)

    def model_device(self):
        from .system_profile import detect_profile
        profile = self._profile or detect_profile()
        self._profile = profile
        adapters = [item for item in profile.adapters if item.directx12 and item.dxgi_index >= 0]
        selected = next((item for item in adapters if item.luid == self._status.device), None)
        selected = selected or max(adapters, key=lambda item: item.dedicated_bytes, default=None)
        return (selected.dxgi_index, selected.luid) if selected is not None else (0, "dxgi:0")

    def create_device_model(self, path, spec, runtime_path, directml_path, cancelled):
        import hashlib
        from .device_model_session import DeviceModelSession, DeviceTensorSession
        from ..core.contracts.gpu import DeviceTensorModelSpec
        execution = current_execution.get()
        priority = execution.priority if execution is not None else self.compute.scheduling()[0]
        with self.compute.borrow(cancelled, priority=priority, threads=1):
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    if cancelled():
                        raise CancelledError()
                    digest.update(chunk)
            identity = digest.hexdigest(), str(runtime_path), self._path(str(runtime_path)), self._path(str(directml_path))
        memory = path.stat().st_size * 4 + spec.tensor_bytes
        with self.borrow(cancelled, priority=priority):
            if self._device is None or self.snapshot().state != "ready":
                raise GpuExecutionError("model_device", "GPU model device is unavailable")
            if execution is not None and execution.backend_identity != self.snapshot().identity:
                raise GpuExecutionError("device_changed", "GPU model request belongs to a retired device")
            device = self._device
            budget = self._budget(device.info())
            self._cache.clear()
            device.trim()
            self._release_scratch()
            if device.info()["allocated_bytes"]+memory > budget or self._status.tier == "B" and self._host_available() < memory*2:
                self.routes.trim_models()
                device.trim()
                budget = self._budget(device.info())
            if device.info()["allocated_bytes"] + memory > budget:
                raise GpuExecutionError("memory_pressure", "GPU model exceeds device budget")
            device.configure(budget, self._idle_limit(budget))
            held = None
            try:
                if self._status.tier == "B":
                    held = self.resources.try_hold_memory(memory)
                with self.host_memory(path.stat().st_size * 2, cancelled):
                    if isinstance(spec, DeviceTensorModelSpec):
                        model = device.create_tensor_model(str(runtime_path), str(directml_path), str(path), spec.input_size,
                            [list(shape) for shape in spec.output_shapes], spec.channels, spec.layout == "nchw",
                            spec.input_colorspace == "srgb" and spec.source_colorspace == "linear_srgb", spec.scale, spec.bias)
                    else:
                        model = device.create_model(str(runtime_path), str(directml_path), str(path), spec.input_size,
                            spec.output_size, spec.channels, spec.layout == "nchw",
                            spec.input_colorspace == "srgb" and spec.source_colorspace == "linear_srgb",
                            spec.output_colorspace == "srgb" and spec.destination_colorspace == "linear_srgb", spec.scale, spec.bias)
                if cancelled():
                    model.close()
                    raise CancelledError()
                session_type = DeviceTensorSession if isinstance(spec, DeviceTensorModelSpec) else DeviceModelSession
                result = session_type(self, model, spec, identity, held)
                result._budget_bytes = path.stat().st_size * 4
                self._model_bytes += result._budget_bytes
                self._device_models.add(result)
                held = None
                return result
            except CancelledError:
                raise
            except Exception as reason:
                if execution is not None and execution.engine == "gpu":
                    self.failed(reason, self.family(execution.source_path, execution.workflow_identity, display_target.get()))
                code = reason.args[0] if reason.args else "model_execution"
                raise GpuExecutionError(str(code), str(reason)) from reason
            finally:
                if held is not None:
                    held.close()

    def release_device_model(self, session):
        with self._condition:
            self._model_bytes -= session._budget_bytes
            session._budget_bytes = 0
            self._device_models.discard(session)

    @contextmanager
    def model_operation(self, device, cancelled=lambda: False, *, memory_bytes=0, priority=0):
        with self.borrow(cancelled, priority=priority):
            charge = device == self._status.device and self._device is not None
            registered = False
            try:
                if charge:
                    info = self._device.info()
                    budget = self._budget(info)
                    if info["allocated_bytes"] + memory_bytes > budget:
                        self._cache.clear()
                        self._device.trim()
                        self._release_scratch()
                    if self._device.info()["allocated_bytes"] + memory_bytes > budget:
                        raise GpuExecutionError("memory_pressure", "GPU model exceeds device budget")
                    self._model_bytes += memory_bytes
                    registered = True
                    self._device.configure(max(0, budget - memory_bytes), 0)
                with self.host_memory(memory_bytes if charge and self._status.tier == "B" else 0, cancelled):
                    yield
            finally:
                if registered:
                    self._model_bytes -= memory_bytes

    def _native_call(self, operation, prepare=lambda: None):
        for attempt in range(2):
            try:
                prepare()
                return operation()
            except Exception as reason:
                pressure = isinstance(reason, MemoryError) or bool(reason.args and reason.args[0] == "memory_pressure")
                if attempt or not pressure:
                    raise
                self._cache.clear()
                self._device.trim()
                self._release_scratch()
                self.routes.trim_models()
                info = self._device.info()
                budget = self._budget(info)
                self._device.configure(budget, self._idle_limit(budget))

    def _idle_limit(self, budget):
        return 0 if self._status.tier == "B" or self.settings.cache == "off" else min(64 * MIB, budget // 16)

    def _failed_reason(self, family):
        failure = self._failed.get(family)
        if failure is None:
            return None
        reason, threshold = failure
        recovered = (self._host_available() >= threshold[1] if isinstance(threshold, tuple) else
                     threshold is not None and self._device is not None and self._budget(self._device.info()) >= threshold)
        if recovered:
            self._failed.pop(family)
            return None
        return reason

    def _failure(self, family, reason, *, retry_budget=None):
        if reason == "memory_pressure" and retry_budget is None and self._device is not None:
            budget = self._budget(self._device.info())
            retry_budget = budget + max(16 * MIB, budget // 10)
        elif reason == "system_memory_pressure":
            available = self._host_available()
            retry_budget = "system", available + max(16 * MIB, available // 10)
        self._failed[family] = reason, retry_budget
        self._failed.move_to_end(family)
        while len(self._failed) > 128:
            self._failed.popitem(last=False)

    def _host_available(self):
        usage = self.resources.usage()
        return max(0, self.resources.policy.memory_bytes - usage["bytes"] - usage["reserved_bytes"])

    @contextmanager
    def borrow(self, cancelled, *, priority=0):
        token = object()
        created = monotonic()
        with self.compute.suspend(cancelled):
            with self._condition:
                self._waiting[token] = priority, created
                try:
                    while self._busy or token != min(self._waiting,
                            key=lambda item: queue_order(*self._waiting[item], monotonic())):
                        if self._closed or cancelled():
                            raise CancelledError()
                        self._condition.wait(0.05)
                    if self._closed or cancelled():
                        raise CancelledError()
                    self._busy = True
                finally:
                    self._waiting.pop(token, None)
            try:
                logger.debug("GPU 准入 device=%s wait_ms=%.1f", self._status.device, (monotonic() - created) * 1000)
                yield
            finally:
                with self._condition:
                    self._busy = False
                    self._condition.notify_all()

    def operate(self, artifact, name, identity, size, operation, cancelled, *, view=False, working_bytes=None,
                input_identity=None):
        context = current_execution.get()
        priority = context.priority if context is not None else self.compute.scheduling()[0]
        incoming = artifact.resident.identity if input_identity is None else input_identity
        shared_key = self._stages.key("gpu.operation", (artifact.source_path, incoming, name,
            identity, tuple(size), artifact.backend_identity))
        key = artifact.source_path, "operation", name, shared_key[-1], artifact.backend_identity
        def load(abandoned):
            with self.borrow(abandoned, priority=getattr(abandoned, "priority", priority)):
                try:
                    if self.snapshot().identity != artifact.backend_identity:
                        raise GpuExecutionError("device_changed", "GPU selection changed", artifact.family)
                    device = self._device
                    budget = self._budget(device.info())
                    retain = self._trim(budget, artifact.source_path)
                    device.configure(budget, self._idle_limit(budget))
                    cached = self._cache.get(key)
                    if cached is not None:
                        self._cache.move_to_end(key)
                        return cached
                    hold = None
                    try:
                        if self._status.tier == "B" and not view:
                            hold = self.resources.try_hold_memory(size[0] * size[1] * 16)
                        working = working_bytes if working_bytes is not None else size[0] * size[1] * 32
                        with self.host_memory(working if self._status.tier == "B" else 0, abandoned):
                            before = device.info()
                            started = monotonic()
                            carrier = self._native_call(lambda: operation(device, abandoned))
                            after = device.info()
                            if self._status.tier == "B" and monotonic()-started > .5:
                                raise GpuExecutionError("submission_limit", "GPU operation exceeds the integrated-device limit", artifact.family)
                        resident = GpuResident(carrier, artifact.backend_identity,
                            artifact.resident.shared_memory if view else hold, shared_key)
                        hold = None
                        if retain:
                            self._cache[key] = resident
                            self._trim(budget)
                        logger.debug("GPU 算子 name=%s device=%s event=%s upload_bytes=%s readback_bytes=%s",
                            name, artifact.backend_identity[2], carrier.completion,
                            after["uploaded_bytes"] - before["uploaded_bytes"], after["readback_bytes"] - before["readback_bytes"])
                        return resident
                    finally:
                        if hold is not None:
                            hold.close()
                except (CancelledError, InterruptedError):
                    raise CancelledError() from None
                except Exception as reason:
                    self.failed(reason, artifact.family)
        lease = self._stages.acquire(shared_key, load, cancelled, priority, retain=False, optional=True, transient=True)
        try:
            return GpuArtifact(lease.value, self, artifact.family, artifact.source_path)
        finally:
            lease.close()

    def _linear_lease(self, artifact, bounds, cancelled):
        context = current_execution.get()
        if context is not None and context.engine == "gpu":
            with self._condition:
                self._failure(artifact.family, "host_boundary")
            raise GpuExecutionError("host_boundary", "GPU chain requested host pixels", artifact.family)
        if artifact.patches:
            artifact = self.resolve(artifact, cancelled, bounds)
            bounds = (0, 0, *artifact.resident.carrier.size)
        key = self._stages.key("gpu.linear_readback", (artifact.backend_identity, artifact.resident.identity, bounds))
        def load(abandoned):
            with self.borrow(abandoned, priority=getattr(abandoned, "priority", 0)):
                try:
                    if self.snapshot().identity != artifact.backend_identity:
                        raise GpuExecutionError("device_changed", "GPU selection changed", artifact.family)
                    left, top, right, bottom = bounds
                    width, height = right-left, bottom-top
                    if left < 0 or top < 0 or width <= 0 or height <= 0 or right > artifact.resident.carrier.size[0] or bottom > artifact.resident.carrier.size[1]:
                        raise ValueError("GPU 读取范围无效")
                    count = width * height * 16
                    device = self._device
                    budget = self._budget(device.info())
                    self._trim(budget)
                    device.configure(budget, self._idle_limit(budget))
                    def prepare():
                        if self._status.tier == "B" and count > self._scratch_bytes:
                            self._scratch_holds.append(self.resources.try_hold_memory(count-self._scratch_bytes))
                            self._scratch_bytes = count
                    with self.host_memory(count * (3 if self._status.tier == "B" else 2), abandoned):
                        result = self._native_call(lambda: device.read_region(artifact.resident.carrier,
                            left, top, width, height, abandoned), prepare)
                    result.setflags(write=False)
                    logger.debug("执行交接 stage=%s direction=gpu_to_cpu bounds=%s readback_bytes=%s",
                        context.stage_id if context is not None else "", bounds, count)
                    return result
                except (CancelledError, InterruptedError):
                    raise CancelledError() from None
                except Exception as reason:
                    self.failed(reason, artifact.family)
                finally:
                    self._finish_readback()
        priority = context.priority if context is not None else self.compute.scheduling()[0]
        return self._stages.acquire(key, load, cancelled, priority, retain=False)

    def read_region(self, artifact, bounds, cancelled):
        lease = self._linear_lease(artifact, bounds, cancelled)
        try:
            return lease.value
        finally:
            lease.close()

    def face_metadata(self, artifact, cancelled, *, final=False):
        width, height = artifact.resident.carrier.size
        if ((width > 2 or height > 60) and not (final is True and width == 486 and height <= 50)
                and not (final == "region" and width <= 8192 and height <= 8192)):
            raise GpuExecutionError("face_metadata", "Face metadata readback exceeds its boundary")
        with self.borrow(cancelled, priority=getattr(cancelled, "priority", self.compute.scheduling()[0])):
            try:
                if self.snapshot().identity != artifact.backend_identity:
                    raise GpuExecutionError("device_changed", "GPU face metadata belongs to another device")
                count = width*height*16
                budget = self._budget(self._device.info())
                self._trim(budget)
                self._device.configure(budget,self._idle_limit(budget))
                def prepare():
                    if self._status.tier == "B" and count > self._scratch_bytes:
                        self._scratch_holds.append(self.resources.try_hold_memory(count-self._scratch_bytes))
                        self._scratch_bytes = count
                with self.host_memory(count*2,cancelled):
                    result = self._native_call(lambda:self._device.read_region(artifact.resident.carrier,0,0,width,height,cancelled),prepare)
                result.setflags(write=False)
                logger.debug("GPU 人脸交付 boundary=%s readback_bytes=%s", "analysis" if final else "control", count)
                return result
            except (CancelledError, InterruptedError):
                raise CancelledError() from None
            except Exception as reason:
                self.failed(reason,artifact.family)
            finally:
                self._finish_readback()

    def face_input(self, artifact, bounds, size, cancelled):
        import numpy as np
        from .gpu_faces import FaceGeometry
        small = FaceGeometry(self, ("face.detection_input", bounds, size)).op(
            artifact, 0, size, cancelled, args=bounds)
        rgba = self.face_metadata(small, cancelled, final="region")
        return np.rint(rgba[..., :3] * 255).astype(np.uint8)

    def materialize(self, artifact, source, cancelled):
        from ..core.execution_memo import ResourceOwner
        width, height = artifact.resident.carrier.size
        lease = self._linear_lease(artifact, (0, 0, width, height), cancelled)
        try:
            rgba = lease.value
            result = replace(source, pixels=rgba[..., :3], alpha=None if source.alpha is None else rgba[..., 3],
                gpu_image=None, backend_identity=artifact.backend_identity, execution_ownership=ResourceOwner((lease,)))
            lease = None
            return result
        finally:
            if lease is not None:
                lease.close()

    def resolve(self, artifact, cancelled, bounds=None):
        if not artifact.patches and bounds is None:
            return artifact
        from ..core.photo_engine import RenderSource
        from ..core.contracts.gpu import DevicePixels
        from .gpu_geometry import GpuGeometry
        width, height = artifact.resident.carrier.size
        bounds = bounds or (0, 0, width, height)
        left, top, right, bottom = bounds
        base = replace(artifact, patches=())
        pixels = DevicePixels((height, width, 3))
        source = RenderSource(pixels, DevicePixels((height, width)), "linear32", artifact.source_path, False,
                              (width, height), "linear_srgb", gpu_image=base, backend_identity=base.backend_identity)
        geometry = GpuGeometry(self)
        key = artifact.backend_identity, artifact.resident.identity, bounds
        source = geometry.view(source, bounds, key, cancelled, formal_size=(right-left, bottom-top))
        for patch in artifact.patches:
            pl, pt, pr, pb = patch.bounds
            x0, y0, x1, y1 = max(left, pl), max(top, pt), min(right, pr), min(bottom, pb)
            if x0 >= x1 or y0 >= y1:
                continue
            patch_source = RenderSource(DevicePixels((pb-pt, pr-pl, 3)), DevicePixels((pb-pt, pr-pl)), "linear32",
                artifact.source_path, False, patch.reference_size, "linear_srgb", gpu_image=patch.artifact,
                backend_identity=patch.artifact.backend_identity)
            cropped = geometry.view(patch_source, (x0-pl, y0-pt, x1-pl, y1-pt), patch.identity, cancelled)
            source = geometry.compose(source, cropped.gpu_image, (x0-left, y0-top, x1-left, y1-top),
                (key, patch.identity), cancelled, has_alpha=patch.has_alpha)
            key = key, patch.identity, patch.has_alpha
        return source.gpu_image

    def _trim(self, budget, path=None):
        limit = self.settings.cache_bytes(budget)
        if self._cache.nbytes > limit or len(self._cache) > 4096:
            if not self._window:
                while self._cache and (self._cache.nbytes > limit or len(self._cache) > 4096):
                    self._cache.pop(next(iter(self._cache)))
            else:
                if any(source not in self._window for source in self._cache.paths):
                    for key in tuple(self._cache):
                        if self._cache.nbytes <= limit and len(self._cache) <= 4096:
                            break
                        if key[0] not in self._window:
                            self._cache.pop(key)
                for source in reversed(self._window):
                    entries = self._cache.paths.get(source)
                    while entries and (self._cache.nbytes > limit or len(self._cache) > 4096):
                        self._cache.pop(next(iter(entries)))
        if path is not None and self._window and path not in self._window:
            return False
        return limit > 0

    def upload(self, source, context):
        spec = display_target.get()
        family = self.family(source.source_path, context.workflow_identity, spec)
        key = self._stages.key("gpu.upload", self._input_key(source, context))
        def load(abandoned):
            return self._upload_resident(source, replace(context, is_cancelled=abandoned,
                priority=getattr(abandoned, "priority", context.priority)))
        lease = self._stages.acquire(key, load, context.is_cancelled, context.priority, retain=False, optional=True)
        try:
            return GpuArtifact(lease.value, self, family, source.source_path)
        finally:
            lease.close()

    @staticmethod
    def _input_key(source, context):
        target = replace(context.target, size=source.pixels.shape[1::-1], reference_size=source.orig_size,
            proxy=source.is_proxy, alpha=source.alpha is not None, colorspace=source.colorspace,
            dtype=str(source.pixels.dtype), white_point=source.white_point, gamut=source.gamut,
            color_pipeline=source.color_pipeline, output_adapter="gpu-rgba32")
        return (source.source_path, context.content_identity, source.result_identity or context.input_identity,
                source.backend_identity, target, context.backend_identity)

    def _upload_resident(self, source, context):
        spec = display_target.get()
        family = self.family(source.source_path, context.workflow_identity, spec)
        key = self._input_key(source, context)
        cancelled = context.is_cancelled
        window_generation = self._window_generation
        background = lambda: (context.priority() if callable(context.priority) else context.priority) >= 2
        aborted = lambda: cancelled() or (background() and window_generation != self._window_generation
                                         and source.source_path not in self._window)
        with self.borrow(aborted, priority=context.priority):
            try:
                device = self._device
                if device is None or self.snapshot().identity != context.backend_identity:
                    raise GpuExecutionError("device_changed", "GPU selection changed", family)
                info = device.info()
                budget = self._budget(info)
                if not background() and (not self._window or self._window[0] != source.source_path):
                    self.set_window((source.source_path, *(path for path in self._window if path != source.source_path))[:4])
                retain = self._trim(budget, source.source_path)
                device.configure(budget, self._idle_limit(budget))
                resident = self._cache.get(key)
                if resident is not None:
                    self._cache.move_to_end(key)
                    logger.debug("GPU 内容命中 source=%s bytes=%s", source.source_path, resident.carrier.nbytes)
                else:
                    shared = None
                    try:
                        size = source.pixels.shape[0] * source.pixels.shape[1] * 16
                        if self._status.tier == "B":
                            shared = self.resources.try_hold_memory(size)
                        with self.host_memory(size * 2, aborted):
                            import numpy as np
                            with self.compute.borrow(aborted, priority=context.priority, threads=1):
                                height, width = source.pixels.shape[:2]
                                packed = np.empty((height, width, 4), np.float32)
                                for top in range(0, height, 64):
                                    if aborted():
                                        raise CancelledError()
                                    stop = min(top + 64, height)
                                    packed[top:stop, :, :3] = source.pixels[top:stop]
                                    packed[top:stop, :, 3] = 1 if source.alpha is None else source.alpha[top:stop]
                                packed.setflags(write=False)
                            image = self._native_call(lambda: device.upload_packed(packed, aborted))
                        resident = GpuResident(image, context.backend_identity, shared, self._stages.key("gpu.input", key))
                        shared = None
                    finally:
                        if shared is not None:
                            shared.close()
                    if aborted():
                        raise CancelledError()
                    if retain and (not background() or source.source_path in self._window):
                        self._cache[key] = resident
                        self._trim(budget)
                return resident
            except CancelledError:
                raise
            except InterruptedError:
                raise CancelledError() from None
            except Exception as reason:
                self.failed(reason, family)

    def failed(self, reason, family):
        code = ("system_memory_pressure" if isinstance(reason, MemoryError) else reason.code if isinstance(reason, GpuExecutionError)
                else str(reason.args[0]) if reason.args else "execution_failed")
        with self._condition:
            self._failure(family, code)
            self._cache.clear()
            if code == "device_lost":
                self._lost()
        logger.debug("GPU 尝试结束 code=%s family=%s", code, family)
        if self._device is not None:
            self._device.trim()
        self._release_scratch()
        raise GpuExecutionError(code, str(reason), family) from reason

    def _lost(self):
        if self._closed or self._session_lost:
            return
        self._cache.clear()
        self._status = replace(self._status, state="lost", tier="C", reason="device_lost")
        self._session_lost = True
        self._submit(self._retire)

    def display(self, artifact, size, cancelled):
        if artifact.patches:
            artifact = self.resolve(artifact, cancelled)
        context = current_execution.get()
        priority = context.priority if context is not None else self.compute.scheduling()[0]
        key = self._stages.key("gpu.readback", (artifact.backend_identity, artifact.resident.identity, size))
        lease = self._stages.acquire(key, lambda abandoned: self._display(artifact, size, abandoned),
                                     cancelled, priority, retain=False, optional=True)
        try:
            return lease.value
        finally:
            lease.close()

    def _display(self, artifact, size, cancelled):
        started = monotonic()
        context = current_execution.get()
        priority = context.priority if context is not None else self.compute.scheduling()[0]
        priority = getattr(cancelled, "priority", priority)
        with self.borrow(cancelled, priority=priority):
            try:
                if self.snapshot().identity != artifact.backend_identity:
                    raise GpuExecutionError("device_changed", "GPU result belongs to a different device", artifact.family)
                device = self._device
                count = size[0] * size[1] * 4
                budget = self._budget(device.info())
                self._trim(budget)
                device.configure(budget, self._idle_limit(budget))
                def prepare():
                    if self._status.tier == "B" and count > self._scratch_bytes:
                        self._scratch_holds.append(self.resources.try_hold_memory(count - self._scratch_bytes))
                        self._scratch_bytes = count
                with self.host_memory(count if self._status.tier == "B" else 0, cancelled):
                    rgba = self._native_call(lambda: device.display(artifact.resident.carrier, *size, cancelled), prepare)
                rgba.setflags(write=False)
                logger.debug("GPU 显示完成 device=%s event=%s readback_bytes=%s duration_ms=%.1f",
                    artifact.backend_identity[2], device.info()["completed_event"], count, (monotonic() - started) * 1000)
                return rgba
            except CancelledError:
                raise
            except InterruptedError:
                raise CancelledError() from None
            except Exception as reason:
                self.failed(reason, artifact.family)
            finally:
                self._finish_readback()

    def _finish_readback(self):
        if self.settings.cache == "off":
            if self._device is not None:
                self._device.trim()
            self._release_scratch()

    def set_window(self, paths):
        with self._condition:
            paths = tuple(paths[:4])
            if paths == self._window:
                return
            self._window, self._window_generation = paths, self._window_generation + 1
            for key in tuple(self._cache):
                if key[0] not in paths:
                    self._cache.pop(key)

    def _release_scratch(self):
        holds, self._scratch_holds = self._scratch_holds, []
        self._scratch_bytes = 0
        for hold in holds:
            hold.close()

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            self._tasks.clear()
            self._cache.clear()
            self._status = GpuStatus(state="disabled", reason="closed", generation=self._generation)
            self._condition.notify_all()


