"""Phase 4 客观基建验收（Auto 自适应 headroom、常数集中表、零调整往返）。

只覆盖**客观**面（无 LR 参考、无主观标定）：
  1. ProCalib 常数集中化：锚值 == Phase 3 classic 平移字面量，核谱与原内联
     表达式逐位等价 ⇒ pro Phase 2/3 sign-off 不回归（铁律：classic 作锚）。
  2. Auto 骨架：estimate_linear_headroom_stops 信号处理性质钉死；
     adapt_auto_params_pro 恒等锚（A/B 安全）+ 调大系数后的单调/钳位结构。
"""

from dataclasses import replace

import numpy as np
import pytest
from PIL import Image

import src.core.photo_engine as photo_engine
from src.core.looks import LEGACY_OPS, OPS_STYLE, RAW_BASE, look_stages
from src.core.photo_engine import (
    AUTO_CALIB,
    PRO_CALIB,
    AutoCalib,
    RawDecodeMeta,
    RenderPipeline,
    RenderSource,
    _apply_dcp_layer,
    _apply_look,
    _apply_look_stage,
    _src_from_pil,
    adapt_auto_params_for_ui,
    adapt_auto_params_pro,
    estimate_linear_headroom_stops,
    linear_to_srgb,
)


# --------------------------------------------------------------------------- #
# 1. ProCalib 集中化：锚值 + 核谱逐位等价（pro 不回归）
# --------------------------------------------------------------------------- #

def test_procalib_anchor_values_are_phase3_literals():
    """集中表数值 == Phase 3 散落字面量（只搬不调，作 Phase 4 标定的锚）。"""
    assert np.float32(PRO_CALIB.zone_peak) == np.float32(45.0 / 255.0)
    assert np.float32(PRO_CALIB.gain_knee) == np.float32(16.0 / 255.0)
    assert PRO_CALIB.tex_eps == 0.10 ** 2
    assert PRO_CALIB.clarity_eps == 0.14 ** 2
    assert PRO_CALIB.dehaze_eps == 0.20 ** 2
    assert PRO_CALIB.tex_strength == 1.0
    assert PRO_CALIB.clarity_strength == 0.8
    assert PRO_CALIB.dehaze_strength == 0.5
    assert PRO_CALIB.dehaze_sat == 0.10


