import logging
import time
from threading import get_ident

import numpy as np
from PIL import Image

from ..contracts.inference import CPU

logger = logging.getLogger(__name__)


class U2NetMask:
    def __init__(self, model_path, *, execution_threads: int = 1, create_session=None):
        if type(execution_threads) is not int or execution_threads < 1:
            raise ValueError("计算线程数必须为正整数")
        self._model_path = model_path
        self._execution_threads = execution_threads
        self._create_session = create_session
        self._session = None
        self.backend = CPU
        self._ensure_session()

    def _ensure_session(self):
        if self._session is not None:
            return
        started = time.perf_counter()
        if self._create_session is None:
            import onnxruntime
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = self._execution_threads
            options.inter_op_num_threads = 1
            self._session = onnxruntime.InferenceSession(
                self._model_path, sess_options=options, providers=["CPUExecutionProvider"])
        else:
            self._session, self.backend = self._create_session(self._model_path, self._execution_threads)
        logger.debug("模型加载 tool=bg_remove backend=onnx.u2net.%s thread=%s execution_threads=%s duration_ms=%.1f",
                     self.backend, get_ident(), self._execution_threads, (time.perf_counter() - started) * 1000)

    def close(self):
        self._session = None

    def __call__(self, image):
        mask = self.weights(image)
        return Image.fromarray(np.rint(mask * 255).astype(np.uint8)).resize(image.size, Image.Resampling.LANCZOS)

    def weights(self, image):
        self._ensure_session()
        rgb = np.asarray(image.convert("RGB").resize((320, 320), Image.Resampling.LANCZOS), dtype=np.float32)
        rgb /= max(float(rgb.max()), 1.0)
        rgb = (rgb - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array([0.229, 0.224, 0.225], dtype=np.float32)
        tensor = rgb.transpose(2, 0, 1)[None, ...]
        result = self._session.run(None, {self._session.get_inputs()[0].name: tensor})[0][0, 0]
        if result.ndim != 2 or not np.isfinite(result).all():
            raise ValueError("抠图模型返回无效蒙版")
        low, high = float(result.min()), float(result.max())
        mask = (result - low) / (high - low) if high > low else np.clip(result, 0, 1)
        return mask
