"""DCP 色彩科学层（Phase 2，RAW 像 LR 的核心）。

已冻结、退出默认渲染路：默认 RAW 比色走 libraw `output_color=XYZ`（E1，见
photo_engine 模块说明）。本层只在显式设置 OPS_DCP_PROFILE / OPS_DCP_DIR 时
启用，保留作比色对照与可选的 .dcp 导入；不打包 Adobe profile。

自研、零新依赖（numpy + struct）。把 **相机原生线性 RGB** 经 Adobe DCP
参考渲染 → 线性 sRGB(D65) 工作空间。流水线按 DNG 规范第 6 章 + Profile look：

    相机原生线性 RGB
      → 由 AsShotNeutral 推 CCT（双光照时迭代收敛）
      → 按 CCT 在 illum1/2 间插值 ColorMatrix/ForwardMatrix/CameraCalibration
         （矩阵全部参与插值，不只 ColorMatrix）
      → WB = AsShotNeutral 归一对角阵，**并入矩阵链一次性施加**（rawpy 不再前乘，防双重白平衡）
      → CIE XYZ(D50)  → ProPhoto(D50, 线性，参考空间)
      → [可选] HueSatMap   （环形 hue、非均匀网格、双光照按 CCT 混合）
      → [可选] LookTable   （同上，创意 look）
      → [可选] ProfileToneCurve（Adobe 基调曲线 = "看起来的样子"）
      → ProPhoto(D50) → XYZ(D50) → Bradford D50→D65 → 线性 sRGB(D65)

设计边界：本层只**可验证地复现到 DCP（含其 ToneCurve）这一层**；
LR 未公开的 highlight rolloff / 颜色压缩不强求逐位复现，留给 Phase 4 视觉标定。

验收：与 RawTherapee 同 Profile 数值对拍，逐层 isolate（纯矩阵 → +ToneCurve
→ +HSV → +双光照），线性域 RGB MSE 均值 ≤ 1e-4。RT 数值 sign-off 依赖本机
环境（RT + .dcp + RAW），见 tests/test_dcp_color_science.py。

降级须**显式不静默**：无匹配 Profile 时回退相机内置矩阵，
color_pipeline 标 'matrix' 且 UI 可见，跨机基准色差异不被悄悄吞掉。
"""

from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)


# ===========================================================================
# 1. TIFF/EP 容器解析（自研 TIFF-tag 读取，DNG 规范公开字段，零外部工具）
# ===========================================================================

# TIFF 字段类型 → (struct 码, 字节数)。SRATIONAL/RATIONAL 特殊处理。
_TIFF_TYPE = {
    1: ("B", 1),    # BYTE
    2: ("s", 1),    # ASCII
    3: ("H", 2),    # SHORT
    4: ("I", 4),    # LONG
    5: ("II", 8),   # RATIONAL  (num/den, 无符号)
    6: ("b", 1),    # SBYTE
    7: ("B", 1),    # UNDEFINED
    8: ("h", 2),    # SSHORT
    9: ("i", 4),    # SLONG
    10: ("ii", 8),  # SRATIONAL (num/den, 有符号)
    11: ("f", 4),   # FLOAT
    12: ("d", 8),   # DOUBLE
}

# DNG/DCP 标签（十进制，DNG 1.4 规范 / Adobe DCP）。
_TAG = {
    "UniqueCameraModel": 50708,
    "ColorMatrix1": 50721,
    "ColorMatrix2": 50722,
    "CameraCalibration1": 50723,
    "CameraCalibration2": 50724,
    "AnalogBalance": 50727,
    "AsShotNeutral": 50728,
    "CalibrationIlluminant1": 50778,
    "CalibrationIlluminant2": 50779,
    "ForwardMatrix1": 50964,
    "ForwardMatrix2": 50965,
    "ProfileName": 50936,
    "ProfileHueSatMapDims": 50937,
    "ProfileHueSatMapData1": 50938,
    "ProfileHueSatMapData2": 50939,
    "ProfileToneCurve": 50940,
    "ProfileEmbedPolicy": 50941,
    # ↓ DNG 规范真实 ID（实测 Adobe Camera Matching profile 校正）：
    #   LookTable=50981/50982（非 51107/8）；Encoding/BEO 在 5110x 段。
    "ProfileLookTableDims": 50981,
    "ProfileLookTableData": 50982,
    "ProfileHueSatMapEncoding": 51107,
    "ProfileLookTableEncoding": 51108,
    "BaselineExposureOffset": 51109,
    "DefaultBlackRender": 51110,
}
_TAG_NAME = {v: k for k, v in _TAG.items()}