@pytest.mark.parametrize("minhw", [1, 8, 47, 48, 96, 200, 512, 2048, 6000])
def test_procalib_kernels_match_legacy_inline_expressions(minhw):
    """核谱 == 原内联 max(floor, minhw//div) / int(clip(minhw/sdiv, lo, hi))。"""
    assert PRO_CALIB.localtone_kernel(minhw) == (
        max(4, minhw // 48), int(np.clip(minhw / 256.0, 2, 6)))
    assert PRO_CALIB.tex_kernel(minhw) == (
        max(2, minhw // 256), int(np.clip(minhw / 512.0, 1, 4)))
    assert PRO_CALIB.clarity_kernel(minhw) == (
        max(4, minhw // 24), int(np.clip(minhw / 200.0, 2, 6)))
    assert PRO_CALIB.dehaze_kernel(minhw) == (
        max(8, minhw // 8), int(np.clip(minhw / 128.0, 4, 12)))


def _render(img, params):
    return RenderPipeline().render(_src_from_pil(img), params)


def test_pro_pipeline_deterministic_after_centralization():
    """重构后 pro 管线确定（同输入逐位同输出）；恒等参数仍逐位往返。"""
    a = np.random.default_rng(11).integers(0, 256, (40, 56, 3), np.uint8)
    img = Image.fromarray(a, "RGB")
    p = {"shadows": 35, "highlights": -28, "blacks": -12, "whites": 18,
         "texture": 30, "clarity": 22, "dehaze": 17}
    r1 = _render(img, p).pixels
    r2 = _render(img, p).pixels
    assert np.array_equal(r1, r2)
    # 恒等参数：pro 零调整 ≤1 LSB 往返（与 Phase 0/3 同口径锚）。
    src = _src_from_pil(img)
    out = RenderPipeline().render(src, {})
    d0 = np.clip(linear_to_srgb(src.pixels), 0, 1)
    d1 = np.clip(linear_to_srgb(out.pixels), 0, 1)
    assert np.max(np.abs(d0 - d1) * 255.0) <= 1.0


# --------------------------------------------------------------------------- #
# 2. estimate_linear_headroom_stops — 纯信号处理性质
# --------------------------------------------------------------------------- #

def _flat(val, n=4096):
    return np.full((1, n, 3), np.float32(val), np.float32)


def test_headroom_zero_when_no_above_white():
    """高百分位未过白 ⇒ 无 above-white headroom（无 headroom 不硬拉）。"""
    assert estimate_linear_headroom_stops(_flat(0.5)) == 0.0
    assert estimate_linear_headroom_stops(_flat(1.0)) == 0.0


def test_headroom_zero_on_clipped_plateau():
    """削顶平台（亮信息挤在 p_hi、尾部近无斜率）⇒ 不可回收 ⇒ 0。"""
    L = np.full(8192, 0.4, np.float32)
    L[:60] = 1.6                       # ~0.7% 像素硬贴 1.6，[0.9,1.6] 内近平台
    px = np.repeat(L[None, :, None], 1, 0).repeat(3, 2)
    assert estimate_linear_headroom_stops(px) == 0.0


def test_headroom_positive_on_smooth_recoverable_tail():
    """平滑可回收尾部（线性 ramp 0→2.0）⇒ 正 headroom，封顶 recon_cap。"""
    ramp = np.linspace(0.0, 2.0, 8192, dtype=np.float32)
    px = np.repeat(ramp[None, :, None], 1, 0).repeat(3, 2)
    hs = estimate_linear_headroom_stops(px)
    assert hs > 0.0
    assert hs <= AUTO_CALIB.recon_cap_stops + 1e-6      # 再压一档封顶


def test_headroom_capped_and_monotone():
    """实测 headroom 随真实余量单调不降，且被 recon_cap 封顶。"""
    cap = AUTO_CALIB.recon_cap_stops
    small = estimate_linear_headroom_stops(
        np.repeat(np.linspace(0, 1.3, 4096, dtype=np.float32)[None, :, None], 1, 0).repeat(3, 2))
    big = estimate_linear_headroom_stops(
        np.repeat(np.linspace(0, 3.0, 4096, dtype=np.float32)[None, :, None], 1, 0).repeat(3, 2))
    assert 0.0 <= small <= big <= cap + 1e-6


def test_headroom_empty_safe():
    assert estimate_linear_headroom_stops(np.empty((0, 0, 3), np.float32)) == 0.0


# --------------------------------------------------------------------------- #
# 3. adapt_auto_params_pro — 恒等锚（A/B 安全）+ 调系数后的结构
# --------------------------------------------------------------------------- #

def _src(fmt, pixels):
    return RenderSource(
        pixels=np.asarray(pixels, np.float32), alpha=None, source_format=fmt,
        source_path="x", is_proxy=False, orig_size=(1, 1),
        color_pipeline="srgb-direct" if fmt != "raw" else "dcp:t",
    )


_BASE = {"highlights": -20, "shadows": 30, "contrast_amt": 12,
         "exposure_amt": 8, "vibrance_amt": 14, "curves": None}


@pytest.mark.parametrize("fmt", ["raw", "srgb8", "srgb16"])
def test_adapt_is_identity_under_neutral_calib(fmt):
    """显式传入恒等配置时：任意 src 返回 base 等价副本（A/B 安全证明）。"""
    big_headroom = np.repeat(
        np.linspace(0, 3.0, 4096, dtype=np.float32)[None, :, None], 1, 0).repeat(3, 2)
    neutral_calib = AutoCalib(recovery_per_stop=0.0, jpg_conservative=1.0)
    out = adapt_auto_params_pro(_BASE, _src(fmt, big_headroom), calib=neutral_calib)
    assert out == _BASE
    assert out is not _BASE                  # 新副本，未原地改


def test_adapt_under_default_calib_jpg_and_raw():
    """默认 AUTO_CALIB 规范实标（jpg_conservative=0.65, recovery_per_stop=15.0）：
    - srgb 源按 0.65 保守缩放；
    - RAW 源在有可回收高光时按 15.0/stop 自动回收。
    """
    # 1. srgb 源
    srgb_out = adapt_auto_params_pro(_BASE, _src("srgb8", _flat(0.5)))
    assert srgb_out["highlights"] == round(-20 * 0.65)
    assert srgb_out["shadows"] == round(30 * 0.65)
    assert srgb_out["contrast_amt"] == round(12 * 0.65)
    assert srgb_out["exposure_amt"] == _BASE["exposure_amt"]

    # 2. RAW 源（无 headroom 不硬拉）
    raw_flat = adapt_auto_params_pro(_BASE, _src("raw", _flat(0.5)))
    assert raw_flat["highlights"] == _BASE["highlights"]

    # 3. RAW 源（有可回收 headroom）
    ramp = np.repeat(np.linspace(0, 2.0, 4096, dtype=np.float32)[None, :, None], 1, 0).repeat(3, 2)
    raw_ramp = adapt_auto_params_pro(_BASE, _src("raw", ramp))
    assert raw_ramp["highlights"] < _BASE["highlights"]


def test_adapt_does_not_mutate_base():
    before = dict(_BASE)
    adapt_auto_params_pro(_BASE, _src("raw", _flat(2.0)),
                          calib=AutoCalib(recovery_per_stop=40.0))
    assert _BASE == before


def test_adapt_raw_headroom_drives_recovery_and_clamps():
    """调大 recovery_per_stop：RAW 实测 headroom 越大、高光回收越负，钳 [-100,100]。"""
    ramp = np.repeat(np.linspace(0, 3.0, 8192, dtype=np.float32)[None, :, None], 1, 0).repeat(3, 2)
    tuned = AutoCalib(recovery_per_stop=30.0)
    out = adapt_auto_params_pro(_BASE, _src("raw", ramp), calib=tuned)
    assert out["highlights"] < _BASE["highlights"]      # 更负 = 更多回收
    assert out["highlights"] >= -100
    # 无 headroom 的 RAW：即便调大系数也不动（无 headroom 不硬拉）。
    flat = adapt_auto_params_pro(_BASE, _src("raw", _flat(0.5)), calib=tuned)
    assert flat["highlights"] == _BASE["highlights"]
    # 极端：巨系数 → 钳到 -100，不溢出。
    huge = adapt_auto_params_pro(
        _BASE, _src("raw", ramp), calib=AutoCalib(recovery_per_stop=99999.0))
    assert huge["highlights"] == -100


def test_adapt_srgb_conservative_only_scales_down_listed_keys():
    """srgb 源保守钳：只缩不放、仅作用 jpg_keys，其余键不动。"""
    tuned = AutoCalib(jpg_conservative=0.6)
    out = adapt_auto_params_pro(_BASE, _src("srgb8", _flat(0.5)), calib=tuned)
    assert out["highlights"] == round(-20 * 0.6)
    assert out["shadows"] == round(30 * 0.6)
    assert out["contrast_amt"] == round(12 * 0.6)
    assert out["exposure_amt"] == _BASE["exposure_amt"]   # 非 jpg_keys 不动
    assert out["vibrance_amt"] == _BASE["vibrance_amt"]
    # 保守系数 ≤1：jpg_conservative>1 被 min(1,·) 截成不放大。
    noamp = adapt_auto_params_pro(_BASE, _src("srgb8", _flat(0.5)),
                                  calib=AutoCalib(jpg_conservative=2.0))
    assert noamp == _BASE


# --------------------------------------------------------------------------- #
# 3b. adapt_auto_params_for_ui — 入口 A 桥（display PIL → 参数）
# --------------------------------------------------------------------------- #

def _proxy(value: int = 200, size=(8, 8)) -> Image.Image:
    """构造 display sRGB PIL（已 clamp [0,1] 等价）。"""
    arr = np.full((size[1], size[0], 3), value, np.uint8)
    return Image.fromarray(arr, "RGB")


@pytest.mark.parametrize("fmt", ["raw", "srgb8", "srgb16"])
def test_for_ui_identity_under_neutral_calib(fmt):
    """显式传入恒等配置时：桥对任意源返回 base 等价副本（A/B 安全）。"""
    neutral_calib = AutoCalib(recovery_per_stop=0.0, jpg_conservative=1.0)
    out = adapt_auto_params_for_ui(_BASE, _proxy(200), fmt, calib=neutral_calib)
    assert out == _BASE
    assert out is not _BASE                  # 新副本，未原地改


def test_for_ui_raw_proxy_noop_even_when_tuned():
    """入口 A 边界：display proxy 已 clamp [0,1]，RAW scene-linear
    headroom 恒为 0 ⇒ 即便调大 recovery_per_stop 也 no-op。真实高光回收须
    入口 B 端到端 16-bit（Phase 5）——此用例钉死该边界，防回归误判已实现。"""
    tuned = AutoCalib(recovery_per_stop=30.0)
    out = adapt_auto_params_for_ui(_BASE, _proxy(255), "raw", calib=tuned)
    assert out["highlights"] == _BASE["highlights"]
    assert out == _BASE


def test_for_ui_srgb_threads_calib_and_scales():
    """srgb 源：桥把 calib + source_format 正确透传，感知保守钳在入口 A 即生效
    （不受 display-clamp 限制，区别于 RAW 分支）。"""
    tuned = AutoCalib(jpg_conservative=0.6)
    out = adapt_auto_params_for_ui(_BASE, _proxy(128), "srgb8", calib=tuned)
    assert out["highlights"] == round(-20 * 0.6)
    assert out["shadows"] == round(30 * 0.6)
    assert out["contrast_amt"] == round(12 * 0.6)
    assert out["exposure_amt"] == _BASE["exposure_amt"]   # 非 jpg_keys 不动


# --------------------------------------------------------------------------- #
# 4. 默认 libraw 比色路接线（客观、无 Adobe Color 参考）
# --------------------------------------------------------------------------- #

def _raw_meta(cam_xyz=None):
    """最小 RawDecodeMeta。E1 默认路 neutral/cam_xyz 已不参与（libraw
    已做 WB+camera→XYZ），保留字段仅为构造合法 meta + opt-in 路 resolver 用。"""
    return RawDecodeMeta(
        camera_whitebalance=[1.0, 1.0, 1.0, 1.0],
        daylight_whitebalance=[1.0, 1.0, 1.0, 1.0],
        raw_pattern_shape=(2, 2), black_level=[0, 0, 0, 0], white_level=16383,
        camera_model="Nikon Z 50", postprocess_kwargs={},
        as_shot_neutral=[1.0, 1.0, 1.0], cam_xyz_matrix=cam_xyz,
    )


def test_apply_dcp_layer_defaults_to_libraw_without_optin(monkeypatch):
    """默认：无 OPS_DCP_PROFILE/_DIR opt-in → libraw 比色，
    color_pipeline='libraw'、note=''、dcp_has_tone_curve=False（非降级、非告警）。"""
    monkeypatch.delenv("OPS_DCP_PROFILE", raising=False)
    monkeypatch.delenv("OPS_DCP_DIR", raising=False)
    cam = np.full((4, 4, 3), 0.5, np.float32)
    out, pipeline, note, has_tc = _apply_dcp_layer("x.nef", cam, _raw_meta())
    assert pipeline == "libraw"
    assert note == ""
    assert has_tc is False
    assert out.shape == cam.shape and np.all(np.isfinite(out))


def test_apply_dcp_layer_does_not_walk_resolver_by_default(monkeypatch):
    """冻结铁律：默认路绝不调自动 resolver（即便 dcp.resolve_profile_path 存在）。"""
    monkeypatch.delenv("OPS_DCP_PROFILE", raising=False)
    monkeypatch.delenv("OPS_DCP_DIR", raising=False)
    from src.core import dcp

    def _boom(_model):
        raise AssertionError("默认路不得调用自动 DCP resolver（DCP 已冻结）")

    monkeypatch.setattr(dcp, "resolve_profile_path", _boom)
    out, pipeline, _n, _t = _apply_dcp_layer("x.nef", np.full((2, 2, 3), 0.3, np.float32),
                                             _raw_meta())
    assert pipeline == "libraw"


def _libraw_src(pixels):
    """默认 libraw RAW 路 source（① 的唯一标定/施加范围）。"""
    return RenderSource(
        pixels=np.asarray(pixels, np.float32), alpha=None, source_format="raw",
        source_path="x", is_proxy=False, orig_size=(1, 1),
        color_pipeline="libraw", dcp_has_tone_curve=False,
    )


def _ref_one_look(d, lut, sat):
    """影调曲线与色相饱和度组合的独立参考实现。"""
    rec = np.array([0.2126, 0.7152, 0.0722], np.float32)
    x = np.clip(d, 0.0, 1.0).astype(np.float32)
    xs = np.linspace(0.0, 1.0, len(lut))
    L = x @ rec
    Lp = np.interp(L, xs, lut).astype(np.float32)
    g = (Lp / np.maximum(L, 1e-4))[..., None]
    t = np.clip(x * g, 0.0, 1.0).astype(np.float32)
    r, gg, b = t[..., 0], t[..., 1], t[..., 2]
    mx, mn = t.max(-1), t.min(-1)
    diff = mx - mn
    h = np.zeros_like(mx)
    safe = diff > 1e-8
    rm = safe & (mx == r)
    gm = safe & (mx == gg) & ~rm
    bm = safe & (mx == b) & ~rm & ~gm
    dd = np.where(safe, diff, 1.0)
    h[rm] = (60.0 * ((gg[rm] - b[rm]) / dd[rm]) + 360.0) % 360.0
    h[gm] = (60.0 * ((b[gm] - r[gm]) / dd[gm]) + 120.0)
    h[bm] = (60.0 * ((r[bm] - gg[bm]) / dd[bm]) + 240.0)
    nb = len(sat)
    binw = 360.0 / nb
    centers = (np.arange(nb) + 0.5) * binw
    xp = np.concatenate([[centers[-1] - 360.0], centers, [centers[0] + 360.0]])
    fp = np.concatenate([[sat[-1]], sat, [sat[0]]])
    lt = t @ rec
    hg = np.interp(h, xp, fp).astype(np.float32)[..., None]
    chroma = t - lt[..., None]
    return np.clip(lt[..., None] + hg * chroma, 0.0, 1.0).astype(np.float32)


def test_look_stage_disabled_is_bit_exact():
    """两项强度都为 0 的层不参与，观感逐位早退。"""
    rng = np.random.default_rng(0)
    d = rng.random((8, 8, 3), dtype=np.float32)
    off = replace(OPS_STYLE, tone_strength=0.0, sat_strength=0.0)
    assert not off.active
    assert np.array_equal(_apply_look(d, tuple(s for s in (off,) if s.active)), d)


@pytest.mark.parametrize("pipeline", ["dcp:t", "dcp:none", "unknown"])
def test_look_not_applied_to_dcp_or_other_sources(pipeline):
    """冻结的 DCP opt-in 路：新旧观感都不施加。"""
    for look in ("none", "ops_base", "ops_v2"):
        assert look_stages(look, pipeline) == ()


def test_look_stages_by_source():
    """RAW 先出片底子再走共用风格；已出片的 sRGB 源只走共用风格；旧版单层只服务 RAW。"""
    assert look_stages("ops_v2", "libraw") == (RAW_BASE, OPS_STYLE)
    assert look_stages("ops_v2", "srgb-direct") == (OPS_STYLE,)
    assert look_stages("ops_base", "libraw") == (LEGACY_OPS,)
    assert look_stages("ops_base", "srgb-direct") == ()
    assert look_stages("none", "libraw") == ()


def test_look_names_match_color_contract():
    from src.core.looks import LOOKS
    from src.core.tools.color import COLOR_LOOKS, COLOR_LOOK_ALIASES
    assert LOOKS == set(COLOR_LOOKS) | COLOR_LOOK_ALIASES.keys()


@pytest.mark.parametrize("stage", [LEGACY_OPS, RAW_BASE, OPS_STYLE])
def test_look_stage_matches_fit_harness(stage):
    """每层与 fit_base_look 独立转写逐位一致（= 标定报告验证的同一数学）。"""
    rng = np.random.default_rng(2)
    d = rng.random((24, 24, 3), dtype=np.float32)
    out = _apply_look_stage(d, stage)
    assert not np.array_equal(out, d)
    ref = _ref_one_look(d, np.asarray(stage.tone_lut, np.float32), np.asarray(stage.hue_sat, np.float32))
    np.testing.assert_array_equal(out, ref)


@pytest.mark.parametrize("stage", [LEGACY_OPS, RAW_BASE, OPS_STYLE])
def test_look_stage_monotone_and_anchored(stage):
    """灰阶 ramp：单调非降、过 (0,0)/(1,1)（单调不变量）。"""
    x = np.linspace(0.0, 1.0, 512, dtype=np.float32)
    x = np.repeat(x[None, :, None], 1, 0).repeat(3, 2)
    y = _apply_look_stage(x, stage)[..., 0].ravel()
    assert np.all(np.diff(y) >= -1e-7)
    assert abs(float(y[0])) < 1e-6
    assert abs(float(y[-1]) - 1.0) < 1e-6


@pytest.mark.parametrize("stage", [LEGACY_OPS, RAW_BASE, OPS_STYLE])
def test_look_stage_is_hue_preserving_in_gamut(stage):
    """等增益影调 + 沿 chroma 方向缩放：未触 [0,1] 边界时色相角不变。"""
    rng = np.random.default_rng(3)
    base = rng.uniform(0.10, 0.30, (48, 48, 1)).astype(np.float32)
    chroma = rng.uniform(-0.06, 0.06, (48, 48, 3)).astype(np.float32)
    d = np.clip(base + chroma, 0.03, 0.45).astype(np.float32)
    out = _apply_look_stage(d, stage)
    clipped = np.any((out <= 1e-6) | (out >= 1.0 - 1e-6), axis=-1)
    sat = d.max(-1) - d.min(-1)
    m = (sat > 0.02) & ~clipped
    assert int(m.sum()) > 200
    hi = photo_engine._hue_deg(d)
    ho = photo_engine._hue_deg(out)
    dh = np.abs((ho - hi + 180.0) % 360.0 - 180.0)
    assert float(dh[m].max()) < 0.5


def test_look_stage_independent_tone_and_sat_strengths():
    """影调与饱和强度各自独立。"""
    rng = np.random.default_rng(42)
    d = np.clip(rng.uniform(0.1, 0.8, (16, 16, 3)), 0.0, 1.0).astype(np.float32)
    out_tone_only = _apply_look_stage(d, replace(LEGACY_OPS, sat_strength=0.0))
    assert not np.array_equal(out_tone_only, d)
    out_sat_only = _apply_look_stage(d, replace(LEGACY_OPS, tone_strength=0.0))
    assert not np.array_equal(out_sat_only, d)
    np.testing.assert_allclose(out_sat_only @ photo_engine._REC709_LUMA, d @ photo_engine._REC709_LUMA, atol=1e-5)
