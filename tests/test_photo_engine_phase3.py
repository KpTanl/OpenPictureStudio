"""Phase 3 验收（局部 tone 与细节算子，以及调色不变量）。

覆盖 Tier 2 局部 tone：
  - 局部高光/阴影/白/黑：边缘感知 base/detail、点控件全局 L、保色相等增益。
  - texture/clarity/dehaze：复用多尺度 guided-filter 金字塔（细/中/粗）。
  - 调色不变量逐条回归：milky-haze 黑场锚定、虫纹保色相、不发灰
    （detail 加回保微对比）、单调连续、点控件设定黑白点。
"""

import numpy as np
import pytest
from PIL import Image
from dataclasses import replace

import src.core.photo_engine as photo_engine
from src.core.photo_engine import (
    PRO_CALIB,
    RenderPipeline,
    _detail_effects,
    _local_tone,
    _localtone_adaptive_pivot,
    _REC709_LUMA,
    _ZONE_PEAK,
    _zone_offset,
    apply_chain_v2,
    fast_guided_filter,
    linear_to_srgb,
    srgb_to_linear,
    _src_from_pil,
)
from src.core.tools.color import is_color_neutral


def _img(seed=7):
    a = np.random.default_rng(seed).integers(0, 256, (48, 64, 3), np.uint8)
    return Image.fromarray(a, "RGB")


def _disp(src):
    return np.clip(linear_to_srgb(src.pixels), 0, 1)


def _render(img, params):
    return RenderPipeline().render(_src_from_pil(img), params)


def _luma(d):
    return d @ np.asarray(_REC709_LUMA, np.float32)