def _read_tiff_tags(blob: bytes) -> dict[str, object]:
    """解析 TIFF/EP 头 + IFD0，返回 {标签名: python 值}。

    DCP 是单 IFD 的 TIFF/EP；只读 IFD0 即可。endianness、inline(≤4B) vs
    offset、RATIONAL/SRATIONAL 全部按 TIFF 6.0 规范处理。未知标签跳过。
    """
    if len(blob) < 8:
        raise ValueError("DCP 文件过短，非合法 TIFF 容器")
    bo = blob[:2]
    if bo == b"II":
        end = "<"
    elif bo == b"MM":
        end = ">"
    else:
        raise ValueError(f"非法 TIFF 字节序标记: {bo!r}")
    # 42 = 标准 TIFF/DNG；0x4352 = Adobe DCP 专用 magic（实测 Adobe DNG
    # Converter 产出的 .dcp 用后者，dcamprof/exiftool 同样接受）。
    (magic,) = struct.unpack(end + "H", blob[2:4])
    if magic not in (42, 0x4352):
        raise ValueError(f"非法 TIFF/DCP magic: {magic}（应为 42 或 0x4352）")
    (ifd_off,) = struct.unpack(end + "I", blob[4:8])

    (n_entries,) = struct.unpack(end + "H", blob[ifd_off : ifd_off + 2])
    out: dict[str, object] = {}
    pos = ifd_off + 2
    for _ in range(n_entries):
        entry = blob[pos : pos + 12]
        pos += 12
        tag, ftype, count = struct.unpack(end + "HHI", entry[:8])
        name = _TAG_NAME.get(tag)
        if name is None or ftype not in _TIFF_TYPE:
            continue
        _, tsize = _TIFF_TYPE[ftype]
        total = tsize * count
        # 值 ≤4 字节内联在 entry[8:12]，否则 entry[8:12] 是偏移。
        if total <= 4:
            raw = entry[8 : 8 + total]
        else:
            (voff,) = struct.unpack(end + "I", entry[8:12])
            raw = blob[voff : voff + total]
        out[name] = _decode_tag_value(end, ftype, count, raw)
    return out


def _decode_tag_value(end: str, ftype: int, count: int, raw: bytes):
    if ftype == 2:  # ASCII（含尾部 NUL）
        return raw.split(b"\x00", 1)[0].decode("ascii", "replace")
    if ftype in (5, 10):  # RATIONAL / SRATIONAL
        code = "I" if ftype == 5 else "i"
        vals = struct.unpack(end + code * (2 * count), raw[: 8 * count])
        nums = vals[0::2]
        dens = vals[1::2]
        return [
            (float(n) / float(d) if d != 0 else 0.0) for n, d in zip(nums, dens)
        ]
    code, tsize = _TIFF_TYPE[ftype]
    vals = list(struct.unpack(end + code * count, raw[: tsize * count]))
    return vals if count > 1 else vals[0]


# ===========================================================================
# 2. 标定光源 → 色温（EXIF LightSource 枚举，dng_sdk 对齐）
# ===========================================================================

# EXIF LightSource → 相关色温 K（与 Adobe dng_sdk dng_temperature 近似一致）。
_ILLUM_TEMP = {
    0: 0.0, 1: 6504.0, 2: 4200.0, 3: 3200.0, 4: 5500.0,
    9: 5500.0, 10: 6504.0, 11: 7504.0, 12: 6500.0, 13: 5000.0,
    14: 4200.0, 15: 3450.0, 17: 2856.0, 18: 4874.0, 19: 6774.0,
    20: 5500.0, 21: 6504.0, 22: 7504.0, 23: 5003.0, 24: 3200.0,
}


def _illuminant_to_temp(code: int) -> float:
    return _ILLUM_TEMP.get(int(code), 0.0)


# ===========================================================================
# 3. 解析后的语义 Profile
# ===========================================================================

@dataclass
class DCPProfile:
    name: str = ""
    camera_model: str = ""
    # 光照 1/2 色温（K）；temp2==0 ⇒ 单光照
    temp1: float = 0.0
    temp2: float = 0.0
    color_matrix_1: np.ndarray | None = None      # 3x3 XYZ(D50)→camera
    color_matrix_2: np.ndarray | None = None
    forward_matrix_1: np.ndarray | None = None     # 3x3 camera(白平衡后)→XYZ(D50)
    forward_matrix_2: np.ndarray | None = None
    camera_calibration_1: np.ndarray | None = None  # 3x3，默认 I
    camera_calibration_2: np.ndarray | None = None
    analog_balance: np.ndarray | None = None        # 3 对角，默认 I
    # HueSatMap：dims=(hue,sat,val)，data shape (val,hue,sat,3)
    hsm_dims: tuple[int, int, int] | None = None
    hsm_data_1: np.ndarray | None = None
    hsm_data_2: np.ndarray | None = None
    hsm_encoding: int = 0                            # 0 linear / 1 sRGB（val 轴）
    look_dims: tuple[int, int, int] | None = None
    look_data: np.ndarray | None = None
    look_encoding: int = 0
    tone_curve: np.ndarray | None = None             # Nx2 (x,y) ∈ [0,1]，单调
    baseline_exposure_offset: float = 0.0
    embed_policy: int = 0

    @property
    def is_dual_illuminant(self) -> bool:
        return (
            self.temp2 > 0.0
            and self.color_matrix_2 is not None
            and abs(self.temp1 - self.temp2) > 1e-3
        )

    @property
    def has_tone_curve(self) -> bool:
        return self.tone_curve is not None and len(self.tone_curve) >= 2


