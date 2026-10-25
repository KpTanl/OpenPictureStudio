from __future__ import annotations

import logging
from threading import Lock

from ..core.contracts.inference import CPU, DIRECTML, BackendStatus

logger = logging.getLogger(__name__)

AUTO = "auto"
_DIRECTML_PROVIDER = "DmlExecutionProvider"


class _SerialSession:
    """GPU 推理全局串行；其余调用转发给原会话。"""

    def __init__(self, session, lock):
        self._session = session
        self._lock = lock

    def run(self, *args, **kwargs):
        with self._lock:
            return self._session.run(*args, **kwargs)

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

    def set_preference(self, preference):
        if preference not in (AUTO, CPU):
            raise ValueError("推理偏好无效")
        with self._lock:
            self._preference, self._actual, self._reason = preference, None, ""

    def target(self):
        """下一次创建会话时请求的后端。"""
        if self._preference == CPU:
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
                session = ort.InferenceSession(str(path), options,
                                               providers=[_DIRECTML_PROVIDER, "CPUExecutionProvider"])
            except Exception:
                logger.exception("GPU 推理会话创建失败，改用 CPU：%s", path)
                reason = "inference.reason.gpu_failed"
            else:
                # 运行库在 GPU 不可用时会自行改用 CPU 且不报错，以实际后端为准。
                if session.get_providers()[0] == _DIRECTML_PROVIDER:
                    self._record(DIRECTML, "")
                    return _SerialSession(session, self._gpu), DIRECTML
                logger.warning("GPU 后端不可用，改用 CPU：%s", path)
                reason = "inference.reason.gpu_unavailable"
        return self._cpu_session(path, threads, reason)

    def create_cpu_session(self, path, threads):
        return self._cpu_session(path, threads, "")

    def _cpu_session(self, path, threads, reason):
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self._record(CPU, reason)
        return session, CPU

    def _record(self, backend, reason):
        with self._lock:
            self._actual, self._reason = backend, reason
