"""统一调色引擎 — Phase 0 基建。

包含：
  - 工作空间：线性 sRGB / Rec.709 基色、scene-referred、float32、允许 >1.0，出口前不裁切
  - RenderSource 数据结构（含宽色域/HDR 预留字段）
  - load_render_source 入口（与现有 raw_loader.load_image 并存）
  - 入口 A（PIL→线性→PIL，float 仅引擎内部）/ 入口 B（RenderSource 端到端）
  - fast guided filter（积分图 box blur + 降采样近似，不引 scipy/opencv）

RAW 比色默认走 E1：libraw `output_color=XYZ` 给出它自己的 camera→XYZ(D65)，
白平衡由 libraw 按拍摄值一次性施加，本模块只做 XYZ(D65)→线性 sRGB。早先直接对
libraw cam_xyz（Adobe ColorMatrix）取逆的做法已弃用：ColorMatrix 要配合
ForwardMatrix 与参考中性（DNG 规范第 6 章）才构成合法的 camera→XYZ，单独取逆
会把蓝/绿/橙整片旋成品红，中性像素也不再是灰。DCP 色彩科学层（dcp.py）已冻结、
退出默认路，只在显式设置 OPS_DCP_PROFILE / OPS_DCP_DIR 时启用，用作比色对照。

Phase 0 范围：色彩空间编解码、box blur、guided filter、数据结构与入口骨架。
Phase 1：Tier 1 float 算子（曝光/对比/曲线/鲜艳·饱和）。
Phase 2：RAW 经 DCP 色彩科学层（见 dcp.py）注入到工作空间。
"""

from __future__ import annotations

import logging
import math
import os
import struct
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from time import monotonic

import numpy as np
import PIL
from PIL import Image

from .constants import RAW_EXTENSIONS
from .mixer_sampling import (MIXER_CENTERS as _COLOR_MIXER_CENTERS,
                             MIXER_WIDTHS as _COLOR_MIXER_WIDTHS,
                             linear_to_srgb, mixer_weights, rgb_to_hsl as _rgb_to_hsl)
from .looks import LookStage, look_stages
from .tools.color import MASK_BAND_ROWS, color_identity, is_color_neutral, validate_params
from .raw_loader import raw_oriented_size, raw_sizes_match_output

logger = logging.getLogger(__name__)

try:
    import rawpy  # type: ignore
except ImportError:
    rawpy = None

try:
    import cv2  # type: ignore
    _HAS_CV2 = True
except ImportError:
    cv2 = None
    _HAS_CV2 = False

import sys
_root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_native_target = os.path.join(_root_dir, "native", "ops_accel", "target", "release")
if os.path.isdir(_native_target) and _native_target not in sys.path:
    sys.path.insert(0, _native_target)
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)

try:
    import ops_accel  # type: ignore
    _HAS_RUST_ACCEL = True
except ImportError:
    _HAS_RUST_ACCEL = False


_RENDER_BACKENDS_REPORTED = set()



# ---------------------------------------------------------------------------
# 硬件多核（4/8/16核）与 AVX2 指令集自适应加速层
# ---------------------------------------------------------------------------

def is_avx2_available() -> bool:
    """返回 Rust 模块检测到的 CPU AVX2 支持状态。"""
    return _HAS_RUST_ACCEL and bool(getattr(ops_accel, "_AVX2_AVAILABLE", False))


def configure_hardware_acceleration(thread_count: int | None = None) -> int:
    """根据 4/8/16 核心硬件拓扑动态配置 OpenCV 与计算线程数。"""
    if thread_count is None:
        cpu_cores = os.cpu_count() or 4
        if cpu_cores <= 4:
            threads = cpu_cores
        elif cpu_cores <= 8:
            threads = cpu_cores
        else:
            threads = min(cpu_cores, 16)
    else:
        threads = max(1, int(thread_count))

    if _HAS_CV2 and cv2 is not None:
        try:
            cv2.setNumThreads(threads)
            cv2.setUseOptimized(True)
        except Exception:
            pass
    return threads


# ---------------------------------------------------------------------------
# 色彩空间编解码（标准 sRGB 传递函数，无新依赖）
# ---------------------------------------------------------------------------

_SRGB_EOTF_THRESHOLD = np.float32(0.04045)


def srgb_to_linear(x: np.ndarray, *, out=None, work=None) -> np.ndarray:
    """sRGB EOTF（decode）：display-referred sRGB [0,1] → scene-linear。

    c<=0.04045 ? c/12.92 : ((c+0.055)/1.055)**2.4
    输出、工作缓冲与输入互不别名。
    """
    x = np.asarray(x, dtype=np.float32)
    if out is None:
        out = np.empty_like(x)
    if work is None:
        work = np.empty_like(x)
    np.divide(x, np.float32(12.92), out=out)
    np.add(x, np.float32(0.055), out=work)
    np.divide(work, np.float32(1.055), out=work)
    np.power(work, np.float32(2.4), out=work)
    np.copyto(out, work, where=~(x <= _SRGB_EOTF_THRESHOLD))
    return out


# 亮度权重用线性 Rec.709（0.299/0.587/0.114 是 gamma 编码域的 luma 系数，线性下不正确）
_REC709_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def linear_luma(rgb: np.ndarray) -> np.ndarray:
    """线性 Rec.709 亮度 Y=0.2126R+0.7152G+0.0722B。输入 HxWx3 → 输出 HxW。"""
    return (np.asarray(rgb, dtype=np.float32) @ _REC709_LUMA).astype(np.float32)


# ---------------------------------------------------------------------------
# 积分图 box blur + fast guided filter（支持 C++ 多核 AVX2 与 Pure NumPy 回退）
# ---------------------------------------------------------------------------

def box_filter(img: np.ndarray, radius: int) -> np.ndarray:
    """(2r+1) 窗口均值，积分图实现，边缘按实际窗口面积归一化。

    支持 HxW 或 HxWxC。承认常数大但 O(N)（导出走 fast_guided_filter 的降采样近似）。
    """
    img = np.asarray(img, dtype=np.float32)
    if radius <= 0:
        return img.copy()

    squeeze = img.ndim == 2
    if squeeze:
        img = img[:, :, None]

    h, w, c = img.shape
    integral = np.zeros((h + 1, w + 1, c), dtype=np.float64)
    np.cumsum(np.cumsum(img.astype(np.float64), axis=0), axis=1, out=integral[1:, 1:])

    r = int(radius)
    rows = np.arange(h)
    cols = np.arange(w)
    r0 = np.clip(rows - r, 0, h)
    r1 = np.clip(rows + r + 1, 0, h)
    c0 = np.clip(cols - r, 0, w)
    c1 = np.clip(cols + r + 1, 0, w)

    A = integral[np.ix_(r1, c1)]
    B = integral[np.ix_(r0, c1)]
    C = integral[np.ix_(r1, c0)]
    D = integral[np.ix_(r0, c0)]
    window_sum = A - B - C + D

    area = ((r1 - r0)[:, None] * (c1 - c0)[None, :]).astype(np.float64)
    out = (window_sum / area[:, :, None]).astype(np.float32)
    return out[:, :, 0] if squeeze else out


def _resize_bilinear(img: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    """双线性缩放。若 OpenCV 可用且开启优化，使用 C++ 多核硬件加速；否则回退 Pure NumPy。"""
    img = np.asarray(img, dtype=np.float32)
    in_h, in_w = img.shape[:2]
    if (in_h, in_w) == (out_h, out_w):
        return img.copy()

    if _HAS_CV2 and cv2 is not None and cv2.useOptimized():
        return cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_LINEAR)

    # Pure NumPy 回退路径
    ys = (np.arange(out_h, dtype=np.float32) + 0.5) * (in_h / out_h) - 0.5
    xs = (np.arange(out_w, dtype=np.float32) + 0.5) * (in_w / out_w) - 0.5
    ys = np.clip(ys, 0, in_h - 1)
    xs = np.clip(xs, 0, in_w - 1)

    y0 = np.floor(ys).astype(np.int64)
    x0 = np.floor(xs).astype(np.int64)
    y1 = np.minimum(y0 + 1, in_h - 1)
    x1 = np.minimum(x0 + 1, in_w - 1)
    wy = (ys - y0)[:, None]
    wx = (xs - x0)[None, :]

    if img.ndim == 3:
        wy = wy[:, :, None]
        wx = wx[:, :, None]

    Ia = img[np.ix_(y0, x0)] if img.ndim == 2 else img[y0][:, x0]
    Ib = img[y0][:, x1] if img.ndim == 3 else img[np.ix_(y0, x1)]
    Ic = img[y1][:, x0] if img.ndim == 3 else img[np.ix_(y1, x0)]
    Id = img[y1][:, x1] if img.ndim == 3 else img[np.ix_(y1, x1)]

    top = Ia * (1 - wx) + Ib * wx
    bot = Ic * (1 - wx) + Id * wx
    return (top * (1 - wy) + bot * wy).astype(np.float32)


_GUIDED_FILTER_EXECUTION = ContextVar("guided_filter_execution", default=None)


@contextmanager
def guided_filter_execution(threads, cancelled):
    token = _GUIDED_FILTER_EXECUTION.set((threads, cancelled))
    try:
        yield
    finally:
        _GUIDED_FILTER_EXECUTION.reset(token)


def guided_filter_working_memory(pixels):
    return pixels * 64 if _HAS_RUST_ACCEL and hasattr(ops_accel, "guided_filter_exact") else 0


def guided_filter(guide: np.ndarray, src: np.ndarray, radius: int, eps: float) -> np.ndarray:
    """灰度引导滤波（He et al. 2010）。guide/src 均为 HxW 单通道。"""
    guide = np.asarray(guide, dtype=np.float32)
    src = np.asarray(src, dtype=np.float32)

    execution = _GUIDED_FILTER_EXECUTION.get()
    native = getattr(ops_accel, "guided_filter_exact", None) if _HAS_RUST_ACCEL and execution is not None else None
    if native is not None and guide.size:
        threads, cancelled = execution
        try:
            return native(guide, src, max(0, int(radius)), eps, threads=threads, cancelled=cancelled)
        except InterruptedError:
            raise CancelledError() from None

    mean_g = box_filter(guide, radius)
    mean_s = box_filter(src, radius)
    corr_g = box_filter(guide * guide, radius)
    corr_gs = box_filter(guide * src, radius)

    var_g = corr_g - mean_g * mean_g
    cov_gs = corr_gs - mean_g * mean_s

    a = cov_gs / (var_g + np.float32(eps))
    b = mean_s - a * mean_g

    mean_a = box_filter(a, radius)
    mean_b = box_filter(b, radius)
    return (mean_a * guide + mean_b).astype(np.float32)


