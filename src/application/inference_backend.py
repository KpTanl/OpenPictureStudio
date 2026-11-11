from __future__ import annotations

import logging
from pathlib import Path
from contextlib import nullcontext
from threading import Lock
from time import monotonic

from ..core.contracts.inference import CPU, DIRECTML, BackendStatus
from ..core.contracts.execution import current_execution

logger = logging.getLogger(__name__)

AUTO = "auto"
_DIRECTML_PROVIDER = "DmlExecutionProvider"


class _ObservedSession:

    def __init__(self, session, backend, lock=None, *, gpu=None, device=""):
        self._session = session
        self._lock = lock
        self._backend = backend
        self._gpu_coordinator, self._device = gpu, device

    def run(self, *args, **kwargs):
        execution = current_execution.get()
        if execution is not None and execution.engine == CPU and self._backend != CPU:
            raise ValueError("CPU 工具链不能使用 GPU 模型会话")
        started = monotonic()
        values = args[1] if len(args) > 1 else kwargs.get("input_feed", {})
        memory = sum(getattr(value, "nbytes", 0) for value in values.values()) * 4
        admission = (self._gpu_coordinator.model_operation(self._device,
            execution.is_cancelled if execution is not None else lambda: False, memory_bytes=memory,
            priority=execution.priority if execution is not None else 0) if self._gpu_coordinator is not None
            and self._backend == DIRECTML else self._lock if self._lock is not None else nullcontext())
        with admission:
            admitted = monotonic()
            failed = True
            try:
                result = self._session.run(*args, **kwargs)
                failed = False
                return result
            finally:
                logger.debug("模型调用完成 request=%s stage=%s engine=%s backend=%s device=%s wait_ms=%.1f duration_ms=%.1f failed=%s event=runtime_return",
                             execution.request_id if execution else "", execution.stage_id if execution else "",
                             execution.engine if execution else "auto", self._backend,
                             self._device or (execution.device if execution else self._backend),
                             (admitted - started) * 1000, (monotonic() - admitted) * 1000, failed)

    def get_inputs(self):
        return self._session.get_inputs()

    def get_outputs(self):
        return self._session.get_outputs()