def _as_matrix(vals) -> np.ndarray | None:
    if vals is None:
        return None
    a = np.asarray(vals, dtype=np.float64)
    if a.size != 9:
        return None
    return a.reshape(3, 3)


def _as_hsm(data, dims) -> np.ndarray | None:
    """ProfileHueSatMapData → (valDiv, hueDiv, satDiv, 3) float32。

    DNG 数据布局：index = ((v*hueDiv + h)*satDiv + s)，每项 3 float
    (hueShift°, satScale, valScale)。
    """
    if data is None or dims is None:
        return None
    hd, sd, vd = int(dims[0]), int(dims[1]), int(dims[2])
    arr = np.asarray(data, dtype=np.float32)
    if arr.size != hd * sd * vd * 3:
        logger.warning("HueSatMap 数据大小与 dims 不符，忽略该表")
        return None
    return arr.reshape(vd, hd, sd, 3)


def parse_dcp(path: str) -> DCPProfile:
    """解析 .dcp 文件为语义 Profile（自研 TIFF-tag，零外部工具）。

    解析结果须与 dcpTool/dcamprof 同名字段逐项对拍（Phase 2 单测）。
    """
    with open(path, "rb") as f:
        blob = f.read()
    t = _read_tiff_tags(blob)

    prof = DCPProfile(
        name=str(t.get("ProfileName", "") or ""),
        camera_model=str(t.get("UniqueCameraModel", "") or ""),
        color_matrix_1=_as_matrix(t.get("ColorMatrix1")),
        color_matrix_2=_as_matrix(t.get("ColorMatrix2")),
        forward_matrix_1=_as_matrix(t.get("ForwardMatrix1")),
        forward_matrix_2=_as_matrix(t.get("ForwardMatrix2")),
        camera_calibration_1=_as_matrix(t.get("CameraCalibration1")),
        camera_calibration_2=_as_matrix(t.get("CameraCalibration2")),
        hsm_encoding=int(t.get("ProfileHueSatMapEncoding", 0) or 0),
        look_encoding=int(t.get("ProfileLookTableEncoding", 0) or 0),
        embed_policy=int(t.get("ProfileEmbedPolicy", 0) or 0),
    )

    ab = t.get("AnalogBalance")
    if ab is not None and len(ab) == 3:
        prof.analog_balance = np.diag(np.asarray(ab, dtype=np.float64))

    ci1 = t.get("CalibrationIlluminant1")
    ci2 = t.get("CalibrationIlluminant2")
    prof.temp1 = _illuminant_to_temp(ci1) if ci1 is not None else 0.0
    prof.temp2 = _illuminant_to_temp(ci2) if ci2 is not None else 0.0

    hsm_dims = t.get("ProfileHueSatMapDims")
    if hsm_dims is not None and len(hsm_dims) == 3:
        prof.hsm_dims = (int(hsm_dims[0]), int(hsm_dims[1]), int(hsm_dims[2]))
        prof.hsm_data_1 = _as_hsm(t.get("ProfileHueSatMapData1"), hsm_dims)
        prof.hsm_data_2 = _as_hsm(t.get("ProfileHueSatMapData2"), hsm_dims)

    look_dims = t.get("ProfileLookTableDims")
    if look_dims is not None and len(look_dims) == 3:
        prof.look_dims = (int(look_dims[0]), int(look_dims[1]), int(look_dims[2]))
        prof.look_data = _as_hsm(t.get("ProfileLookTableData"), look_dims)

    tc = t.get("ProfileToneCurve")
    if tc is not None and len(tc) >= 4 and len(tc) % 2 == 0:
        pairs = np.asarray(tc, dtype=np.float64).reshape(-1, 2)
        # 规范要求按 x 升序、首尾 (0,0)/(1,1) 附近；排序保稳。
        pairs = pairs[np.argsort(pairs[:, 0], kind="stable")]
        prof.tone_curve = pairs

    beo = t.get("BaselineExposureOffset")
    if beo is not None:
        prof.baseline_exposure_offset = float(beo[0] if isinstance(beo, list) else beo)

    return prof


# ===========================================================================
# 4. 色彩空间常量与色适应
# ===========================================================================

_XYZ_D50 = np.array([0.96422, 1.00000, 0.82521], dtype=np.float64)
_XYZ_D65 = np.array([0.95047, 1.00000, 1.08883], dtype=np.float64)

_BRADFORD = np.array(
    [[0.8951, 0.2664, -0.1614],
     [-0.7502, 1.7135, 0.0367],
     [0.0389, -0.0685, 1.0296]],
    dtype=np.float64,
)


def _bradford_cat(src_w: np.ndarray, dst_w: np.ndarray) -> np.ndarray:
    """Bradford 色适应矩阵 src 白点 → dst 白点（DCP 链尾 D50→D65 用）。"""
    s = _BRADFORD @ src_w
    d = _BRADFORD @ dst_w
    return np.linalg.inv(_BRADFORD) @ np.diag(d / s) @ _BRADFORD


