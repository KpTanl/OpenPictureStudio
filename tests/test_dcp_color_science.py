"""Phase 2 DCP 色彩科学层验收。

两部分（同 test_wb_matrix_contract.py 的「离线断言 + 需环境对拍」结构）：

  1. **离线不变量断言**（无需 RAW/RT）：自构合成 .dcp 做 TIFF-tag 解析
     round-trip（dcpTool 的离线替身）、矩阵链中性不变量、HSV 环形 hue 连续、
     双光照 CCT 收敛与混合连续、ToneCurve 单调、显式降级。这些是比色层
     「写得出但证不对」高风险项的离线证伪。
  2. **RT 数值对拍 dumper**（需用户环境）：给定 RAW + .dcp，导出 DCP 渲染
     结果供与 RawTherapee 同 Profile 逐层 isolate 对拍（线性域 RGB MSE 均值 ≤ 1e-4）。无环境
     时 skip 并打印手动步骤——数值 sign-off 依赖用户的 RT + .dcp + RAW。

设 fixture：OPS_RAW_FIXTURE=<raw>，OPS_DCP_PROFILE=<.dcp>（或自动探测
Adobe DNG Converter Profile 目录）。
"""

import glob
import os
import struct

import numpy as np
import pytest

from src.core import dcp


# --------------------------------------------------------------------------
# 合成 .dcp 构造器：写合法单 IFD TIFF/EP（解析 round-trip 的离线对照源）
# --------------------------------------------------------------------------

_T_ASCII, _T_SHORT, _T_LONG, _T_RATIONAL, _T_SRATIONAL, _T_FLOAT = 2, 3, 4, 5, 10, 11
_TSIZE = {_T_ASCII: 1, _T_SHORT: 2, _T_LONG: 4, _T_RATIONAL: 8, _T_SRATIONAL: 8, _T_FLOAT: 4}


def _encode_values(end: str, ftype: int, values) -> bytes:
    if ftype == _T_ASCII:
        return values.encode("ascii") + b"\x00"
    if ftype in (_T_RATIONAL, _T_SRATIONAL):
        code = "I" if ftype == _T_RATIONAL else "i"
        out = b""
        for v in values:
            num = int(round(v * 1_000_000))
            out += struct.pack(end + code + code, num, 1_000_000)
        return out
    code = {_T_SHORT: "H", _T_LONG: "I", _T_FLOAT: "f"}[ftype]
    return struct.pack(end + code * len(values), *values)


def build_dcp(tags: dict[int, tuple[int, object]], *, big_endian: bool = False) -> bytes:
    """tags: {tag_id: (tiff_type, values)}。values: str(ASCII) 或 list。"""
    end = ">" if big_endian else "<"
    entries = sorted(tags.items())
    n = len(entries)
    ifd_off = 8
    ifd_size = 2 + 12 * n + 4
    data_off = ifd_off + ifd_size

    ifd = struct.pack(end + "H", n)
    data = b""
    for tag, (ftype, values) in entries:
        count = len(values) if ftype != _T_ASCII else len(values) + 1
        payload = _encode_values(end, ftype, values)
        total = len(payload)
        ifd += struct.pack(end + "HHI", tag, ftype, count)
        if total <= 4:
            ifd += payload + b"\x00" * (4 - total)
        else:
            ifd += struct.pack(end + "I", data_off + len(data))
            data += payload
    ifd += struct.pack(end + "I", 0)  # 无下一 IFD

    header = (b"MM" if big_endian else b"II") + struct.pack(end + "HI", 42, ifd_off)
    return header + ifd + data


_TG = dcp._TAG  # 标签名→ID


def _identity_hsm():
    """1x1x1 恒等 HueSatMap：(hueShift°, satScale, valScale)=(0,1,1)。"""
    return [0.0, 1.0, 1.0]


def _make_single_illum_dcp(extra: dict | None = None) -> bytes:
    """单光照（D65）、ForwardMatrix 路径、恒等 HSM。CM/FM 用 sRGB↔XYZ(D50)
    的标准矩阵，使「相机=线性 sRGB」时链路近似恒等，便于中性不变量断言。"""
    # _XYZ_D50_TO_SRGB: XYZ(D50)→sRGB；其逆 = sRGB→XYZ(D50) = ForwardMatrix。
    fm = np.linalg.inv(dcp._XYZ_D50_TO_SRGB)              # sRGB→XYZ(D50)
    cm = dcp._XYZ_D50_TO_SRGB                              # XYZ(D50)→camera(=sRGB)
    tags = {
        _TG["ProfileName"]: (_T_ASCII, "SynthUnit"),
        _TG["UniqueCameraModel"]: (_T_ASCII, "ACME TestCam"),
        _TG["CalibrationIlluminant1"]: (_T_SHORT, [21]),  # D65
        _TG["ColorMatrix1"]: (_T_SRATIONAL, list(cm.reshape(-1))),
        _TG["ForwardMatrix1"]: (_T_SRATIONAL, list(fm.reshape(-1))),
        _TG["ProfileHueSatMapDims"]: (_T_LONG, [1, 1, 1]),
        _TG["ProfileHueSatMapData1"]: (_T_FLOAT, _identity_hsm()),
    }
    if extra:
        tags.update(extra)
    return build_dcp(tags)


