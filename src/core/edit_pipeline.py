"""Unified edit pipeline and session for in-window studio editing.

Provides a non-destructive, cascaded multi-stage pipeline:
  Source Image -> Color Adjust -> Denoise -> Beauty -> Crop/Rotate
"""

from __future__ import annotations

import copy
import logging
from dataclasses import replace
from typing import Any, Dict, Optional

import numpy as np
from PIL import Image

from .color_adjuster import ColorAdjuster
from .tools.color import migrate_legacy_color, ui_to_engine
from .photo_engine import (
    load_render_source,
    render_source,
    _pil_from,
)
from .beauty_enhancer import BeautyConfig, BeautyEnhancer
from .denoiser import DenoiseConfig, ImageDenoiser, get_recommended_model_path

logger = logging.getLogger(__name__)

PROXY_MAX_DIM = 1600


from .tools.stage_defaults import (
    is_color_default as _is_color_default, is_beauty_default as _is_beauty_default,
    is_denoise_default as _is_denoise_default, is_crop_default as _is_crop_default,
)


class UnifiedEditSession:
    """统一编辑会话：承载单张图片所有阶段的编辑参数与撤销历史。"""

    def __init__(self, image_path: str, original_image: Optional[Image.Image] = None):
        self.image_path = image_path
        self._original_image = original_image
        self._render_source = None

        # 各阶段参数
        self.color_params: Dict[str, Any] = {}
        self.beauty_params: Dict[str, Any] = {}
        self.denoise_params: Dict[str, Any] = {}
        self.crop_params: Dict[str, Any] = {}
        self.tool_params: Dict[str, Dict[str, Any]] = {}

        # 撤销与重做栈
        self._undo_stack: list[dict[str, Any]] = []
        self._redo_stack: list[dict[str, Any]] = []

    @property
    def original_image(self) -> Optional[Image.Image]:
        if self._original_image is None and self.image_path:
            if self._render_source is None:
                self._render_source = load_render_source(self.image_path)
            self._original_image = _pil_from(render_source(self._render_source, {}))
        return self._original_image

    def release_pixels(self) -> None:
        self._original_image = None
        self._render_source = None

    def render_snapshot(self) -> "UnifiedEditSession":
        snapshot = copy.copy(self)
        snapshot._restore_snapshot(self.snapshot())
        snapshot._undo_stack = []
        snapshot._redo_stack = []
        return snapshot

    def is_dirty(self) -> bool:
        """检查是否有任何非默认的编辑项。"""
        return bool(self.tool_params) or not (
            _is_denoise_default(self.denoise_params)
            and _is_color_default(self.color_params)
            and _is_beauty_default(self.beauty_params)
            and _is_crop_default(self.crop_params)
        )

    def snapshot(self) -> dict[str, Any]:
        """抓取当前全量参数快照。"""
        return {
            "color": copy.deepcopy(self.color_params),
            "beauty": copy.deepcopy(self.beauty_params),
            "denoise": copy.deepcopy(self.denoise_params),
            "crop": copy.deepcopy(self.crop_params),
            "tools": copy.deepcopy(self.tool_params),
        }

    def push_undo(self) -> None:
        """记录一步全局历史。"""
        if len(self._undo_stack) >= 30:
            self._undo_stack.pop(0)
        self._undo_stack.append(self.snapshot())
        self._redo_stack.clear()

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        self._redo_stack.append(self.snapshot())
        state = self._undo_stack.pop()
        self._restore_snapshot(state)
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        self._undo_stack.append(self.snapshot())
        state = self._redo_stack.pop()
        self._restore_snapshot(state)
        return True

    def _restore_snapshot(self, state: dict[str, Any]) -> None:
        color = state.get("color", {})
        self.color_params = migrate_legacy_color(color, ui=True) if color else {}
        self.beauty_params = copy.deepcopy(state.get("beauty", {}))
        self.denoise_params = copy.deepcopy(state.get("denoise", {}))
        self.crop_params = copy.deepcopy(state.get("crop", {}))
        self.tool_params = copy.deepcopy(state.get("tools", {}))

    def reset_all(self) -> None:
        """重置所有阶段参数。"""
        self.push_undo()
        self.color_params.clear()
        self.beauty_params.clear()
        self.denoise_params.clear()
        self.crop_params.clear()
        self.tool_params.clear()

    def reset_stage(self, stage: str) -> None:
        """重置指定阶段参数。"""
        self.push_undo()
        if stage in ("color", "color_adjust"):
            self.color_params.clear()
        elif stage == "beauty":
            self.beauty_params.clear()
        elif stage == "denoise":
            self.denoise_params.clear()
        elif stage == "crop":
            self.crop_params.clear()
        else:
            self.tool_params.pop(stage, None)