_CAT_D50_TO_D65 = _bradford_cat(_XYZ_D50, _XYZ_D65)

# XYZ(D65) → 线性 sRGB(D65)（IEC 61966-2-1 标准矩阵）。
_XYZ_D65_TO_SRGB = np.array(
    [[3.2404542, -1.5371385, -0.4985314],
     [-0.9692660, 1.8760108, 0.0415560],
     [0.0556434, -0.2040259, 1.0572252]],
    dtype=np.float64,
)

# ProPhoto/ROMM (D50, 线性) ↔ XYZ(D50)。DCP 参考空间 = D50，故 HSV/look/tone
# 在 ProPhoto-linear(D50) 域施加（与 RawTherapee 参考路径一致）。
_PROPHOTO_D50_TO_XYZ = np.array(
    [[0.7976749, 0.1351917, 0.0313534],
     [0.2880402, 0.7118741, 0.0000857],
     [0.0000000, 0.0000000, 0.8252100]],
    dtype=np.float64,
)
_XYZ_TO_PROPHOTO_D50 = np.linalg.inv(_PROPHOTO_D50_TO_XYZ)

# XYZ(D50) → 线性 sRGB(D65)：Bradford 串一次（DCP 链尾）。
_XYZ_D50_TO_SRGB = _XYZ_D65_TO_SRGB @ _CAT_D50_TO_D65


def _cct_from_xy(x: float, y: float) -> float:
    """McCamy 近似：CIE xy → 相关色温 K。"""
    n = (x - 0.3320) / (0.1858 - y + 1e-12)
    return 449.0 * n**3 + 3525.0 * n**2 + 6823.3 * n + 5520.33


# ===========================================================================
# 5. 双光照插值 + AsShotNeutral→CCT 迭代收敛
# ===========================================================================

def _interp_weight(temp: float, t1: float, t2: float) -> float:
    """倒数(mired)空间插值权重 g：返回 illum1 的权重，∈[0,1]。

    Adobe/RT 约定：在 1/T 空间线性插值。g=1 ⇒ 纯 illum1。
    """
    if t1 <= 0 or t2 <= 0 or abs(t1 - t2) < 1e-6:
        return 1.0
    inv, i1, i2 = 1.0 / temp, 1.0 / t1, 1.0 / t2
    g = (inv - i2) / (i1 - i2)
    return float(np.clip(g, 0.0, 1.0))


def _lerp_mat(m1, m2, g: float):
    if m1 is None:
        return m2
    if m2 is None:
        return m1
    return g * m1 + (1.0 - g) * m2


def _color_matrix_at(prof: DCPProfile, g: float) -> np.ndarray:
    cm = _lerp_mat(prof.color_matrix_1, prof.color_matrix_2, g)
    if cm is None:
        raise ValueError("DCP 缺 ColorMatrix，无法构建矩阵链")
    return cm


def _neutral_to_xy(prof: DCPProfile, neutral: np.ndarray, g: float) -> tuple[float, float]:
    cm = _color_matrix_at(prof, g)
    xyz = np.linalg.solve(cm, neutral)          # inv(ColorMatrix) @ neutral
    s = xyz.sum()
    if s <= 0:
        return float(_XYZ_D50[0] / _XYZ_D50.sum()), float(_XYZ_D50[1] / _XYZ_D50.sum())
    return float(xyz[0] / s), float(xyz[1] / s)


def _solve_cct(prof: DCPProfile, neutral: np.ndarray) -> float:
    """DNG NeutralToXY 迭代：矩阵依赖 CCT、CCT 依赖中性 → 不动点收敛。"""
    if not prof.is_dual_illuminant:
        return prof.temp1 if prof.temp1 > 0 else 5500.0
    temp = 5000.0
    for _ in range(6):
        g = _interp_weight(temp, prof.temp1, prof.temp2)
        x, y = _neutral_to_xy(prof, neutral, g)
        new_t = _cct_from_xy(x, y)
        if not np.isfinite(new_t) or new_t <= 0:
            break
        if abs(new_t - temp) < 0.25:
            temp = new_t
            break
        temp = float(np.clip(new_t, 2000.0, 25000.0))
    return temp


@dataclass
class _ResolvedMatrices:
    forward: np.ndarray | None
    color: np.ndarray
    calib: np.ndarray
    hsm: np.ndarray | None
    hsm_dims: tuple[int, int, int] | None
    cct: float
    g: float


def _resolve(prof: DCPProfile, neutral: np.ndarray) -> _ResolvedMatrices:
    """按场景中性解出（双光照已 CCT 插值）的矩阵 + HSV 表。"""
    cct = _solve_cct(prof, neutral)
    g = _interp_weight(cct, prof.temp1, prof.temp2) if prof.is_dual_illuminant else 1.0

    fm = _lerp_mat(prof.forward_matrix_1, prof.forward_matrix_2, g)
    cm = _color_matrix_at(prof, g)
    cc = _lerp_mat(prof.camera_calibration_1, prof.camera_calibration_2, g)
    if cc is None:
        cc = np.eye(3, dtype=np.float64)

    # 双光照下两张 HSV map 也按 CCT 混合（不只矩阵，否则肤色出现断层）。
    hsm = None
    if prof.hsm_data_1 is not None or prof.hsm_data_2 is not None:
        if prof.hsm_data_2 is None:
            hsm = prof.hsm_data_1
        elif prof.hsm_data_1 is None:
            hsm = prof.hsm_data_2
        else:
            hsm = (g * prof.hsm_data_1 + (1.0 - g) * prof.hsm_data_2).astype(np.float32)

    return _ResolvedMatrices(fm, cm, cc, hsm, prof.hsm_dims, cct, g)