# ========================================================== 1. 离线不变量

def test_parse_roundtrip_little_and_big_endian():
    cm = list(np.arange(9, dtype=float) / 10.0)
    for be in (False, True):
        blob = build_dcp(
            {
                _TG["ProfileName"]: (_T_ASCII, "RT Check"),
                _TG["CalibrationIlluminant1"]: (_T_SHORT, [17]),   # StdA
                _TG["CalibrationIlluminant2"]: (_T_SHORT, [21]),   # D65
                _TG["ColorMatrix1"]: (_T_SRATIONAL, cm),
                _TG["ProfileHueSatMapDims"]: (_T_LONG, [2, 1, 1]),
                _TG["ProfileToneCurve"]: (_T_FLOAT, [0.0, 0.0, 0.5, 0.6, 1.0, 1.0]),
            },
            big_endian=be,
        )
        t = dcp._read_tiff_tags(blob)
        assert t["ProfileName"] == "RT Check"
        assert t["CalibrationIlluminant1"] == 17
        assert t["CalibrationIlluminant2"] == 21
        np.testing.assert_allclose(t["ColorMatrix1"], cm, atol=1e-6)
        assert list(t["ProfileHueSatMapDims"]) == [2, 1, 1]
        np.testing.assert_allclose(
            t["ProfileToneCurve"], [0.0, 0.0, 0.5, 0.6, 1.0, 1.0], atol=1e-6
        )


def test_parse_dcp_semantic_fields():
    blob = _make_single_illum_dcp(
        {_TG["ProfileToneCurve"]: (_T_FLOAT, [0.0, 0.0, 1.0, 1.0])}
    )
    p = tmp_write_and_parse(blob)
    assert p.name == "SynthUnit"
    assert p.camera_model == "ACME TestCam"
    assert p.temp1 == 6504.0 and p.temp2 == 0.0
    assert not p.is_dual_illuminant
    assert p.has_tone_curve
    assert p.forward_matrix_1 is not None and p.forward_matrix_1.shape == (3, 3)


def tmp_write_and_parse(blob: bytes) -> dcp.DCPProfile:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".dcp", delete=False) as f:
        f.write(blob)
        path = f.name
    try:
        return dcp.parse_dcp(path)
    finally:
        os.unlink(path)


def test_illuminant_temp_table():
    assert dcp._illuminant_to_temp(17) == 2856.0   # Standard A
    assert dcp._illuminant_to_temp(21) == 6504.0   # D65
    assert dcp._illuminant_to_temp(23) == 5003.0   # D50
    assert dcp._illuminant_to_temp(999) == 0.0


def test_interp_weight_mired_space():
    t1, t2 = 2856.0, 6504.0
    assert dcp._interp_weight(t1, t1, t2) == pytest.approx(1.0)   # 纯 illum1
    assert dcp._interp_weight(t2, t1, t2) == pytest.approx(0.0)   # 纯 illum2
    mid = dcp._interp_weight(4000.0, t1, t2)
    assert 0.0 < mid < 1.0
    # mired 线性：单调
    g_lo = dcp._interp_weight(3000.0, t1, t2)
    g_hi = dcp._interp_weight(6000.0, t1, t2)
    assert g_lo > mid > g_hi
    # 越界 clamp
    assert dcp._interp_weight(1000.0, t1, t2) == pytest.approx(1.0)
    assert dcp._interp_weight(20000.0, t1, t2) == pytest.approx(0.0)
    # 单光照退化
    assert dcp._interp_weight(5000.0, 6504.0, 0.0) == 1.0


def test_matrix_chain_neutral_maps_to_gray():
    """比色层核心不变量：相机中性输入 → 输出近中性灰（R≈G≈B）。

    链序/WB 并入错则中性会偏色——这是无 RT 也能证伪的关键断言。
    """
    p = tmp_write_and_parse(_make_single_illum_dcp())
    neutral = np.array([0.55, 0.62, 0.48], dtype=np.float64)
    cam = np.tile(neutral.astype(np.float32), (4, 4, 1))
    out = dcp.render_camera_to_linear_srgb(
        cam, neutral, p, use_tone_curve=False, use_look_table=False
    )
    px = out.reshape(-1, 3)
    spread = (px.max(axis=1) - px.min(axis=1)).max()
    assert spread < 1e-3, f"中性输入未映射到灰（spread={spread:.4g}）—链序/WB 错"
    assert np.all(np.isfinite(out))


def test_matrix_chain_scales_with_input():
    p = tmp_write_and_parse(_make_single_illum_dcp())
    neutral = np.array([0.5, 0.5, 0.5], dtype=np.float64)
    a = dcp.render_camera_to_linear_srgb(
        np.full((2, 2, 3), 0.2, np.float32), neutral, p,
        use_tone_curve=False, use_look_table=False)
    b = dcp.render_camera_to_linear_srgb(
        np.full((2, 2, 3), 0.4, np.float32), neutral, p,
        use_tone_curve=False, use_look_table=False)
    assert b.mean() > a.mean()                       # 线性单调