class InferenceBackend:
    def __init__(self, preference=AUTO):
        if preference not in (AUTO, CPU):
            raise ValueError("推理偏好无效")
        self._preference = preference
        self._gpu = Lock()
        self._lock = Lock()
        self._actual = None
        self._reason = ""
        self.gpu = None
        self.image_compute_preference = "auto"
        self._device_runtime = None
        self.device_runtime_changed = lambda: None

    def device_runtime_paths(self):
        with self._lock:
            selected = self._device_runtime
        if selected is not None:
            return selected
        import onnxruntime
        directory = Path(onnxruntime.__file__).resolve().parent / "capi"
        return directory / "onnxruntime.dll", directory / "DirectML.dll"

    def device_runtime_identity(self):
        return (self._preference, self.image_compute_preference,
                tuple(self.gpu._path(str(path)) for path in self.device_runtime_paths()))

    def configure_device_runtime(self, runtime_path, directml_path):
        paths = tuple(Path(path).resolve() for path in (runtime_path, directml_path))
        if not all(path.is_file() for path in paths):
            raise ValueError("GPU 推理运行库不存在")
        with self._lock:
            if self._device_runtime == paths:
                return
            self._device_runtime = paths
        self.device_runtime_changed()
        if self.gpu is not None:
            self.gpu.reset_models()

    def set_preference(self, preference):
        if preference not in (AUTO, CPU):
            raise ValueError("推理偏好无效")
        with self._lock:
            self._preference, self._actual, self._reason = preference, None, ""

    def target(self):
        """下一次创建会话时请求的后端。"""
        execution = current_execution.get()
        if execution is not None:
            if execution.engine == CPU:
                return CPU
            from ..core.contracts.gpu import GpuExecutionError
            raise GpuExecutionError("model_capability", "GPU 推理需要显式设备张量适配器")
        if (self._preference == CPU or self.image_compute_preference == "cpu"
                or self.gpu is not None and self.gpu.settings.engine == "cpu"):
            return CPU
        try:
            import onnxruntime as ort
        except ImportError:
            return CPU
        return DIRECTML if _DIRECTML_PROVIDER in ort.get_available_providers() else CPU

    def status(self):
        with self._lock:
            actual, reason = self._actual, self._reason
        return BackendStatus(self._preference, actual or self.target(), reason)

    def create_session(self, path, threads):
        import onnxruntime as ort
        reason = ""
        if self.target() == DIRECTML:
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            options.enable_mem_pattern = False
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            try:
                device_index, device = self.gpu.model_device() if self.gpu is not None else (0, "dxgi:0")
                admission = self.gpu.model_operation(device, memory_bytes=Path(path).stat().st_size * 4) if self.gpu is not None else nullcontext()
                with admission:
                    session = ort.InferenceSession(str(path), options,
                        providers=[(_DIRECTML_PROVIDER, {"device_id": device_index}), "CPUExecutionProvider"])
            except Exception:
                logger.exception("GPU 推理会话创建失败，改用 CPU：%s", path)
                reason = "inference.reason.gpu_failed"
            else:
                # 运行库在 GPU 不可用时会自行改用 CPU 且不报错，以实际后端为准。
                if session.get_providers()[0] == _DIRECTML_PROVIDER:
                    self._record(DIRECTML, "")
                    return _ObservedSession(session, DIRECTML, self._gpu, gpu=self.gpu, device=device), DIRECTML
                logger.warning("GPU 后端不可用，改用 CPU：%s", path)
                reason = "inference.reason.gpu_unavailable"
        return self._cpu_session(path, threads, reason)

    def create_cpu_session(self, path, threads):
        return self._cpu_session(path, threads, "")

    def device_models_enabled(self, *, planning=False):
        execution = current_execution.get()
        return (self.gpu is not None and self._preference != CPU and self.image_compute_preference != CPU
                and self.gpu.settings.engine != CPU and (execution is None or execution.engine == "gpu"
                    or planning and not execution.force_cpu))

    def create_face_session(self, path, *, bundle=None, detection_spec=None, locator_path=None,
                            runtime_path=None, directml_path=None, cancelled=lambda: False):
        from .gpu_faces import GpuFaceSession
        from ..core.contracts.gpu import GpuExecutionError
        if not self.device_models_enabled():
            raise GpuExecutionError("model_engine", "GPU face analysis is unavailable for this engine")
        defaults = self.device_runtime_paths()
        return GpuFaceSession(self, path, bundle=bundle, detection_spec=detection_spec,
            locator_path=locator_path,runtime_path=runtime_path or defaults[0],directml_path=directml_path or defaults[1],cancelled=cancelled)

    def create_device_session(self, path, spec, *, runtime_path=None, directml_path=None, cancelled=lambda: False):
        from ..core.contracts.gpu import DeviceModelSpec, DeviceTensorModelSpec, GpuExecutionError
        if not isinstance(spec, (DeviceModelSpec, DeviceTensorModelSpec)) or not self.device_models_enabled():
            raise GpuExecutionError("model_capability", "GPU model adapter is unavailable")
        execution = current_execution.get()
        if execution is not None and execution.engine != "gpu":
            raise GpuExecutionError("model_engine", "CPU chain requires a CPU model session")
        if runtime_path is None or directml_path is None:
            defaults = self.device_runtime_paths()
            runtime_path = runtime_path or defaults[0]
            directml_path = directml_path or defaults[1]
        result = self.gpu.create_device_model(Path(path).resolve(), spec,
            Path(runtime_path).resolve(), Path(directml_path).resolve(), cancelled)
        result.enabled = self.device_models_enabled
        return result

    def _cpu_session(self, path, threads, reason):
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self._record(CPU, reason)
        return _ObservedSession(session, CPU), CPU

    def _record(self, backend, reason):
        with self._lock:
            self._actual, self._reason = backend, reason
        execution = current_execution.get()
        logger.debug("模型后端 request=%s engine=%s device=%s backend=%s reason=%s",
                     execution.request_id if execution else "", execution.engine if execution else "auto",
                     execution.device if execution else backend, backend, reason)