# ===========================================================================
# 6. 相机原生 → XYZ(D50)（WB 并入矩阵链一次性施加）
# ===========================================================================

def _camera_to_xyz_d50(rm: _ResolvedMatrices, prof: DCPProfile,
                        neutral: np.ndarray) -> np.ndarray:
    """构建 camera-native → XYZ(D50) 的 3x3。

    白平衡 **绝不** 在 rawpy 前乘，而是把 AsShotNeutral 归一为
    对角阵并入矩阵链，在此一次性施加（防双重白平衡）。

    ForwardMatrix 路径（Adobe DNG Converter Profile 必含，RT 同款，优先）：
        AB·CC = 相机标定；refNeutral = inv(AB·CC) @ neutral
        D     = diag(1 / refNeutral)               （白平衡对角阵）
        cam→XYZ(D50) = FM · D · inv(AB·CC)
    无 ForwardMatrix 时退 ColorMatrix 逆路径：
        cam→XYZ = inv(AB·CC·CM)，再 Bradford 把中性适应到 D50。
    """
    ab = prof.analog_balance if prof.analog_balance is not None else np.eye(3)
    abcc = ab @ rm.calib
    abcc_inv = np.linalg.inv(abcc)

    if rm.forward is not None:
        ref_neutral = abcc_inv @ neutral
        ref_neutral = np.maximum(ref_neutral, 1e-9)
        d = np.diag(1.0 / ref_neutral)
        return rm.forward @ d @ abcc_inv

    # ColorMatrix 逆路径：cam→XYZ(场景白点)，再色适应到 D50。
    cam_to_xyz = np.linalg.inv(abcc @ rm.color)
    xyz_neutral = cam_to_xyz @ neutral
    s = xyz_neutral.sum()
    if s > 0:
        src_w = xyz_neutral / xyz_neutral[1]
        cat = _bradford_cat(src_w, _XYZ_D50)
        cam_to_xyz = cat @ cam_to_xyz
    return cam_to_xyz


# ===========================================================================
# 7. HSV / Look 表（环形 hue、非均匀网格、val 轴编码）
# ===========================================================================

def _rgb_to_hsv6(rgb: np.ndarray):
    """线性参考 RGB → (H∈[0,6), S∈[0,1], V≥0)。RT/Adobe HSV 约定。"""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    v = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    c = v - mn
    s = np.where(v > 1e-12, c / np.maximum(v, 1e-12), 0.0)
    h = np.zeros_like(v)
    nz = c > 1e-12
    rm = nz & (v == r)
    gm = nz & (v == g) & ~rm
    bm = nz & (v == b) & ~rm & ~gm
    h = np.where(rm, ((g - b) / np.where(c == 0, 1, c)) % 6.0, h)
    h = np.where(gm, ((b - r) / np.where(c == 0, 1, c)) + 2.0, h)
    h = np.where(bm, ((r - g) / np.where(c == 0, 1, c)) + 4.0, h)
    h = h % 6.0
    return h, s, v


def _hsv6_to_rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    h = h % 6.0
    i = np.floor(h).astype(np.int64)
    f = h - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i = i % 6
    r = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5],
                  [v, q, p, p, t, v])
    g = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5],
                  [t, v, v, q, p, p])
    b = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5],
                  [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1).astype(np.float32)


def _srgb_encode(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92,
                    1.055 * np.power(x, 1.0 / 2.4) - 0.055)