def test_hsv_identity_table_is_noop():
    p = tmp_write_and_parse(_make_single_illum_dcp())  # HSM = 恒等
    rng = np.random.default_rng(3)
    cam = rng.uniform(0.05, 0.9, (8, 8, 3)).astype(np.float32)
    neutral = np.array([0.5, 0.5, 0.5], dtype=np.float64)
    with_hsm = dcp.render_camera_to_linear_srgb(
        cam, neutral, p, use_tone_curve=False, use_look_table=False)
    p_nohsm = tmp_write_and_parse(
        _make_single_illum_dcp())
    p_nohsm.hsm_data_1 = None
    no_hsm = dcp.render_camera_to_linear_srgb(
        cam, neutral, p_nohsm, use_tone_curve=False, use_look_table=False)
    np.testing.assert_allclose(with_hsm, no_hsm, atol=2e-3)


def test_hsv_circular_hue_continuity():
    """hue 环形插值——seam(0°↔360°) 两侧必须连续，错则色相断层。"""
    hd = 6
    # 仅 hue 平移 +30° 的表（全网格相同 delta），sat/val 不变。
    table = np.zeros((1, hd, 1, 3), dtype=np.float32)
    table[..., 0] = 30.0   # hueShift 30°
    table[..., 1] = 1.0
    table[..., 2] = 1.0
    dims = (hd, 1, 1)
    # 取一圈 hue 上的纯色（含跨 seam 处），施加后角差应处处≈常数（连续无跳变）。
    H = np.linspace(0.0, 6.0, 64, endpoint=False)
    rgb = dcp._hsv6_to_rgb(H, np.full_like(H, 0.8), np.full_like(H, 0.7))
    out = dcp._apply_hsv_table(rgb[None, :, :], table, dims, 0)
    h2, _, _ = dcp._rgb_to_hsv6(out[0])
    dh = (h2 - H) % 6.0
    # 全部样本（含 seam）平移量一致 → 标准差极小（断层会在 seam 爆出大方差）
    assert np.std(dh) < 1e-2, f"环形 hue 不连续，std={np.std(dh):.4g}"
    assert np.allclose(dh, dh[0], atol=2e-2)


def test_hsv_sat_scale_clamped():
    hd = 4
    table = np.zeros((1, hd, 1, 3), dtype=np.float32)
    table[..., 0] = 0.0
    table[..., 1] = 4.0   # 4x 饱和 → 必须 clamp 到 1
    table[..., 2] = 1.0
    rng = np.random.default_rng(5)
    rgb = rng.uniform(0.1, 0.9, (4, 4, 3)).astype(np.float32)
    out = dcp._apply_hsv_table(rgb, table, (hd, 1, 1), 0)
    _, s, _ = dcp._rgb_to_hsv6(out)
    assert s.max() <= 1.0 + 1e-5


def test_dual_illuminant_cct_converges_and_blends_continuously():
    """双光照 CCT 迭代收敛，且随中性连续无跳变（矩阵+map 混合）。"""
    cm1 = dcp._XYZ_D50_TO_SRGB * 1.0
    cm2 = dcp._XYZ_D50_TO_SRGB * 1.0
    fm = np.linalg.inv(dcp._XYZ_D50_TO_SRGB)
    blob = build_dcp({
        _TG["ProfileName"]: (_T_ASCII, "Dual"),
        _TG["CalibrationIlluminant1"]: (_T_SHORT, [17]),   # StdA 2856
        _TG["CalibrationIlluminant2"]: (_T_SHORT, [21]),   # D65 6504
        _TG["ColorMatrix1"]: (_T_SRATIONAL, list(cm1.reshape(-1))),
        _TG["ColorMatrix2"]: (_T_SRATIONAL, list(cm2.reshape(-1))),
        _TG["ForwardMatrix1"]: (_T_SRATIONAL, list(fm.reshape(-1))),
        _TG["ForwardMatrix2"]: (_T_SRATIONAL, list((fm * 1.05).reshape(-1))),
        _TG["ProfileHueSatMapDims"]: (_T_LONG, [1, 1, 1]),
        _TG["ProfileHueSatMapData1"]: (_T_FLOAT, [10.0, 1.0, 1.0]),
        _TG["ProfileHueSatMapData2"]: (_T_FLOAT, [-10.0, 1.0, 1.0]),
    })
    p = tmp_write_and_parse(blob)
    assert p.is_dual_illuminant
    cam = np.full((2, 2, 3), 0.5, np.float32)
    prev = None
    for warm in np.linspace(0.40, 0.62, 12):           # 中性从暖到冷扫
        neutral = np.array([warm, 0.5, 1.0 - warm], dtype=np.float64)
        cct = dcp._solve_cct(p, neutral / neutral[1])
        assert np.isfinite(cct) and 2000.0 <= cct <= 25000.0
        out = dcp.render_camera_to_linear_srgb(
            cam, neutral, p, use_tone_curve=False, use_look_table=False)
        assert np.all(np.isfinite(out))
        if prev is not None:
            assert np.abs(out - prev).max() < 0.15      # 连续，无 CCT 跳变断层
        prev = out