def fast_guided_filter(
    guide: np.ndarray,
    src: np.ndarray,
    radius: int,
    eps: float,
    subsample: int = 4,
) -> np.ndarray:
    """fast guided filter（He et al. 2015，LR 同款思路）。

    引导/源降采样 → 低分辨率求 a/b → 双线性上采样 a/b → full-res guide 套用。
    每帧 full-res guided filter 调用次数封顶 1（性能路径）。
    """
    guide = np.asarray(guide, dtype=np.float32)
    src = np.asarray(src, dtype=np.float32)
    s = max(1, int(subsample))
    if s == 1:
        return guided_filter(guide, src, radius, eps)

    h, w = guide.shape[:2]
    sh, sw = max(1, h // s), max(1, w // s)
    r_sub = max(1, radius // s)

    g_sub = _resize_bilinear(guide, sh, sw)
    s_sub = _resize_bilinear(src, sh, sw)

    mean_g = box_filter(g_sub, r_sub)
    mean_s = box_filter(s_sub, r_sub)
    var_g = box_filter(g_sub * g_sub, r_sub) - mean_g * mean_g
    cov_gs = box_filter(g_sub * s_sub, r_sub) - mean_g * mean_s

    a = cov_gs / (var_g + np.float32(eps))
    b = mean_s - a * mean_g

    mean_a = _resize_bilinear(box_filter(a, r_sub), h, w)
    mean_b = _resize_bilinear(box_filter(b, r_sub), h, w)
    return (mean_a * guide + mean_b).astype(np.float32)


# ---------------------------------------------------------------------------
# RenderSource（数据结构与 load_render_source 入口；预留宽色域/HDR 字段）
# ---------------------------------------------------------------------------

@dataclass
class RenderSource:
    pixels: np.ndarray              # HxWx3 float32, 线性 sRGB, 可 >1.0
    alpha: np.ndarray | None        # HxW float32 [0,1] 或 None
    source_format: str              # 'raw' | 'srgb8' | 'srgb16'
    source_path: str
    is_proxy: bool                  # True = 预览代理（半尺寸/8-bit 解码）
    orig_size: tuple[int, int]      # (width, height)
    color_pipeline: str             # 'dcp:<profile>' | 'libraw' | 'srgb-direct'
    # ↓ 当前固定值，为未来宽色域/HDR 预留，避免日后大改
    colorspace: str = "linear_srgb"
    white_point: str = "D65"
    gamut: str = "srgb"
    # 降级显式不静默：非空 = UI 应展示的色彩基准提示。
    note: str = ""
    # base curve 门控输入：RAW 且 DCP 含 ToneCurve ⇒ True（禁用编辑 base
    # curve，look 已由 DCP 提供，防 double tone mapping）。matrix 降级时为 False。
    dcp_has_tone_curve: bool = False
    content_identity: tuple = ()
    effect_status: tuple = ()
    content_identity: tuple = ()
    effect_status: tuple = ()
    raw_decode_meta: object = None


@dataclass
class RenderMeta:
    """入口 A（PIL 管线）随解码带出的色彩管线 sidecar。

    UI/线程全程 PIL，RAW 的 DCP 注入在解码层（load_render_pil）完成；此结构
    把 RAW 经 DCP 后的门控/状态透传给 apply_chain_v2，使 base curve 门控
    对 RAW 正确，并供 UI 显式展示 color_pipeline/note。
    """
    color_pipeline: str = "srgb-direct"     # 'dcp:<name>' | 'libraw' | 'srgb-direct'
    note: str = ""                           # 非空 = UI 诊断提示（仅显式 DCP 导入失败；libraw 默认空）
    dcp_has_tone_curve: bool = False
    source_format: str = "srgb8"             # 'raw' | 'srgb8' | 'srgb16'


@dataclass
class RawDecodeMeta:
    """RAW 解码元数据。

    WB 契约（防白平衡施加两次）：仅 **冻结 DCP opt-in 路** 要求白平衡绝不在 rawpy
    前乘、AsShotNeutral/增益原样带出供 DNG 矩阵链一次性施加。
    默认 E1 路 WB 由 libraw 一次性完成（= as-shot，无双重），meta 仅信息性。
    """
    camera_whitebalance: list[float]    # raw.camera_whitebalance
    daylight_whitebalance: list[float]  # raw.daylight_whitebalance
    raw_pattern_shape: tuple[int, int]
    black_level: list[float]
    white_level: int
    camera_model: str
    postprocess_kwargs: dict            # 实际传给 rawpy 的参数，供契约对拍断言
    as_shot_neutral: list[float]        # AsShotNeutral（由 camera_wb 推，DCP 路的 WB 用）
    cam_xyz_matrix: list[list[float]] | None  # libraw cam_xyz，无 DCP 时显式降级用
    reference_size: tuple[int, int] = (0, 0)
    effective_whitebalance: tuple[float, ...] = ()
    color_description: str = ""
    num_colors: int = 0


def _is_raw(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in RAW_EXTENSIONS


def _dcp_opt_in() -> bool:
    """冻结 DCP 仅经显式 env opt-in 进入。决定 RAW 解码走
    E1 默认路（libraw 权威 camera→XYZ）还是冻结 DCP 路（相机原生喂 ForwardMatrix）。
    """
    return bool(os.environ.get("OPS_DCP_PROFILE") or os.environ.get("OPS_DCP_DIR"))


def linear_proxy_decode_identity(path: str) -> tuple[str, ...] | None:
    identity = (f"pillow:{PIL.__version__}", f"numpy:{np.__version__}")
    if _is_raw(path):
        if _dcp_opt_in() or rawpy is None:
            return None
        convert = getattr(ops_accel, "raw_xyz_to_linear_srgb", None) if _HAS_RUST_ACCEL else None
        backend = "native" if convert is not None else "numpy"
        return ("raw-e1-linear-v1", f"rawpy:{rawpy.__version__}",
                "libraw:" + ".".join(map(str, rawpy.libraw_version)), f"conversion:{backend}", *identity)
    if os.path.splitext(path)[1].lower() in (".tif", ".tiff"):
        import tifffile
        return ("tiff-linear-v1", f"tifffile:{tifffile.__version__}", *identity)
    return ("bitmap-linear-v1", *identity)


def supports_raw_initial_proxy(path: str) -> bool:
    if not _is_raw(path) or rawpy is None or _dcp_opt_in():
        return False
    with rawpy.imread(path) as raw:
        sizes = raw.sizes
        return (raw.raw_pattern is not None and raw.raw_pattern.shape == (2, 2)
                and raw_sizes_match_output(sizes) and sizes.flip in (0, 3, 5, 6)
                and sizes.width % 2 == 0 and sizes.height % 2 == 0)


# 默认 RAW 路 = E1（libraw 权威 camera→XYZ(D65) + libraw 一次性 WB）。
# 旧契约（output_color=raw / use_camera_wb=False / user_wb=1，WB 不在 rawpy
# 烘焙）**仅适用于冻结 DCP opt-in 路**（for_dcp=True）：DCP 矩阵链自己施加 WB，
# rawpy 再做一次就成了双重白平衡。E1 下 WB 只由 libraw 做一次，同样不重复。
def _raw_postprocess_kwargs(for_preview: bool, *, for_dcp: bool = False) -> dict:
    kwargs = dict(
        gamma=(1, 1),                       # 关 gamma → 线性
        no_auto_bright=True,                # 关自动提亮 → 保高光余量
        half_size=for_preview,
    )
    if for_dcp:
        # 冻结 DCP opt-in 路：相机原生、WB 留给 DCP 矩阵链
        kwargs["use_camera_wb"] = False             # WB 不在 rawpy 做
        kwargs["user_wb"] = [1.0, 1.0, 1.0, 1.0]    # 显式单位增益，杜绝隐式 WB
        if rawpy is not None:
            kwargs["output_color"] = rawpy.ColorSpace.raw      # 相机原生色
    else:
        # 默认 E1 路：libraw 权威 camera→XYZ(D65) + libraw 一次性 WB
        kwargs["use_camera_wb"] = True              # libraw 做 WB（= as-shot，无双重）
        if rawpy is not None:
            kwargs["output_color"] = rawpy.ColorSpace.XYZ      # libraw 权威 camera→XYZ(D65)
    if rawpy is not None:
        kwargs["output_bps"] = 16  # 预览与全图均保持 16-bit 浮点精度与真实高光 Headroom
    return kwargs


def _camera_model_from_exif(path: str) -> str:
    """从 RAW(TIFF/EP，如 NEF/CR2/ARW/DNG/ORF/PEF/RW2) 的 IFD0 读
    Make(271)/Model(272)。

    rawpy/libraw 不同构建未必暴露 make/model（实测本机 rawpy 无），故自读
    EXIF。仅用于 DCP Profile **自动匹配**。

    **已知边界（有意，非意外）**：仅覆盖 TIFF/EP 容器的 RAW。CR3(ISO-BMFF)、
    RAF(Fuji 私有头) 非 TIFF，会返回 ""——此时机型名缺失，profile 自动匹配
    失败，由 resolve_profile_path 走**显式不静默降级**（color_pipeline=
    'matrix' + RenderSource.note），或用户用 OPS_DCP_PROFILE
    显式指定（不依赖机型名）。即降级路径明确可见，非悄悄出错。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            if head[:2] == b"II":
                end = "<"
            elif head[:2] == b"MM":
                end = ">"
            else:
                return ""
            (ifd_off,) = struct.unpack(end + "I", head[4:8])
            f.seek(ifd_off)
            (n,) = struct.unpack(end + "H", f.read(2))
            entries = f.read(12 * n)
        make = model = ""
        for i in range(n):
            tag, ftype, count = struct.unpack(end + "HHI", entries[i * 12 : i * 12 + 8])
            if tag not in (271, 272) or ftype != 2:
                continue
            (voff,) = struct.unpack(end + "I", entries[i * 12 + 8 : i * 12 + 12])
            with open(path, "rb") as f:
                f.seek(voff)
                s = f.read(min(count, 64)).split(b"\x00", 1)[0].decode("ascii", "replace").strip()
            if tag == 271:
                make = s
            else:
                model = s
        # Model 常已含厂商前缀（如 "NIKON Z 50"）；去重避免 "NIKON NIKON Z 50"
        if make and model and model.upper().startswith(make.upper()):
            return model
        return f"{make} {model}".strip()
    except Exception:
        return ""


def decode_raw_native(
    path: str, *, for_preview: bool = False, for_dcp: bool = False
) -> tuple[np.ndarray, RawDecodeMeta]:
    """RAW 解码。

    **默认 E1（for_dcp=False）**：libraw 权威 camera→XYZ(D65) +
    libraw 一次性 WB ⇒ 返回的 HxWx3 float32（归一 [0,1]）是 **XYZ(D65)**，
    由 `matrix_fallback_to_linear_srgb` 接最后一段 XYZ→线性 sRGB。

    **for_dcp=True（冻结 DCP opt-in 路）**：相机原生
    线性 RGB、WB 不在 rawpy 烘焙（留 DCP 矩阵链一次性施加，避免双重白平衡）。

    两路 meta 一致（camera_whitebalance/AsShotNeutral/cam_xyz 等照常带出）。
    """
    rgb, meta, maxval = _decode_raw_u16(path, for_preview=for_preview, for_dcp=for_dcp)
    return rgb.astype(np.float32) / np.float32(maxval), meta


def _decode_raw_u16(path, *, for_preview=False, for_dcp=False):
    if rawpy is None:
        raise RuntimeError("rawpy 未安装，无法解码 RAW。")

    from . import dcp

    kwargs = _raw_postprocess_kwargs(for_preview, for_dcp=for_dcp)
    with rawpy.imread(path) as raw:
        reference_size = raw_oriented_size(raw.sizes)
        rgb = raw.postprocess(**kwargs)
        bps = kwargs.get("output_bps", 16)
        maxval = float((1 << bps) - 1)
        cam_wb = list(map(float, raw.camera_whitebalance))
        try:
            cam_xyz = np.asarray(raw.rgb_xyz_matrix, dtype=float).tolist()
        except Exception:
            cam_xyz = None
        meta = RawDecodeMeta(
            camera_whitebalance=cam_wb,
            daylight_whitebalance=list(map(float, raw.daylight_whitebalance)),
            raw_pattern_shape=tuple(raw.raw_pattern.shape) if raw.raw_pattern is not None else (0, 0),
            black_level=list(map(float, raw.black_level_per_channel)),
            white_level=int(raw.white_level),
            camera_model=_camera_model_from_exif(path),
            postprocess_kwargs=dict(kwargs),
            as_shot_neutral=dcp.neutral_from_camera_wb(cam_wb).tolist(),
            cam_xyz_matrix=cam_xyz,
            reference_size=reference_size,
            effective_whitebalance=tuple(getattr(raw, "auto_whitebalance", None) or ()),
            color_description=raw.color_desc.decode("ascii", "replace").rstrip("\x00"),
            num_colors=raw.num_colors,
        )
    return rgb, meta, maxval


def _apply_dcp_layer(
    path: str, cam_rgb: np.ndarray, meta: RawDecodeMeta, *, for_preview: bool = False,
    threads: int = 1,
) -> tuple[np.ndarray, str, str, bool]:
    """RAW 解码结果 → 线性 sRGB(D65)，返回 (像素, color_pipeline, note,
    dcp_has_tone_curve)。

    **默认 E1 = libraw 权威 camera→XYZ(D65) → 我方 XYZ→线性
    sRGB**（cam_rgb 即 libraw XYZ，由 for_dcp=False 解码）。任意相机零 profile
    文件、开源干净、比色正确——设计选择**非降级**（color_pipeline='libraw'、
    note=''）。

    DCP 路**已冻结**：默认绝不走自动 resolver；仅当用户**显式**设
    OPS_DCP_PROFILE / OPS_DCP_DIR 时启用（相机原生喂 ForwardMatrix，由
    for_dcp=True 解码）。显式导入解析失败 → 以 E1 重解码回退 libraw 比色
    （默认路即正解，无「非 LR 基准」告警）。
    """
    from . import dcp

    # 冻结：DCP 仅经显式 env opt-in 进入；默认绝不走自动 resolver。
    if _dcp_opt_in():
        prof_path, _source = dcp.resolve_profile_path(meta.camera_model)
        if prof_path is not None:
            try:
                prof = dcp.parse_dcp(prof_path)
                neutral = np.asarray(meta.as_shot_neutral, dtype=np.float64)
                # cam_rgb 此处为相机原生（for_dcp=True 解码），喂 ForwardMatrix。
                out = dcp.render_camera_to_linear_srgb(cam_rgb, neutral, prof)
                name = prof.name or os.path.splitext(os.path.basename(prof_path))[0]
                return out, f"dcp:{name}", "", prof.has_tone_curve
            except Exception as exc:  # 显式导入失败 → 回退默认 E1（非告警态）
                logger.warning("显式 DCP 导入解析失败（%s），回退 libraw 比色：%s",
                               prof_path, exc)
        # opt-in 已开但 DCP 不可用：cam_rgb 是相机原生，E1 需 libraw XYZ →
        # 以 E1 参数重解码（罕见防御路，代价一次重解码换正确比色）。
        out, _ = _decode_e1_linear(path, for_preview=for_preview, threads=threads)
        return out, "libraw", "", False

    # 默认 E1：cam_rgb 是 libraw XYZ(D65) → 我方 XYZ→线性 sRGB（与 libraw 自身 sRGB 输出逐位一致）。
    neutral = np.asarray(meta.as_shot_neutral, dtype=np.float64)
    cam_xyz = (
        np.asarray(meta.cam_xyz_matrix, dtype=np.float64)
        if meta.cam_xyz_matrix is not None
        else None
    )
    out = dcp.matrix_fallback_to_linear_srgb(cam_rgb, neutral, cam_xyz)
    return out, "libraw", "", False


_raw_fallback_logged = False


def _decode_e1_linear(path, *, for_preview=False, threads=1):
    global _raw_fallback_logged
    from . import dcp

    rgb, meta, maxval = _decode_raw_u16(path, for_preview=for_preview)
    convert = getattr(ops_accel, "raw_xyz_to_linear_srgb", None) if _HAS_RUST_ACCEL else None
    if convert is not None:
        pixels = convert(rgb, maxval, dcp.xyz_d65_to_srgb_matrix(), threads=threads)
    else:
        if not _raw_fallback_logged:
            _raw_fallback_logged = True
            logger.warning("RAW 转换 backend=numpy：ops_accel.raw_xyz_to_linear_srgb 不可用")
        xyz = rgb.astype(np.float32) / np.float32(maxval)
        pixels = dcp.matrix_fallback_to_linear_srgb(xyz, None, None)
    return pixels, meta


def load_render_source(path: str, *, for_preview: bool = False, threads: int = 1) -> RenderSource:
    """引擎入口（与 raw_loader.load_image 并存，不替换非引擎调用者）。

    - JPG/PNG/WebP：Pillow 读入 → /255 → sRGB EOTF → 线性 sRGB。
    - RAW：**默认 E1**——libraw 权威 camera→XYZ(D65) → 我方 XYZ→线性
      sRGB（color_pipeline='libraw'，任意相机零文件、比色正确、设计选择非
      降级）。仅显式 OPS_DCP_PROFILE/_DIR opt-in 才走冻结 DCP 路 →
      'dcp:<name>'（相机原生喂 ForwardMatrix）。
    """
    norm = os.path.normpath(path)

    if _is_raw(norm):
        opt_in = _dcp_opt_in()
        if opt_in:
            cam_rgb, meta = decode_raw_native(norm, for_preview=for_preview, for_dcp=True)
            pixels, pipeline, note, has_tc = _apply_dcp_layer(
                norm, cam_rgb, meta, for_preview=for_preview, threads=threads)
        else:
            pixels, meta = _decode_e1_linear(norm, for_preview=for_preview, threads=threads)
            pipeline, note, has_tc = "libraw", "", False
        h, w = pixels.shape[:2]
        return RenderSource(
            pixels=pixels,
            alpha=None,
            source_format="raw",
            source_path=norm,
            is_proxy=for_preview,
            orig_size=meta.reference_size if for_preview else (w, h),
            color_pipeline=pipeline,
            note=note,
            dcp_has_tone_curve=has_tc,
            raw_decode_meta=meta,
        )

    if os.path.splitext(norm)[1].lower() in (".tif", ".tiff"):
        from .render_codec import load_tiff
        return load_tiff(norm)

    with Image.open(norm) as src:
        orig_w, orig_h = src.size
        img = src.copy()

    is_proxy = False
    if for_preview and max(orig_w, orig_h) > 1280:
        scale = 1280.0 / max(orig_w, orig_h)
        nw = max(1, int(round(orig_w * scale)))
        nh = max(1, int(round(orig_h * scale)))
        img = img.resize((nw, nh), Image.Resampling.BILINEAR)
        is_proxy = True

    return _src_from_pil(
        img,
        source_path=norm,
        is_proxy=is_proxy,
        orig_size=(orig_w, orig_h),
    )


def load_render_pil(
    path: str, *, engine: str = "pro", for_preview: bool = False
) -> tuple[Image.Image, RenderMeta]:
    """解码为 display PIL + 色彩管线 sidecar。

    - RAW 文件：load_render_source（经 DCP 层）→ 线性 sRGB → _pil_from（OETF + 单次量化）→ PIL；
      meta 带出 color_pipeline/note/dcp_has_tone_curve。
    - 非 RAW：使用 raw_loader.load_image 加载。
    """
    from .raw_loader import is_raw_file, load_image

    if is_raw_file(path):
        src = load_render_source(path, for_preview=for_preview)
        return _pil_from(src), RenderMeta(
            color_pipeline=src.color_pipeline,
            note=src.note,
            dcp_has_tone_curve=src.dcp_has_tone_curve,
            source_format=src.source_format,
        )

    img = load_image(path, for_preview=for_preview)
    return img, RenderMeta(
        color_pipeline="srgb-direct",
        source_format="srgb8",
    )


# ---------------------------------------------------------------------------
# 入口 A / B + RenderPipeline
# ---------------------------------------------------------------------------

def _src_from_pil(
    image: Image.Image,
    *,
    source_path: str = "",
    is_proxy: bool = False,
    orig_size: tuple[int, int] | None = None,
) -> RenderSource:
    """PIL（display sRGB）→ 线性 RenderSource。alpha 为线性覆盖度，不做 gamma。"""
    alpha = None
    source_format = "srgb8"

    if image.mode == "RGBA":
        rgb = image.convert("RGB")
        alpha = np.asarray(image.split()[3], dtype=np.float32) / np.float32(255.0)
        arr = np.asarray(rgb, dtype=np.float32) / np.float32(255.0)
    elif image.mode == "LA":
        rgba = image.convert("RGBA")
        rgb = rgba.convert("RGB")
        alpha = np.asarray(rgba.split()[3], dtype=np.float32) / np.float32(255.0)
        arr = np.asarray(rgb, dtype=np.float32) / np.float32(255.0)
    elif image.mode == "P":
        if "transparency" in image.info or (getattr(image, "palette", None) and image.palette.mode == "RGBA"):
            rgba = image.convert("RGBA")
            rgb = rgba.convert("RGB")
            alpha = np.asarray(rgba.split()[3], dtype=np.float32) / np.float32(255.0)
        else:
            rgb = image.convert("RGB")
            alpha = None
        arr = np.asarray(rgb, dtype=np.float32) / np.float32(255.0)
    elif image.mode in ("I;16", "I;16L", "I;16B", "I;16N"):
        raw_arr = np.asarray(image, dtype=np.float32)
        arr = np.clip(raw_arr / np.float32(65535.0), 0.0, 1.0)
        if arr.ndim == 2:
            arr = np.stack([arr, arr, arr], axis=-1)
        source_format = "srgb16"
    elif image.mode == "RGB":
        arr = np.asarray(image, dtype=np.float32) / np.float32(255.0)
    else:
        rgb = image.convert("RGB")
        arr = np.asarray(rgb, dtype=np.float32) / np.float32(255.0)

    pixels = srgb_to_linear(arr)
    w, h = image.size
    return RenderSource(
        pixels=pixels,
        alpha=alpha,
        source_format=source_format,
        source_path=source_path,
        is_proxy=is_proxy,
        orig_size=orig_size if orig_size is not None else (w, h),
        color_pipeline="srgb-direct",
    )


def _pil_from(src: RenderSource, *, parallelism: int = 1) -> Image.Image:
    """线性 RenderSource → PIL（display sRGB, 8-bit），出口前 clip + 单次量化。"""
    def convert(pixels):
        disp = linear_to_srgb(pixels)
        np.clip(disp, 0.0, 1.0, out=disp)
        np.multiply(disp, np.float32(255.0), out=disp)
        np.rint(disp, out=disp)
        return disp.astype(np.uint8)

    height = src.pixels.shape[0]
    bands = min(parallelism, height)
    if bands <= 1:
        rgb8 = convert(src.pixels)
    else:
        rgb8 = np.empty(src.pixels.shape, dtype=np.uint8)

        def convert_band(index):
            start, end = index * height // bands, (index + 1) * height // bands
            rgb8[start:end] = convert(src.pixels[start:end])

        with ThreadPoolExecutor(max_workers=bands, thread_name_prefix="ops-display") as pool:
            tuple(pool.map(convert_band, range(bands)))
    out = Image.fromarray(rgb8, mode="RGB")
    if src.alpha is not None:
        a8 = np.round(np.clip(src.alpha, 0.0, 1.0) * np.float32(255.0)).astype(np.uint8)
        out = out.convert("RGBA")
        out.putalpha(Image.fromarray(a8, mode="L"))
    return out


_display_fallback_logged = False


def display_rgba8(src: RenderSource, *, parallelism: int = 1) -> np.ndarray:
    """线性 RenderSource → 显示用 RGBA8（HxWx4 uint8，只读）。

    语义同 _pil_from（OETF → clip → round(x*255)，Alpha 直通，无 Alpha 时为 255）。
    原生函数与 _pil_from 的 8 位结果逐像素相差不超过 1 LSB；原生函数不可用时走 _pil_from。
    """
    global _display_fallback_logged
    convert = getattr(ops_accel, "linear_to_display_rgba8", None) if _HAS_RUST_ACCEL else None
    if convert is not None:
        rgba = convert(np.ascontiguousarray(src.pixels, dtype=np.float32),
                       None if src.alpha is None else np.ascontiguousarray(src.alpha, dtype=np.float32),
                       threads=parallelism)
    else:
        if not _display_fallback_logged:
            _display_fallback_logged = True
            logger.warning("显示转换 backend=numpy：ops_accel.linear_to_display_rgba8 不可用")
        with _pil_from(src, parallelism=parallelism) as image:
            if image.mode == "RGBA":
                rgba = np.array(image)
            else:
                with image.convert("RGBA") as converted:
                    rgba = np.array(converted)
    rgba.setflags(write=False)
    return rgba


# ---------------------------------------------------------------------------
# Phase 1 — Tier 1 float 算子（RenderPipeline 所列顺序的子集；局部 tone/WB/texture 见 Phase 3/5）
#
# 设计约束：
#   - 曝光在**线性光**做（sRGB 编码域里做乘法不是物理曝光），高光走 filmic 软肩而非硬裁。
#   - 对比/曲线/鲜艳/饱和在**感知（sRGB 编码）域**做——用 bracket：
#     linear→sRGB 一次进、一组算子全程 float 不量化、sRGB→linear 一次出，
#     最终唯一 8-bit 量化仍在 _pil_from（消除步间 8-bit 量化累积出的虫纹与暗部发灰）。
#   - 鲜艳/饱和按 luma 等增益缩放 chroma → 保色相。
#   - **零调整 → 逐位往返 ≤1 LSB**：每个算子在中性参数处恒等跳过；
#     base tone curve 在 Phase 1 对 sRGB 为恒等占位（给 JPG 无端加 S 会违反
#     「JPG 起点像素与原图相同」；RAW 的 look 属于 DCP，Phase 2 才接）。
# ---------------------------------------------------------------------------

def _white_balance_gain(temp: int, tint: int) -> np.ndarray:
    """线性光 von-Kries 白平衡对角增益（含感知亮度守恒归一化）。

    temp: -100 ~ +100（-100 对应 2750K 极冷深蓝，+100 对应 11000K+ 浓郁夕阳暖黄，±1.35 EV 跨度）；
    tint: -100 ~ +100（负偏绿、正偏洋红，±0.80 EV G 通道对数调节）。
    """
    t_ev = float(np.clip(temp, -100, 100)) / 100.0 * 1.35
    tint_ev = float(np.clip(tint, -100, 100)) / 100.0 * 0.80

    kr = np.float32(2.0 ** t_ev)
    kg = np.float32(2.0 ** (-tint_ev))
    kb = np.float32(2.0 ** (-t_ev))

    # 亮度守恒保护：防止极暖时黄色高亮过曝炸白，或极冷时画面明显塌陷变暗
    lum_scale = np.float32(0.2126) * kr + np.float32(0.7152) * kg + np.float32(0.0722) * kb
    lum_scale = np.maximum(lum_scale, np.float32(1e-4))

    return np.array([kr / lum_scale, kg / lum_scale, kb / lum_scale], dtype=np.float32)


def white_balance_gain(temp: int, tint: int) -> np.ndarray:
    return _white_balance_gain(temp, tint)


def _white_balance(lin: np.ndarray, temp: int, tint: int, *, out=None, gain=None) -> np.ndarray:
    """应用线性光白平衡增益，独占输出允许复用输入缓冲。"""
    if temp == 0 and tint == 0:
        if out is not None:
            np.copyto(out, lin)
            return out
        return lin
    if gain is None:
        gain = _white_balance_gain(temp, tint)
    if out is None:
        return (lin * gain).astype(np.float32)
    return np.multiply(lin, gain, out=out)


def _exposure_gain(ev: float, process_version: int) -> np.float32:
    ev = float(np.clip(ev, -10.0, 10.0))
    if process_version == 2:
        ev = float(np.float32(ev))
    return np.float32(2.0 ** ev)


def _exposure_v2(lin: np.ndarray, ev: float, *, gain=None) -> np.ndarray:
    if ev == 0:
        return lin
    if gain is None:
        gain = _exposure_gain(ev, 2)
    if gain <= np.float32(1.0):
        return lin * gain
    one, knee = np.float32(1.0), np.float32(0.75)
    white = np.sqrt(gain)
    coefficient = (white - one) / ((one - knee) * (white - knee))

    def shoulder(values):
        y = values * gain
        distance = np.maximum(y - knee, np.float32(0.0))
        return np.minimum(np.where(y <= knee, y, knee + distance / (one + distance * coefficient)), one)

    rgb = np.clip(lin, np.float32(0.0), one)
    high = rgb.max(axis=-1, keepdims=True)
    low = rgb.min(axis=-1, keepdims=True)
    span = high - low
    high_out = shoulder(high)
    low_out = shoulder(low)
    np.subtract(rgb, low, out=rgb)
    np.divide(rgb, span, out=rgb, where=span > np.float32(0.0))
    np.multiply(rgb, high_out - low_out, out=rgb)
    np.add(rgb, low_out, out=rgb)
    return rgb


def _exposure(lin: np.ndarray, ev: float, process_version: int = 1, *, gain=None) -> np.ndarray:
    """按处理版本执行线性曝光。"""
    if process_version == 2:
        return _exposure_v2(lin, ev, gain=gain)
    if process_version != 1:
        raise ValueError("曝光处理版本无效")
    if ev == 0:
        return lin
    if gain is None:
        gain = _exposure_gain(ev, 1)
    y = lin * gain
    knee = np.float32(0.75)
    s = np.float32(0.7)  # 软肩强度，渐近线 = knee+s
    over = y > knee
    y_sh = knee + s * (1.0 - np.exp(-(y - knee) / s))
    return np.where(over, y_sh, y).astype(np.float32)


def _contrast(d: np.ndarray, factor: float) -> np.ndarray:
    """感知域 mid-gray(0.5) pivot 单调 S：d^a/(d^a+(1-d)^a)，a=factor。

    a=1 恒等；a>1 增对比、a<1 降；端点 0/1 不动 → 无硬裁（优于 PIL 线性对比）。
    """
    if factor == 1.0:
        return d
    a = np.float32(max(1e-3, float(factor)))
    d = np.clip(d, 0.0, 1.0)
    da = np.power(d, a)
    ia = np.power(1.0 - d, a)
    return (da / (da + ia + np.float32(1e-12))).astype(np.float32)


def _fritsch_carlson_lut(points: list[tuple[int, int]], n: int = 1024) -> np.ndarray:
    """Fritsch-Carlson 单调三次 Hermite → float LUT（classic 算法平移到 float）。

    points 为 UI display 0..255 控制点；返回长度 n、定义域/值域 [0,1] 的单调 LUT。
    """
    pts = sorted(points, key=lambda p: p[0])
    xs = np.array([p[0] for p in pts], dtype=np.float64) / 255.0
    ys = np.array([p[1] for p in pts], dtype=np.float64) / 255.0
    k = len(pts)
    if k < 2:
        return np.linspace(0.0, 1.0, n, dtype=np.float32)

    dx = np.diff(xs)
    delta = np.diff(ys) / np.where(dx == 0, 1.0, dx)
    m = np.zeros(k, dtype=np.float64)
    m[0], m[-1] = delta[0], delta[-1]
    for i in range(1, k - 1):
        m[i] = 0.0 if delta[i - 1] * delta[i] <= 0 else (delta[i - 1] + delta[i]) / 2.0
    for i in range(k - 1):
        if abs(delta[i]) < 1e-12:
            m[i] = m[i + 1] = 0.0
            continue
        a_i, b_i = m[i] / delta[i], m[i + 1] / delta[i]
        if a_i < 0:
            m[i] = 0.0
        if b_i < 0:
            m[i + 1] = 0.0
        tau = a_i * a_i + b_i * b_i
        if tau > 9.0:
            sc = 3.0 / np.sqrt(tau)
            m[i] = sc * a_i * delta[i]
            m[i + 1] = sc * b_i * delta[i]

    grid = np.linspace(0.0, 1.0, n)
    seg = np.clip(np.searchsorted(xs, grid, side="right") - 1, 0, k - 2)
    x0, x1 = xs[seg], xs[seg + 1]
    y0, y1 = ys[seg], ys[seg + 1]
    h = np.where((x1 - x0) < 1e-12, 1.0, x1 - x0)
    t = (grid - x0) / h
    t2, t3 = t * t, t * t * t
    out = ((2 * t3 - 3 * t2 + 1) * y0
           + (t3 - 2 * t2 + t) * h * m[seg]
           + (-2 * t3 + 3 * t2) * y1
           + (t3 - t2) * h * m[seg + 1])
    return np.clip(out, 0.0, 1.0).astype(np.float32)


_IDENTITY_CURVE = [(0, 0), (255, 255)]


def curve_lut(points, n=1024):
    """显示编码域控制点的 Core 插值表，定义域和值域为 0—1。"""
    return _fritsch_carlson_lut(points, n)


def _apply_curve_channel(ch: np.ndarray, pts) -> np.ndarray:
    if not pts or pts == _IDENTITY_CURVE:
        return ch
    lut = _fritsch_carlson_lut(pts)
    idx = np.clip(ch, 0.0, 1.0) * (len(lut) - 1)
    return np.interp(idx, np.arange(len(lut)), lut).astype(np.float32)


def _apply_curves(d: np.ndarray, curves: dict | None) -> np.ndarray:
    """display 域曲线：先 rgb 复合再 r/g/b 单通道（与 classic 同序）。"""
    if not curves:
        return d
    rgb = curves.get("rgb")
    if rgb and rgb != _IDENTITY_CURVE:
        lut = _fritsch_carlson_lut(rgb)
        idx = np.clip(d, 0.0, 1.0) * (len(lut) - 1)
        d = np.interp(idx, np.arange(len(lut)), lut).astype(np.float32)
    out = d.copy()
    for i, key in enumerate(("r", "g", "b")):
        out[..., i] = _apply_curve_channel(d[..., i], curves.get(key))
    return out


def _saturation(d: np.ndarray, factor: float) -> np.ndarray:
    """保色相饱和：绕 Rec.709 display 亮度等增益缩放 chroma。"""
    if factor == 1.0:
        return d
    lum = (d @ _REC709_LUMA)[..., None]
    return (lum + np.float32(factor) * (d - lum)).astype(np.float32)


def _vibrance(d: np.ndarray, amount: int) -> np.ndarray:
    """鲜艳：低饱和优先 + 近中性软死区（沿用 classic「x+(255-x)·k」与 WB 软死区的思路）。

    关键修正：朴素的 (1-sat) 权重在 sat→0 处给**最大**增益，等于把近中性区
    （平坦天空/肤色）的 chroma **噪声**放到最大——出彩虹麻点。改为对饱和度
    加软死区：s<S0（纯中性/噪声）几乎不动，S0→S1 平滑起步，之后才按 (1-s)
    给低饱和加成、对已高饱和回落。既保 LR「提低饱和、护高饱和」手感，又不
    放大平坦区噪声。S0/S1/k 为 Phase 4 对 LR 截图标定的旋钮。
    """
    if amount == 0:
        return d
    strength = float(amount) / 100.0
    lum = (d @ _REC709_LUMA)[..., None]
    chroma = d - lum
    s = np.clip(d.max(axis=-1) - d.min(axis=-1), 0.0, 1.0)[..., None]
    if strength > 0:
        s0, s1 = np.float32(0.03), np.float32(0.12)          # 近中性软死区
        t = np.clip((s - s0) / (s1 - s0), 0.0, 1.0)
        w_lo = t * t * (3.0 - 2.0 * t)                       # smoothstep：两端零斜率
        gain = 1.0 + (strength * 0.6) * w_lo * (1.0 - s)     # 低饱和峰、高饱和落
    else:
        gain = 1.0 + strength * 0.75                          # 负向整体降饱和
    return (lum + chroma * np.float32(gain)).astype(np.float32)


def _hue_deg(d: np.ndarray) -> np.ndarray:
    """标准 HSV 色相角 [0,360)，向量化。

    观感的色相增益分箱按此定义标定。
    """
    r, g, b = d[..., 0], d[..., 1], d[..., 2]
    mx = d.max(-1)
    mn = d.min(-1)
    diff = mx - mn
    h = np.zeros_like(mx)
    safe = diff > 1e-8
    rm = safe & (mx == r)
    gm = safe & (mx == g) & ~rm
    bm = safe & (mx == b) & ~rm & ~gm
    dd = np.where(safe, diff, 1.0)
    h[rm] = (60.0 * ((g[rm] - b[rm]) / dd[rm]) + 360.0) % 360.0
    h[gm] = (60.0 * ((b[gm] - r[gm]) / dd[gm]) + 120.0)
    h[bm] = (60.0 * ((r[bm] - g[bm]) / dd[bm]) + 240.0)
    return h


def _apply_look_stage(d: np.ndarray, stage: LookStage) -> np.ndarray:
    """一层 OPS 观感（core/looks.py 定义），施于 display sRGB。

    分两步施加：
      ① apply_tone     —— 等增益保色相施影调曲线（luma L→L'，乘性增益到 RGB；
                           保色相方向 ⇒ 防虫纹/不发灰，黑场锚 0）；
      ② apply_hue_sat  —— per-hue chroma 增益（保亮度、保色相方向，仅缩放
                           饱和；循环插值连续）。
    每步与该步输入凸组合（强度∈[0,1]）：单调函数凸组合仍单调、共色相方向凸组合
    仍同色相 ⇒ 单调/保色相不变量对任意强度成立。
    """
    s_tone = np.clip(np.float32(stage.tone_strength), 0.0, 1.0)
    s_sat = np.clip(np.float32(stage.sat_strength), 0.0, 1.0)
    x = np.clip(d, 0.0, 1.0).astype(np.float32)

    if s_tone > np.float32(0.0):
        lut = np.asarray(stage.tone_lut, dtype=np.float32)
        xs = np.linspace(0.0, 1.0, len(lut)).astype(np.float32)
        L = x @ _REC709_LUMA
        Lp = np.interp(L, xs, lut).astype(np.float32)
        gain = (Lp / np.maximum(L, np.float32(1e-4)))[..., None]
        t_full = np.clip(x * gain, 0.0, 1.0).astype(np.float32)
        t = ((np.float32(1.0) - s_tone) * x + s_tone * t_full).astype(np.float32)
    else:
        t = x

    if s_sat > np.float32(0.0):
        sat = np.asarray(stage.hue_sat, dtype=np.float32)
        nb = len(sat)
        binw = 360.0 / nb
        centers = (np.arange(nb) + 0.5) * binw
        xp = np.concatenate([[centers[-1] - 360.0], centers, [centers[0] + 360.0]])
        fp = np.concatenate([[sat[-1]], sat, [sat[0]]])
        Lt = t @ _REC709_LUMA
        hg = np.interp(_hue_deg(t), xp, fp).astype(np.float32)[..., None]
        chroma = t - Lt[..., None]
        looked_sat = np.clip(Lt[..., None] + hg * chroma, 0.0, 1.0).astype(np.float32)
        return ((np.float32(1.0) - s_sat) * t + s_sat * looked_sat).astype(np.float32)
    return t


def _apply_look(d: np.ndarray, stages: tuple[LookStage, ...]) -> np.ndarray:
    for stage in stages:
        d = _apply_look_stage(d, stage)
    return d


def _gamut_compress(d: np.ndarray) -> np.ndarray:
    """保色相 gamut 压缩：沿 chroma 方向等比缩放使 RGB ∈ [0,1]。"""
    lum = (d @ _REC709_LUMA)[..., None]       # HxWx1
    chroma = d - lum                           # HxWx3
    eps = np.float32(1e-8)
    pos = chroma > eps
    neg = chroma < -eps

    safe_hi = np.where(pos, (1.0 - lum) / np.maximum(chroma, eps), np.float32(1e6))
    safe_lo = np.where(neg, -lum / np.minimum(chroma, -eps), np.float32(1e6))
    max_s = np.clip(np.minimum(safe_hi, safe_lo).min(axis=-1, keepdims=True), 0.0, 1.0)
    return (lum + chroma * max_s).astype(np.float32)


# ---------------------------------------------------------------------------
# Phase 3 — Tier 2 局部 tone（不变量：单调连续、保色相、不发灰、阴影+ 不抬黑场）
#
# 设计要点（与 classic _build_zone_lut/_apply_lut_rgb 语义平移，但**局部边缘
# 感知**而非全局 luma LUT——全局 LUT 压天空会连白裙一起压）：
#   - 区间控件（阴影 0-128 / 高光 128-255，point_control=False）：Hann 窗
#     sin(πd)，锚定极值端与离开端**都→0**，峰值在区间内部。施加在
#     **guided-filter base** 上，detail 原样加回 → 保微对比、不发灰（防
#     milky-haze：阴影+ 不抬绝对黑场，由 sin 在 L=0 处=0 保证）。
#   - 点控件（黑 0-80 / 白 175-255，point_control=True）：权重 1−d 在极值
#     端最强，**设定**黑/白点；走全局 L（端点锚定须空间无关，
#     否则 milky-haze/黑场不变量变成位置相关、回归不可钉死）。
#   - 一律 luma 等增益施加到 RGB（保色相，防虫纹）；深暗部色度≈0 时
#     增益爆炸，按亮度在乘法保色相与中性加法间平滑过渡（平移 _apply_lut_rgb）。
#   - texture/clarity/dehaze：复用同一组 guided-filter base 构成 fine→mid→
#     coarse 多尺度金字塔（复用 detail 层）。每层 fast_guided_filter
#     的 box filter 全在降采样分辨率算、仅末步 mean_a*guide+mean_b 走 full-res
#     guide（= joint upsampling；重 full-res box-filter GF 调用
#     数 = 0）。
#
# 偏移/羽化常数（45/255 峰值、16/255 暗部混合膝点、各尺度半径/eps）为 classic
# 既有值平移的**起点**；渲染链一变，Auto/pro 常数须对 LR 截图重标。
# Phase 4 客观基建：把这些散落字面量集中进 ProCalib 单一表（算法码不再出现
# 裸感知字面量），Phase 4 主观标定只动该表、classic 在 color_adjuster.py
# 物理隔离不受影响（A/B 锚 = 不同文件 + 不同符号 + 数值上等价回放）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _KernelSpec:
    """guided-filter 尺度核：半径 = max(r_floor, minhw // r_div)，
    子采样 = int(clip(minhw / s_div, s_lo, s_hi))。返回与原内联表达式逐位等价。
    """

    r_floor: int
    r_div: int
    s_div: float
    s_lo: int
    s_hi: int

    def __call__(self, minhw: int) -> tuple[int, int]:
        r = max(self.r_floor, minhw // self.r_div)
        s = int(np.clip(minhw / self.s_div, self.s_lo, self.s_hi))
        return r, s


@dataclass(frozen=True)
class ProCalib:
    """Phase 4 可标定常数集中表（渲染链一变，表内常数须重新标定）。

    当前值 = classic 体感平移**起点/锚**（color_adjuster._build_zone_lut /
    _apply_lut_rgb 同值）。Phase 4 在本表内对 LR 截图逐张重标，**算法码不再
    出现裸感知字面量**；classic 在 color_adjuster.py 完全不受本表影响（A/B 锚，
    新旧常数物理隔离 = 不同文件 + 不同符号）。本轮仅集中化，数值未动 ⇒ pro
    输出逐位不变（Phase 2/3 sign-off 保持，回归测试钉死）。

    有意排除（保留为算法内字面量，**非本表标定面**）：
      - 局部 tone base eps 0.04²：guided filter 的固定设定，与渲染链标定**解耦**，永不重标。
      - 固定 zone 边界 128/80/175（_Z_*）：classic _build_zone_lut 语义分区；
        adaptive pivot 仅经 localtone_adaptive 显式门控进入，默认不改现路。
      - filmic 肩 knee/s、数值防爆 eps（1e-3/255、1e-8 等）：spec/数值，非感知。
    """

    # —— 局部 tone（_zone_offset / _apply_equal_gain）——
    zone_peak: float = 45.0 / 255.0     # ±100 滑块峰值亮度偏移
    gain_knee: float = 16.0 / 255.0     # 暗部 乘法↔加法 混合膝点
    localtone_kernel: _KernelSpec = _KernelSpec(4, 48, 256.0, 2, 6)
    localtone_adaptive: bool = False    # False = 固定 zone A/B 锚；True = p50 自适应 pivot
    localtone_adapt_k: float = 0.60     # z_mid = fixed + k·(p50-fixed)，保守但有效
    localtone_adapt_radius: float = 0.18  # pivot 相对固定 128/255 的最大位移
    localtone_span_min: float = 0.20    # shadows/highlights 最小有效区间宽度
    localtone_flat_eps: float = 8.0 / 255.0  # p90-p10 小于此视为塌缩，回落固定锚
    # —— detail 金字塔（_detail_effects）He et al. 细节增强档 ——
    tex_eps: float = 0.10 ** 2
    tex_strength: float = 1.0           # ×(texture/100)
    tex_kernel: _KernelSpec = _KernelSpec(2, 256, 512.0, 1, 4)
    clarity_eps: float = 0.14 ** 2
    clarity_strength: float = 0.8       # ×(clarity/100)
    clarity_kernel: _KernelSpec = _KernelSpec(4, 24, 200.0, 2, 6)
    dehaze_eps: float = 0.20 ** 2
    dehaze_strength: float = 0.5        # ×(dehaze/100)
    dehaze_sat: float = 0.10            # 正去朦胧增饱和 ×(dehaze/100)
    dehaze_kernel: _KernelSpec = _KernelSpec(8, 8, 128.0, 4, 12)


PRO_CALIB = ProCalib()

# 从单一表派生原 float32 量化常数：np.float32(PRO_CALIB.x) 与旧 np.float32(字面量)
# 数值完全相同 ⇒ 下游算术逐位一致（pro Phase 2/3 sign-off 不回归）。
_ZONE_PEAK = np.float32(PRO_CALIB.zone_peak)   # ±100 滑块峰值亮度偏移（classic 同值）
_GAIN_KNEE = np.float32(PRO_CALIB.gain_knee)   # 暗部乘法↔加法混合膝点（classic 同值）
_Z_SHADOWS = (np.float32(0.0), np.float32(128.0 / 255.0))
_Z_HIGHLIGHTS = (np.float32(128.0 / 255.0), np.float32(1.0))
_Z_BLACKS = (np.float32(0.0), np.float32(80.0 / 255.0))
_Z_WHITES = (np.float32(175.0 / 255.0), np.float32(1.0))
_Z_LOCALTONE_PIVOT = np.float32(128.0 / 255.0)


def _percentile_256(x: np.ndarray, pct: float) -> np.float32:
    """256-bin 直方图百分位，匹配 classic UI 直方图口径并避免纹理级抖动。"""
    arr = np.asarray(x, dtype=np.float32).reshape(-1)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return np.float32(0.0)

    q = np.clip(np.rint(np.clip(arr, 0.0, 1.0) * np.float32(255.0)), 0, 255).astype(np.uint8)
    hist = np.bincount(q, minlength=256)
    target = float(hist.sum()) * float(np.clip(pct, 0.0, 1.0))
    idx = int(np.searchsorted(np.cumsum(hist), target, side="left"))
    return np.float32(np.clip(idx, 0, 255) / 255.0)


def _localtone_adaptive_pivot(
    base: np.ndarray, calib: ProCalib = PRO_CALIB
) -> np.float32:
    """Task A：按低频 base 的 p50 生成 shadows/highlights 共用自适应 pivot。

    flag=False 或平图退化时返回固定 128/255，确保现有固定 zone 路逐位可锚。
    """
    fixed = _Z_LOCALTONE_PIVOT
    if not calib.localtone_adaptive:
        return fixed

    p10 = _percentile_256(base, 0.10)
    p50 = _percentile_256(base, 0.50)
    p90 = _percentile_256(base, 0.90)
    if float(p90 - p10) < float(calib.localtone_flat_eps):
        return fixed

    k = np.float32(np.clip(float(calib.localtone_adapt_k), 0.0, 1.0))
    radius = np.float32(max(0.0, float(calib.localtone_adapt_radius)))
    span_min = np.float32(np.clip(float(calib.localtone_span_min), 1e-3, 0.49))

    raw = fixed + k * (p50 - fixed)
    lo = max(float(fixed - radius), float(span_min))
    hi = min(float(fixed + radius), float(1.0 - span_min))
    if lo > hi:
        return fixed
    return np.float32(np.clip(raw, lo, hi))


def _localtone_zones(
    base: np.ndarray, calib: ProCalib = PRO_CALIB
) -> tuple[tuple[np.float32, np.float32], tuple[np.float32, np.float32]]:
    """返回 shadows/highlights 的 zone；首版 KISS：二者共用同一个 z_mid。"""
    z_mid = _localtone_adaptive_pivot(base, calib)
    return (np.float32(0.0), z_mid), (z_mid, np.float32(1.0))


def _zone_offset(
    L: np.ndarray,
    z0: float,
    z1: float,
    amount: int,
    *,
    point_control: bool,
    bright_anchored: bool,
) -> np.ndarray:
    """classic _build_zone_lut 的向量化连续平移：返回 display [0,1] 域的亮度
    偏移数组（zone 外恒 0、处处连续、离开端羽化→0，保单调不回折）。

    d: 0 = 锚定极值端（黑场/白点），1 = 离开端（中间调侧）。
      - point_control=False（区间）：w=sin(πd)，两端→0，峰值在内部；
      - point_control=True （端点）：w=1−d，极值端最强（设定黑/白点）。
    smoothstep + 峰值 _ZONE_PEAK，与 classic 逐项对应。
    """
    if amount == 0:
        return np.zeros_like(L)
    span = z1 - z0
    u = np.clip((L - z0) / span, 0.0, 1.0)
    d = (1.0 - u) if bright_anchored else u
    w = (1.0 - d) if point_control else np.sin(np.float32(np.pi) * d)
    in_zone = (L >= z0) & (L <= z1)
    w = np.where(in_zone, np.clip(w, 0.0, 1.0), np.float32(0.0))
    w = w * w * (np.float32(3.0) - np.float32(2.0) * w)        # smoothstep（classic 同）
    return (np.float32(amount) / np.float32(100.0) * w * _ZONE_PEAK).astype(np.float32)


def _apply_equal_gain(d: np.ndarray, L: np.ndarray, Lp: np.ndarray) -> np.ndarray:
    """以保色相方式把亮度 L→Lp 的变换施加到 display RGB（平移 _apply_lut_rgb）。

    乘法等增益保 R:G:B 比例 → 保色相/不引虫纹；深暗部色度≈0 时增益爆炸放大
    色噪，故按亮度在「乘法保色相」与「中性加法」间 smooth 过渡（classic 同款）。
    """
    eps = np.float32(1e-3 / 255.0)
    gain = (Lp / np.maximum(L, eps))[..., None]
    delta = (Lp - L)[..., None]
    blend = np.clip(L / _GAIN_KNEE, 0.0, 1.0)[..., None]
    out = blend * (d * gain) + (np.float32(1.0) - blend) * (d + delta)
    return out.astype(np.float32)


def _local_tone(d: np.ndarray, params: dict) -> np.ndarray:
    """局部高光/阴影/白/黑：区间控件在 guided-filter base 上推、detail
    原样加回（边缘感知、保微对比）；点控件走全局 L 锚定端点。恒等参数跳过。
    """
    sh = int(params.get("shadows", 0))
    hl = int(params.get("highlights", 0))
    bk = int(params.get("blacks", 0))
    wh = int(params.get("whites", 0))
    if sh == 0 and hl == 0 and bk == 0 and wh == 0:
        return d

    L = (d @ _REC709_LUMA).astype(np.float32)   # display 域 Rec.709 luma（与 pro 其余感知域算子一致）
    h, w = L.shape
    minhw = min(h, w)

    if sh != 0 or hl != 0:
        # 边缘感知 tone base：单次 fast guided filter（重 GF 封顶 1）。
        # 核尺度集中进 ProCalib；eps=0.04² 是固定设定，与标定解耦留字面量。
        r, s = PRO_CALIB.localtone_kernel(minhw)
        base = fast_guided_filter(L, L, r, 0.04 ** 2, s)
        detail = L - base
        z_sh, z_hl = _localtone_zones(base, PRO_CALIB)
        b = base
        if sh != 0:
            b = b + _zone_offset(base, *z_sh, sh,
                                 point_control=False, bright_anchored=False)
        if hl != 0:
            b = b + _zone_offset(base, *z_hl, hl,
                                 point_control=False, bright_anchored=True)
        Lr = (b + detail).astype(np.float32)    # detail 原样加回 → 不发灰
    else:
        Lr = L

    # 点控件走全局 luma（端点锚定须空间无关）。
    Lp = Lr
    if bk != 0:
        Lp = Lp + _zone_offset(Lr, *_Z_BLACKS, bk,
                               point_control=True, bright_anchored=False)
    if wh != 0:
        Lp = Lp + _zone_offset(Lr, *_Z_WHITES, wh,
                               point_control=True, bright_anchored=True)
    Lp = np.clip(Lp, 0.0, 1.0).astype(np.float32)

    return _apply_equal_gain(d, L, Lp)


def _detail_effects(d: np.ndarray, params: dict) -> np.ndarray:
    """texture/clarity/dehaze/sharpness：复用 fine→mid→coarse 多尺度 guided-filter
    金字塔（base 逐层复用，"复用 detail 层"）。所有 box filter 在降采样域算，
    末步 full-res guide 上采样（性能路径）。亮度域处理后等增益保色相；
    正 dehaze 伴随轻微增饱和（classic 同款语义）。恒等参数跳过。
    """
    tx = int(params.get("texture", 0))
    shp = float(params.get("sharpness", 1.0))
    if shp != 1.0:
        tx = int(np.clip(tx + int(round((shp - 1.0) * 250)), -100, 100))
    cl = int(params.get("clarity", 0))
    dz = int(params.get("dehaze", 0))
    if tx == 0 and cl == 0 and dz == 0:
        return d

    L = (d @ _REC709_LUMA).astype(np.float32)
    h, w = L.shape
    minhw = min(h, w)
    Lp = L.copy()

    # eps 取 He et al. 细节增强档（~0.1²–0.2²）：guide==src 时 eps≪局部方差
    # 会使 a≈1、base≈L、各 detail 带能量≈0；细节金字塔必须让 base 是该尺度
    # 的真实低通。eps/半径/强度集中进 PRO_CALIB（classic 平移起点，数值未动 ⇒
    # 逐位不变）；渲染链变化后由 Phase 4 在该表内对 LR 截图重标。
    cb = PRO_CALIB
    if tx != 0 or cl != 0:
        r_f, s_f = cb.tex_kernel(minhw)
        base_fine = fast_guided_filter(L, L, r_f, cb.tex_eps, s_f)
        if tx != 0:                              # 细节带：小半径残差
            Lp = Lp + np.float32(cb.tex_strength * tx / 100.0) * (L - base_fine)
        if cl != 0:                              # 中尺度带：复用 base_fine
            r_m, s_m = cb.clarity_kernel(minhw)
            base_mid = fast_guided_filter(L, L, r_m, cb.clarity_eps, s_m)
            Lp = Lp + np.float32(cb.clarity_strength * cl / 100.0) * (base_fine - base_mid)

    if dz != 0:                                  # 大尺度去朦胧：粗 base 的局部对比
        r_c, s_c = cb.dehaze_kernel(minhw)
        base_coarse = fast_guided_filter(L, L, r_c, cb.dehaze_eps, s_c)
        Lp = Lp + np.float32(cb.dehaze_strength * dz / 100.0) * (L - base_coarse)

    Lp = np.clip(Lp, 0.0, 1.0).astype(np.float32)
    out = _apply_equal_gain(d, L, Lp)

    if dz > 0:                                   # 正去朦胧轻微增饱和（classic：Color +0.10·s）
        lum = (out @ _REC709_LUMA)[..., None]
        out = (lum + (np.float32(1.0) + np.float32(cb.dehaze_sat * dz / 100.0)) * (out - lum)).astype(np.float32)
    return out


_LOCAL_TONE_KEYS = ("highlights", "shadows", "whites", "blacks")
_DETAIL_KEYS = ("texture", "clarity", "dehaze")


def minimum_render_threads(params, operator_threads):
    if (any(params.get(key, 0) for key in _LOCAL_TONE_KEYS + _DETAIL_KEYS)
            or params.get("sharpness", 1.0) != 1.0
            or any(mask.get("enabled", True) for mask in params.get("masks") or ())):
        return operator_threads
    return 1

# ---------------------------------------------------------------------------
# Color Mixer: 8色保亮度色彩混合器 (Luma-Chroma 投影空间)
# ---------------------------------------------------------------------------

def _hsl_to_rgb(H: np.ndarray, S: np.ndarray, L: np.ndarray) -> np.ndarray:
    """将 H (0..360), S (0..1), L (0..1) 转换回 RGB [0, 1]。"""
    C = (np.float32(1.0) - np.abs(np.float32(2.0) * L - np.float32(1.0))) * S
    h = (H / np.float32(60.0)) % np.float32(6.0)
    X = C * (np.float32(1.0) - np.abs((h % np.float32(2.0)) - np.float32(1.0)))
    m = L - (C * np.float32(0.5))

    hi = np.floor(h).astype(np.int32) % 6
    r1 = np.zeros_like(L)
    g1 = np.zeros_like(L)
    b1 = np.zeros_like(L)

    mask0 = hi == 0
    r1[mask0] = C[mask0]
    g1[mask0] = X[mask0]

    mask1 = hi == 1
    r1[mask1] = X[mask1]
    g1[mask1] = C[mask1]

    mask2 = hi == 2
    g1[mask2] = C[mask2]
    b1[mask2] = X[mask2]

    mask3 = hi == 3
    g1[mask3] = X[mask3]
    b1[mask3] = C[mask3]

    mask4 = hi == 4
    r1[mask4] = X[mask4]
    b1[mask4] = C[mask4]

    mask5 = hi == 5
    r1[mask5] = C[mask5]
    b1[mask5] = X[mask5]

    r = np.clip(r1 + m, 0.0, 1.0)
    g = np.clip(g1 + m, 0.0, 1.0)
    b = np.clip(b1 + m, 0.0, 1.0)

    return np.stack([r, g, b], axis=-1).astype(np.float32)


def _color_mixer(d: np.ndarray, mixer_params: dict | None) -> np.ndarray:
    """基于标准 sRGB 域 HSL 模型的 8 色色彩混合器。

    在 HSL 空间内分别调整各色相区间的色相（Hue）、饱和度（Saturation）和明度（Lightness），
    采用边界收敛公式保证天然不溢出，带中性区软死区保护与重叠权重归一化。
    """
    if not mixer_params or not isinstance(mixer_params, dict):
        return d

    # 校验并提取有效调节参数
    active_channels: list[tuple[str, float, float, float, float, float]] = []
    for ch, center in _COLOR_MIXER_CENTERS.items():
        vals = mixer_params.get(ch)
        if not vals or not isinstance(vals, dict):
            continue
        try:
            h_raw = float(vals.get("hue", 0))
            s_raw = float(vals.get("sat", 0))
            l_raw = float(vals.get("lum", 0))
        except (ValueError, TypeError):
            continue
        if not (math.isfinite(h_raw) and math.isfinite(s_raw) and math.isfinite(l_raw)):
            continue
        h_val = float(np.clip(h_raw, -100.0, 100.0))
        s_val = float(np.clip(s_raw, -100.0, 100.0))
        l_val = float(np.clip(l_raw, -100.0, 100.0))
        if h_val != 0.0 or s_val != 0.0 or l_val != 0.0:
            width = _COLOR_MIXER_WIDTHS[ch]
            active_channels.append((ch, center, width, h_val, s_val, l_val))

    if not active_channels:
        return d

    H, S, L = _rgb_to_hsl(d)

    weights = mixer_weights(H, S, (channel[0] for channel in active_channels))

    delta_H = np.zeros_like(H)
    delta_S = np.zeros_like(S)
    delta_L = np.zeros_like(L)

    for (ch, center, width, h_val, s_val, l_val), (_channel, eff_w) in zip(active_channels, weights):
        if h_val != 0.0:
            delta_H += eff_w * (h_val / 100.0 * 45.0)

        if s_val > 0.0:
            delta_S += eff_w * (s_val / 100.0 * (1.0 - S))
        elif s_val < 0.0:
            delta_S += eff_w * (s_val / 100.0 * S)

        if l_val > 0.0:
            delta_L += eff_w * (l_val / 100.0 * (1.0 - L))
        elif l_val < 0.0:
            delta_L += eff_w * (l_val / 100.0 * L)

    new_H = (H + delta_H) % np.float32(360.0)
    new_H[new_H < 0] += np.float32(360.0)

    new_S = np.clip(S + delta_S, 0.0, 1.0)
    new_L = np.clip(L + delta_L, 0.0, 1.0)

    return _hsl_to_rgb(new_H, new_S, new_L)


# ---------------------------------------------------------------------------
# Local Masks: 归一化连续场局部渐变蒙版 (Linear & Radial Gradient)
# ---------------------------------------------------------------------------

def _compute_mask_array(h: int, w: int, mask_def: dict, *, coordinates=None) -> np.ndarray:
    """按归一化坐标 [0,1] 计算连续渐变蒙版，分辨率无关。"""
    mtype = mask_def.get("type", "linear")
    if coordinates is None:
        ys = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    else:
        ys, xs = coordinates

    if mtype == "linear":
        p0 = mask_def.get("p0", (0.5, 0.0))
        p1 = mask_def.get("p1", (0.5, 1.0))
        du = p1[0] - p0[0]
        dv = p1[1] - p0[1]
        len_sq = du * du + dv * dv
        if len_sq < 1e-8:
            return np.ones((h, w), dtype=np.float32)
        t = ((xs - p0[0]) * du + (ys - p0[1]) * dv) / len_sq
        t_clamped = np.clip(t, 0.0, 1.0)
        return (1.0 - (t_clamped * t_clamped * (3.0 - 2.0 * t_clamped))).astype(np.float32)

    elif mtype == "radial":
        center = mask_def.get("center", (0.5, 0.5))
        radius = mask_def.get("radius", (0.3, 0.3))
        angle_deg = float(mask_def.get("angle", 0.0))
        feather = float(mask_def.get("feather", 0.5))

        rx, ry = max(1e-4, radius[0]), max(1e-4, radius[1])
        dx = xs - center[0]
        dy = ys - center[1]

        if angle_deg != 0.0:
            rad = np.radians(angle_deg)
            cos_a = np.cos(rad)
            sin_a = np.sin(rad)
            dx_rot = dx * cos_a + dy * sin_a
            dy_rot = -dx * sin_a + dy * cos_a
        else:
            dx_rot = dx
            dy_rot = dy

        dist = np.sqrt((dx_rot / rx) ** 2 + (dy_rot / ry) ** 2)
        feather = np.clip(feather, 0.01, 1.0)
        r_inner = max(0.0, 1.0 - feather)
        t = np.clip((dist - r_inner) / feather, 0.0, 1.0)
        return (1.0 - (t * t * (3.0 - 2.0 * t))).astype(np.float32)

    return np.ones((h, w), dtype=np.float32)


def mask_working_memory(source: RenderSource, params: dict, *, threads=1) -> int:
    """宿主通用工作区之外，蒙版独占与临时缓冲的峰值预留。"""
    if params.get("process_version") in (3, 4):
        from .local_color import working_memory
        return working_memory(source, params, threads=threads)
    masks = [mask for mask in params.get("masks") or ()
             if mask.get("enabled", True) and mask.get("params")]
    if not masks:
        return 0
    h, w = source.pixels.shape[:2]
    pixels = h * w
    band_pixels = min(h, MASK_BAND_ROWS) * w
    workers = min(max(1, threads), max(1, (h + MASK_BAND_ROWS - 1) // MASK_BAND_ROWS))
    coordinates = 4 * (h + w)
    band_buffers = 24 * workers * band_pixels
    fixed = 24 * pixels + coordinates + band_buffers
    point_peak = 4 * pixels + 51 * workers * band_pixels
    peak = point_peak

    for mask in masks:
        weight_peak = (20 if mask.get("type", "linear") == "linear" else 56) * pixels + coordinates
        peak = max(peak, weight_peak)
        local = mask["params"]
        if any(local.get(key, 0) for key in _LOCAL_TONE_KEYS):
            tone_peak = 76 * pixels
            if local.get("shadows", 0) or local.get("highlights", 0):
                _, subsample = PRO_CALIB.localtone_kernel(min(h, w))
                sh, sw = max(1, h // subsample), max(1, w // subsample)
                small_pixels = sh * sw
                integral_pixels = (sh + 1) * (sw + 1)
                filtering = 4 * pixels + 92 * small_pixels + 8 * integral_pixels + 40 * (sh + sw)
                resizing = 60 * pixels + 36 * small_pixels + 36 * (h + w)
                tone_peak = max(tone_peak, 4 * pixels + max(filtering, resizing))
            peak = max(peak, 4 * pixels + tone_peak)
        if local.get("saturation", 1.0) != 1.0:
            peak = max(peak, 32 * pixels)
    return fixed + peak


def _check_render_cancelled(cancelled):
    if cancelled():
        raise CancelledError()


def _apply_masks(d: np.ndarray, masks: list[dict] | None, base_src: RenderSource,
                 process_version: int = 1, *, threads=None, cancelled=lambda: False) -> np.ndarray:
    """局部蒙版管线：依次对各蒙版区域进行局部曝光/高光/阴影/色温/饱和度调整与 Alpha 混合。"""
    if not masks:
        return d

    _check_render_cancelled(cancelled)
    out = d.copy()
    h, w = d.shape[:2]
    workers = min(max(1, int(threads or 1)), max(1, (h + MASK_BAND_ROWS - 1) // MASK_BAND_ROWS))
    coordinates = None
    adjusted = None
    buffers = None
    pool = None
    mask_arr = None

    def run_bands(function):
        if pool is None:
            function(0)
        else:
            futures = [pool.submit(function, index) for index in range(workers)]
            for future in futures:
                future.result()

    with ExitStack() as operations:
        for mask_index, mdef in enumerate(masks):
            _check_render_cancelled(cancelled)
            if not mdef.get("enabled", True):
                continue
            m_params = mdef.get("params", {})
            if not m_params:
                continue

            weight_started = monotonic()
            if coordinates is None:
                ys = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
                xs = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
                ys.setflags(write=False)
                xs.setflags(write=False)
                coordinates = ys, xs
            mask_arr = _compute_mask_array(h, w, mdef, coordinates=coordinates)[..., None]
            _check_render_cancelled(cancelled)
            if not np.any(mask_arr > 1e-4):
                mask_arr = None
                continue
            mask_arr.setflags(write=False)
            weight_ms = (monotonic() - weight_started) * 1000

            if adjusted is None:
                adjusted = np.empty_like(out)
                shape = (min(h, MASK_BAND_ROWS), w, 3)
                buffers = [(np.empty(shape, dtype=np.float32), np.empty(shape, dtype=np.float32))
                           for _ in range(workers)]
                if workers > 1:
                    pool = operations.enter_context(ThreadPoolExecutor(max_workers=workers,
                                                                      thread_name_prefix="ops-mask"))

            temp = int(m_params.get("temperature", 0))
            tint = int(m_params.get("tint", 0))
            wb_gain = _white_balance_gain(temp, tint) if temp != 0 or tint != 0 else None
            ev = float(m_params.get("exposure", 0))
            exposure_gain = _exposure_gain(ev, process_version) if ev != 0 else None

            def adjust_band(index):
                linear_buffer, work_buffer = buffers[index]
                for start in range(index * MASK_BAND_ROWS, h, workers * MASK_BAND_ROWS):
                    _check_render_cancelled(cancelled)
                    stop = min(h, start + MASK_BAND_ROWS)
                    rows = stop - start
                    linear = srgb_to_linear(out[start:stop], out=linear_buffer[:rows], work=work_buffer[:rows])
                    if wb_gain is not None:
                        _white_balance(linear, temp, tint, out=linear, gain=wb_gain)
                    if ev != 0:
                        linear = _exposure(linear, ev, process_version, gain=exposure_gain)
                    linear_to_srgb(linear, out=adjusted[start:stop])
                    np.clip(adjusted[start:stop], 0.0, 1.0, out=adjusted[start:stop])

            point_started = monotonic()
            run_bands(adjust_band)
            _check_render_cancelled(cancelled)
            point_ms = (monotonic() - point_started) * 1000

            tone_started = monotonic()
            local_result = _local_tone(adjusted, m_params)
            _check_render_cancelled(cancelled)
            if local_result is not adjusted:
                np.copyto(adjusted, local_result)
            del local_result
            tone_ms = (monotonic() - tone_started) * 1000

            saturation_started = monotonic()
            sat = float(m_params.get("saturation", 1.0))
            if sat != 1.0:
                saturated = _saturation(adjusted, sat)
                _check_render_cancelled(cancelled)
                np.copyto(adjusted, saturated)
                del saturated
            saturation_ms = (monotonic() - saturation_started) * 1000
            _check_render_cancelled(cancelled)

            def mix_band(index):
                left, right = buffers[index]
                for start in range(index * MASK_BAND_ROWS, h, workers * MASK_BAND_ROWS):
                    _check_render_cancelled(cancelled)
                    stop = min(h, start + MASK_BAND_ROWS)
                    rows = stop - start
                    weight = mask_arr[start:stop]
                    inverse = np.float32(1.0) - weight
                    np.multiply(inverse, out[start:stop], out=left[:rows])
                    np.multiply(weight, adjusted[start:stop], out=right[:rows])
                    np.add(left[:rows], right[:rows], out=out[start:stop])

            mix_started = monotonic()
            run_bands(mix_band)
            _check_render_cancelled(cancelled)
            mix_ms = (monotonic() - mix_started) * 1000
            mask_arr = None
            logger.debug("蒙版计算 render=%x mask=%s size=%sx%s threads=%s weight_ms=%.1f point_ms=%.1f "
                         "tone_ms=%.1f saturation_ms=%.1f mix_ms=%.1f",
                         id(out), mask_index, w, h, workers, weight_ms, point_ms, tone_ms, saturation_ms, mix_ms)

    _check_render_cancelled(cancelled)
    np.clip(out, 0.0, 1.0, out=out)
    return out


class RenderPipeline:
    """统一编辑层管线。

    线性域：白平衡(色温/色调) → 曝光 → [bracket: linear→sRGB] base curve
    → 局部高光/阴影/白/黑 → 对比 → 曲线 → 8色 Color Mixer → 鲜艳 → 饱和 → 纹理/清晰/去朦胧
    → 保色相色域压缩 → 局部蒙版 → [bracket: sRGB→linear] → 出（_pil_from 唯一 OETF + 唯一量化）。
    """

    def render(self, src: RenderSource, params: dict, *, threads=None,
               cancelled=lambda: False, sampling=None) -> RenderSource:
        _check_render_cancelled(cancelled)
        validate_params(params)
        look, process_version = color_identity(params)
        if process_version in (3, 4):
            from .local_color import render
            return render(src, params, backend="native", threads=threads or 1,
                          cancelled=cancelled, sampling=sampling)
        stages = look_stages(look, src.color_pipeline)
        if not stages and is_color_neutral(params, include_look=False):
            if sampling is not None:
                sampling.capture_neutral(src)
            return src
        started = monotonic()
        masks = params.get("masks")
        has_enabled_masks = any(mask.get("enabled", True) for mask in masks or ())
        supported = getattr(ops_accel, "SUPPORTED_PROCESS_VERSIONS", (1,)) if _HAS_RUST_ACCEL else ()
        backend = "rust" if process_version in supported else "python"
        reason = "" if backend == "rust" else "处理版本不受原生模块支持" if _HAS_RUST_ACCEL else "原生模块不可用"
        if backend == "rust" and stages and not getattr(ops_accel, "LOOK_STAGES", False):
            backend, reason = "python", "原生模块不支持观感层参数"
        capture_native = getattr(ops_accel, "render_pipeline_fast_with_mixer_input", None) if _HAS_RUST_ACCEL else None
        if sampling is not None and backend == "rust" and capture_native is None:
            backend, reason = "python", "原生模块缺少混色器输入采集入口"
        report = (backend, process_version, reason)
        if report not in _RENDER_BACKENDS_REPORTED:
            _RENDER_BACKENDS_REPORTED.add(report)
            logger.info("调色 backend=%s process_version=%s reason=%s", *report)

        # ── 1. 高性能 Rust 多核加速路径 (Rayon + 零拷贝) ──
        if backend == "rust":
            x_out, mixer_input = None, None
            try:
                rust_params = dict(params)
                rust_params["process_version"] = process_version
                rust_params["look_stages"] = [
                    (list(stage.tone_lut), list(stage.hue_sat), stage.tone_strength, stage.sat_strength)
                    for stage in stages]

                # 预处理 Curves LUT
                curves = params.get("curves")
                if curves and isinstance(curves, dict):
                    curves_lut = {}
                    for k in ("rgb", "r", "g", "b"):
                        pts = curves.get(k)
                        if pts and pts != _IDENTITY_CURVE:
                            curves_lut[k] = _fritsch_carlson_lut(pts).tolist()
                    if curves_lut:
                        rust_params["curves_lut"] = curves_lut

                # 确保输入是 C-contiguous float32
                pix_in = np.ascontiguousarray(src.pixels, dtype=np.float32)
                if sampling is None:
                    x_out = ops_accel.render_pipeline_fast(pix_in, rust_params, threads=threads)
                else:
                    with sampling.memory(pix_in.nbytes):
                        x_out, mixer_input = capture_native(pix_in, rust_params, threads=threads)
                        sampling.capture(mixer_input, src.alpha, "rust")
                        del mixer_input
                _check_render_cancelled(cancelled)

                # 局部蒙版后处理（如果启用）
                if has_enabled_masks:
                    d = np.clip(linear_to_srgb(x_out), 0.0, 1.0)
                    d = _apply_masks(d, masks, src, process_version, threads=threads, cancelled=cancelled)
                    _check_render_cancelled(cancelled)
                    x_out = srgb_to_linear(d)

                _check_render_cancelled(cancelled)
                logger.debug("调色计算 backend=rust size=%sx%s duration_ms=%.1f",
                             src.pixels.shape[1], src.pixels.shape[0], (monotonic() - started) * 1000)
                return replace(src, pixels=x_out)
            except (CancelledError, MemoryError):
                raise
            except Exception as exc:
                x_out, mixer_input = None, None
                if sampling is not None:
                    sampling.close()
                logger.warning("Rust 多核加速渲染失败，平滑回退到 Python 引擎: %s", exc)

        # ── 2. Python 纯算法回退兜底路径 ──
        x = src.pixels
        temp = int(params.get("temperature", 0))
        tint = int(params.get("tint", 0))
        if temp != 0 or tint != 0:
            x = _white_balance(x, temp, tint)

        ev = float(params.get("exposure", 0))
        b = float(params.get("brightness", 1.0))
        if b > 0 and b != 1.0:
            ev += math.log2(b)
        x = _exposure(x, ev, process_version)
        _check_render_cancelled(cancelled)

        d = np.clip(linear_to_srgb(x), 0.0, 1.0)
        d = _apply_look(d, stages)
        d = _local_tone(d, params)
        _check_render_cancelled(cancelled)
        d = _contrast(d, float(params.get("contrast", 1.0)))
        d = _apply_curves(d, params.get("curves"))
        if sampling is not None:
            sampling.capture(d, src.alpha, "python")
        d = _color_mixer(d, params.get("color_mixer"))
        d = _vibrance(d, int(params.get("vibrance", 0)))
        d = _saturation(d, float(params.get("saturation", 1.0)))
        d = _detail_effects(d, params)
        _check_render_cancelled(cancelled)
        d = _gamut_compress(d)
        if has_enabled_masks:
            d = np.clip(d, 0.0, 1.0)
            d = _apply_masks(d, masks, src, process_version,
                             threads=threads, cancelled=cancelled)
            _check_render_cancelled(cancelled)
        x = srgb_to_linear(d)

        _check_render_cancelled(cancelled)
        logger.debug("调色计算 backend=python size=%sx%s duration_ms=%.1f",
                     src.pixels.shape[1], src.pixels.shape[0], (monotonic() - started) * 1000)
        return replace(src, pixels=x)



def apply_chain_v2(
    image: Image.Image,
    params: dict,
    copy_if_unchanged: bool = True,
    *,
    meta: RenderMeta | None = None,
    threads=None,
) -> Image.Image:
    """入口 A：PIL 进 PIL 出，float 仅引擎内部，UI 零改动。"""
    src = _src_from_pil(image)
    if meta is not None:
        src = replace(
            src,
            color_pipeline=meta.color_pipeline,
            note=meta.note,
            dcp_has_tone_curve=meta.dcp_has_tone_curve,
            source_format=meta.source_format,
        )
    out = RenderPipeline().render(src, params, threads=threads)
    return _pil_from(out)


def render_source(src: RenderSource, params: dict, *, threads=None,
                  cancelled=lambda: False, sampling=None) -> RenderSource:
    """入口 B：RenderSource 端到端（RAW 必走，Phase 2 起经 DCP 层）。"""
    return RenderPipeline().render(src, params, threads=threads, cancelled=cancelled, sampling=sampling)


def save_render_source(
    src: RenderSource,
    target_path: str,
    *,
    ext: str | None = None,
    quality: int = 95,
) -> None:
    """将 RenderSource 保存到指定路径。
    若目标为 .tif / .tiff：以标准 16-bit uint16 TIFF 保存（经过 sRGB OETF 传递函数）；
    否则：转为 8-bit PIL 图像保存（支持 jpg/png/webp 等）。
    """
    if ext is None:
        ext = os.path.splitext(target_path)[1].lower()
    else:
        ext = ext.lower()

    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)

    if ext in (".tif", ".tiff"):
        from .render_codec import write_tiff
        write_tiff(src, target_path)
        return

    encode_render_source(src, target_path, ext=ext, quality=quality)


def encode_render_source(src: RenderSource, target, *, ext: str, quality: int = 95) -> None:
    """将线性源图编码为 8 位图像，target 可为路径或二进制流。"""

    ext = ext.lower()
    formats = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".bmp": "BMP"}
    # JPEG 和 BMP 的透明区域在线性空间合成白色背景。
    if ext in (".jpg", ".jpeg", ".bmp") and src.alpha is not None:
        from dataclasses import replace
        src = replace(src, pixels=src.pixels * src.alpha[..., None] + (1 - src.alpha[..., None]), alpha=None)
    with _pil_from(src) as pil_img:
        if ext in (".jpg", ".jpeg", ".webp"):
            pil_img.save(target, format=formats.get(ext), quality=quality)
        else:
            pil_img.save(target, format=formats.get(ext))


# ---------------------------------------------------------------------------
# Phase 4 客观基建 — Auto 自适应 headroom + JPG 感知域补偿（骨架）
#
# 与 classic _analyze_auto_params 物理隔离：classic 在 color_adjuster.py
# **完全不改**（A/B 锚，分析逻辑不动）。本层是 pro 路的纯函数后处理：
# 输入 = classic 基础 UI 参数 + RenderSource，输出 = 调整后参数副本。
#
# objective vs subjective 分界（本轮只做 objective）：
#   - estimate_linear_headroom_stops：纯信号处理，scene-linear P99.5 + 尾部
#     斜率判削顶，可单测钉死。
#   - adapt_auto_params_pro：装配/契约 + JPG「只缩不放」保守钳。
#   - **量值系数（recovery_per_stop / jpg_conservative）当前为恒等锚**：
#     0 headroom ⇒ 输出 == 传入 base（A/B 安全、不需 LR）。Phase 4 主观标定
#     在 AutoCalib 内对 LR 调大这些系数（铁律：新旧常数隔离、classic 作锚）。
# 调色面板的自动调整经 adapt_auto_params_for_ui（入口 A）接入本层；RAW 高光
# 回收在入口 A 上结构性不生效，原因见该函数说明。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoCalib:
    """Auto 自适应 headroom / JPG 感知补偿的**标定面**（Phase 4b 规范实标）。

    锚不变量：检测到 0 可回收 headroom 时 adapt_auto_params_pro(base, src)
    == base（A/B 安全、不改 classic 语义）。实标参数：
      - jpg_conservative=0.65：厂商直出 JPG 已带 baked look，Auto 调色适度保守收敛，防死白死黑；
      - recovery_per_stop=15.0：RAW 实测可回收 headroom 按斜率驱动高光回收（每 stop 回收 15 格）。
    """

    ref_white: float = 1.0              # scene-linear 显示白基准（线性可 >1）
    hi_percentile: float = 0.995        # 高百分位（P99.5）定真实高光位
    tail_lo: float = 0.90               # 尾部斜率窗口下端（× ref_white，线性域）
    flat_tail_ratio: float = 0.15       # 尾部斜率 < 此 ⇒ 视为削顶平台不可回收
    headroom_floor_stops: float = 0.05  # < 此 stop ⇒ 视为无可回收 headroom（不动）
    recon_cap_stops: float = 1.0        # highlight 重建未实现 ⇒ headroom 利用封顶（再压一档）
    recovery_per_stop: float = 15.0     # 每 stop 追加 highlight 回收量（规范实标：15.0）
    jpg_conservative: float = 0.65      # srgb 源 tone/对比推送保守系数（规范实标：0.65）
    jpg_keys: tuple = ("highlights", "shadows", "contrast_amt")


AUTO_CALIB = AutoCalib()


def estimate_linear_headroom_stops(
    pixels: np.ndarray, calib: AutoCalib = AUTO_CALIB
) -> float:
    """从 scene-linear 像素估真实可回收高光 headroom（stop）。

    objective：P99.5 高百分位定真实高光位（避开个别热点）；再看
    [tail_lo·ref, P99.5] 尾部斜率代理——平滑上升 = 有层次可回收；近平台
    （削顶）= 不可回收（硬拉只出块，无 headroom 不硬拉），headroom 归 0。
    封顶 recon_cap（highlight 重建未实现前再压一档）。返回 ≥ 0。
    """
    p = np.asarray(pixels, dtype=np.float32)
    L = (p @ _REC709_LUMA) if (p.ndim == 3 and p.shape[-1] == 3) else p
    L = L.reshape(-1)
    if L.size == 0:
        return 0.0
    ref = float(calib.ref_white)
    p_hi = float(np.quantile(L, calib.hi_percentile))
    if p_hi <= ref:
        return 0.0                                  # 高百分位未过白 ⇒ 无 above-white headroom
    lo = ref * calib.tail_lo
    band = L[(L >= lo) & (L <= p_hi)]
    if band.size < 16:
        return 0.0
    span = p_hi - lo
    # 斜率代理：尾部样本 std / 区间宽度。削顶时样本挤在 p_hi 附近 → std≪span。
    slope = float(band.std()) / max(span, 1e-6)
    if slope < calib.flat_tail_ratio:
        return 0.0                                  # 削顶平台：不可回收
    stops = float(np.log2(p_hi / ref))
    if stops < calib.headroom_floor_stops:
        return 0.0
    return float(min(stops, calib.recon_cap_stops))


def adapt_auto_params_pro(
    base: dict,
    src: RenderSource,
    *,
    calib: AutoCalib = AUTO_CALIB,
) -> dict:
    """pro 路 Auto 后处理（骨架）：RAW 由实测 headroom 驱动高光回收，
    srgb 源走感知保守钳。**纯函数、不改 base、不接 classic**（A/B 锚）。

    锚约束（本轮）：calib 为恒等锚 ⇒ 任意 src 下返回 base 的等价副本
    （recovery_per_stop=0、jpg_conservative=1）。Phase 4 调大系数 vs LR。
    """
    out = dict(base)
    fmt = src.source_format
    if fmt == "raw":
        stops = estimate_linear_headroom_stops(src.pixels, calib)
        extra = -int(round(calib.recovery_per_stop * stops))   # headroom↑ ⇒ 回收↑（负向）
        if extra != 0:
            hi = int(out.get("highlights", 0))
            out["highlights"] = int(np.clip(hi + extra, -100, 100))
    elif fmt in ("srgb8", "srgb16"):
        f = min(1.0, calib.jpg_conservative)        # 厂商已 bake：只缩不放
        if f != 1.0:
            for k in calib.jpg_keys:
                if out.get(k):
                    out[k] = int(round(out[k] * f))
    return out


# 自适应技术校正的升级路径占位（现不实现，门留着）：当前为直方图启发式（headroom +
# 灰世界/白点 + JPG 保守钳），不懂专业审美、只懂技术正确——已知接受项。
# 升级 = 预训练视觉骨干（ViT 可）+ 小回归头，在用户标注成对数据
# （MIT-Adobe FiveK 式）微调输出 Basic 滑块量；**不从零训 transformer**。
# 接口边界不变：仍走「base UI 参数 + RenderSource → 调整后参数」契约，
# 替换的仅 estimate_*/adapt_* 内部，AutoCalib 容器与调用点不动。


def adapt_auto_params_for_ui(
    base: dict,
    proxy: Image.Image,
    source_format: str,
    *,
    calib: AutoCalib = AUTO_CALIB,
) -> dict:
    """入口 A（display PIL）侧 pro Auto 后处理桥。

    把对话框可拿到的 display PIL proxy + 解码 sidecar 的真实 source_format
    组装成 RenderSource，转调 adapt_auto_params_pro。**纯函数、不改 base**
    （返回副本）。恒等锚（calib 默认 recovery_per_stop=0 / jpg_conservative=1）
    下任意输入返回 base 等价副本 ⇒ A/B 安全、无需 LR。

    ⚠️ 入口 A 边界：proxy 是 display sRGB（已 clamp [0,1]），
    故 RAW 分支的 scene-linear headroom 恒为 0 → RAW 高光回收在此**结构性
    no-op**，须待入口 B 端到端 16-bit（Phase 5）。srgb 源的感知保守钳不受
    此限、在此即生效（jpg_conservative<1 标定后）。
    """
    arr = np.asarray(proxy.convert("RGB"), dtype=np.float32) / np.float32(255.0)
    src = RenderSource(
        pixels=srgb_to_linear(arr),
        alpha=None,
        source_format=source_format,
        source_path="",
        is_proxy=True,
        orig_size=proxy.size,
        color_pipeline="",
    )
    return adapt_auto_params_pro(base, src, calib=calib)