def _apply_hsv_table(rgb: np.ndarray, table: np.ndarray,
                     dims: tuple[int, int, int], encoding: int) -> np.ndarray:
    """对线性参考 RGB 施加 HueSatMap / LookTable。

    高风险实现要点：
      - hue 维**环形插值**（359°→0° 连续，错则肤色/天空色相跳变断层）；
      - sat/val 维线性插值，网格可非均匀（val 轴按 encoding 决定是否 sRGB）；
      - table shape = (valDiv, hueDiv, satDiv, 3)，每项 (hueShift°, satScale, valScale)。
    """
    hd, sd, vd = dims
    h, s, v = _rgb_to_hsv6(rgb)

    # --- hue：环形，步长 6/hd，左索引 + 分数，wrap 到 0 ---
    hpos = (h / 6.0) * hd
    h0 = np.floor(hpos).astype(np.int64) % hd
    hf = hpos - np.floor(hpos)
    h1 = (h0 + 1) % hd                                   # 环形：最后一格回 0

    # --- sat：[0,1] → [0, sd-1]，端点 clamp（无环形）---
    spos = np.clip(s, 0.0, 1.0) * (sd - 1)
    s0 = np.clip(np.floor(spos).astype(np.int64), 0, sd - 1)
    s1 = np.minimum(s0 + 1, sd - 1)
    sf = np.clip(spos - s0, 0.0, 1.0)

    # --- val：vd==1 无 val 依赖；否则按 encoding 决定 sRGB 编码后再索引 ---
    if vd <= 1:
        v0 = np.zeros_like(h0)
        v1 = v0
        vf = np.zeros_like(hf)
    else:
        venc = _srgb_encode(v) if encoding == 1 else np.clip(v, 0.0, 1.0)
        vpos = venc * (vd - 1)
        v0 = np.clip(np.floor(vpos).astype(np.int64), 0, vd - 1)
        v1 = np.minimum(v0 + 1, vd - 1)
        vf = np.clip(vpos - v0, 0.0, 1.0)

    def gather(vi, hi, si):
        return table[vi, hi, si]                          # (...,3)

    hf3, sf3, vf3 = hf[..., None], sf[..., None], vf[..., None]
    # 三线性：先 hue，再 sat，再 val。
    c000 = gather(v0, h0, s0); c001 = gather(v0, h0, s1)
    c010 = gather(v0, h1, s0); c011 = gather(v0, h1, s1)
    c00 = c000 * (1 - sf3) + c001 * sf3
    c01 = c010 * (1 - sf3) + c011 * sf3
    c0 = c00 * (1 - hf3) + c01 * hf3
    if vd <= 1:
        delta = c0
    else:
        c100 = gather(v1, h0, s0); c101 = gather(v1, h0, s1)
        c110 = gather(v1, h1, s0); c111 = gather(v1, h1, s1)
        c10 = c100 * (1 - sf3) + c101 * sf3
        c11 = c110 * (1 - sf3) + c111 * sf3
        c1 = c10 * (1 - hf3) + c11 * hf3
        delta = c0 * (1 - vf3) + c1 * vf3

    h_new = (h + delta[..., 0] * (6.0 / 360.0)) % 6.0     # hueShift 存°，H 用 6 制
    s_new = np.clip(s * delta[..., 1], 0.0, 1.0)
    v_new = np.clip(v * delta[..., 2], 0.0, 1.0)
    return _hsv6_to_rgb(h_new, s_new, v_new)


# ===========================================================================
# 8. ProfileToneCurve（Adobe 基调曲线；与编辑 base curve 二选一）
# ===========================================================================

def _apply_tone_curve(rgb: np.ndarray, curve: np.ndarray) -> np.ndarray:
    """ProfileToneCurve：Adobe **保色相** rgbTone 法（dng_sdk dng_render /
    RawTherapee dcp.cc `rgbtone` 同款），非朴素逐通道。

    根源正确性（非"过容差即可"）：朴素逐通道曲线会在非灰像素上漂移
    hue/saturation——本机 profile 曲线不够极端故仍在对拍容差内，但高饱和/高反差
    图上即"plausible but wrong"。Adobe 规范法：对**最大、最小**通道施曲线，
    **中间**通道按其在 [min,max] 的相对位置插值 → 保持通道序(hue)与中通道
    比例(近似保 saturation)。曲线为密采样点列，线性插值即足够还原。
    """
    xs = np.clip(curve[:, 0], 0.0, 1.0)
    ys = np.clip(curve[:, 1], 0.0, 1.0)
    a = np.clip(rgb, 0.0, 1.0).astype(np.float32)
    order = np.argsort(np.argsort(a, axis=-1), axis=-1)   # 各通道秩，用于还原
    s = np.sort(a, axis=-1)
    lo, mid, hi = s[..., 0], s[..., 1], s[..., 2]
    lo2 = np.interp(lo, xs, ys).astype(np.float32)
    hi2 = np.interp(hi, xs, ys).astype(np.float32)
    den = hi - lo
    frac = np.where(den > 1e-7, (mid - lo) / np.maximum(den, 1e-7), 0.0)
    mid2 = np.where(
        den > 1e-7,
        lo2 + (hi2 - lo2) * frac,
        np.interp(mid, xs, ys),                            # 退化(三通道相等)：直接施曲线
    ).astype(np.float32)
    toned_sorted = np.stack([lo2, mid2, hi2], axis=-1)
    return np.take_along_axis(toned_sorted, order, axis=-1).astype(np.float32)


# ===========================================================================
# 9. 端到端：相机原生线性 RGB → 线性 sRGB(D65)
# ===========================================================================

def neutral_from_camera_wb(camera_whitebalance: list[float]) -> np.ndarray:
    """rawpy camera_whitebalance(乘数) → AsShotNeutral(相机中性坐标)。

    WB 乘数 m 使 raw·m 中性 ⇒ 中性相机坐标 ∝ 1/m。归一到绿=1（DNG 约定，
    D 对角阵会再把它映到 (1,1,1)，整体尺度不影响色相）。
    """
    m = np.asarray(camera_whitebalance[:3], dtype=np.float64)
    m = np.where(np.abs(m) < 1e-9, 1.0, m)
    n = 1.0 / m
    return (n / n[1]).astype(np.float64)