def test_tone_curve_identity_and_monotone():
    p_id = tmp_write_and_parse(
        _make_single_illum_dcp(
            {_TG["ProfileToneCurve"]: (_T_FLOAT, [0.0, 0.0, 1.0, 1.0])}))
    p_lift = tmp_write_and_parse(
        _make_single_illum_dcp(
            {_TG["ProfileToneCurve"]: (_T_FLOAT, [0.0, 0.0, 0.5, 0.7, 1.0, 1.0])}))
    cam = np.full((4, 4, 3), 0.4, np.float32)
    neutral = np.array([0.5, 0.5, 0.5], dtype=np.float64)
    base = dcp.render_camera_to_linear_srgb(
        cam, neutral, p_id, use_look_table=False)
    lift = dcp.render_camera_to_linear_srgb(
        cam, neutral, p_lift, use_look_table=False)
    no_tc = dcp.render_camera_to_linear_srgb(
        cam, neutral, p_id, use_tone_curve=False, use_look_table=False)
    np.testing.assert_allclose(base, no_tc, atol=2e-3)   # 恒等曲线 = 无曲线
    assert lift.mean() > base.mean()                     # 提升曲线整体变亮


def test_tone_curve_is_hue_preserving_not_naive_per_channel():
    """根源护栏：tone curve 必须用 Adobe 保色相 rgbTone，**非朴素逐通道**。

    朴素逐通道在非灰像素上漂移 hue（通道序可能翻转）。Adobe/RT 规范法对
    max/min 施曲线、mid 按比例插值 → 通道序(hue)严格不变。强非线性曲线 +
    高饱和像素下断言：① 通道序保持；② 与朴素逐通道结果明显不同（证明确
    走了 rgbTone 而非退化成逐通道）。
    """
    curve = np.array([[0, 0], [0.2, 0.7], [0.5, 0.9], [1, 1]], dtype=np.float64)
    rng = np.random.default_rng(2)
    rgb = rng.uniform(0.02, 0.95, (200, 3)).astype(np.float32)  # 高饱和杂色
    out = dcp._apply_tone_curve(rgb, curve)
    # ① 每像素通道大小序不变（hue 保持）
    assert np.array_equal(np.argsort(rgb, 1), np.argsort(out, 1)), "通道序变了→hue 漂移"
    # ② 与朴素逐通道明显不同（否则等于没用 rgbTone）
    xs, ys = curve[:, 0], curve[:, 1]
    naive = np.interp(np.clip(rgb, 0, 1), xs, ys).astype(np.float32)
    assert np.abs(out - naive).max() > 0.02, "与朴素逐通道无差异→未走 rgbTone"
    # ③ 退化：三通道相等时 rgbTone == 直接施曲线
    g = np.full((5, 3), 0.4, np.float32)
    np.testing.assert_allclose(
        dcp._apply_tone_curve(g, curve), np.interp(0.4, xs, ys), atol=1e-6)


def test_dng_tag_ids_match_authoritative_spec():
    """根源护栏：**整张** _TAG 表逐项 == 权威 DNG/EXIF tag ID。

    非"只钉修过的那几条"——曾误写 LookTable=51107/8（应 50981/2）致整张相机
    look 静默丢弃。下表是 ExifTool EXIF 标签库（exiftool.org/TagNames/EXIF.html，
    Adobe DNG 规范的事实权威）核对值，逐项 + 全表完备性双向断言，杜绝
    "本机 profile 没用到的 tag 其实是错的" 这类潜伏 bug。
    """
    authoritative = {                                    # 0xHEX 见注释
        "UniqueCameraModel": 50708,                      # 0xc614
        "ColorMatrix1": 50721, "ColorMatrix2": 50722,    # 0xc621/2
        "CameraCalibration1": 50723, "CameraCalibration2": 50724,  # 0xc623/4
        "AnalogBalance": 50727,                          # 0xc627
        "AsShotNeutral": 50728,                          # 0xc628
        "CalibrationIlluminant1": 50778,                 # 0xc65a
        "CalibrationIlluminant2": 50779,                 # 0xc65b
        "ForwardMatrix1": 50964, "ForwardMatrix2": 50965,  # 0xc714/5
        "ProfileName": 50936,                            # 0xc6f8
        "ProfileHueSatMapDims": 50937,                   # 0xc6f9
        "ProfileHueSatMapData1": 50938,                  # 0xc6fa
        "ProfileHueSatMapData2": 50939,                  # 0xc6fb
        "ProfileToneCurve": 50940,                       # 0xc6fc
        "ProfileEmbedPolicy": 50941,                     # 0xc6fd
        "ProfileLookTableDims": 50981,                   # 0xc725
        "ProfileLookTableData": 50982,                   # 0xc726
        "ProfileHueSatMapEncoding": 51107,               # 0xc7a3
        "ProfileLookTableEncoding": 51108,               # 0xc7a4
        "BaselineExposureOffset": 51109,                 # 0xc7a5
        "DefaultBlackRender": 51110,                     # 0xc7a6
    }
    for name, tid in authoritative.items():
        assert dcp._TAG.get(name) == tid, f"{name}: {dcp._TAG.get(name)} != {tid}"
    # 完备性：_TAG 不得有未经核对的额外条目（防新增即引入错误常量）
    assert set(dcp._TAG) == set(authoritative), (
        f"_TAG 含未核对项: {set(dcp._TAG) ^ set(authoritative)}"
    )
    # 反向索引一致
    assert dcp._TAG_NAME == {v: k for k, v in dcp._TAG.items()}