class EditPipeline:
    """图像级联处理流水线。

    执行次序：
      1. Color Adjust (浮点调色)
      2. Denoise (降噪)
      3. Beauty (美颜)
      4. Crop & Orientation (裁切与几何旋转)
    """

    def __init__(self):
        self._color_adjuster = ColorAdjuster(engine="pro")
        self._beauty_enhancer = BeautyEnhancer()
        self._image_denoiser = ImageDenoiser()

    def _compute_proxy(self, image: Image.Image, max_dim: int = PROXY_MAX_DIM) -> Image.Image:
        """为原图生成等比缩放的低分辨率代理图，保证交互极速流畅。"""
        w, h = image.size
        if max(w, h) <= max_dim:
            return image.copy()
        scale = max_dim / max(w, h)
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        return image.resize((nw, nh), Image.Resampling.BILINEAR)

    def _to_engine_color_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """将 UI 层的调色参数字典转换为 photo_engine / ColorAdjuster 可识别的键值。"""
        return ui_to_engine(params)

    def _run_stage_denoise(self, image: Image.Image, params: Dict[str, Any]) -> Image.Image:
        """执行降噪阶段。"""
        if _is_denoise_default(params):
            return image
        m = params.get("method", "bilateral")
        mode = "ai" if m == "ai" else "basic"
        basic_method = m if m in ("gaussian", "median", "bilateral") else "bilateral"
        luma = params.get("strength", 40)
        chroma = params.get("color_strength", 30)
        model_path = params.get("model_path")
        if mode == "ai" and not model_path:
            model_path = str(get_recommended_model_path())
        tile_size = params.get("tile_size", 512)
        cfg = DenoiseConfig(
            mode=mode,
            basic_method=basic_method,
            luma_strength=int(luma),
            chroma_strength=int(chroma),
            model_path=model_path,
            tile_size=tile_size,
        )
        try:
            res = self._image_denoiser.denoise(image, cfg)
            if res is None:
                raise RuntimeError("denoise")
            if image.mode == "RGBA":
                res = res.convert("RGBA")
                res.putalpha(image.getchannel("A"))
            return res
        except Exception:
            logger.exception("EditPipeline: Denoise stage failed")
            raise

    def _run_stage_color(self, image: Image.Image, params: Dict[str, Any]) -> Image.Image:
        """执行调色阶段。"""
        if _is_color_default(params):
            return image
        engine_params = self._to_engine_color_params(params)
        try:
            res = self._color_adjuster.apply_chain(image, engine_params, copy_if_unchanged=False)
            if res is None:
                raise RuntimeError("color_adjust")
            return res
        except Exception:
            logger.exception("EditPipeline: Color stage failed")
            raise

    def _run_stage_beauty(self, image: Image.Image, params: Dict[str, Any]) -> Image.Image:
        """执行美颜阶段。"""
        if _is_beauty_default(params):
            return image
        cfg = BeautyConfig(
            skin_smooth=params.get("skin_smooth", 0),
            skin_brighten=params.get("skin_brighten", 0),
            skin_rosy=params.get("skin_rosy", 0),
            sharpen=params.get("sharpen", 0),
            eye_enlarge=params.get("eye_enlarge", 0),
            eye_distance=params.get("eye_distance", 0),
            face_slim=params.get("face_slim", 0),
            nose_slim=params.get("nose_slim", 0),
            teeth_whiten=params.get("teeth_whiten", 0),
            spot_remove=params.get("spot_remove", 0),
            eye_bag_remove=params.get("eye_bag_remove", 0),
        )
        try:
            res = self._beauty_enhancer.enhance(image, cfg)
            if res is None:
                raise RuntimeError("beauty")
            if image.mode == "RGBA":
                res = res.convert("RGBA")
                res.putalpha(image.getchannel("A"))
            return res
        except Exception:
            logger.exception("EditPipeline: Beauty stage failed")
            raise

    def _run_stage_crop(
        self, image: Image.Image, params: Dict[str, Any], scale_ratio: float = 1.0
    ) -> Image.Image:
        """执行裁切与旋转阶段。"""
        if _is_crop_default(params):
            return image

        crop_rect = params.get("crop_rect")
        rotation = float(params.get("rotation_angle", 0.0))

        result = image
        # 旋转
        if abs(rotation) > 1e-4:
            result = result.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=True)

        # 裁切
        if crop_rect:
            x1, y1, x2, y2 = crop_rect
            if scale_ratio != 1.0:
                x1 = int(round(x1 * scale_ratio))
                y1 = int(round(y1 * scale_ratio))
                x2 = int(round(x2 * scale_ratio))
                y2 = int(round(y2 * scale_ratio))
            w, h = result.size
            x1 = max(0, min(w - 1, x1))
            y1 = max(0, min(h - 1, y1))
            x2 = max(x1 + 1, min(w, x2))
            y2 = max(y1 + 1, min(h, y2))
            result = result.crop((x1, y1, x2, y2))

        return result

    def render_pipeline(
        self,
        session: UnifiedEditSession,
        use_proxy: bool = True,
        skip_crop: bool = False,
    ) -> Optional[Image.Image]:
        """运行完整流水线。"""
        base_img = session.original_image
        if base_img is None:
            return None

        img_for_pipeline = self._compute_proxy(base_img) if use_proxy else base_img

        if session._render_source is not None:
            source = session._render_source
            if use_proxy and img_for_pipeline.size != base_img.size:
                size = img_for_pipeline.size
                pixels = np.stack([
                    np.asarray(Image.fromarray(source.pixels[:, :, channel]).resize(
                        size, Image.Resampling.BILINEAR), dtype=np.float32)
                    for channel in range(3)
                ], axis=-1)
                alpha = None if source.alpha is None else np.asarray(
                    Image.fromarray(source.alpha).resize(size, Image.Resampling.BILINEAR),
                    dtype=np.float32,
                )
                source = replace(source, pixels=pixels, alpha=alpha, is_proxy=True)
            colored = _pil_from(render_source(source, self._to_engine_color_params(session.color_params)))
            out_color = self._run_stage_denoise(colored, session.denoise_params)
        else:
            colored = self._run_stage_color(img_for_pipeline, session.color_params)
            out_color = self._run_stage_denoise(colored, session.denoise_params)

        # Stage 3: Beauty
        out_beauty = self._run_stage_beauty(out_color, session.beauty_params)

        if skip_crop:
            return out_beauty

        # Stage 4: Crop
        scale_ratio = 1.0
        if use_proxy and img_for_pipeline.size != base_img.size:
            scale_ratio = img_for_pipeline.width / float(base_img.width)

        out_final = self._run_stage_crop(out_beauty, session.crop_params, scale_ratio=scale_ratio)
        return out_final
