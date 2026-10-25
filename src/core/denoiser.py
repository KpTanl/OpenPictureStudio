"""Image denoise engine with basic filters and optional ONNX AI inference."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from threading import get_ident
from typing import Callable, Optional

from PIL import Image
from PIL import ImageEnhance, ImageFilter

from .contracts.result import CoreOperationError, CoreResult, ErrorCode
from ..utils.model_paths import get_project_model_root

logger = logging.getLogger(__name__)

try:
    import numpy as np  # type: ignore
except ImportError:
    np = None

try:
    import cv2  # type: ignore
except ImportError:
    cv2 = None


RECOMMENDED_AI_MODELS = {
    "nafnet_sidd_width32": {
        "name": "NAFNet SIDD width32 (Compact)",
        "filename": "NAFNet-SIDD-width32.onnx",
        "url": "https://huggingface.co/spaces/svjack/NAFNet/resolve/main/NAFNet-SIDD-width32.onnx",
        "size_mb": 18.0,
        "recommended_pc": "CPU 4 cores+, RAM 8 GB+, SSD recommended",
        "download_sources": [
            "https://huggingface.co/spaces/svjack/NAFNet/resolve/main/NAFNet-SIDD-width32.onnx",
            "https://huggingface.co/xinntao/NAFNet/resolve/main/NAFNet-SIDD-width32.onnx",
        ],
    },
    "nafnet_sidd_width64": {
        "name": "NAFNet SIDD width64 (High Quality)",
        "filename": "NAFNet-SIDD-width64.onnx",
        "url": "https://huggingface.co/xinntao/NAFNet/resolve/main/NAFNet-SIDD-width64.onnx",
        "size_mb": 69.0,
        "recommended_pc": "CPU 6 cores+, RAM 16 GB+, SSD strongly recommended",
        "download_sources": [
            "https://huggingface.co/xinntao/NAFNet/resolve/main/NAFNet-SIDD-width64.onnx",
            "https://huggingface.co/spaces/svjack/NAFNet/resolve/main/NAFNet-SIDD-width64.onnx",
        ],
    },
}

DEFAULT_AI_MODEL_ID = "nafnet_sidd_width32"
BASIC_DENOISE_VERSION = 3


def is_onnxruntime_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def is_numpy_available() -> bool:
    return np is not None


def is_ai_model_available(model_path: Optional[str]) -> bool:
    """True only if ONNX runtime is importable and the model file exists."""
    if not model_path or not is_onnxruntime_available():
        return False
    path_obj = Path(model_path)
    return path_obj.exists() and path_obj.is_file()


def get_plugin_root() -> Path:
    return get_project_model_root()


def get_model_storage_dir() -> Path:
    return get_plugin_root()


def get_recommended_model_path(model_id: str = DEFAULT_AI_MODEL_ID) -> Path:
    info = RECOMMENDED_AI_MODELS.get(model_id)
    if not info:
        info = RECOMMENDED_AI_MODELS[DEFAULT_AI_MODEL_ID]
    return get_model_storage_dir() / info["filename"]


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


@dataclass
class DenoiseConfig:
    mode: str = "basic"
    basic_method: str = "bilateral"
    luma_strength: int = 45
    chroma_strength: int = 35
    model_path: Optional[str] = None
    tile_size: int = 512
    tile_overlap: int = 24
    sharpen: int = 0
    basic_version: int = 2

    def normalized(self) -> "DenoiseConfig":
        mode = self.mode if self.mode in ("basic", "ai") else "basic"
        method = self.basic_method if self.basic_method in ("gaussian", "median", "bilateral") else "bilateral"
        if type(self.basic_version) is not int or self.basic_version not in (2, BASIC_DENOISE_VERSION):
            raise ValueError("不支持的基础降噪版本")

        return DenoiseConfig(
            mode=mode,
            basic_method=method,
            luma_strength=_clamp(self.luma_strength, 0, 100),
            chroma_strength=_clamp(self.chroma_strength, 0, 100),
            model_path=self.model_path,
            tile_size=max(64, int(self.tile_size)),
            tile_overlap=max(0, int(self.tile_overlap)),
            sharpen=_clamp(self.sharpen, 0, 100),
            basic_version=self.basic_version,
        )


class ImageDenoiser:
    """Image denoise processor supporting basic and ONNX AI modes."""

    def __init__(self, *, execution_threads: int = 1) -> None:
        if type(execution_threads) is not int or execution_threads < 1:
            raise ValueError("计算线程数必须为正整数")
        self._execution_threads = execution_threads
        self._session = None
        self._session_model_path: Optional[str] = None
        self._input_name: Optional[str] = None
        self._output_name: Optional[str] = None
        self._input_shape = None

    def close(self) -> None:
        self._session = None
        self._session_model_path = None
        self._input_name = None
        self._output_name = None
        self._input_shape = None

    def denoise(
        self,
        image: Image.Image,
        config: DenoiseConfig,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Image.Image:
        cfg = config.normalized()

        if cfg.mode == "ai":
            if is_ai_model_available(cfg.model_path):
                result = self.denoise_ai(
                    image=image,
                    model_path=cfg.model_path,
                    luma_strength=cfg.luma_strength,
                    chroma_strength=cfg.chroma_strength,
                    tile_size=cfg.tile_size,
                    tile_overlap=cfg.tile_overlap,
                    progress_callback=progress_callback,
                )
                return self.apply_sharpen(result, cfg.sharpen)
            logger.warning(
                "AI denoise unavailable (model=%s, onnxruntime=%s); "
                "falling back to basic '%s' denoise",
                cfg.model_path,
                is_onnxruntime_available(),
                cfg.basic_method,
            )

        if progress_callback:
            progress_callback(100)
        basic_filter = self._denoise_basic_v3 if cfg.basic_version == BASIC_DENOISE_VERSION else self.denoise_basic
        result = basic_filter(
            image=image,
            method=cfg.basic_method,
            luma_strength=cfg.luma_strength,
            chroma_strength=cfg.chroma_strength,
        )
        return self.apply_sharpen(result, cfg.sharpen)

    @staticmethod
    def apply_sharpen(image: Image.Image, strength: int) -> Image.Image:
        if strength <= 0:
            return image
        return ImageEnhance.Sharpness(image).enhance(1.0 + strength / 100.0)

    def denoise_basic(
        self,
        image: Image.Image,
        method: str = "bilateral",
        luma_strength: int = 45,
        chroma_strength: int = 35,
    ) -> Image.Image:
        """Denoise image with classic filters and independent luma/chroma controls."""
        if cv2 is None or np is None:
            return self._denoise_basic_pillow_fallback(
                image=image,
                method=method,
                luma_strength=luma_strength,
                chroma_strength=chroma_strength,
            )

        rgb = np.array(image.convert("RGB"), dtype=np.uint8)
        ycrcb = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb)

        y = ycrcb[:, :, 0]
        cr = ycrcb[:, :, 1]
        cb = ycrcb[:, :, 2]

        y_d = self._denoise_channel(y, method, _clamp(luma_strength, 0, 100), is_luma=True)
        cr_d = self._denoise_channel(cr, method, _clamp(chroma_strength, 0, 100), is_luma=False)
        cb_d = self._denoise_channel(cb, method, _clamp(chroma_strength, 0, 100), is_luma=False)

        merged = cv2.merge((y_d, cr_d, cb_d))
        result = cv2.cvtColor(merged, cv2.COLOR_YCrCb2RGB)
        return Image.fromarray(result, mode="RGB")

    def _denoise_basic_v3(
        self,
        image: Image.Image,
        method: str,
        luma_strength: int,
        chroma_strength: int,
    ) -> Image.Image:
        if not (luma_strength or chroma_strength):
            return image.convert("RGB")
        if cv2 is None or np is None:
            raise CoreOperationError(CoreResult.fail(
                ErrorCode.TOOL_INCOMPATIBLE, "denoise.backend_unavailable").error)

        with image.convert("RGB") as converted:
            rgb = np.asarray(converted, dtype=np.float32) / np.float32(255.0)
        ycrcb = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb)
        del rgb
        for index, strength in enumerate((luma_strength, chroma_strength, chroma_strength)):
            if strength:
                channel = np.ascontiguousarray(ycrcb[:, :, index])
                ycrcb[:, :, index] = self._denoise_channel_v3(channel, method, strength, index == 0)
        result = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2RGB)
        np.multiply(result, np.float32(255.0), out=result)
        np.rint(result, out=result)
        np.clip(result, 0, 255, out=result)
        return Image.fromarray(result.astype(np.uint8), mode="RGB")

    @staticmethod
    def _denoise_channel_v3(channel, method, strength, is_luma):
        amount = np.float32(strength / 100.0)
        if method == "gaussian":
            sigma = 0.35 + 0.65 * amount if is_luma else 0.5 + amount
            filtered = cv2.GaussianBlur(channel, (0, 0), sigmaX=float(sigma), sigmaY=float(sigma))
        elif method == "median":
            filtered = cv2.medianBlur(channel, 3 if is_luma else 5)
        else:
            sigma_color = (3 + 17 * amount if is_luma else 6 + 34 * amount) / 255.0
            filtered = cv2.bilateralFilter(channel, 5 if is_luma else 7,
                                           float(sigma_color), float(1 + 2 * amount))
        filtered -= channel
        filtered *= amount
        filtered += channel
        return filtered

    def denoise_ai(
        self,
        image: Image.Image,
        model_path: Optional[str],
        luma_strength: int,
        chroma_strength: int,
        tile_size: int = 512,
        tile_overlap: int = 24,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Image.Image:
        """Run ONNX denoise model with tiled inference for large images."""
        if np is None:
            raise ImportError("numpy is not installed")

        if not is_onnxruntime_available():
            raise ImportError("onnxruntime is not installed")

        if not model_path:
            raise FileNotFoundError("AI model path is not set")

        path_obj = Path(model_path)
        if not path_obj.exists() or not path_obj.is_file():
            raise FileNotFoundError(f"AI model not found: {model_path}")

        self._ensure_session(str(path_obj))

        original_rgb = np.array(image.convert("RGB"), dtype=np.uint8)
        image_fp32 = original_rgb.astype(np.float32) / 255.0

        denoised = self._run_tiled(
            image_fp32,
            tile_size=tile_size,
            tile_overlap=tile_overlap,
            progress_callback=progress_callback,
        )

        denoised_rgb = np.clip(np.round(denoised * 255.0), 0, 255).astype(np.uint8)
        blended = self._blend_luma_chroma(
            original_rgb,
            denoised_rgb,
            luma_alpha=_clamp(luma_strength, 0, 100) / 100.0,
            chroma_alpha=_clamp(chroma_strength, 0, 100) / 100.0,
        )
        return Image.fromarray(blended, mode="RGB")

    def _denoise_channel(self, channel: np.ndarray, method: str, strength: int, is_luma: bool) -> np.ndarray:
        if cv2 is None:
            return channel.copy()

        if strength <= 0:
            return channel.copy()

        if method == "gaussian":
            sigma = 0.2 + (strength / 100.0) * (4.8 if is_luma else 3.0)
            kernel = int(max(3, min(31, round(sigma * 6) | 1)))
            return cv2.GaussianBlur(channel, (kernel, kernel), sigmaX=sigma, sigmaY=sigma)

        if method == "median":
            kernel = 1 + int(strength / 10) * 2
            kernel = max(1, min(kernel, 11))
            if kernel <= 1:
                return channel.copy()
            return cv2.medianBlur(channel, kernel)

        diameter = max(3, min(15, 5 + int(strength / 20) * 2))
        sigma_color = 10 + strength * (2.8 if is_luma else 1.9)
        sigma_space = 8 + strength * (1.3 if is_luma else 0.9)
        return cv2.bilateralFilter(channel, diameter, sigma_color, sigma_space)

    def _denoise_basic_pillow_fallback(
        self,
        image: Image.Image,
        method: str,
        luma_strength: int,
        chroma_strength: int,
    ) -> Image.Image:
        """Fallback basic denoise when OpenCV is unavailable."""
        source = image.convert("RGB")

        if method == "gaussian":
            luma_radius = (max(0, luma_strength) / 100.0) * 2.2
            chroma_radius = (max(0, chroma_strength) / 100.0) * 1.6
            blur_radius = max(luma_radius, chroma_radius)
            if blur_radius <= 0.01:
                return source.copy()
            return source.filter(ImageFilter.GaussianBlur(radius=blur_radius))

        if method == "median":
            size = 3 + int(max(luma_strength, chroma_strength) / 25) * 2
            size = max(3, min(size, 9))
            return source.filter(ImageFilter.MedianFilter(size=size))

        # Bilateral fallback approximation: median + light gaussian blend.
        median_size = 3 + int(max(luma_strength, chroma_strength) / 30) * 2
        median_size = max(3, min(median_size, 7))
        med = source.filter(ImageFilter.MedianFilter(size=median_size))

        blur_radius = (max(0, luma_strength) / 100.0) * 1.0
        if blur_radius > 0.01:
            gauss = source.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        else:
            gauss = source

        alpha = max(0.0, min(1.0, (luma_strength + chroma_strength) / 200.0))
        return Image.blend(med, gauss, alpha)

    def _ensure_session(self, model_path: str) -> None:
        if self._session is not None and self._session_model_path == model_path:
            return

        import onnxruntime as ort

        started = time.perf_counter()
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = self._execution_threads
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            model_path,
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

        inputs = session.get_inputs()
        outputs = session.get_outputs()
        if not inputs:
            raise RuntimeError("Invalid ONNX model: no inputs")
        if not outputs:
            raise RuntimeError("Invalid ONNX model: no outputs")

        self._session = session
        self._session_model_path = model_path
        self._input_name = inputs[0].name
        self._output_name = outputs[0].name
        self._input_shape = tuple(inputs[0].shape)
        logger.debug("模型加载 tool=denoise backend=onnx thread=%s execution_threads=%s duration_ms=%.1f",
                     get_ident(), self._execution_threads, (time.perf_counter() - started) * 1000)

    def _fixed_input_hw(self) -> tuple[Optional[int], Optional[int]]:
        if not self._input_shape or len(self._input_shape) < 4:
            return None, None

        h = self._input_shape[2]
        w = self._input_shape[3]
        h_int = h if isinstance(h, int) and h > 0 else None
        w_int = w if isinstance(w, int) and w > 0 else None
        return h_int, w_int

    def _run_tiled(
        self,
        image_hwc: np.ndarray,
        tile_size: int,
        tile_overlap: int,
        progress_callback: Optional[Callable[[int], None]],
    ) -> np.ndarray:
        img_h, img_w, _ = image_hwc.shape
        fixed_h, fixed_w = self._fixed_input_hw()

        run_tile_size = tile_size
        if fixed_h and fixed_w:
            run_tile_size = min(run_tile_size, fixed_h, fixed_w)
        run_tile_size = max(64, run_tile_size)

        overlap = min(max(0, tile_overlap), max(0, run_tile_size // 4))
        stride = max(8, run_tile_size - overlap * 2)

        if img_h <= run_tile_size and img_w <= run_tile_size:
            if progress_callback:
                progress_callback(10)
            out = self._run_single_tile(image_hwc)
            if progress_callback:
                progress_callback(100)
            return out

        out_sum = np.zeros_like(image_hwc, dtype=np.float32)
        out_weight = np.zeros((img_h, img_w, 1), dtype=np.float32)

        y_positions = list(range(0, img_h, stride))
        x_positions = list(range(0, img_w, stride))
        total_tiles = max(1, len(y_positions) * len(x_positions))
        done = 0

        for y0 in y_positions:
            for x0 in x_positions:
                y1 = min(y0 + run_tile_size, img_h)
                x1 = min(x0 + run_tile_size, img_w)

                tile = image_hwc[y0:y1, x0:x1, :]
                out_tile = self._run_single_tile(tile)
                mask = self._build_blend_mask(
                    tile_h=out_tile.shape[0],
                    tile_w=out_tile.shape[1],
                    overlap=overlap,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    img_w=img_w,
                    img_h=img_h,
                )

                out_sum[y0:y1, x0:x1, :] += out_tile * mask
                out_weight[y0:y1, x0:x1, :] += mask

                done += 1
                if progress_callback:
                    progress_callback(int(done * 100 / total_tiles))

        return out_sum / np.maximum(out_weight, 1e-6)

    def _run_single_tile(self, tile_hwc: np.ndarray) -> np.ndarray:
        if self._session is None or self._input_name is None:
            raise RuntimeError("ONNX session is not initialized")

        tile_h, tile_w = tile_hwc.shape[0], tile_hwc.shape[1]
        fixed_h, fixed_w = self._fixed_input_hw()
        target_h = fixed_h or tile_h
        target_w = fixed_w or tile_w

        input_tile = tile_hwc
        if tile_h > target_h or tile_w > target_w:
            input_tile = input_tile[:target_h, :target_w, :]
            tile_h, tile_w = input_tile.shape[0], input_tile.shape[1]

        pad_h = max(0, target_h - tile_h)
        pad_w = max(0, target_w - tile_w)
        if pad_h or pad_w:
            input_tile = np.pad(input_tile, ((0, pad_h), (0, pad_w), (0, 0)), mode="reflect")

        input_nchw = np.transpose(input_tile, (2, 0, 1))[None, ...].astype(np.float32)

        if self._output_name:
            outputs = self._session.run([self._output_name], {self._input_name: input_nchw})
            pred = outputs[0]
        else:
            pred = self._session.run(None, {self._input_name: input_nchw})[0]

        if pred.ndim != 4:
            raise RuntimeError(f"Unsupported ONNX output shape: {pred.shape}")

        pred_hwc = np.transpose(pred[0], (1, 2, 0))
        if pred_hwc.shape[2] > 3:
            pred_hwc = pred_hwc[:, :, :3]

        if pred_hwc.max() > 1.5 or pred_hwc.min() < -0.2:
            pred_hwc = (pred_hwc + 1.0) / 2.0

        pred_hwc = np.clip(pred_hwc, 0.0, 1.0)
        return pred_hwc[:tile_h, :tile_w, :].astype(np.float32)

    def _build_blend_mask(
        self,
        tile_h: int,
        tile_w: int,
        overlap: int,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        img_w: int,
        img_h: int,
    ) -> np.ndarray:
        if overlap <= 0:
            return np.ones((tile_h, tile_w, 1), dtype=np.float32)

        wx = np.ones(tile_w, dtype=np.float32)
        wy = np.ones(tile_h, dtype=np.float32)

        edge_x = min(overlap, tile_w)
        edge_y = min(overlap, tile_h)

        if edge_x > 0:
            ramp_x = np.linspace(0.0, 1.0, edge_x, dtype=np.float32)
            if x0 > 0:
                wx[:edge_x] = np.minimum(wx[:edge_x], ramp_x)
            if x1 < img_w:
                wx[-edge_x:] = np.minimum(wx[-edge_x:], ramp_x[::-1])

        if edge_y > 0:
            ramp_y = np.linspace(0.0, 1.0, edge_y, dtype=np.float32)
            if y0 > 0:
                wy[:edge_y] = np.minimum(wy[:edge_y], ramp_y)
            if y1 < img_h:
                wy[-edge_y:] = np.minimum(wy[-edge_y:], ramp_y[::-1])

        mask = np.outer(wy, wx).astype(np.float32)
        mask = np.maximum(mask, 1e-3)
        return mask[:, :, None]

    @staticmethod
    def _blend_luma_chroma(
        original_rgb: np.ndarray,
        denoised_rgb: np.ndarray,
        luma_alpha: float,
        chroma_alpha: float,
    ) -> np.ndarray:
        luma_alpha = max(0.0, min(1.0, float(luma_alpha)))
        chroma_alpha = max(0.0, min(1.0, float(chroma_alpha)))

        if luma_alpha <= 0 and chroma_alpha <= 0:
            return original_rgb.copy()

        if cv2 is None:
            alpha = max(0.0, min(1.0, (luma_alpha + chroma_alpha) * 0.5))
            mixed = original_rgb.astype(np.float32) * (1.0 - alpha) + denoised_rgb.astype(np.float32) * alpha
            return np.clip(mixed, 0, 255).astype(np.uint8)

        original_ycrcb = cv2.cvtColor(original_rgb, cv2.COLOR_RGB2YCrCb).astype(np.float32)
        denoised_ycrcb = cv2.cvtColor(denoised_rgb, cv2.COLOR_RGB2YCrCb).astype(np.float32)

        mixed = np.empty_like(original_ycrcb)
        mixed[:, :, 0] = original_ycrcb[:, :, 0] * (1.0 - luma_alpha) + denoised_ycrcb[:, :, 0] * luma_alpha
        mixed[:, :, 1] = original_ycrcb[:, :, 1] * (1.0 - chroma_alpha) + denoised_ycrcb[:, :, 1] * chroma_alpha
        mixed[:, :, 2] = original_ycrcb[:, :, 2] * (1.0 - chroma_alpha) + denoised_ycrcb[:, :, 2] * chroma_alpha

        mixed = np.clip(mixed, 0, 255).astype(np.uint8)
        return cv2.cvtColor(mixed, cv2.COLOR_YCrCb2RGB)