def test_look_table_and_baseline_exposure_parse_and_apply():
    """LookTable（90×16×16 量级）+ BaselineExposureOffset 必须解析且生效——
    Adobe Camera Matching profile 的相机 look 主要靠 LookTable，不能再丢。"""
    hd, sd, vd = 6, 2, 2
    flat = []
    for _ in range(hd * sd * vd):
        flat += [20.0, 1.1, 1.0]                          # 全网格 +20° hue, +sat
    blob = build_dcp({
        _TG["ProfileName"]: (_T_ASCII, "Looky"),
        _TG["CalibrationIlluminant1"]: (_T_SHORT, [21]),
        _TG["ColorMatrix1"]: (_T_SRATIONAL, list(dcp._XYZ_D50_TO_SRGB.reshape(-1))),
        _TG["ForwardMatrix1"]: (
            _T_SRATIONAL, list(np.linalg.inv(dcp._XYZ_D50_TO_SRGB).reshape(-1))),
        _TG["ProfileLookTableDims"]: (_T_LONG, [hd, sd, vd]),
        _TG["ProfileLookTableData"]: (_T_FLOAT, flat),
        _TG["BaselineExposureOffset"]: (_T_SRATIONAL, [-0.35]),
    })
    p = tmp_write_and_parse(blob)
    assert p.look_dims == (hd, sd, vd)
    assert p.look_data is not None and p.look_data.shape == (vd, hd, sd, 3)
    assert p.baseline_exposure_offset == pytest.approx(-0.35, abs=1e-4)

    cam = np.random.default_rng(9).uniform(0.1, 0.8, (8, 8, 3)).astype(np.float32)
    neutral = np.array([0.5, 0.5, 0.5], dtype=np.float64)
    with_look = dcp.render_camera_to_linear_srgb(
        cam, neutral, p, use_tone_curve=False)
    no_look = dcp.render_camera_to_linear_srgb(
        cam, neutral, p, use_look_table=False, use_tone_curve=False)
    assert np.abs(with_look - no_look).max() > 1e-3       # look 确实改变了输出（不再被丢）


def test_baseline_exposure_applied_in_linear_before_tone_curve():
    """回归护栏：BaselineExposureOffset 是场景**线性**曝光，必须在 tone
    curve **之前**于线性域施加（Adobe dng_render 顺序）。

    曾误置链尾 sRGB 上——tone curve 高度非线性，×2^beo 前/后差异巨大；
    真机对拍 RT（Nikon Z 50）stage4 因此 6.5e-4，移到 tone 前降到 9.5e-5
    达对拍容差（≤ 1e-4）。stage1/3 无 tone curve 时 beo 仅线性缩放、顺序无关，
    故该 bug 只在含 tone curve 的 profile 暴露——此测试正是钉死该顺序。
    """
    nonlinear_tc = [0.0, 0.0, 0.25, 0.62, 0.55, 0.90, 1.0, 1.0]   # 强非线性
    beo = 1.0                                                     # ×2
    blob = _make_single_illum_dcp({
        _TG["ProfileToneCurve"]: (_T_FLOAT, nonlinear_tc),
        _TG["BaselineExposureOffset"]: (_T_SRATIONAL, [beo]),
    })
    p = tmp_write_and_parse(blob)
    assert p.baseline_exposure_offset == pytest.approx(beo, abs=1e-4)

    cam = np.random.default_rng(4).uniform(0.05, 0.45, (6, 6, 3)).astype(np.float32)
    neutral = np.array([0.5, 0.5, 0.5], dtype=np.float64)
    out = dcp.render_camera_to_linear_srgb(cam, neutral, p)

    # 复算两种顺序的期望（用模块内部件）
    nn = neutral / max(neutral[1], 1e-9)
    rm = dcp._resolve(p, nn)
    M = dcp._camera_to_xyz_d50(rm, p, nn)
    ref = (cam.reshape(-1, 3).astype(np.float64) @ M.T) @ dcp._XYZ_TO_PROPHOTO_D50.T
    ref = np.maximum(ref, 0.0).astype(np.float32).reshape(cam.shape)
    scale = np.float32(2.0 ** beo)

    def to_srgb(r):
        x = r.reshape(-1, 3).astype(np.float64) @ dcp._PROPHOTO_D50_TO_XYZ.T
        return (x @ dcp._XYZ_D50_TO_SRGB.T).astype(np.float32).reshape(r.shape)

    correct = to_srgb(dcp._apply_tone_curve(ref * scale, p.tone_curve))     # beo 在 tone 前
    wrong = to_srgb(dcp._apply_tone_curve(ref, p.tone_curve)) * scale       # beo 在链尾(旧 bug)

    np.testing.assert_allclose(out, correct, atol=1e-5)
    assert np.abs(out - wrong).max() > 1e-2, "顺序无差异=未真正在 tone 前施加 beo"