def render_camera_to_linear_srgb(
    cam_rgb: np.ndarray,
    neutral: np.ndarray,
    prof: DCPProfile,
    *,
    use_hsv_map: bool = True,
    use_look_table: bool = True,
    use_tone_curve: bool = True,
) -> np.ndarray:
    """完整 DCP 链：相机原生线性 RGB(HxWx3) → 线性 sRGB(D65)。

    逐层可关（use_hsv_map / use_look_table / use_tone_curve）以支持
    逐层 isolate 对拍：纯矩阵 → +HSV → +Look → +ToneCurve，每层独立对拍
    RawTherapee（与 RT DCP tab 的 Base/Look/Tone 勾选框一一对应）。
    """
    neutral = np.asarray(neutral, dtype=np.float64)
    neutral = neutral / max(neutral[1], 1e-9)
    rm = _resolve(prof, neutral)
    cam_to_xyz = _camera_to_xyz_d50(rm, prof, neutral)

    flat = cam_rgb.reshape(-1, 3).astype(np.float64)
    xyz = flat @ cam_to_xyz.T                              # → XYZ(D50)
    ref = (xyz @ _XYZ_TO_PROPHOTO_D50.T).astype(np.float32)  # → ProPhoto(D50) 线性
    ref = np.maximum(ref, 0.0).reshape(cam_rgb.shape)

    # BaselineExposureOffset 是 **场景线性** 曝光量，须在 look/tone 之前于线性
    # 域施加（Adobe dng_render 顺序）。曾误置于链尾 sRGB 上：tone curve 高度
    # 非线性，×2^beo 前/后施加差异巨大——stage4 对拍 RT 6.5e-4→9.5e-5 实证
    # （stage1/3 无 tone curve，beo 只是线性缩放，故顺序不影响、未暴露此 bug）。
    if prof.baseline_exposure_offset:
        ref = ref * np.float32(2.0 ** prof.baseline_exposure_offset)

    if use_hsv_map and rm.hsm is not None and rm.hsm_dims is not None:
        ref = _apply_hsv_table(ref, rm.hsm, rm.hsm_dims, prof.hsm_encoding)

    if use_look_table and prof.look_data is not None and prof.look_dims is not None:
        ref = _apply_hsv_table(ref, prof.look_data, prof.look_dims, prof.look_encoding)

    if use_tone_curve and prof.has_tone_curve:
        ref = _apply_tone_curve(ref, prof.tone_curve)

    # ProPhoto(D50) → XYZ(D50) → Bradford D50→D65 → 线性 sRGB(D65)
    flat = ref.reshape(-1, 3).astype(np.float64)
    xyz = flat @ _PROPHOTO_D50_TO_XYZ.T
    srgb = (xyz @ _XYZ_D50_TO_SRGB.T).astype(np.float32)
    return srgb.reshape(cam_rgb.shape)


# ===========================================================================
# 10. Profile 三层来源（env/手动 → Adobe DNG Converter → 无；显式不静默 #8）
# ===========================================================================

def _adobe_profile_dirs() -> list[str]:
    """Adobe DNG Converter / Camera Raw 的 CameraProfiles 目录（跨平台）。"""
    dirs: list[str] = []
    if os.name == "nt":
        for env in ("LOCALAPPDATA", "APPDATA", "PROGRAMDATA"):
            base = os.environ.get(env)
            if base:
                dirs.append(os.path.join(base, "Adobe", "CameraRaw", "CameraProfiles"))
    else:
        home = os.path.expanduser("~")
        dirs += [
            os.path.join(home, "Library", "Application Support", "Adobe",
                         "CameraRaw", "CameraProfiles"),
            "/Library/Application Support/Adobe/CameraRaw/CameraProfiles",
            os.path.join(home, ".local", "share", "Adobe", "CameraRaw",
                         "CameraProfiles"),
        ]
    return [d for d in dirs if os.path.isdir(d)]


def _norm_model(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum())


# 厂商写进 EXIF Model 的噪声词（"NIKON CORPORATION NIKON Z 50" 的 maker 噪声）。
# 剥噪声 + 折叠重复 maker token 后与 DCP UniqueCameraModel 做**权威等值**匹配。
_MODEL_NOISE = frozenset((
    "corporation", "company", "co", "co.", "co.,ltd", "co.,ltd.", "ltd",
    "ltd.", "inc", "inc.", "imaging", "optical", "digital", "camera",
))

# Adobe profile variant 偏好顺序（锚 = Camera Standard，与 Phase 2 真机
# sign-off 1.4e-5/3.0e-5/8.8e-5 一致；含 ToneCurve ⇒ 正确禁用编辑 base
# curve、防 double tone map）。不在表内的 variant（Vivid/Portrait/Mono…）优先级最低。
_VARIANT_PREF = ("camera standard", "adobe standard")


