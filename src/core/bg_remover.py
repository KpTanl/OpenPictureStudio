import logging
import time
from pathlib import Path
from threading import get_ident

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from .contracts.result import CoreError, CoreOperationError, ErrorCode

logger = logging.getLogger(__name__)


class BackgroundRemover:
    """
    负责背景移除的核心逻辑，支持颜色容差模式、AI 模式（插件化）以及边缘优化。
    """
    def __init__(self, *, model_path=None, execution_threads: int = 1, create_session=None):
        if type(execution_threads) is not int or execution_threads < 1:
            raise ValueError("计算线程数必须为正整数")
        self._execution_threads = execution_threads
        self._model_path = Path(model_path) if model_path is not None else None
        self._create_session = create_session
        self._session = None

    def close(self):
        self._session = None

    def remove_by_color(self, img: Image.Image, target_color: tuple, tolerance: int, *, sampling=None) -> Image.Image:
        """
        基于颜色容差抠图。
        使用 PIL + numpy 向量化运算替代逐像素循环，大幅提升处理速度。
        """
        img = img.convert("RGBA")
        arr = np.array(img)
        rgb = arr[:, :, :3].astype(np.float32)
        target = np.array(target_color[:3], dtype=np.float32)
        dist = np.linalg.norm(rgb - target, axis=2)
        mask = dist <= tolerance
        arr[mask] = [0, 0, 0, 0]
        if sampling is not None and sampling.active:
            sampling.apply_mask((~mask).astype(np.float32))
        return Image.fromarray(arr)

    def is_ai_available(self) -> bool:
        return self._model_path is not None and self._model_path.is_file()

    def remove_by_ai(self, img: Image.Image, *, sampling=None) -> Image.Image:
        """
        使用旧版 U2Net 算法进行 AI 抠图，返回 RGBA 透明背景图。
        """
        if not self.is_ai_available():
            raise CoreOperationError(CoreError(ErrorCode.TOOL_RESOURCE_MISSING,
                "error.tool.resource_missing", {"tool_id": "bg_remove", "resource_id": "model"}))

        if self._session is None:
            try:
                import onnxruntime
            except ImportError as error:
                raise CoreOperationError(CoreError(ErrorCode.TOOL_INCOMPATIBLE,
                    "models.runtime.missing", {"tool_id": "bg_remove", "runtime": "onnxruntime"})) from error

            started = time.perf_counter()
            if self._create_session is None:
                options = onnxruntime.SessionOptions()
                options.intra_op_num_threads = self._execution_threads
                options.inter_op_num_threads = 1
                self._session = onnxruntime.InferenceSession(str(self._model_path),
                    sess_options=options, providers=["CPUExecutionProvider"])
            else:
                self._session, _ = self._create_session(self._model_path, self._execution_threads)
            logger.debug("模型加载 tool=bg_remove backend=legacy.u2net.cpu thread=%s execution_threads=%s duration_ms=%.1f",
                         get_ident(), self._execution_threads, (time.perf_counter() - started) * 1000)
        img = ImageOps.exif_transpose(img)
        resized = img.convert("RGB").resize((320, 320), Image.Resampling.LANCZOS)
        pixels = np.array(resized)
        pixels = pixels / max(np.max(pixels), 1e-6)
        normalized = np.zeros((pixels.shape[0], pixels.shape[1], 3))
        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
        for channel in range(3):
            normalized[:, :, channel] = (pixels[:, :, channel] - mean[channel]) / std[channel]
        tensor = np.expand_dims(normalized.transpose((2, 0, 1)), 0).astype(np.float32)
        outputs = self._session.run(None, {self._session.get_inputs()[0].name: tensor})
        prediction = outputs[0][:, 0, :, :]
        high, low = np.max(prediction), np.min(prediction)
        prediction = np.squeeze((prediction - low) / (high - low))
        mask = Image.fromarray((prediction.clip(0, 1) * 255).astype("uint8"), mode="L")
        mask = mask.resize(img.size, Image.Resampling.LANCZOS)
        if sampling is not None and sampling.active:
            sampling.apply_mask(np.asarray(mask, dtype=np.float32) / np.float32(255))
        empty = Image.new("RGBA", img.size, 0)
        return Image.composite(img, empty, mask)

    def optimize_edges(self, img: Image.Image, radius: int = 2, feather: bool = True, *, sampling=None) -> Image.Image:
        """
        边缘优化：对 alpha 通道进行平滑处理，减少锯齿感。
        """
        if img.mode != 'RGBA':
            return img

        r, g, b, a = img.split()

        if feather:
            previous_alpha = np.asarray(a, dtype=np.float32) if sampling is not None and sampling.active else None
            a = a.filter(ImageFilter.GaussianBlur(radius=radius))
            a = a.point(lambda p: int(p * 1.2) if p > 0 else 0)
            a = a.point(lambda p: min(p, 255))
            if previous_alpha is not None:
                next_alpha = np.asarray(a, dtype=np.float32)
                sampling.transform_coverage(lambda coverage: np.clip(np.divide(
                    coverage * next_alpha, previous_alpha, out=np.zeros_like(coverage),
                    where=previous_alpha > 0), 0, 1))

        return Image.merge('RGBA', (r, g, b, a))