def _hue(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return np.arctan2(np.sqrt(3) * (g - b), 2 * r - g - b)


def _gray_ramp(n=256, h=8):
    """flat-color 灰阶 ramp（chroma=0 → 隔离亮度变换，等增益平凡）。"""
    row = np.linspace(0.0, 1.0, n, dtype=np.float32)
    return np.repeat(row[None, :, None], h, 0).repeat(3, 2).copy()


def _gray_band(lo=0.0, hi=1.0, n=256, h=32):
    row = np.linspace(lo, hi, n, dtype=np.float32)
    return np.repeat(row[None, :, None], h, 0).repeat(3, 2).copy()


def _band_amp(x, r, s=2):
    """半径 r 以下的局部对比能量。用大 eps 的 guided filter 当**真实低通**
    （eps≫局部方差 → a≈0 → ≈box-mean），度量与结构无关、不依赖被测算子。
    """
    L = _luma(x) if x.ndim == 3 else x
    lo = fast_guided_filter(L, L, r, 0.30 ** 2, s)
    return float((L - lo).std())


def _enable_adaptive(monkeypatch, **kwargs):
    calib = replace(PRO_CALIB, localtone_adaptive=True, **kwargs)
    monkeypatch.setattr(photo_engine, "PRO_CALIB", calib)
    return calib


# --------------------------------------------------------- 铁律：新参数不破坏中性
def test_is_neutral_includes_phase3_keys():
    assert is_color_neutral({"shadows": 0, "highlights": 0, "whites": 0, "blacks": 0,
                        "texture": 0, "clarity": 0, "dehaze": 0})
    assert not is_color_neutral({"shadows": 30})
    assert not is_color_neutral({"dehaze": -10})


def test_phase3_neutral_roundtrip_le_1_lsb():
    img = _img()
    p = {"shadows": 0, "highlights": 0, "whites": 0, "blacks": 0,
         "texture": 0, "clarity": 0, "dehaze": 0}
    out = apply_chain_v2(img, p)
    assert np.abs(np.asarray(img, np.int16) - np.asarray(out, np.int16)).max() <= 1


def test_local_tone_and_detail_identity_at_zero():
    d = np.clip(np.asarray(_img(), np.float32) / 255.0, 0, 1).astype(np.float32)
    np.testing.assert_array_equal(_local_tone(d, {}), d)
    np.testing.assert_array_equal(_detail_effects(d, {}), d)
    np.testing.assert_array_equal(_local_tone(d, {"shadows": 0, "blacks": 0}), d)
    np.testing.assert_array_equal(_detail_effects(d, {"texture": 0, "dehaze": 0}), d)


# --------------------------------------------------------- milky-haze：阴影+ 不抬绝对黑场
def test_shadows_lift_anchors_absolute_black():
    blk = np.zeros((32, 32, 3), np.float32)
    out = _local_tone(blk, {"shadows": 100})
    # 旧 bug：阴影+ 把黑场推到 +0.176；正确实现 sin(π·0)=0 → 黑场原地不动。
    assert out.max() < 2.0 / 255.0, f"absolute black lifted to {out.max():.4f} (milky-haze regression)"


def test_shadows_lift_interior_but_keeps_black_floor_on_ramp():
    d = _gray_ramp()
    out = _local_tone(d, {"shadows": 100})
    Lin, Lout = _luma(d)[0], _luma(out)[0]
    # 左墙锚定：最暗端几乎不动（直方图不离开左墙）。
    assert Lout[:3].max() < 4.0 / 255.0
    # 区间内部（阴影带中段）确被提亮。
    mid = (Lin > 0.12) & (Lin < 0.45)
    assert (Lout[mid] - Lin[mid]).mean() > 0.04


def test_blacks_is_endpoint_setter_unlike_shadows():
    """点控件 blacks 设定黑场（可移动 L=0），区间 shadows 不动黑场——点控件与区间控件的核心区分。"""
    d = _gray_ramp()
    L0 = _luma(d)[0, 0]
    sh = _luma(_local_tone(d, {"shadows": 100}))[0, 0]
    bk_up = _luma(_local_tone(d, {"blacks": 100}))[0, 0]
    bk_dn = _luma(_local_tone(d, {"blacks": -100}))[0, 0]
    assert abs(sh - L0) < 4.0 / 255.0          # shadows 不设定黑场
    assert bk_up - L0 > 0.05                     # blacks+ 抬黑场（设定）
    assert bk_dn <= L0 + 1e-6                     # blacks- 压黑场（设定，下压/不抬）


def test_highlights_region_anchors_white():
    d = _gray_ramp()
    out = _local_tone(d, {"highlights": -100})
    Lin, Lout = _luma(d)[0], _luma(out)[0]
    assert Lout[-3:].min() > 1.0 - 4.0 / 255.0      # 白点锚定（Hann 在 L=1 →0）
    hi = (Lin > 0.55) & (Lin < 0.9)
    assert (Lin[hi] - Lout[hi]).mean() > 0.04        # 内部高光被回收（压低）


def test_whites_is_endpoint_setter():
    d = _gray_ramp()
    L0 = _luma(d)[0, -1]
    wh_up = _luma(_local_tone(d, {"whites": 100}))[0, -1]
    wh_dn = _luma(_local_tone(d, {"whites": -100}))[0, -1]
    assert wh_up >= L0 - 1e-6                          # whites+ 推白点（L=1 已饱和则≈不变）
    assert L0 - wh_dn > 0.05                            # whites- 拉低白点（设定）


# --------------------------------------------------------- 虫纹：暗部提亮保色相
def test_shadow_black_lift_is_hue_safe_no_worm_artifact():
    rng = np.random.default_rng(3)
    # 暗-中调彩色块（多色相），亮度在乘法保色相区（L > 膝点）。
    base = rng.uniform(0.10, 0.45, (40, 40, 3)).astype(np.float32)
    out = _local_tone(base, {"shadows": 80, "blacks": 60})
    color = (base.max(-1) - base.min(-1)) > 0.05
    dh = np.abs(_hue(out) - _hue(base))[color]
    dh = np.minimum(dh, 2 * np.pi - dh)
    assert np.nanmedian(dh) < 0.03
    assert np.nanmax(dh) < 0.12, f"max hue shift {np.nanmax(dh):.3f} rad — 虫纹回归"


# --------------------------------------------------------- 不发灰：detail 加回保微对比
def test_shadow_lift_preserves_microcontrast_not_flattened():
    """阴影提亮保留原有微对比（detail 原样加回）。本图最暗约为 0.015，
    区间 shadows 提亮暗部细节；绝对黑锚定由
    test_shadows_lift_anchors_absolute_black / *_on_ramp 覆盖。
    """
    rng = np.random.default_rng(5)
    h, w = 96, 96
    yy = np.linspace(0.04, 0.30, h, dtype=np.float32)[:, None]      # 暗部平滑基底
    fine = (0.025 * np.sin(np.arange(w) * 0.9)).astype(np.float32)  # 高频微对比（周期≈7px）
    L = np.clip(yy + fine[None, :], 0, 1)
    d = np.repeat(L[:, :, None], 3, 2).copy()

    out = _local_tone(d, {"shadows": 90})
    Lin, Lout = _luma(d), _luma(out)
    assert Lout.mean() > Lin.mean() + 0.03                          # 阴影确被提亮
    # 不发灰：detail 原样加回 → 高频微对比基本保留（非全局抬平）。
    assert _band_amp(Lout, 4) > 0.85 * _band_amp(Lin, 4), "micro-contrast collapsed (发灰)"


# --------------------------------------------------------- 单调连续（无回折/断崖）
@pytest.mark.parametrize("ctrl,amt", [
    ("shadows", 60), ("shadows", -60), ("highlights", 60), ("highlights", -60),
    ("blacks", 60), ("blacks", -60), ("whites", 60), ("whites", -60),
])
def test_single_control_is_monotone_on_ramp(ctrl, amt):
    d = _gray_ramp(n=512)
    L = _luma(_local_tone(d, {ctrl: amt}))[0]
    # 单调不减；smoothstep 限斜率，容差对齐 classic 非严格整数行为。
    assert np.min(np.diff(L)) > -2.0 / 255.0, f"{ctrl}={amt} 回折 min Δ={np.min(np.diff(L)):.5f}"


@pytest.mark.parametrize("ctrl", ["shadows", "highlights", "blacks", "whites"])
def test_single_control_is_continuous_no_cliff(ctrl):
    d = _gray_ramp(n=512)
    L = _luma(_local_tone(d, {ctrl: 100}))[0]
    assert np.max(np.abs(np.diff(L))) < 0.02, f"{ctrl} 断崖 max|Δ|={np.max(np.abs(np.diff(L))):.4f}"


# --------------------------------------------------------- Task A：local tone 分布自适应 pivot
def test_adaptive_pivot_disabled_returns_fixed_anchor():
    base = np.linspace(0.0, 0.3, 1024, dtype=np.float32).reshape(32, 32)
    assert _localtone_adaptive_pivot(base, PRO_CALIB) == np.float32(128.0 / 255.0)


def test_adaptive_pivot_falls_back_on_flat_histogram(monkeypatch):
    _enable_adaptive(monkeypatch)
    base = np.full((32, 32), 0.3, np.float32)
    assert _localtone_adaptive_pivot(base, photo_engine.PRO_CALIB) == np.float32(128.0 / 255.0)


def test_adaptive_pivot_moves_with_low_and_high_key_scenes(monkeypatch):
    calib = _enable_adaptive(monkeypatch)
    fixed = np.float32(128.0 / 255.0)
    low = np.linspace(0.0, 0.35, 4096, dtype=np.float32).reshape(64, 64)
    high = np.linspace(0.65, 1.0, 4096, dtype=np.float32).reshape(64, 64)
    z_low = _localtone_adaptive_pivot(low, calib)
    z_high = _localtone_adaptive_pivot(high, calib)
    assert z_low < fixed - 0.08
    assert z_high > fixed + 0.08
    assert abs(float(z_low - fixed)) <= calib.localtone_adapt_radius + 1e-6
    assert abs(float(z_high - fixed)) <= calib.localtone_adapt_radius + 1e-6


def test_adaptive_pivot_respects_span_min_on_random_inputs(monkeypatch):
    calib = _enable_adaptive(monkeypatch)
    rng = np.random.default_rng(31)
    for _ in range(20):
        base = rng.random((37, 41), dtype=np.float32)
        z = _localtone_adaptive_pivot(base, calib)
        assert z >= calib.localtone_span_min
        assert 1.0 - z >= calib.localtone_span_min


def test_adaptive_pivot_drift_is_limited_to_quantized_steps(monkeypatch):
    calib = _enable_adaptive(monkeypatch)
    base = np.linspace(0.15, 0.85, 4096, dtype=np.float32).reshape(64, 64)
    shifted = np.clip(base + (1.0 / 255.0), 0.0, 1.0)
    z0 = _localtone_adaptive_pivot(base, calib)
    z1 = _localtone_adaptive_pivot(shifted, calib)
    assert abs(float(z1 - z0)) <= 2.0 / 255.0


def test_zone_offset_peak_strength_is_independent_of_adaptive_span():
    amount = 50
    expected = np.float32(amount / 100.0) * _ZONE_PEAK
    for pivot in (np.float32(0.32), np.float32(0.50), np.float32(0.68)):
        L = np.array([[0.0, pivot / 2.0, pivot]], dtype=np.float32)
        off = _zone_offset(L, np.float32(0.0), pivot, amount,
                           point_control=False, bright_anchored=False)
        assert np.isclose(off.max(), expected, atol=1e-6)


def test_adaptive_shadows_effect_centroid_moves_with_scene(monkeypatch):
    _enable_adaptive(monkeypatch)
    low = _gray_band(0.0, 0.35, n=512, h=48)
    high = _gray_band(0.65, 1.0, n=512, h=48)

    def centroid(d):
        Lin = _luma(d)
        Lout = _luma(_local_tone(d, {"shadows": 50}))
        delta = np.maximum(Lout - Lin, 0.0)
        return float((Lin * delta).sum() / max(float(delta.sum()), 1e-8))

    assert centroid(high) - centroid(low) > 0.20


def test_adaptive_flat_scene_matches_fixed_zone_output(monkeypatch):
    d = np.full((40, 48, 3), 0.3, np.float32)
    fixed = _local_tone(d, {"shadows": 60, "highlights": -40})
    _enable_adaptive(monkeypatch)
    adaptive = _local_tone(d, {"shadows": 60, "highlights": -40})
    np.testing.assert_array_equal(adaptive, fixed)


# --------------------------------------------------------- texture/clarity/dehaze
def test_texture_boosts_then_softens_fine_detail():
    # 结构化细纹（周期≈4px），落在 texture 细节带内。
    w = 128
    fine = (0.05 * np.sin(np.arange(w, dtype=np.float32) * (np.pi / 2))).astype(np.float32)
    L = np.clip(0.5 + fine[None, :], 0.05, 0.95).repeat(96, 0)
    d = np.repeat(L[:, :, None], 3, 2).copy()
    a0 = _band_amp(d, 3)
    assert _band_amp(_detail_effects(d, {"texture": 90}), 3) > 1.20 * a0
    assert _band_amp(_detail_effects(d, {"texture": -90}), 3) < 0.85 * a0


def test_clarity_increases_mid_local_contrast():
    # 中尺度特征（周期≈12px），落在 r_fine~r_mid 带内（用 192px 让相对半径有意义）。
    h = w = 192
    mid = 0.5 + 0.10 * np.sin(np.linspace(0, w / 6.0 * np.pi, w, dtype=np.float32))
    L = np.clip(mid[None, :].repeat(h, 0), 0.05, 0.95).astype(np.float32)
    d = np.repeat(L[:, :, None], 3, 2).copy()
    r_m = max(4, w // 24)
    assert _band_amp(_detail_effects(d, {"clarity": 90}), r_m) > 1.10 * _band_amp(d, r_m)


def test_dehaze_raises_contrast_and_saturation_hue_safe():
    rng = np.random.default_rng(17)
    h, w = 80, 80
    # 低对比偏灰的彩色图（朦胧）。
    d = (0.45 + 0.06 * rng.standard_normal((h, w, 3))).astype(np.float32)
    d += np.array([0.05, 0.0, -0.04], np.float32)          # 轻微色偏 → 有色相
    d = np.clip(d, 0.02, 0.98)
    out = _detail_effects(d, {"dehaze": 80})
    assert _luma(out).std() > _luma(d).std()                # 大尺度对比上升
    sat = lambda x: (x.max(-1) - x.min(-1)).mean()
    assert sat(out) > sat(d)                                 # 伴随增饱和（classic 同款）
    color = (d.max(-1) - d.min(-1)) > 0.03
    dh = np.abs(_hue(out) - _hue(d))[color]
    dh = np.minimum(dh, 2 * np.pi - dh)
    assert np.nanmedian(dh) < 0.05                            # 增饱和沿 chroma 方向 → 仍保色相


def test_dehaze_negative_adds_haze_flattens():
    rng = np.random.default_rng(19)
    L = np.clip(0.5 + 0.2 * np.sin(np.linspace(0, 4 * np.pi, 80, np.float32))
                + rng.normal(0, 0.01, (80, 80)), 0.05, 0.95).astype(np.float32)
    d = np.repeat(L[:, :, None], 3, 2).copy()
    out = _detail_effects(d, {"dehaze": -80})
    assert _luma(out).std() < _luma(d).std()                 # 负去朦胧 → 加雾压对比


# --------------------------------------------------------- 端到端：pro 全 Phase-3 链路可跑
def test_full_pipeline_phase3_params_runs_and_shapes_ok():
    img = _img()
    out = apply_chain_v2(img, {
        "exposure": 0.3, "shadows": 40, "highlights": -30, "blacks": -15,
        "whites": 20, "texture": 25, "clarity": 30, "dehaze": 20,
        "contrast": 1.1, "saturation": 1.05, "vibrance": 10,
    })
    assert out.size == img.size and out.mode == "RGB"
    a = np.asarray(out)
    assert a.min() >= 0 and a.max() <= 255 and np.isfinite(a).all()