def test_neutral_from_camera_wb():
    n = dcp.neutral_from_camera_wb([2.0, 1.0, 1.5, 1.0])
    assert n[1] == pytest.approx(1.0)                    # 归一到绿=1
    assert n[0] < n[1] and n[2] < n[1]                   # 高 R 增益 → 低 R 中性坐标


def test_profile_resolution_explicit_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_DCP_PROFILE", raising=False)
    monkeypatch.delenv("OPS_DCP_DIR", raising=False)
    monkeypatch.setattr(dcp, "_adobe_profile_dirs", lambda: [])
    path, source = dcp.resolve_profile_path("Nikon Z9")
    assert path is None and source == "matrix"           # 显式降级，非静默

    dcp_file = tmp_path / "ACME TestCam.dcp"
    dcp_file.write_bytes(_make_single_illum_dcp())
    monkeypatch.setenv("OPS_DCP_PROFILE", str(dcp_file))
    path, source = dcp.resolve_profile_path("ACME TestCam")
    assert path == str(dcp_file) and source.startswith("env:")


# ---- resolver DNG-权威匹配修复（gap：Z 50 NEF 静默降级 matrix）-------------

@pytest.mark.parametrize("raw,clean", [
    ("NIKON CORPORATION NIKON Z 50", "NIKON Z 50"),   # Make 前缀污染 + 重复折叠
    ("NIKON Z 50", "NIKON Z 50"),
    ("NIKON CORPORATION NIKON Z 50 2", "NIKON Z 50 2"),
    ("Canon EOS R5", "Canon EOS R5"),
    ("SONY ILCE-7M4", "SONY ILCE-7M4"),
    ("", ""),
])
def test_clean_camera_model(raw, clean):
    assert dcp._clean_camera_model(raw) == clean


def test_variant_rank_prefers_camera_standard():
    r = dcp._variant_rank
    assert r("Nikon Z 50 Camera Standard.dcp") == 0
    assert r("Nikon Z 50 Adobe Standard.dcp") == 1
    assert r("Nikon Z 50 Camera Vivid.dcp") == len(dcp._VARIANT_PREF)
    assert r("Nikon Z 50 Camera Standard.dcp") < r("Nikon Z 50 Adobe Standard.dcp")


def _write_dcp(d, fn, ucm, *, tone=False):
    extra = {_TG["UniqueCameraModel"]: (_T_ASCII, ucm)}
    if tone:
        extra[_TG["ProfileToneCurve"]] = (_T_FLOAT, [0.0, 0.0, 1.0, 1.0])
    (d / fn).write_bytes(_make_single_illum_dcp(extra))
    return d / fn


def test_resolver_authoritative_uniquecameramodel_match(tmp_path, monkeypatch):
    """Z 50 NEF：Make 前缀污染下仍命中 **Camera Standard**，且不误配
    Z 50 II / Adobe Standard（DNG UniqueCameraModel 精确等值 + variant 偏好）。
    """
    monkeypatch.delenv("OPS_DCP_PROFILE", raising=False)
    monkeypatch.delenv("OPS_DCP_DIR", raising=False)
    cs = _write_dcp(tmp_path, "Nikon Z 50 Camera Standard.dcp", "NIKON Z 50", tone=True)
    _write_dcp(tmp_path, "Nikon Z 50 Adobe Standard.dcp", "NIKON Z 50")
    z502 = _write_dcp(tmp_path, "Nikon Z 50 2 Camera Standard.dcp", "Nikon Z 50 2", tone=True)
    monkeypatch.setattr(dcp, "_adobe_profile_dirs", lambda: [str(tmp_path)])

    # EXIF 原串（带 "NIKON CORPORATION NIKON" 噪声）→ 必须命中 Camera Standard
    path, source = dcp.resolve_profile_path("NIKON CORPORATION NIKON Z 50")
    assert path == str(cs)                                   # 非 Adobe Standard、非 Z 50 II
    assert source.startswith("adobe:")
    prof = dcp.parse_dcp(path)
    assert dcp._norm_model(prof.camera_model) == "nikonz50"
    assert prof.has_tone_curve                               # Camera Standard 含 ToneCurve

    # Z 50 II 精确等值，绝不与 Z 50 交叉污染（杜绝 plausible-but-wrong）
    p2, _ = dcp.resolve_profile_path("NIKON CORPORATION NIKON Z 50 2")
    assert p2 == str(z502)

    # 未知机型 → 显式 matrix 降级，绝不"随便挑一个 .dcp"兜底（那是静默错配）
    assert dcp.resolve_profile_path("Canon EOS R5") == (None, "matrix")
    assert dcp.resolve_profile_path("") == (None, "matrix")