def _clean_camera_model(model: str) -> str:
    """剥厂商噪声 + 折叠相邻重复 token：
    'NIKON CORPORATION NIKON Z 50' → 'NIKON Z 50'（与 DCP UniqueCameraModel
    DNG 规范权威绑定键对齐，杜绝 Make 前缀污染导致的静默 matrix 降级）。
    """
    out: list[str] = []
    for tok in model.replace("_", " ").split():
        low = tok.lower().strip(".,")
        if low in _MODEL_NOISE:
            continue
        if out and out[-1].lower() == tok.lower():   # 折叠 'NIKON NIKON'
            continue
        out.append(tok)
    return " ".join(out)


def _variant_rank(filename: str) -> int:
    low = filename.lower()
    for i, v in enumerate(_VARIANT_PREF):
        if v in low:
            return i
    return len(_VARIANT_PREF)                          # 表外 variant：最低优先


def resolve_profile_path(camera_model: str) -> tuple[str | None, str]:
    """三层来源 → (.dcp 路径 | None, 来源说明)。降级显式不静默。

      1. OPS_DCP_PROFILE（文件，直指）/ OPS_DCP_DIR（目录）
      2. 自动探测 Adobe Profile 目录，按 **DCP UniqueCameraModel 权威等值**
         匹配（非文件名子串——后者会被 Make 前缀污染、且 Z 50 误配 Z 50 II /
         Adobe Standard，结果看似合理实则错配），同机身多 variant
         按 _VARIANT_PREF 确定性选 Camera Standard
      3. 都没有 → (None, 'matrix') —— 调用方退相机内置矩阵且 UI 显式提示
         （不再"随便挑一个 .dcp"兜底——那是静默错配）
    """
    from ..utils.user_paths import environment_value
    env_file = environment_value("OPS_DCP_PROFILE")
    if env_file and os.path.isfile(env_file):
        return env_file, f"env:{os.path.basename(env_file)}"

    target = _norm_model(_clean_camera_model(camera_model))
    if not target:
        return None, "matrix"                          # 机型未知 → 显式降级，不臆测

    search_dirs: list[str] = []
    env_dir = environment_value("OPS_DCP_DIR")
    if env_dir and os.path.isdir(env_dir):
        search_dirs.append(env_dir)
    search_dirs += _adobe_profile_dirs()

    # (variant_rank, path, filename) —— 仅收 UniqueCameraModel 精确等值的候选。
    matches: list[tuple[int, str, str]] = []
    for d in search_dirs:
        for root, _sub, files in os.walk(d):
            for fn in files:
                if not fn.lower().endswith(".dcp"):
                    continue
                # 廉价文件名预筛（避免解析 4000+ 文件）；正确性由下方权威
                # 等值兜底，预筛宽松不影响结果、只决定解析谁。
                if target not in _norm_model(os.path.splitext(fn)[0]):
                    continue
                full = os.path.join(root, fn)
                try:
                    ucm = parse_dcp(full).camera_model
                except Exception as exc:               # 坏档跳过，不静默错配
                    logger.warning("跳过无法解析的 .dcp（%s）：%s", full, exc)
                    continue
                if _norm_model(ucm) == target:          # DNG 权威绑定键精确等值
                    matches.append((_variant_rank(fn), full, fn))

    if not matches:
        return None, "matrix"
    rank, full, fn = min(matches, key=lambda m: (m[0], m[1]))  # 确定性：variant 优先, path 次之
    return full, f"adobe:{fn}"


# ===========================================================================
# 11. 默认 RAW 比色路（E1）：XYZ(D65) → 线性 sRGB
# ===========================================================================

def xyz_d65_to_srgb_matrix() -> np.ndarray:
    return _XYZ_D65_TO_SRGB.copy()


def matrix_fallback_to_linear_srgb(
    cam_rgb: np.ndarray,
    neutral: np.ndarray,
    cam_xyz_matrix: np.ndarray | None,
) -> np.ndarray:
    """**E1（默认 RAW 比色路）**：cam_rgb = libraw 权威
    `output_color=XYZ` 输出的 **XYZ(D65)**（libraw 已完成 camera→XYZ + 一次性
    WB）。本函数仅接最后一段 XYZ(D65) → 线性 sRGB（`_XYZ_D65_TO_SRGB`，
    IEC 61966-2-1；真机实测与 libraw 自身 sRGB 输出 χ²≈0、逐位一致）。

    早先这里对 libraw cam_xyz 取逆当作 camera→XYZ，实测把蓝/绿/橙旋成品红：
    ColorMatrix 须配 ForwardMatrix 与参考中性才成立，单独取逆不是合法变换。

    `neutral` / `cam_xyz_matrix` 保留以兼容调用签名；WB 与 camera→XYZ
    均已由 libraw 完成，此处不再进行相机矩阵变换。
    """
    del neutral, cam_xyz_matrix  # E1：libraw 已做 WB+camera→XYZ，二者不参与
    xyz = np.asarray(cam_rgb, dtype=np.float64)
    flat = xyz.reshape(-1, 3)
    srgb = (flat @ _XYZ_D65_TO_SRGB.T).astype(np.float32)
    return srgb.reshape(xyz.shape)
