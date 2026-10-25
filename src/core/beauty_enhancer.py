"""Beauty enhancement engine using MediaPipe Face Mesh for precise landmark detection.

Features:
- Teeth whitening (de-yellow + brighten teeth area via mouth landmarks)
- Eye enlargement + interocular distance adjustment
- Face slimming (jawline contour warping)
- Spot/blemish removal (local contrast detection + inpainting)
- Skin smoothing, brightening, rosy tone

Uses MediaPipe Face Mesh (468 landmarks) when available;
falls back to Haar cascade for basic face detection with reduced precision.
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
from .contracts.result import CoreError, CoreOperationError, ErrorCode

try:
    import numpy as np
except ImportError:
    np = None

try:
    import cv2
except ImportError:
    cv2 = None

logger = logging.getLogger(__name__)


# Tasks-API model: 478 landmarks, matches legacy FaceMesh(refine_landmarks=True).
_FACE_LANDMARKER_MODEL_FILENAME = "face_landmarker.task"


def _resolve_model_dir() -> Path:
    from ..utils.model_paths import get_project_model_root
    return get_project_model_root()


def _ensure_face_landmarker_model() -> Optional[Path]:
    model_path = _resolve_model_dir() / _FACE_LANDMARKER_MODEL_FILENAME
    return model_path if model_path.is_file() and model_path.stat().st_size > 1024 else None


def face_landmarker_model_path() -> Path:
    return _resolve_model_dir() / _FACE_LANDMARKER_MODEL_FILENAME


def _clamp_int(value: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _notify_progress(callback: Optional[Callable[[int], None]], value: int) -> None:
    if callback:
        callback(_clamp_int(value, 0, 100))


# ── MediaPipe Face Mesh landmark indices ────────────────────────────────────

# Lower face contour from MediaPipe FaceMesh face oval — left ear → chin → right ear.
# Ordered so fillPoly traces the jaw cleanly without self-intersecting.
_JAWLINE_LEFT = [234, 93, 132, 58, 172, 136, 150, 149, 176, 148, 152]
_JAWLINE_RIGHT = [152, 377, 400, 378, 379, 365, 397, 288, 361, 323, 454]
_JAWLINE_ALL = list(dict.fromkeys(_JAWLINE_LEFT + _JAWLINE_RIGHT))

# Left eye
_L_EYE_TOP = 159
_L_EYE_BOTTOM = 145
_L_EYE_ALL = [33, 246, 161, 160, 159, 158, 157, 173, 133, 155, 154, 153, 145, 144, 163, 7]

# Right eye
_R_EYE_TOP = 386
_R_EYE_BOTTOM = 374
_R_EYE_ALL = [362, 398, 384, 385, 386, 387, 388, 466, 263, 249, 390, 373, 374, 380, 381, 382]

# Mouth inner (teeth region)
_MOUTH_INNER = [78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95]

# Nose bridge (center line top→bottom)
_NOSE_BRIDGE = [6, 197, 195, 5]

# Nose wings (left outer, right outer)
_L_NOSE_WING = 48
_R_NOSE_WING = 278

# Under-eye lower contour for dark circle / eye bag removal
_L_UNDER_EYE = [33, 7, 163, 144, 145]
_R_UNDER_EYE = [263, 249, 390, 373, 374]


# 只有五官关键点才能定位的处理；只有基础人脸检测时这几项做不了
LANDMARK_SETTINGS = ("nose_slim", "teeth_whiten", "eye_bag_remove")


@dataclass
class BeautyConfig:
    skin_smooth: int = 50
    skin_brighten: int = 20
    skin_rosy: int = 15
    sharpen: int = 25

    face_slim: int = 0
    eye_enlarge: int = 0
    eye_distance: int = 0
    nose_slim: int = 0
    teeth_whiten: int = 0
    spot_remove: int = 0
    eye_bag_remove: int = 0

    def normalized(self) -> "BeautyConfig":
        return BeautyConfig(
            skin_smooth=_clamp_int(self.skin_smooth, 0, 100),
            skin_brighten=_clamp_int(self.skin_brighten, 0, 100),
            skin_rosy=_clamp_int(self.skin_rosy, 0, 100),
            sharpen=_clamp_int(self.sharpen, 0, 100),
            face_slim=_clamp_int(self.face_slim, 0, 100),
            eye_enlarge=_clamp_int(self.eye_enlarge, 0, 100),
            eye_distance=_clamp_int(self.eye_distance, -100, 100),
            nose_slim=_clamp_int(self.nose_slim, 0, 100),
            teeth_whiten=_clamp_int(self.teeth_whiten, 0, 100),
            spot_remove=_clamp_int(self.spot_remove, 0, 100),
            eye_bag_remove=_clamp_int(self.eye_bag_remove, 0, 100),
        )


class BeautyEnhancer:

    def __init__(self, *, backend="auto", model_path=None) -> None:
        self._backend = backend
        self._model_path = model_path
        self._detector = None
        self._haar_detector = None
        # _detect_normalized: callable(np.ndarray RGB) -> list[list[(x_norm, y_norm)]]
        self._detect_normalized = None

    def _ensure_mp_face_mesh(self) -> bool:
        if self._backend == "haar":
            if np is None or cv2 is None:
                raise RuntimeError("美颜检测后端不可用")
            return False
        if self._detect_normalized is not None:
            return True

        # Try legacy `mediapipe.solutions.face_mesh` first.
        try:
            if self._backend == "mediapipe_tasks":
                raise ImportError("使用 Tasks 检测后端")
            import mediapipe as mp
            if self._backend == "mediapipe_legacy":
                from mediapipe.python.solutions.face_mesh import FaceMesh
            else:
                import mediapipe.solutions.face_mesh
                FaceMesh = mp.solutions.face_mesh.FaceMesh
            started = time.perf_counter()
            face_mesh = FaceMesh(
                static_image_mode=True,
                max_num_faces=5,
                refine_landmarks=True,
                min_detection_confidence=0.5,
            )

            def _legacy_detect(image_rgb):
                res = face_mesh.process(image_rgb)
                if not res.multi_face_landmarks:
                    return []
                return [
                    [(lm.x, lm.y) for lm in fl.landmark]
                    for fl in res.multi_face_landmarks
                ]

            self._detect_normalized = _legacy_detect
            self._detector = face_mesh
            logger.debug("模型加载 tool=beauty backend=mediapipe_legacy thread=%s duration_ms=%.1f",
                         get_ident(), (time.perf_counter() - started) * 1000)
            return True
        except Exception:
            if self._backend == "mediapipe_legacy":
                raise
            pass

        # Fall back to the new Tasks API (mediapipe >= 0.10.x where solutions was removed).
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions
            from mediapipe.tasks.python import vision as mp_vision

            model_path = self._model_path if self._backend == "mediapipe_tasks" else _ensure_face_landmarker_model()
            if model_path is None:
                if self._backend == "mediapipe_tasks":
                    raise FileNotFoundError("美颜检测资源不可用")
                logger.warning("face_landmarker.task model unavailable; falling back to Haar cascade")
                return False

            options = mp_vision.FaceLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(model_path)),
                running_mode=mp_vision.RunningMode.IMAGE,
                num_faces=5,
                min_face_detection_confidence=0.5,
            )
            started = time.perf_counter()
            landmarker = mp_vision.FaceLandmarker.create_from_options(options)

            def _tasks_detect(image_rgb):
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=image_rgb)
                res = landmarker.detect(mp_image)
                if not res.face_landmarks:
                    return []
                return [
                    [(lm.x, lm.y) for lm in face]
                    for face in res.face_landmarks
                ]

            self._detect_normalized = _tasks_detect
            self._detector = landmarker
            logger.debug("模型加载 tool=beauty backend=mediapipe_tasks thread=%s duration_ms=%.1f",
                         get_ident(), (time.perf_counter() - started) * 1000)
            return True
        except Exception:
            if self._backend == "mediapipe_tasks":
                raise
            logger.warning("mediapipe not installed; falling back to Haar cascade")
            return False

    def close(self):
        detector = self._detector
        self._detector = None
        self._detect_normalized = None
        self._haar_detector = None
        if detector is not None:
            detector.close()

    # ── main entry ──────────────────────────────────────────────────────────

    def enhance(
        self,
        image: Image.Image,
        config: BeautyConfig,
        progress_callback: Optional[Callable[[int], None]] = None,
        *,
        sampling=None,
    ) -> Image.Image:
        if np is None or cv2 is None:
            if self._backend != "auto":
                raise RuntimeError("美颜检测后端不可用")
            return self._fallback_pillow(image, config, progress_callback)

        cfg = config.normalized()
        _notify_progress(progress_callback, 2)

        rgb = image.convert("RGB")

        has_any_warp = cfg.face_slim > 0 or cfg.eye_enlarge > 0 or cfg.eye_distance != 0 or cfg.nose_slim > 0
        has_any_color = (
            cfg.skin_smooth > 0 or cfg.skin_brighten > 0 or cfg.skin_rosy > 0
            or cfg.teeth_whiten > 0 or cfg.spot_remove > 0 or cfg.eye_bag_remove > 0
        )
        if not has_any_warp and not has_any_color and cfg.sharpen == 0:
            _notify_progress(progress_callback, 100)
            return rgb

        np_img = np.array(rgb)
        h, w = np_img.shape[:2]

        use_mp = self._ensure_mp_face_mesh()
        if (self._backend != "auto" and not use_mp
                and any(getattr(cfg, name) for name in LANDMARK_SETTINGS)):
            raise CoreOperationError(CoreError(ErrorCode.TOOL_INCOMPATIBLE,
                "beauty.landmarks_required", {"tool_id": "beauty"}))
        _notify_progress(progress_callback, 5)

        if use_mp:
            face_data_list = self._detect_faces_mediapipe(np_img)
        else:
            face_data_list = self._detect_faces_haar(np_img)

        if not face_data_list:
            if (self._backend != "auto" and (has_any_warp or cfg.teeth_whiten
                                             or cfg.spot_remove or cfg.eye_bag_remove)):
                raise CoreOperationError(CoreError(ErrorCode.INVALID_INPUT,
                    "beauty.face_not_found", {"tool_id": "beauty"}))
            _notify_progress(progress_callback, 100)
            return self._fallback_pillow(rgb, cfg, progress_callback)

        _notify_progress(progress_callback, 10)

        result_f32 = np_img.astype(np.float32)

        _MAX_PROC = 512

        num_faces = len(face_data_list)
        for idx, fd in enumerate(face_data_list):
            base_progress = 10 + int(80 * idx / max(1, num_faces))
            sub_progress = lambda v: _notify_progress(progress_callback, base_progress + int(v * 0.8 / num_faces))

            x1, y1, x2, y2 = fd.bbox
            face_h, face_w = y2 - y1, x2 - x1
            if face_h <= 0 or face_w <= 0:
                continue

            face_roi = result_f32[y1:y2, x1:x2]

            scale = min(1.0, _MAX_PROC / max(face_h, face_w))
            if scale < 1.0:
                new_w = max(4, int(round(face_w * scale)))
                new_h = max(4, int(round(face_h * scale)))
                face_roi = cv2.resize(face_roi, (new_w, new_h), interpolation=cv2.INTER_AREA)
                proc_landmarks = (
                    [(int(round((gx - x1) * scale)), int(round((gy - y1) * scale))) for gx, gy in fd.landmarks]
                    if fd.landmarks is not None else None
                )
                proc_x, proc_y = 0, 0
                proc_h, proc_w = new_h, new_w
            else:
                proc_landmarks = fd.landmarks
                proc_x, proc_y = x1, y1
                proc_h, proc_w = face_h, face_w

            processed = face_roi.astype(np.uint8)
            face_mask_2d = self._build_ellipse_mask(proc_h, proc_w)
            skin_mask_2d = self._build_skin_mask(processed)
            blend_mask_2d = np.clip(face_mask_2d * skin_mask_2d, 0.0, 1.0)
            blend_3c = np.repeat(blend_mask_2d[:, :, None], 3, axis=2)

            if cfg.skin_smooth > 0:
                processed = self._skin_smooth(processed, cfg.skin_smooth)
            if cfg.skin_brighten > 0:
                processed = self._skin_brighten(processed, cfg.skin_brighten)
            if cfg.skin_rosy > 0:
                processed = self._skin_rosy(processed, cfg.skin_rosy)

            if cfg.teeth_whiten > 0 and proc_landmarks is not None:
                processed = self._teeth_whiten(processed, proc_landmarks, proc_x, proc_y, cfg.teeth_whiten)

            if cfg.eye_bag_remove > 0 and proc_landmarks is not None:
                processed = self._eye_bag_remove(processed, proc_landmarks, proc_x, proc_y, cfg.eye_bag_remove)

            if cfg.spot_remove > 0:
                processed = self._spot_remove(processed, cfg.spot_remove)

            map_x = map_y = None
            if has_any_warp:
                y_grid, x_grid = np.mgrid[0:proc_h, 0:proc_w].astype(np.float32)
                delta_x = np.zeros((proc_h, proc_w), dtype=np.float32)
                delta_y = np.zeros((proc_h, proc_w), dtype=np.float32)

                if cfg.face_slim > 0:
                    warp_mask_2d = self._build_jawline_mask(proc_h, proc_w, proc_landmarks, proc_x, proc_y) if proc_landmarks is not None else None
                    dx, dy = self._slim_face_deltas(proc_h, proc_w, cfg.face_slim, proc_landmarks, proc_x, proc_y, warp_mask_2d, x_grid, y_grid)
                    delta_x += dx
                    delta_y += dy
                    sub_progress(0.3)

                if cfg.eye_enlarge > 0 or cfg.eye_distance != 0:
                    dx, dy = self._eye_transform_deltas(proc_h, proc_w, cfg.eye_enlarge, cfg.eye_distance, proc_landmarks, proc_x, proc_y, x_grid, y_grid)
                    delta_x += dx
                    delta_y += dy
                    sub_progress(0.6)

                if cfg.nose_slim > 0 and proc_landmarks is not None:
                    dx, dy = self._nose_slim_deltas(proc_h, proc_w, cfg.nose_slim, proc_landmarks, proc_x, proc_y, x_grid, y_grid)
                    delta_x += dx
                    delta_y += dy
                    sub_progress(0.8)

                map_x = x_grid + delta_x
                map_y = y_grid + delta_y
                warped = cv2.remap(processed, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE).astype(np.float32)
            else:
                warped = processed.astype(np.float32)

            if sampling is not None and sampling.active:
                sampling.beauty_roi((x1, y1, x2, y2), (proc_w, proc_h), map_x, map_y, blend_3c)
            if scale < 1.0:
                warped = cv2.resize(warped, (face_w, face_h), interpolation=cv2.INTER_LINEAR)
                blend_3c = cv2.resize(blend_3c, (face_w, face_h), interpolation=cv2.INTER_LINEAR)

            result_f32[y1:y2, x1:x2] = warped * blend_3c + result_f32[y1:y2, x1:x2] * (1.0 - blend_3c)
            sub_progress(0.9)

        final = Image.fromarray(np.clip(result_f32, 0, 255).astype(np.uint8), mode="RGB")

        if cfg.sharpen > 0:
            final = ImageEnhance.Sharpness(final).enhance(1.0 + cfg.sharpen / 220.0)

        _notify_progress(progress_callback, 100)
        return final

    def face_regions(self, image: Image.Image) -> tuple:
        """输入画面中的人脸矩形，坐标归一到 0—1，使用当前后端的检测器。"""
        if np is None or cv2 is None:
            raise CoreOperationError(CoreError(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible",
                                               {"tool_id": "beauty"}))
        with image.convert("RGB") as sample:
            sample.thumbnail((800, 800), Image.Resampling.BILINEAR)
            pixels = np.asarray(sample)
            faces = (self._detect_faces_mediapipe(pixels) if self._ensure_mp_face_mesh()
                     else self._detect_faces_haar(pixels))
            width, height = sample.size
            return tuple((face.bbox[0] / width, face.bbox[1] / height,
                          face.bbox[2] / width, face.bbox[3] / height) for face in faces)

    def analyze_auto_beauty(self, image: Image.Image) -> dict[str, int]:
        """基于 MediaPipe 人脸检测与 Auto 亮度/色彩分析，智能生成最佳美颜参数。"""
        defaults = {
            "skin_smooth": 55,
            "skin_brighten": 30,
            "skin_rosy": 20,
            "sharpen": 15,
            "eye_enlarge": 15,
            "eye_distance": 0,
            "face_slim": 20,
            "nose_slim": 10,
            "teeth_whiten": 25,
            "spot_remove": 35,
            "eye_bag_remove": 30,
        }
        if np is None or cv2 is None or image is None:
            return defaults

        rgb = image.convert("RGB")
        w, h = rgb.size
        max_dim = 800
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            sample = rgb.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.BILINEAR)
        else:
            sample = rgb

        np_sample = np.array(sample)
        use_mp = self._ensure_mp_face_mesh()
        if use_mp:
            faces = self._detect_faces_mediapipe(np_sample)
        else:
            faces = self._detect_faces_haar(np_sample)

        if not faces:
            if self._backend != "auto":
                raise CoreOperationError(CoreError(ErrorCode.INVALID_INPUT,
                    "beauty.face_not_found", {"tool_id": "beauty"}))
            gray = cv2.cvtColor(np_sample, cv2.COLOR_RGB2GRAY)
            med = float(np.median(gray))
            brighten = int(np.clip(round((135.0 - med) * 0.4), 15, 45))
            defaults["skin_brighten"] = brighten
            return defaults

        main_face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        x1, y1, x2, y2 = main_face.bbox
        face_roi = np_sample[y1:y2, x1:x2]
        if face_roi.size == 0:
            return defaults

        face_gray = cv2.cvtColor(face_roi, cv2.COLOR_RGB2GRAY)
        luma_med = float(np.median(face_gray))
        luma_std = float(np.std(face_gray))

        # 1. 智能提亮：目标面部舒适中位亮度 ≈ 145
        brighten = int(np.clip(round((145.0 - luma_med) * 0.5), 10, 55))

        # 2. 智能磨皮：粗糙度高时适度增强磨皮
        if luma_std > 38.0:
            smooth = min(75, int(round(50.0 + (luma_std - 38.0) * 0.8)))
        else:
            smooth = max(40, int(round(50.0 - (38.0 - luma_std) * 0.4)))

        # 3. 智能红润：估算肤色冷暖程度
        r = float(np.mean(face_roi[..., 0]))
        g = float(np.mean(face_roi[..., 1]))
        b = float(np.mean(face_roi[..., 2]))
        warmth = r - (g + b) / 2.0
        if warmth < 15.0:
            rosy = int(np.clip(round(25.0 + (15.0 - warmth) * 0.5), 20, 40))
        else:
            rosy = 15

        # 4. 五官自适应微调
        has_mesh = main_face.landmarks is not None
        slim = 20 if has_mesh else 15
        eye_enl = 18 if has_mesh else 10
        teeth = 25 if has_mesh else 0
        eye_bag = 30 if has_mesh else 0
        spot = 35

        return {
            "skin_smooth": smooth,
            "skin_brighten": brighten,
            "skin_rosy": rosy,
            "sharpen": 15,
            "eye_enlarge": eye_enl,
            "eye_distance": 0,
            "face_slim": slim,
            "nose_slim": 10 if has_mesh else 0,
            "teeth_whiten": teeth,
            "spot_remove": spot,
            "eye_bag_remove": eye_bag,
        }

    # ── face detection ──────────────────────────────────────────────────────

    def _detect_faces_mediapipe(self, image_rgb: np.ndarray) -> list:
        results: list = []
        h, w = image_rgb.shape[:2]

        faces_norm = self._detect_normalized(image_rgb)
        if not faces_norm:
            return results

        for face_norm in faces_norm:
            pts_px = [(int(x * w), int(y * h)) for (x, y) in face_norm]
            if not pts_px:
                continue

            xs = [p[0] for p in pts_px]
            ys = [p[1] for p in pts_px]
            fx1, fy1 = min(xs), min(ys)
            fx2, fy2 = max(xs), max(ys)

            pad_x = int((fx2 - fx1) * 0.18)
            pad_y = int((fy2 - fy1) * 0.20)
            x1 = max(0, fx1 - pad_x)
            y1 = max(0, fy1 - pad_y)
            x2 = min(w, fx2 + pad_x)
            y2 = min(h, fy2 + pad_y)

            if x2 - x1 <= 20 or y2 - y1 <= 20:
                continue

            class _FaceData:
                __slots__ = ("bbox", "landmarks", "landmarks_local")
            fd = _FaceData()
            fd.bbox = (x1, y1, x2, y2)
            fd.landmarks = pts_px
            fd.landmarks_local = [(px - x1, py - y1) for (px, py) in pts_px]
            results.append(fd)

        return results

    def _detect_faces_haar(self, image_rgb: np.ndarray) -> list:
        results: list = []
        h, w = image_rgb.shape[:2]
        gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)

        created = self._haar_detector is None
        if created:
            started = time.perf_counter()
            cascade_path = self._model_path or os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            self._haar_detector = cv2.CascadeClassifier(cascade_path)
        detector = self._haar_detector
        if detector.empty():
            self._haar_detector = None
            if self._backend == "haar":
                raise FileNotFoundError("美颜检测资源不可用")
            return results
        if created:
            logger.debug("模型加载 tool=beauty backend=haar thread=%s duration_ms=%.1f",
                         get_ident(), (time.perf_counter() - started) * 1000)

        faces = detector.detectMultiScale(
            gray, scaleFactor=1.08, minNeighbors=3,
            minSize=(max(30, w // 16), max(30, h // 16)),
        )
        for (x, y, fw, fh) in faces:
            pad_x = int(fw * 0.18)
            pad_y = int(fh * 0.2)
            x1 = max(0, x - pad_x)
            y1 = max(0, y - pad_y)
            x2 = min(w, x + fw + pad_x)
            y2 = min(h, y + fh + pad_y)
            if x2 - x1 > 20 and y2 - y1 > 20:
                class _Fd:
                    __slots__ = ("bbox", "landmarks", "landmarks_local")
                fd = _Fd()
                fd.bbox = (x1, y1, x2, y2)
                fd.landmarks = None
                fd.landmarks_local = None
                results.append(fd)

        return results

    # ── skin smoothing ──────────────────────────────────────────────────────

    def _skin_smooth(self, face_rgb: np.ndarray, strength: int) -> np.ndarray:
        t = strength / 100.0
        alpha = 0.15 + t * 0.30
        detail_restore = 0.15 + t * 0.18
        smooth = cv2.bilateralFilter(face_rgb, d=0, sigmaColor=14 + t * 32.0, sigmaSpace=5 + t * 6.0)
        result = cv2.addWeighted(face_rgb, 1.0 - alpha, smooth, alpha, 0)
        result = cv2.addWeighted(
            result, 1.0 + detail_restore,
            cv2.GaussianBlur(result, (0, 0), 1.0), -detail_restore, 0,
        )
        return result

    # ── skin brightening ────────────────────────────────────────────────────

    def _skin_brighten(self, face_rgb: np.ndarray, strength: int) -> np.ndarray:
        lab = cv2.cvtColor(face_rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        boost = 2.0 + (strength / 100.0) * 8.0
        lab[:, :, 0] = np.clip(lab[:, :, 0] + boost, 0, 255)
        return cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2RGB)

    # ── rosy tone ───────────────────────────────────────────────────────────

    def _skin_rosy(self, face_rgb: np.ndarray, strength: int) -> np.ndarray:
        hsv = cv2.cvtColor(face_rgb, cv2.COLOR_RGB2HSV).astype(np.float32)
        sat_boost = 1.0 + (strength / 100.0) * 0.12
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * sat_boost, 0, 255)
        red_shift = (strength / 100.0) * 2.5
        hsv[:, :, 0] = np.clip(hsv[:, :, 0] - red_shift, 0, 179)
        return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)

    # ── teeth whitening ─────────────────────────────────────────────────────

    def _teeth_whiten(
        self, face_rgb: np.ndarray,
        landmarks_global: list[tuple[int, int]],
        face_x: int, face_y: int, strength: int,
    ) -> np.ndarray:
        """Whiten teeth in the inner mouth region using MediaPipe lip landmarks."""
        if not landmarks_global:
            return face_rgb

        face_h, face_w = face_rgb.shape[:2]

        # Convert inner mouth landmarks to face-ROI-local coordinates
        mouth_pts = []
        for idx in _MOUTH_INNER:
            if idx < len(landmarks_global):
                gx, gy = landmarks_global[idx]
                lx = gx - face_x
                ly = gy - face_y
                if 0 <= lx < face_w and 0 <= ly < face_h:
                    mouth_pts.append((lx, ly))

        if len(mouth_pts) < 4:
            return face_rgb

        # Build mouth polygon mask
        mouth_mask = np.zeros((face_h, face_w), dtype=np.uint8)
        hull = cv2.convexHull(np.array(mouth_pts, dtype=np.int32))
        cv2.fillPoly(mouth_mask, [hull], 255)

        # Expand mask slightly
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        mouth_mask = cv2.dilate(mouth_mask, kernel, iterations=2)
        mouth_mask = cv2.GaussianBlur(mouth_mask, (9, 9), 0)

        # Detect white-ish pixels (teeth) inside mouth region: low saturation, high value
        hsv = cv2.cvtColor(face_rgb, cv2.COLOR_RGB2HSV).astype(np.float32)
        s_channel = hsv[:, :, 1]
        v_channel = hsv[:, :, 2]

        teeth_likely = (s_channel < 60) & (v_channel > 100) & (mouth_mask > 30)

        # De-yellow: reduce saturation, boost brightness
        t = strength / 100.0

        s_channel[teeth_likely] = np.clip(s_channel[teeth_likely] * (1.0 - t * 0.7), 0, 255)
        v_channel[teeth_likely] = np.clip(v_channel[teeth_likely] + t * 30.0, 0, 255)
        hsv[:, :, 1] = s_channel
        hsv[:, :, 2] = v_channel

        whitened = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)

        # Soft blend within mouth region
        blend_weight = np.clip(mouth_mask.astype(np.float32) / 255.0 * t * 0.9, 0.0, 1.0)
        blend_3c = np.repeat(blend_weight[:, :, None], 3, axis=2)

        return (whitened.astype(np.float32) * blend_3c + face_rgb.astype(np.float32) * (1.0 - blend_3c)).astype(np.uint8)

    # ── spot / blemish removal ──────────────────────────────────────────────

    def _spot_remove(self, face_rgb: np.ndarray, strength: int) -> np.ndarray:
        """Remove blemishes using frequency separation (heal layer method).

        Instead of crude inpainting, we separate the image into a low-frequency
        base (skin tone) and a high-frequency detail layer (texture, blemishes).
        Detected blemish pixels have their detail suppressed and are blended toward
        the smooth base, preserving skin colour and avoiding the plastic inpaint look.
        """
        face_h, face_w = face_rgb.shape[:2]
        t = strength / 100.0

        base = cv2.bilateralFilter(face_rgb, d=0, sigmaColor=40, sigmaSpace=20)
        detail = face_rgb.astype(np.float32) - base.astype(np.float32)

        skin_mask = self._build_skin_mask(face_rgb)
        skin_binary = (skin_mask > 0.4).astype(np.uint8)

        detail_gray = np.mean(detail, axis=2)
        dark_thresh = max(10.0, 20.0 - strength * 0.08)
        light_thresh = max(12.0, 24.0 - strength * 0.08)
        dark_spots = (detail_gray < -dark_thresh).astype(np.uint8)
        light_spots = (detail_gray > light_thresh).astype(np.uint8)
        spots = cv2.bitwise_or(dark_spots, light_spots)
        spots = cv2.bitwise_and(spots, skin_binary)

        kernel_small = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        spots = cv2.morphologyEx(spots, cv2.MORPH_OPEN, kernel_small, iterations=1)
        spots = cv2.morphologyEx(spots, cv2.MORPH_CLOSE, kernel_small, iterations=1)

        _, labels, stats, _ = cv2.connectedComponentsWithStats(spots, connectivity=8)
        max_area = int(face_h * face_w * 0.012)
        label_values = (stats[:, cv2.CC_STAT_AREA] <= max_area).astype(np.uint8) * 255
        label_values[0] = 0
        clean_spots = label_values[labels]

        if np.count_nonzero(clean_spots) < 3:
            return face_rgb

        blur_k = max(7, min(15, int(face_h * 0.025) * 2 + 1))
        spot_mask = cv2.GaussianBlur(clean_spots.astype(np.float32) / 255.0, (blur_k, blur_k), 0)
        alpha = np.clip(spot_mask * t * 0.55, 0.0, 0.45)
        alpha_3c = alpha[:, :, None]

        result = base.astype(np.float32) + detail * (1.0 - alpha_3c)

        return np.clip(result, 0, 255).astype(np.uint8)

    # ── face slimming ───────────────────────────────────────────────────────

    def _slim_face_deltas(
        self, face_h: int, face_w: int, strength: int,
        landmarks_global: Optional[list[tuple[int, int]]],
        face_x: int, face_y: int,
        warp_mask: Optional[np.ndarray],
        x_grid: np.ndarray, y_grid: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (delta_x, delta_y) displacement fields for face slimming."""
        t = strength / 100.0
        cx = face_w / 2.0
        delta_x = np.zeros((face_h, face_w), dtype=np.float32)

        if landmarks_global and warp_mask is not None:
            jaw_local = []
            for idx in _JAWLINE_ALL:
                if idx < len(landmarks_global):
                    gx, gy = landmarks_global[idx]
                    lx = gx - face_x
                    ly = gy - face_y
                    if 0 <= lx < face_w and 0 <= ly < face_h:
                        jaw_local.append((lx, ly))

            if jaw_local:
                for (jx, jy) in jaw_local:
                    offset = jx - cx
                    dist = abs(offset)
                    if dist < 5:
                        continue
                    sign = 1.0 if offset > 0 else -1.0
                    push = sign * t * min(dist * 0.03, 4.0)
                    sigma_x = dist * 0.45 + 12
                    sigma_y = dist * 0.30 + 10
                    gauss = np.exp(-((x_grid - jx) ** 2) / (2 * sigma_x ** 2) - ((y_grid - jy) ** 2) / (2 * sigma_y ** 2))
                    delta_x += push * gauss
        else:
            left_falloff = np.clip(x_grid / (face_w * 0.35), 0.0, 1.0)
            right_falloff = np.clip((face_w - x_grid) / (face_w * 0.35), 0.0, 1.0)
            weight = np.where(x_grid < cx, left_falloff, right_falloff)
            weight *= np.clip(1.5 - y_grid / (face_h * 0.75), 0.0, 1.0)
            weight[:, :int(face_w * 0.08)] = 0.0
            weight[:, int(face_w * 0.92):] = 0.0
            weight[:max(1, int(face_h * 0.25)), :] *= np.linspace(0, 1, max(1, int(face_h * 0.25)))[:, None]

            push = t * weight * 16.0
            delta_x += np.where(x_grid < cx, push, -push * 0.6)

        if warp_mask is not None:
            delta_x = delta_x * np.clip(warp_mask, 0.0, 1.0)

        return delta_x, np.zeros_like(delta_x)

    # ── eye enlargement + distance ──────────────────────────────────────────

    def _eye_transform_deltas(
        self, face_h: int, face_w: int,
        enlarge: int, distance: int,
        landmarks_global: Optional[list[tuple[int, int]]],
        face_x: int, face_y: int,
        x_grid: np.ndarray, y_grid: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (delta_x, delta_y) displacement fields for eye transform."""
        delta_x = np.zeros((face_h, face_w), dtype=np.float32)
        delta_y = np.zeros((face_h, face_w), dtype=np.float32)

        enlarge_t = enlarge / 100.0
        distance_t = distance / 100.0

        left_eye_center = None
        right_eye_center = None
        left_eye_radius = None
        right_eye_radius = None
        left_pts = []
        right_pts = []

        if landmarks_global:
            for idx in _L_EYE_ALL:
                if idx < len(landmarks_global):
                    gx, gy = landmarks_global[idx]
                    left_pts.append((gx - face_x, gy - face_y))
            for idx in _R_EYE_ALL:
                if idx < len(landmarks_global):
                    gx, gy = landmarks_global[idx]
                    right_pts.append((gx - face_x, gy - face_y))

            if left_pts:
                l_arr = np.array(left_pts, dtype=np.float32)
                left_eye_center = (np.mean(l_arr[:, 0]), np.mean(l_arr[:, 1]))
                radii = np.sqrt((l_arr[:, 0] - left_eye_center[0]) ** 2 + (l_arr[:, 1] - left_eye_center[1]) ** 2)
                left_eye_radius = float(np.percentile(radii, 90)) * 1.3
            if right_pts:
                r_arr = np.array(right_pts, dtype=np.float32)
                right_eye_center = (np.mean(r_arr[:, 0]), np.mean(r_arr[:, 1]))
                radii = np.sqrt((r_arr[:, 0] - right_eye_center[0]) ** 2 + (r_arr[:, 1] - right_eye_center[1]) ** 2)
                right_eye_radius = float(np.percentile(radii, 90)) * 1.3
        else:
            left_eye_center = (face_w * 0.30, face_h * 0.35)
            right_eye_center = (face_w * 0.70, face_h * 0.35)
            left_eye_radius = min(face_w, face_h) * 0.08
            right_eye_radius = min(face_w, face_h) * 0.08

        if enlarge_t > 0 and left_eye_center and left_eye_radius:
            pull = enlarge_t * 0.18
            for ec, radius in [(left_eye_center, left_eye_radius), (right_eye_center, right_eye_radius)]:
                if ec is None or radius is None:
                    continue
                r = radius * 1.15
                dx = x_grid - ec[0]
                dy = y_grid - ec[1]
                dist = np.sqrt(dx ** 2 + dy ** 2)
                inside = dist < r
                if not np.any(inside):
                    continue
                norm = np.clip(dist / r, 0.0, 1.0)
                falloff = np.where(inside, (1.0 - norm * norm) ** 2, 0.0)

                if landmarks_global is not None:
                    is_left = (ec == left_eye_center)
                    eye_pts = left_pts if is_left and landmarks_global else right_pts
                    if eye_pts and len(eye_pts) >= 4:
                        arr = np.array(eye_pts, dtype=np.float64)
                        eye_w = float(np.max(arr[:, 0]) - np.min(arr[:, 0]))
                        eye_h = float(np.max(arr[:, 1]) - np.min(arr[:, 1]))
                        ratio = max(eye_w / max(eye_h, 1.0), 1.0)
                        scale_x = ratio ** 0.35
                        scale_y = 1.0 / (ratio ** 0.35)
                        delta_x -= dx * falloff * pull * scale_x
                        delta_y -= dy * falloff * pull * scale_y
                        continue

                delta_x -= dx * falloff * pull
                delta_y -= dy * falloff * pull

        if distance_t != 0 and left_eye_center and right_eye_center:
            lcx, _ = left_eye_center
            rcx, _ = right_eye_center
            mid_x = (lcx + rcx) / 2.0
            eye_gap = max(20.0, abs(rcx - lcx))
            max_shift = eye_gap * 0.06
            l_radius = left_eye_radius or (min(face_w, face_h) * 0.08)
            r_radius = right_eye_radius or (min(face_w, face_h) * 0.08)

            for ec, radius in [(left_eye_center, l_radius), (right_eye_center, r_radius)]:
                if ec is None:
                    continue
                side_sign = 1.0 if ec[0] > mid_x else -1.0
                r = radius * 2.0
                dx = x_grid - ec[0]
                dy = y_grid - ec[1]
                dist = np.sqrt(dx ** 2 + dy ** 2)
                inside = dist < r
                if not np.any(inside):
                    continue
                norm = np.clip(dist / r, 0.0, 1.0)
                falloff = np.where(inside, (1.0 - norm) ** 2, 0.0)
                delta_x += -side_sign * distance_t * falloff * max_shift

        return delta_x, delta_y

    # ── nose slimming ─────────────────────────────────────────────────────────

    def _nose_slim_deltas(
            self, face_h: int, face_w: int, strength: int,
            landmarks_global: list[tuple[int, int]],
            face_x: int, face_y: int,
            x_grid: np.ndarray, y_grid: np.ndarray,
        ) -> tuple[np.ndarray, np.ndarray]:
            """Return (delta_x, delta_y) displacement fields for nose slimming."""
            t = strength / 100.0

            bridge_pts = []
            for idx in _NOSE_BRIDGE:
                if idx < len(landmarks_global):
                    gx, gy = landmarks_global[idx]
                    lx, ly = gx - face_x, gy - face_y
                    if 0 <= lx < face_w and 0 <= ly < face_h:
                        bridge_pts.append((float(lx), float(ly)))

            if _L_NOSE_WING >= len(landmarks_global) or _R_NOSE_WING >= len(landmarks_global):
                return np.zeros((face_h, face_w), dtype=np.float32), np.zeros((face_h, face_w), dtype=np.float32)
            left_wing_x = landmarks_global[_L_NOSE_WING][0] - face_x
            right_wing_x = landmarks_global[_R_NOSE_WING][0] - face_x

            if len(bridge_pts) < 2:
                return np.zeros((face_h, face_w), dtype=np.float32), np.zeros((face_h, face_w), dtype=np.float32)

            center_x = float(np.mean([p[0] for p in bridge_pts]))
            nose_top_y = min(p[1] for p in bridge_pts)
            nose_bot_y = max(p[1] for p in bridge_pts)
            nose_height = max(nose_bot_y - nose_top_y, 1.0)
            nose_half_w = max(abs(right_wing_x - left_wing_x) * 0.5, 5.0)

            margin = nose_height * 0.4
            top_fade = np.clip((y_grid - (nose_top_y - margin * 0.5)) / max(margin * 0.5, 1.0), 0, 1)
            bot_fade = np.clip(1.0 - (y_grid - nose_bot_y) / max(margin * 0.4, 1.0), 0, 1)
            y_weight = top_fade * bot_fade

            dx = x_grid - center_x
            norm_dx = np.abs(dx) / max(nose_half_w, 1.0)
            h_weight = np.clip(1.0 - (norm_dx - 0.15) / 1.35, 0, 1)

            squeeze = 1.0 + t * 0.22 * y_weight * h_weight
            delta_x = center_x + dx * squeeze - x_grid

            return delta_x, np.zeros((face_h, face_w), dtype=np.float32)

    # ── eye bag / dark circle removal ────────────────────────────────────────

    def _eye_bag_remove(
        self, face_rgb: np.ndarray,
        landmarks_global: list[tuple[int, int]],
        face_x: int, face_y: int, strength: int,
    ) -> np.ndarray:
        """Brighten and smooth dark circles / eye bags under the eyes."""
        face_h, face_w = face_rgb.shape[:2]
        t = strength / 100.0
        result = face_rgb.astype(np.float32)
        l_ch = None
        smooth_lab = None

        for eye_indices, top_lm, bot_lm in [
            (_L_UNDER_EYE, _L_EYE_TOP, _L_EYE_BOTTOM),
            (_R_UNDER_EYE, _R_EYE_TOP, _R_EYE_BOTTOM),
        ]:
            if top_lm >= len(landmarks_global) or bot_lm >= len(landmarks_global):
                continue

            pts = []
            for idx in eye_indices:
                if idx < len(landmarks_global):
                    gx, gy = landmarks_global[idx]
                    lx, ly = int(gx - face_x), int(gy - face_y)
                    if 0 <= lx < face_w and 0 <= ly < face_h:
                        pts.append((lx, ly))
            if len(pts) < 3:
                continue

            eye_top_y = landmarks_global[top_lm][1] - face_y
            eye_bot_y = landmarks_global[bot_lm][1] - face_y
            eye_h = max(abs(eye_bot_y - eye_top_y), 5.0)
            extend = eye_h * 1.2

            bottom_pts = [(px, min(int(py + extend), face_h - 1)) for px, py in reversed(pts)]
            polygon = np.array(pts + bottom_pts, dtype=np.int32)

            mask = np.zeros((face_h, face_w), dtype=np.uint8)
            cv2.fillPoly(mask, [polygon], 255)
            blur_k = max(3, int(eye_h * 0.4) * 2 + 1)
            mask = cv2.GaussianBlur(mask, (blur_k, blur_k), 0)
            mask_f = np.clip(mask.astype(np.float32) / 255.0, 0, 1)

            if np.max(mask_f) < 0.01:
                continue

            if l_ch is None:
                l_ch = cv2.cvtColor(face_rgb, cv2.COLOR_RGB2LAB)[:, :, 0].astype(np.float32)

            skin_region = mask_f > 0.3
            pixels = l_ch[skin_region]
            if len(pixels) < 5:
                continue
            mean_l = float(np.mean(pixels))

            dark_thresh = mean_l - 12
            is_dark = (l_ch < dark_thresh) & (mask_f > 0.2)

            l_corrected = l_ch.copy()
            l_corrected[is_dark] += t * (mean_l - l_ch[is_dark]) * 0.65
            l_corrected = np.clip(l_corrected, 0, 255)

            if smooth_lab is None:
                smooth = cv2.bilateralFilter(face_rgb, d=0, sigmaColor=15 + t * 20, sigmaSpace=3 + t * 3)
                smooth_lab = cv2.cvtColor(smooth, cv2.COLOR_RGB2LAB).astype(np.float32)
            # 两只眼共用这份滤波结果：亮度通道每次整个重写，色度通道不动，所以可以复用
            smooth_lab[:, :, 0] = l_corrected
            corrected = cv2.cvtColor(smooth_lab.astype(np.uint8), cv2.COLOR_LAB2RGB)

            alpha = np.clip(mask_f * t * 0.6 + np.where(is_dark, mask_f * t * 0.4, 0.0), 0.0, 1.0)
            alpha_3c = alpha[:, :, None]

            result = corrected.astype(np.float32) * alpha_3c + result * (1.0 - alpha_3c)

        return np.clip(result, 0, 255).astype(np.uint8)

    # ── mask helpers ────────────────────────────────────────────────────────

    def _build_ellipse_mask(self, height: int, width: int) -> np.ndarray:
        mask = np.zeros((height, width), dtype=np.float32)
        center = (width // 2, height // 2)
        axes = (max(1, int(width * 0.43)), max(1, int(height * 0.47)))
        cv2.ellipse(mask, center, axes, 0, 0, 360, color=1.0, thickness=-1)
        blur_k = max(7, ((min(width, height) // 8) | 1))
        mask = cv2.GaussianBlur(mask, (blur_k, blur_k), 0)
        return np.clip(mask, 0.0, 1.0)

    def _build_skin_mask(self, face_rgb: np.ndarray) -> np.ndarray:
        ycrcb = cv2.cvtColor(face_rgb, cv2.COLOR_RGB2YCrCb)

        base_lo = np.array([0, 133, 77], dtype=np.int32)
        base_hi = np.array([255, 173, 127], dtype=np.int32)

        h, w = face_rgb.shape[:2]
        center_region = ycrcb[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
        if center_region.size > 0:
            samples = center_region.reshape(-1, 3).astype(np.float32)
            mean_cr = np.mean(samples[:, 1])
            mean_cb = np.mean(samples[:, 2])
            std_cr = max(np.std(samples[:, 1]), 1.0)
            std_cb = max(np.std(samples[:, 2]), 1.0)

            adapt_lo_cr = max(int(mean_cr - 2.5 * std_cr), 100)
            adapt_hi_cr = min(int(mean_cr + 2.5 * std_cr), 200)
            adapt_lo_cb = max(int(mean_cb - 2.5 * std_cb), 60)
            adapt_hi_cb = min(int(mean_cb + 2.5 * std_cb), 160)

            lo_cr = min(base_lo[1], adapt_lo_cr)
            hi_cr = max(base_hi[1], adapt_hi_cr)
            lo_cb = min(base_lo[2], adapt_lo_cb)
            hi_cb = max(base_hi[2], adapt_hi_cb)
        else:
            lo_cr, hi_cr = base_lo[1], base_hi[1]
            lo_cb, hi_cb = base_lo[2], base_hi[2]

        skin = cv2.inRange(ycrcb, (0, int(lo_cr), int(lo_cb)), (255, int(hi_cr), int(hi_cb)))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        skin = cv2.dilate(skin, kernel, iterations=1)
        skin = cv2.GaussianBlur(skin, (0, 0), sigmaX=3.0)
        soft = skin.astype(np.float32) / 255.0
        return np.clip(soft * 0.85 + 0.15, 0.0, 1.0)

    def _build_jawline_mask(
        self, face_h: int, face_w: int,
        landmarks_global: Optional[list[tuple[int, int]]],
        face_x: int, face_y: int,
    ) -> Optional[np.ndarray]:
        """Build a soft mask for jaw/cheek areas where slimming warp applies."""
        if not landmarks_global:
            return None

        jaw_pts = []
        for idx in _JAWLINE_ALL:
            if idx < len(landmarks_global):
                gx, gy = landmarks_global[idx]
                lx = gx - face_x
                ly = gy - face_y
                if 0 <= lx < face_w and 0 <= ly < face_h:
                    jaw_pts.append((lx, ly))

        if len(jaw_pts) < 3:
            return None

        mask = np.zeros((face_h, face_w), dtype=np.float32)
        jaw_arr = np.array(jaw_pts, dtype=np.int32)
        cv2.fillPoly(mask, [jaw_arr], 1.0)

        # Expand the mask outward to cover cheek/jaw region
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
        mask = cv2.dilate(mask, kernel, iterations=5)
        mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=20.0)

        # Restrict to lower half of face
        mask[:max(1, int(face_h * 0.25)), :] = 0.0
        edge_ramp = min(20, max(1, int(face_h * 0.05)))
        mask[int(face_h * 0.25):int(face_h * 0.25) + edge_ramp, :] *= np.linspace(0, 1, edge_ramp)[:, None]

        return np.clip(mask, 0.0, 1.0)

    # ── PIL fallback ────────────────────────────────────────────────────────

    def _fallback_pillow(self, image: Image.Image, cfg: BeautyConfig, progress_callback=None) -> Image.Image:
        out = image
        if cfg.skin_smooth > 0:
            sigma = 0.5 + cfg.skin_smooth / 130.0
            out = Image.blend(out, out.filter(ImageFilter.GaussianBlur(radius=sigma)),
                             0.2 + (cfg.skin_smooth / 100.0) * 0.35)
        if cfg.skin_brighten > 0:
            out = ImageEnhance.Brightness(out).enhance(1.0 + cfg.skin_brighten / 350.0)
        if cfg.skin_rosy > 0:
            out = ImageEnhance.Color(out).enhance(1.0 + cfg.skin_rosy / 220.0)
        if cfg.sharpen > 0:
            out = ImageEnhance.Sharpness(out).enhance(1.0 + cfg.sharpen / 220.0)
        _notify_progress(progress_callback, 100)
        return out