def test_resolver_honors_ops_dcp_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_DCP_PROFILE", raising=False)
    monkeypatch.setattr(dcp, "_adobe_profile_dirs", lambda: [])
    cs = _write_dcp(tmp_path, "Nikon Z 50 Camera Standard.dcp", "NIKON Z 50", tone=True)
    monkeypatch.setenv("OPS_DCP_DIR", str(tmp_path))
    path, source = dcp.resolve_profile_path("NIKON CORPORATION NIKON Z 50")
    assert path == str(cs) and source.startswith("adobe:")


def test_matrix_fallback_neutral_in_gray_out():
    """E1 比色回归锚：cam_rgb = libraw XYZ(D65)，本函数仅做
    XYZ(D65)→线性 sRGB。D65 白点 XYZ 进 → 中性灰出（spread≈0），灰阶线性
    保持——正是旧 inv(cam_xyz) 路「中性相机像素→R≈2.9/G≈0.06/B≈3.4 品红」的对立锚。
    neutral/cam_xyz 在 E1 下不参与（传任意值/None 结果不变）。"""
    d65_white = np.array([0.95047, 1.0, 1.08883], dtype=np.float64)  # XYZ(D65) 白点
    neutral = np.array([1.0, 1.0, 1.0], dtype=np.float64)

    # D65 白点 XYZ → sRGB ≈ (1,1,1) 中性
    cam = np.tile(d65_white.astype(np.float32), (3, 3, 1))
    out = dcp.matrix_fallback_to_linear_srgb(cam, neutral, None)
    px = out.reshape(-1, 3)
    np.testing.assert_allclose(px, 1.0, atol=2e-3)               # 白点→白
    assert (px.max(1) - px.min(1)).max() < 1e-3                  # 中性进→灰出（无品红）
    assert np.all(np.isfinite(out))

    # 灰阶线性保持 + 无跨通道旋色（旧路品红 bug 的直接反命题）
    ts = np.array([0.1, 0.3, 0.6, 0.9], dtype=np.float32)
    ramp = (ts[:, None] * d65_white.astype(np.float32))[:, None, :]   # (4,1,3) XYZ
    g = dcp.matrix_fallback_to_linear_srgb(ramp, neutral, None).reshape(4, 3)
    assert (g.max(1) - g.min(1)).max() < 2e-3                    # 每级仍中性灰
    np.testing.assert_allclose(g.mean(1), ts, atol=3e-3)         # 灰阶线性
    assert np.all(np.diff(g.mean(1)) > 0)                        # 单调（不回折）

    # neutral/cam_xyz 在 E1 下不参与：传任意 cam_xyz 与 None 逐位相同
    same = dcp.matrix_fallback_to_linear_srgb(cam, neutral, dcp._XYZ_D65_TO_SRGB)
    np.testing.assert_array_equal(same, out)


def test_base_curve_gate_matches_section_4_2():
    from src.core.looks import look_stages

    assert look_stages("ops_v2", "dcp:X") == ()                    # DCP 路不施加 OPS 观感
    assert look_stages("ops_base", "dcp:X") == ()
    assert look_stages("ops_v2", "libraw")                         # 默认 libraw → 施加
    assert look_stages("ops_v2", "srgb-direct")


# ================================ 2. RT 数值对拍 dumper（需用户环境）

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_RAW_GLOBS = ("*.cr2", "*.cr3", "*.nef", "*.arw", "*.dng", "*.raf", "*.orf", "*.pef", "*.rw2")

try:
    import rawpy  # type: ignore
except ImportError:
    rawpy = None


def _find_raw_fixture():
    env = os.environ.get("OPS_RAW_FIXTURE")
    if env and os.path.isfile(env):
        return env
    for pat in _RAW_GLOBS:
        hits = glob.glob(os.path.join(_FIXTURE_DIR, pat))
        if hits:
            return hits[0]
    return None


def _dump_dir() -> str:
    """持久 dump 目录（pytest tmp 会被清，无法做 RT 对拍）。

    OPS_DCP_DUMP_DIR 覆盖；默认 <repo>/dcp_dump（已 gitignore，用户直接
    拿去和 RawTherapee 导出逐层比对）。
    """
    d = os.environ.get("OPS_DCP_DUMP_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "dcp_dump"
    )
    os.makedirs(d, exist_ok=True)
    return d


