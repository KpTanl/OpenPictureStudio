"""Local AI enhancement engine with optional on-demand plugins.

This module provides a single entry point for portrait enhancement,
super-resolution/deblur enhancement, and night denoise. It follows an
offline-first strategy:
- Feature entry is always available.
- Optional runtime/model dependencies are only needed for AI paths.
- If AI dependencies are missing, it falls back to classic processing.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from threading import get_ident
from typing import Callable, Optional

from PIL import Image, ImageEnhance, ImageFilter

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

from .denoiser import (
    DEFAULT_AI_MODEL_ID,
    DenoiseConfig,
    ImageDenoiser,
    RECOMMENDED_AI_MODELS,
    get_recommended_model_path,
    is_onnxruntime_available,
)


RECOMMENDED_DEBLUR_MODELS = {
    "nafnet_deblur_opencv_zoo_2025may": {
        "name": "NAFNet Deblur (OpenCV Zoo, ONNX)",
        "filename": "deblurring_nafnet_2025may.onnx",
        "url": "https://github.com/opencv/opencv_zoo/raw/main/models/deblurring_nafnet/deblurring_nafnet_2025may.onnx",
    },
}

DEFAULT_DEBLUR_MODEL_ID = "nafnet_deblur_opencv_zoo_2025may"


def is_numpy_available() -> bool:
    return np is not None


def get_plugin_root() -> Path:
    return get_project_model_root()


def get_model_storage_dir(scope: str) -> Path:
    _ = scope
    return get_plugin_root()


def get_recommended_deblur_model_path(model_id: str = DEFAULT_DEBLUR_MODEL_ID) -> Path:
    info = RECOMMENDED_DEBLUR_MODELS.get(model_id)
    if not info:
        info = RECOMMENDED_DEBLUR_MODELS[DEFAULT_DEBLUR_MODEL_ID]
    return get_model_storage_dir("deblur") / info["filename"]


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _notify_progress(callback: Optional[Callable[[int], None]], value: int) -> None:
    if callback:
        callback(_clamp_int(value, 0, 100))


@dataclass
class AIEnhanceConfig:
    mode: str = "portrait"
    strength: int = 60
    upscale_factor: int = 2
    prefer_ai: bool = True

    # Portrait sub-controls (0-100 each, -1 means use legacy 'strength')
    skin_smooth: int = -1
    skin_brighten: int = -1
    skin_rosy: int = -1
    sharpen: int = -1

    deblur_model_path: Optional[str] = None

    denoise_model_id: str = DEFAULT_AI_MODEL_ID
    denoise_model_path: Optional[str] = None

    tile_size: int = 512
    tile_overlap: int = 24

    def normalized(self) -> "AIEnhanceConfig":
        mode = self.mode if self.mode in ("portrait", "superres", "night") else "portrait"
        upscale_factor = self.upscale_factor if self.upscale_factor in (1, 2, 4) else 2

        return AIEnhanceConfig(
            mode=mode,
            strength=_clamp_int(self.strength, 0, 100),
            upscale_factor=upscale_factor,
            prefer_ai=bool(self.prefer_ai),
            skin_smooth=self.skin_smooth,
            skin_brighten=self.skin_brighten,
            skin_rosy=self.skin_rosy,
            sharpen=self.sharpen,
            deblur_model_path=self.deblur_model_path,
            denoise_model_id=self.denoise_model_id if self.denoise_model_id in RECOMMENDED_AI_MODELS else DEFAULT_AI_MODEL_ID,
            denoise_model_path=self.denoise_model_path,
            tile_size=max(64, int(self.tile_size)),
            tile_overlap=max(0, int(self.tile_overlap)),
        )


class AIEnhancer:
    """Offline AI enhancer with graceful fallback behavior."""

    # NAFNet-like deblur models downsample multiple times internally.
    # Keep dynamic inputs safely above tiny edge sizes and aligned to avoid
    # collapsing feature-map dimensions to zero in deep stages.
    _DEBLUR_DYNAMIC_MIN_EDGE = 16
    _DEBLUR_DYNAMIC_ALIGN = 8

    def __init__(self, *, detector_path=None, strict=False, execution_threads: int = 1) -> None:
        if type(execution_threads) is not int or execution_threads < 1:
            raise ValueError("计算线程数必须为正整数")
        self._detector_path = detector_path
        self._detector = None
        self._strict = strict
        self._execution_threads = execution_threads
        self._denoiser = ImageDenoiser(execution_threads=execution_threads)
        self._deblur_session = None
        self._deblur_session_model_path: Optional[str] = None
        self._deblur_input_name: Optional[str] = None
        self._deblur_output_name: Optional[str] = None
        self._deblur_input_shape = None

    def close(self) -> None:
        self._detector = None
        self._deblur_session = None
        self._deblur_session_model_path = None
        self._deblur_input_name = None
        self._deblur_output_name = None
        self._deblur_input_shape = None
        self._denoiser.close()

    def enhance(
        self,
        image: Image.Image,
        config: AIEnhanceConfig,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Image.Image:
        cfg = config.normalized()

        if cfg.mode == "superres":
            return self.enhance_superres(image, cfg, progress_callback=progress_callback)
        if cfg.mode == "night":
            return self.enhance_night(image, cfg, progress_callback=progress_callback)
        return self.enhance_portrait(image, cfg, progress_callback=progress_callback)

    def enhance_portrait(
        self,
        image: Image.Image,
        config: AIEnhanceConfig,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Image.Image:
        """Face-aware portrait enhancement without external model dependency."""
        _notify_progress(progress_callback, 5)

        rgb = image.convert("RGB")
        fallback_strength = _clamp_int(config.strength, 0, 100)

        smooth_val = config.skin_smooth if config.skin_smooth >= 0 else fallback_strength
        brighten_val = config.skin_brighten if config.skin_brighten >= 0 else 0
        rosy_val = config.skin_rosy if config.skin_rosy >= 0 else 0
        sharpen_val = config.sharpen if config.sharpen >= 0 else max(0, fallback_strength // 3)

        smooth_val = _clamp_int(smooth_val, 0, 100)
        brighten_val = _clamp_int(brighten_val, 0, 100)
        rosy_val = _clamp_int(rosy_val, 0, 100)
        sharpen_val = _clamp_int(sharpen_val, 0, 100)

        if np is None or cv2 is None:
            if self._strict:
                raise RuntimeError("人像处理依赖不可用")
            result = self._portrait_fallback_pillow(rgb, smooth_val, brighten_val, rosy_val, sharpen_val)
            _notify_progress(progress_callback, 100)
            return result

        np_img = np.array(rgb)
        faces = self._detect_faces(np_img)
        _notify_progress(progress_callback, 20)

        if not faces:
            result = self._portrait_fallback_pillow(rgb, smooth_val, brighten_val, rosy_val, sharpen_val)
            _notify_progress(progress_callback, 100)
            return result

        result = np_img.astype(np.float32)

        for idx, (x1, y1, x2, y2) in enumerate(faces, start=1):
            face_roi = np_img[y1:y2, x1:x2]
            if face_roi.size == 0:
                continue

            face_mask = self._build_face_mask(face_roi.shape[0], face_roi.shape[1])
            skin_mask = self._build_skin_mask(face_roi)
            blend_mask = np.clip(face_mask * skin_mask, 0.0, 1.0)
            blend_3c = np.repeat(blend_mask[:, :, None], 3, axis=2)

            processed = face_roi.copy()

            # 1) Skin smoothing (bilateral filter)
            if smooth_val > 0:
                t = smooth_val / 100.0
                smooth_alpha = 0.2 + t * 0.45      # max 0.65 (was 0.80)
                detail_restore = 0.1 + t * 0.12
                smooth = cv2.bilateralFilter(
                    processed,
                    d=0,
                    sigmaColor=18 + t * 55.0,      # max ~73 (was 175)
                    sigmaSpace=6 + t * 9.0,        # max ~15 (was 57)
                )
                processed = cv2.addWeighted(processed, 1.0 - smooth_alpha, smooth, smooth_alpha, 0)
                processed = cv2.addWeighted(
                    processed, 1.0 + detail_restore,
                    cv2.GaussianBlur(processed, (0, 0), 1.0), -detail_restore, 0,
                )

            # 2) Skin brightening (LAB L-channel boost — capped to avoid blow-out)
            if brighten_val > 0:
                lab = cv2.cvtColor(processed, cv2.COLOR_RGB2LAB).astype(np.float32)
                boost = 3.0 + (brighten_val / 100.0) * 14.0   # max ~17 (was 35)
                lab[:, :, 0] = np.clip(lab[:, :, 0] + boost, 0, 255)
                processed = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2RGB)

            # 3) Rosy/warm tone (gentle saturation + slight hue shift)
            if rosy_val > 0:
                hsv = cv2.cvtColor(processed, cv2.COLOR_RGB2HSV).astype(np.float32)
                sat_boost = 1.0 + (rosy_val / 100.0) * 0.22    # max +22% (was +40%)
                hsv[:, :, 1] = np.clip(hsv[:, :, 1] * sat_boost, 0, 255)
                red_shift = (rosy_val / 100.0) * 4.0
                hsv[:, :, 0] = np.clip(hsv[:, :, 0] - red_shift, 0, 179)
                processed = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)

            original = result[y1:y2, x1:x2]
            result[y1:y2, x1:x2] = processed.astype(np.float32) * blend_3c + original * (1.0 - blend_3c)

            if progress_callback:
                _notify_progress(progress_callback, 20 + int(50 * idx / max(1, len(faces))))

        final = Image.fromarray(np.clip(result, 0, 255).astype(np.uint8), mode="RGB")

        # 4) Sharpness / detail recovery (applied globally)
        if sharpen_val > 0:
            final = ImageEnhance.Sharpness(final).enhance(1.0 + sharpen_val / 120.0)

        _notify_progress(progress_callback, 100)
        return final

    def enhance_superres(
        self,
        image: Image.Image,
        config: AIEnhanceConfig,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Image.Image:
        """Upscale with optional AI deblur pre-pass if runtime/model is available."""
        _notify_progress(progress_callback, 5)

        source = image.convert("RGB")
        work = source
        strength = _clamp_int(config.strength, 0, 100)

        ai_deblur_path = config.deblur_model_path
        if not ai_deblur_path:
            ai_deblur_path = str(get_recommended_deblur_model_path())

        should_try_ai = (
            config.prefer_ai
            and is_numpy_available()
            and is_onnxruntime_available()
            and ai_deblur_path
            and Path(ai_deblur_path).is_file()
        )

        if self._strict and config.prefer_ai and not should_try_ai:
            raise RuntimeError("AI 模型或运行依赖不可用")
        if should_try_ai:
            deblurred = self._run_deblur_onnx(
                work,
                model_path=ai_deblur_path,
                tile_size=config.tile_size,
                tile_overlap=config.tile_overlap,
                progress_callback=(lambda p: _notify_progress(progress_callback, 10 + int(p * 0.45))),
            )
            work = deblurred
            _notify_progress(progress_callback, 60)
        else:
            # Keep a mild classic sharpen pass so mode is useful without plugins.
            blur_radius = 0.4 + strength / 180.0
            work = work.filter(ImageFilter.UnsharpMask(radius=blur_radius, percent=110 + strength, threshold=3))
            _notify_progress(progress_callback, 50)

        factor = config.upscale_factor if config.upscale_factor in (1, 2, 4) else 2
        if factor > 1:
            up_w = max(1, work.width * factor)
            up_h = max(1, work.height * factor)
            work = work.resize((up_w, up_h), Image.Resampling.LANCZOS)

        sharpen_radius = 0.6 + strength / 140.0
        sharpen_percent = 120 + int(strength * 1.2)
        result = work.filter(ImageFilter.UnsharpMask(radius=sharpen_radius, percent=sharpen_percent, threshold=2))

        _notify_progress(progress_callback, 100)
        return result

    def enhance_night(
        self,
        image: Image.Image,
        config: AIEnhanceConfig,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Image.Image:
        """Night enhancement using existing denoise engine (AI when available)."""
        _notify_progress(progress_callback, 5)

        denoiser = self._denoiser
        strength = _clamp_int(config.strength, 0, 100)

        model_path = config.denoise_model_path
        if not model_path:
            model_path = str(get_recommended_model_path(config.denoise_model_id))

        can_use_ai = (
            config.prefer_ai
            and is_numpy_available()
            and is_onnxruntime_available()
            and model_path
            and Path(model_path).is_file()
        )

        if self._strict and config.prefer_ai and not can_use_ai:
            raise RuntimeError("AI 模型或运行依赖不可用")
        if can_use_ai:
            denoise_cfg = DenoiseConfig(
                mode="ai",
                luma_strength=max(15, min(100, 40 + strength // 2)),
                chroma_strength=max(15, min(100, 45 + int(strength * 0.55))),
                model_path=model_path,
                tile_size=config.tile_size,
                tile_overlap=config.tile_overlap,
            )
            denoised = denoiser.denoise(
                image.convert("RGB"),
                denoise_cfg,
                progress_callback=(lambda p: _notify_progress(progress_callback, 10 + int(p * 0.8))),
            )
        else:
            denoise_cfg = DenoiseConfig(
                mode="basic",
                basic_method="bilateral",
                luma_strength=max(10, min(90, 35 + int(strength * 0.45))),
                chroma_strength=max(10, min(90, 30 + int(strength * 0.42))),
            )
            denoised = denoiser.denoise(image.convert("RGB"), denoise_cfg)
            _notify_progress(progress_callback, 80)

        # Recover a bit of local contrast and clarity after denoise.
        post = ImageEnhance.Contrast(denoised).enhance(1.02 + strength / 900.0)
        post = post.filter(ImageFilter.UnsharpMask(radius=1.0, percent=95 + strength, threshold=2))

        _notify_progress(progress_callback, 100)
        return post

    def _portrait_fallback_pillow(
        self, image: Image.Image,
        smooth_val: int, brighten_val: int, rosy_val: int, sharpen_val: int,
    ) -> Image.Image:
        out = image
        if smooth_val > 0:
            sigma = 0.5 + smooth_val / 130.0           # max ~1.27 (was 1.85)
            smooth = out.filter(ImageFilter.GaussianBlur(radius=sigma))
            alpha = 0.2 + (smooth_val / 100.0) * 0.35  # max 0.55 (was 0.70)
            out = Image.blend(out, smooth, alpha)
        if brighten_val > 0:
            out = ImageEnhance.Brightness(out).enhance(1.0 + brighten_val / 350.0)  # max +29% (was +50%)
        if rosy_val > 0:
            out = ImageEnhance.Color(out).enhance(1.0 + rosy_val / 220.0)
        if sharpen_val > 0:
            out = ImageEnhance.Sharpness(out).enhance(1.0 + sharpen_val / 120.0)
        return out

    def _detect_faces(self, image_rgb: "np.ndarray") -> list[tuple[int, int, int, int]]:
        if cv2 is None:
            return []

        gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
        created = self._detector is None
        if created:
            started = time.perf_counter()
            cascade_path = self._detector_path or os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            self._detector = cv2.CascadeClassifier(cascade_path)
        detector = self._detector
        if detector.empty():
            self._detector = None
            if self._strict:
                raise FileNotFoundError("人像检测资源不可用")
            return []
        if created:
            logger.debug("模型加载 tool=ai_enhance backend=haar thread=%s duration_ms=%.1f",
                         get_ident(), (time.perf_counter() - started) * 1000)

        h, w = image_rgb.shape[:2]
        min_w = max(30, w // 16)
        min_h = max(30, h // 16)
        faces = detector.detectMultiScale(
            gray,
            scaleFactor=1.08,
            minNeighbors=3,
            minSize=(min_w, min_h),
        )
        if len(faces) == 0:
            return []

        results: list[tuple[int, int, int, int]] = []
        for (x, y, fw, fh) in faces:
            pad_x = int(fw * 0.18)
            pad_y = int(fh * 0.2)
            x1 = max(0, x - pad_x)
            y1 = max(0, y - pad_y)
            x2 = min(w, x + fw + pad_x)
            y2 = min(h, y + fh + pad_y)
            if x2 - x1 > 20 and y2 - y1 > 20:
                results.append((x1, y1, x2, y2))

        return results

    def _build_face_mask(self, height: int, width: int) -> "np.ndarray":
        if np is None or cv2 is None:
            return np.ones((height, width), dtype=np.float32)  # type: ignore[arg-type]

        mask = np.zeros((height, width), dtype=np.float32)
        center = (width // 2, height // 2)
        axes = (max(1, int(width * 0.43)), max(1, int(height * 0.47)))
        cv2.ellipse(mask, center, axes, 0, 0, 360, color=1.0, thickness=-1)

        blur_k = max(7, ((min(width, height) // 8) | 1))
        mask = cv2.GaussianBlur(mask, (blur_k, blur_k), 0)
        return np.clip(mask, 0.0, 1.0)

    def _build_skin_mask(self, face_rgb: "np.ndarray") -> "np.ndarray":
        """Soft mask of skin-coloured pixels so eyes / brows / lips / hair stay sharp."""
        if np is None or cv2 is None:
            return np.ones(face_rgb.shape[:2], dtype=np.float32)  # type: ignore[arg-type]

        ycrcb = cv2.cvtColor(face_rgb, cv2.COLOR_RGB2YCrCb)
        skin = cv2.inRange(ycrcb, (0, 133, 77), (255, 173, 127))

        # Dilate slightly so we keep mid-tone transition pixels, then soft-blur the edges.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        skin = cv2.dilate(skin, kernel, iterations=1)
        skin = cv2.GaussianBlur(skin, (0, 0), sigmaX=3.0)

        soft = skin.astype(np.float32) / 255.0
        # Floor at 0.15 so non-detected skin still gets a faint pass — avoids a hard cut-out
        # if the YCrCb gate is overly strict on shadowed cheeks.
        return np.clip(soft * 0.85 + 0.15, 0.0, 1.0)

    # ----------------------------- Deblur ONNX (same-size tiled inference)
    def _ensure_deblur_session(self, model_path: str) -> None:
        if self._deblur_session is not None and self._deblur_session_model_path == model_path:
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
            raise RuntimeError("Invalid deblur ONNX model: no inputs")
        if not outputs:
            raise RuntimeError("Invalid deblur ONNX model: no outputs")

        self._deblur_session = session
        self._deblur_session_model_path = model_path
        self._deblur_input_name = inputs[0].name
        self._deblur_output_name = outputs[0].name
        self._deblur_input_shape = tuple(inputs[0].shape)
        logger.debug("模型加载 tool=ai_enhance backend=onnx.deblur thread=%s execution_threads=%s duration_ms=%.1f",
                     get_ident(), self._execution_threads, (time.perf_counter() - started) * 1000)

    def _deblur_fixed_input_hw(self) -> tuple[Optional[int], Optional[int]]:
        if not self._deblur_input_shape or len(self._deblur_input_shape) < 4:
            return None, None

        h = self._deblur_input_shape[2]
        w = self._deblur_input_shape[3]
        h_int = h if isinstance(h, int) and h > 0 else None
        w_int = w if isinstance(w, int) and w > 0 else None
        return h_int, w_int

    def _run_deblur_onnx(
        self,
        image: Image.Image,
        model_path: str,
        tile_size: int,
        tile_overlap: int,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Image.Image:
        if np is None:
            raise ImportError("numpy is not installed")
        if not is_onnxruntime_available():
            raise ImportError("onnxruntime is not installed")

        self._ensure_deblur_session(model_path)

        source = np.array(image.convert("RGB"), dtype=np.uint8)
        source_fp32 = source.astype(np.float32) / 255.0

        output_fp32 = self._run_deblur_tiled(
            source_fp32,
            tile_size=tile_size,
            tile_overlap=tile_overlap,
            progress_callback=progress_callback,
        )
        output = np.clip(np.round(output_fp32 * 255.0), 0, 255).astype(np.uint8)
        return Image.fromarray(output, mode="RGB")

    def _run_deblur_tiled(
        self,
        image_hwc: "np.ndarray",
        tile_size: int,
        tile_overlap: int,
        progress_callback: Optional[Callable[[int], None]],
    ) -> "np.ndarray":
        img_h, img_w, _ = image_hwc.shape
        fixed_h, fixed_w = self._deblur_fixed_input_hw()

        run_tile_size = tile_size
        if fixed_h and fixed_w:
            run_tile_size = min(run_tile_size, fixed_h, fixed_w)
        run_tile_size = max(64, run_tile_size)

        overlap = min(max(0, tile_overlap), max(0, run_tile_size // 4))
        stride = max(8, run_tile_size - overlap * 2)

        if img_h <= run_tile_size and img_w <= run_tile_size:
            _notify_progress(progress_callback, 10)
            out = self._run_single_deblur_tile(image_hwc)
            _notify_progress(progress_callback, 100)
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
                out_tile = self._run_single_deblur_tile(tile)
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
                _notify_progress(progress_callback, int(done * 100 / total_tiles))

        return out_sum / np.maximum(out_weight, 1e-6)

    def _run_single_deblur_tile(self, tile_hwc: "np.ndarray") -> "np.ndarray":
        if self._deblur_session is None or self._deblur_input_name is None:
            raise RuntimeError("Deblur ONNX session is not initialized")

        tile_h, tile_w = tile_hwc.shape[0], tile_hwc.shape[1]
        if tile_h <= 0 or tile_w <= 0:
            raise RuntimeError(f"Invalid deblur tile size: {tile_h}x{tile_w}")

        fixed_h, fixed_w = self._deblur_fixed_input_hw()
        target_h = fixed_h or tile_h
        target_w = fixed_w or tile_w

        if fixed_h is None:
            target_h = max(target_h, self._DEBLUR_DYNAMIC_MIN_EDGE)
            target_h = ((target_h + self._DEBLUR_DYNAMIC_ALIGN - 1) // self._DEBLUR_DYNAMIC_ALIGN) * self._DEBLUR_DYNAMIC_ALIGN
        if fixed_w is None:
            target_w = max(target_w, self._DEBLUR_DYNAMIC_MIN_EDGE)
            target_w = ((target_w + self._DEBLUR_DYNAMIC_ALIGN - 1) // self._DEBLUR_DYNAMIC_ALIGN) * self._DEBLUR_DYNAMIC_ALIGN

        input_tile = tile_hwc
        if tile_h > target_h or tile_w > target_w:
            input_tile = input_tile[:target_h, :target_w, :]
            tile_h, tile_w = input_tile.shape[:2]

        pad_h = max(0, target_h - tile_h)
        pad_w = max(0, target_w - tile_w)
        if pad_h or pad_w:
            pad_mode = "reflect"
            if input_tile.shape[0] < 2 or input_tile.shape[1] < 2:
                pad_mode = "edge"
            input_tile = np.pad(input_tile, ((0, pad_h), (0, pad_w), (0, 0)), mode=pad_mode)

        input_nchw = np.transpose(input_tile, (2, 0, 1))[None, ...].astype(np.float32)

        if self._deblur_output_name:
            outputs = self._deblur_session.run([self._deblur_output_name], {self._deblur_input_name: input_nchw})
            pred = outputs[0]
        else:
            pred = self._deblur_session.run(None, {self._deblur_input_name: input_nchw})[0]

        if pred.ndim != 4:
            raise RuntimeError(f"Unsupported deblur ONNX output shape: {pred.shape}")

        pred_hwc = np.transpose(pred[0], (1, 2, 0))
        if pred_hwc.shape[2] > 3:
            pred_hwc = pred_hwc[:, :, :3]

        if pred_hwc.max() > 1.5 or pred_hwc.min() < -0.2:
            pred_hwc = (pred_hwc + 1.0) / 2.0

        pred_hwc = np.clip(pred_hwc, 0.0, 1.0)
        pred_hwc = pred_hwc[:tile_h, :tile_w, :].astype(np.float32)

        if pred_hwc.shape[0] != tile_h or pred_hwc.shape[1] != tile_w:
            pred_img = Image.fromarray(np.clip(pred_hwc * 255.0, 0, 255).astype(np.uint8), mode="RGB")
            pred_img = pred_img.resize((tile_w, tile_h), Image.Resampling.BICUBIC)
            pred_hwc = np.array(pred_img).astype(np.float32) / 255.0

        return pred_hwc

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
    ) -> "np.ndarray":
        if np is None:
            raise ImportError("numpy is not installed")

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