def test_dcp_render_dump_for_rawtherapee_comparison(monkeypatch):
    """逐层 isolate 数值对拍 dumper。导出 stage1 纯矩阵 → stage2 +HSV
    → stage3 +Look → stage4 +ToneCurve（与 RT DCP tab 的 Base/Look/Tone
    勾选框一一对应）+ meta，供同 Profile 对拍。无环境时 skip 并打印步骤。"""
    profile = os.environ.get("OPS_CI_DCP_PROFILE")
    if profile and not os.environ.get("OPS_DCP_PROFILE"):
        monkeypatch.setenv("OPS_DCP_PROFILE", profile)
    fixture = _find_raw_fixture()
    if rawpy is None:
        pytest.skip("rawpy 未安装：DCP RT 对拍需 rawpy + RawTherapee + RAW + .dcp")
    if fixture is None:
        pytest.skip(
            "未找到 RAW fixture。Phase 2 DCP↔RT 逐层对拍需用户环境：\n"
            "  1) OPS_RAW_FIXTURE=<raw>；OPS_DCP_PROFILE=<.dcp> 或装 Adobe DNG Converter\n"
            "  2) 重跑 → <repo>/dcp_dump/ 下生成 stage{1..4} npy+png + meta\n"
            "  3) RawTherapee 同 Profile 同条件逐层导出对拍（DCP tab 勾选框）\n"
            "  4) 容差：线性域 RGB MSE 均值≤1e-4；肤色/天空/绿叶 ΔE≤2.0"
        )

    import json

    from PIL import Image

    from src.core.photo_engine import decode_raw_native, linear_to_srgb

    # 全分辨率：RT 导出为全尺寸，MSE 须同尺寸同 demosaic 量级对拍。
    # 冻结 DCP 路：for_dcp=True 解到相机原生喂 ForwardMatrix（默认 E1
    # 解的是 XYZ，绝不能喂 DCP 矩阵链，否则静默错）。
    cam_rgb, meta = decode_raw_native(fixture, for_preview=False, for_dcp=True)
    neutral = np.asarray(meta.as_shot_neutral, dtype=np.float64)
    prof_path, source = dcp.resolve_profile_path(meta.camera_model)
    if prof_path is None:
        pytest.skip(
            f"未匹配 {meta.camera_model} 的 .dcp（source={source}）。"
            "设 OPS_DCP_PROFILE 指向该机型 .dcp 后重跑。"
        )

    prof = dcp.parse_dcp(prof_path)
    out_dir = _dump_dir()
    base = os.path.splitext(os.path.basename(fixture))[0]
    cct = dcp._solve_cct(prof, neutral / max(neutral[1], 1e-9))

    # 逐层 isolate：每段只比上一段多开一层，定位偏差落在哪一层。
    stages = {
        "stage1_matrix": dict(use_hsv_map=False, use_look_table=False, use_tone_curve=False),
        "stage2_hsv": dict(use_hsv_map=True, use_look_table=False, use_tone_curve=False),
        "stage3_look": dict(use_hsv_map=True, use_look_table=True, use_tone_curve=False),
        "stage4_full": dict(use_hsv_map=True, use_look_table=True, use_tone_curve=True),
    }
    for tag, kw in stages.items():
        out = dcp.render_camera_to_linear_srgb(cam_rgb, neutral, prof, **kw)
        assert np.all(np.isfinite(out)), f"{tag} 产生 nan/inf"
        np.save(os.path.join(out_dir, f"{base}_{tag}.npy"), out)  # 线性，做 MSE
        disp = np.clip(linear_to_srgb(out), 0.0, 1.0)
        Image.fromarray((disp * 255).round().astype(np.uint8), "RGB").save(
            os.path.join(out_dir, f"{base}_{tag}.png")  # sRGB 预览，肉眼对照
        )

    summary = {
        "camera_model": meta.camera_model,
        "profile": prof_path,
        "profile_name": prof.name,
        "profile_source": source,
        "as_shot_neutral": [round(x, 5) for x in meta.as_shot_neutral],
        "is_dual_illuminant": prof.is_dual_illuminant,
        "calib_temp1_K": prof.temp1,
        "calib_temp2_K": prof.temp2,
        "solved_cct_K": round(cct, 1),
        "has_forward_matrix": (prof.forward_matrix_1 is not None,
                               prof.forward_matrix_2 is not None),
        "has_color_matrix": (prof.color_matrix_1 is not None,
                             prof.color_matrix_2 is not None),
        "hsm_dims": prof.hsm_dims,
        "hsm_encoding": prof.hsm_encoding,
        "hsm_dual": prof.hsm_data_2 is not None,
        "look_dims": prof.look_dims,
        "has_tone_curve": prof.has_tone_curve,
        "tone_curve_pts": (0 if prof.tone_curve is None else len(prof.tone_curve)),
        "baseline_exposure_offset": prof.baseline_exposure_offset,
    }
    with open(os.path.join(out_dir, f"{base}_dcp_meta.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n[Phase 2 DCP dump] {meta.camera_model} via '{prof.name}'  ({source})")
    for k, v in summary.items():
        print(f"  {k:26s}: {v}")
    print(f"  → 持久输出: {out_dir}\\{base}_stage*.{{npy,png}}")
    print("  → RawTherapee 同 Profile 逐层对拍（DCP tab：stage1=全关 / "
          "stage2=Base表 / stage3=+Look / stage4=+Tone），线性域 RGB MSE 均值≤1e-4")
