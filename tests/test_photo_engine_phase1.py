"""Phase 1 验收。

覆盖 Tier 1 float 算子：曝光（线性+软肩）、感知域对比 S、Fritsch-Carlson
曲线、保色相鲜艳/饱和；以及「pro 零调整往返 ≤1 LSB」铁律。
"""

import numpy as np
import pytest
from PIL import Image

from src.core.color_adjuster import ColorAdjuster
from src.core.photo_engine import (
    RenderPipeline,
    _gamut_compress,
    apply_chain_v2,
    linear_to_srgb,
    srgb_to_linear,
    _src_from_pil,
)
from src.core.tools.color import is_color_neutral


def _img(seed=7):
    a = np.random.default_rng(seed).integers(0, 256, (40, 60, 3), np.uint8)
    return Image.fromarray(a, "RGB")


def _disp(src):  # RenderSource(linear) → display [0,1] float
    return np.clip(linear_to_srgb(src.pixels), 0, 1)


def _render(img, params):
    return RenderPipeline().render(_src_from_pil(img), params)


# --------------------------------------------------------------- 零调整往返铁律
def test_neutral_params_roundtrip_le_1_lsb():
    img = _img()
    neutral = {"exposure": 0, "contrast": 1.0, "saturation": 1.0, "vibrance": 0, "curves": None}
    out = apply_chain_v2(img, neutral)
    assert np.abs(np.asarray(img, np.int16) - np.asarray(out, np.int16)).max() <= 1


def test_is_neutral_predicate():
    assert is_color_neutral({})
    assert is_color_neutral({"exposure": 0, "contrast": 1.0, "saturation": 1.0, "vibrance": 0, "curves": None})
    assert not is_color_neutral({"exposure": 0.5})
    assert not is_color_neutral({"curves": {"rgb": [(0, 0), (128, 140), (255, 255)]}})


def test_pro_engine_via_apply_chain_neutral_is_passthrough(monkeypatch):
    monkeypatch.setenv("OPS_ENGINE", "pro")
    img = _img()
    out = ColorAdjuster().apply_chain(img, {"contrast": 1.0, "exposure": 0})
    assert np.abs(np.asarray(img, np.int16) - np.asarray(out, np.int16)).max() <= 1


# ----------------------------------------------------------------- 曝光
def test_exposure_monotone_brightens_without_hard_clip():
    img = _img()
    base = _disp(_render(img, {}))
    up = _disp(_render(img, {"exposure": 1.0}))
    down = _disp(_render(img, {"exposure": -1.0}))
    assert up.mean() > base.mean() > down.mean()
    # filmic 软肩：高光被抬升但不应大面积烧成纯 1.0（softer than hard clip）
    assert (up >= 0.999).mean() < 0.5


def test_exposure_preserves_pure_black():
    blk = Image.fromarray(np.zeros((8, 8, 3), np.uint8), "RGB")
    out = _disp(_render(blk, {"exposure": 2.0}))
    assert out.max() < 1e-3  # 0 × gain = 0


# ----------------------------------------------------------------- 对比
def test_contrast_sigmoid_keeps_endpoints_and_increases_spread():
    grad = np.tile(np.linspace(0, 255, 64, dtype=np.uint8), (8, 1))
    img = Image.fromarray(np.stack([grad] * 3, -1), "RGB")
    base = _disp(_render(img, {}))
    hi = _disp(_render(img, {"contrast": 1.6}))
    assert hi.std() > base.std()                       # 对比增强
    assert hi.min() < 0.02 and hi.max() > 0.98         # 端点近 0/1，无硬裁丢端
    row = hi[0, :, 0]                                   # 沿渐变轴（单行）
    assert np.all(np.diff(row) >= -1e-4)               # 单调不减


# ----------------------------------------------------------------- 曲线
def test_curve_identity_is_noop():
    img = _img()
    out = _render(img, {"curves": {"rgb": [(0, 0), (255, 255)]}})
    assert is_color_neutral({"curves": {"rgb": [(0, 0), (255, 255)]}}) is False  # 非空 dict 仍进管线
    a = np.asarray(apply_chain_v2(img, {"curves": {"rgb": [(0, 0), (255, 255)]}}), np.int16)
    assert np.abs(a - np.asarray(img, np.int16)).max() <= 1


def test_curve_monotone_lift():
    img = _img()
    lift = {"rgb": [(0, 0), (128, 170), (255, 255)]}
    out = _disp(_render(img, {"curves": lift}))
    base = _disp(_render(img, {}))
    assert out.mean() > base.mean()                    # 提亮曲线整体变亮


# --------------------------------------------------------- 饱和/鲜艳 保色相
def _hue(rgb):  # 粗略色相角，用于验证 hue-safe
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return np.arctan2(np.sqrt(3) * (g - b), 2 * r - g - b)


def test_saturation_zero_is_grayscale():
    img = _img()
    out = _disp(_render(img, {"saturation": 0.0}))
    assert np.abs(out - out.mean(axis=-1, keepdims=True)).max() < 0.02


def test_saturation_preserves_hue_and_luma():
    img = _img()
    base = _disp(_render(img, {}))
    sat = _disp(_render(img, {"saturation": 1.3}))
    # 等增益缩放本身保色相/保亮度；display 出口 per-channel clip 会破坏之，
    # 故只校验未被 [0,1] 截断的像素（gamut 内）。
    inb = (base > 0.02).all(-1) & (base < 0.98).all(-1) & (sat > 0.02).all(-1) & (sat < 0.98).all(-1)
    color = base.std(axis=-1) > 0.05
    mask = inb & color
    assert mask.sum() > 50
    dh = np.abs(_hue(sat) - _hue(base))[mask]
    dh = np.minimum(dh, 2 * np.pi - dh)
    assert np.nanmedian(dh) < 0.05                     # 色相基本不动
    luma = lambda a: a @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    np.testing.assert_allclose(luma(sat)[mask], luma(base)[mask], atol=2e-3)  # 等增益→保亮度


def test_gamut_compress_preserves_hue_and_luma():
    # 构造出 gamut 的像素：chroma 推某通道 < 0
    d = np.array([[0.6, 0.8, -0.1],     # B<0 out of gamut
                  [0.1, 0.7, 0.3],       # in gamut
                  [1.2, 0.4, 0.5]],      # R>1 out of gamut
                 dtype=np.float32).reshape(1, 3, 3)
    out = _gamut_compress(d)
    assert out.min() >= -1e-6 and out.max() <= 1.0 + 1e-6   # 全部进 gamut
    w = np.array([0.2126, 0.7152, 0.0722], np.float32)
    np.testing.assert_allclose(out @ w, d @ w, atol=1e-5)    # luma 不变
    # hue 方向不变：chroma 向量方向一致
    lum_o = (out @ w)[..., None]; lum_d = (d @ w)[..., None]
    ch_o = out - lum_o; ch_d = d - lum_d
    for i in range(3):
        mag_o = np.linalg.norm(ch_o[0, i]); mag_d = np.linalg.norm(ch_d[0, i])
        if mag_o > 1e-6 and mag_d > 1e-6:
            cos = np.dot(ch_o[0, i], ch_d[0, i]) / (mag_o * mag_d)
            assert cos > 0.999, f"pixel {i} hue direction changed"


def test_gamut_compress_no_change_in_gamut():
    d = np.array([[[0.2, 0.5, 0.8]]], dtype=np.float32)
    np.testing.assert_allclose(_gamut_compress(d), d, atol=1e-7)


def test_extreme_vibrance_no_rainbow_hue_shift():
    # 模拟绿色植被 — 高 vibrance 不应变黄/青
    a = np.zeros((16, 16, 3), np.uint8)
    a[:] = (80, 160, 60)  # 绿色
    img = Image.fromarray(a, "RGB")
    out = _disp(_render(img, {"vibrance": 100}))
    h = _hue(out)
    h_orig = _hue(_disp(_render(img, {})))
    dh = np.abs(h - h_orig)
    dh = np.minimum(dh, 2 * np.pi - dh)
    assert dh.max() < 0.15, f"max hue shift {dh.max():.3f} rad — rainbow regression"


def test_vibrance_does_not_amplify_near_neutral_chroma_noise():
    # 模拟平坦天空：近中性 + 每通道独立噪声（彩虹麻点的来源）
    rng = np.random.default_rng(11)
    base = np.full((48, 48, 3), 200, np.float32)
    base += rng.normal(0, 3.0, base.shape).astype(np.float32)   # chroma 噪声
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")

    b = _disp(_render(img, {}))
    v = _disp(_render(img, {"vibrance": 60}))
    chroma = lambda x: x - x.mean(-1, keepdims=True)
    noise_in = chroma(b).std()
    noise_out = chroma(v).std()
    # 软死区命中：近中性噪声放大幅度必须很小（修正前约 1.36x → 彩虹）
    assert noise_out / (noise_in + 1e-9) < 1.08


def test_vibrance_still_boosts_genuine_low_mid_saturation():
    a = np.zeros((16, 16, 3), np.uint8)
    a[:] = (190, 120, 130)   # display 饱和 ~0.27，软死区之上
    img = Image.fromarray(a, "RGB")
    b = _disp(_render(img, {}))
    v = _disp(_render(img, {"vibrance": 60}))
    chroma = lambda x: (x.max(-1) - x.min(-1)).mean()
    assert chroma(v) / (chroma(b) + 1e-9) > 1.10   # 真实低-中饱和仍明显提升


def test_vibrance_boosts_low_saturation_more():
    # 左半低饱和、右半高饱和；正鲜艳应让低饱和提升比例更大
    a = np.zeros((16, 16, 3), np.uint8)
    a[:, :8] = (140, 120, 110)   # 低饱和
    a[:, 8:] = (200, 40, 40)     # 高饱和
    img = Image.fromarray(a, "RGB")
    base = _disp(_render(img, {}))
    vib = _disp(_render(img, {"vibrance": 60}))
    chroma = lambda x: x.max(-1) - x.min(-1)
    lo_gain = (chroma(vib)[:, :8].mean() + 1e-6) / (chroma(base)[:, :8].mean() + 1e-6)
    hi_gain = (chroma(vib)[:, 8:].mean() + 1e-6) / (chroma(base)[:, 8:].mean() + 1e-6)
    assert lo_gain > hi_gain


# ----------------------------------------------------------------- 白平衡 (色温/色调)
def test_white_balance_cool_temperature_boosts_blue_and_drops_red():
    """负色温（如 -100 对应 2750K）：B 通道显著增强，R 通道显著衰减。"""
    gray = Image.fromarray(np.full((16, 16, 3), 128, dtype=np.uint8), "RGB")
    out = _disp(_render(gray, {"temperature": -100}))
    # 灰底在 -100 色温下应该明显呈现冷蓝：B > G > R
    r_mean = out[:, :, 0].mean()
    g_mean = out[:, :, 1].mean()
    b_mean = out[:, :, 2].mean()
    assert b_mean > g_mean > r_mean
    assert b_mean > 0.55 and r_mean < 0.45


def test_white_balance_warm_temperature_boosts_red_and_drops_blue():
    """正色温（如 +100 对应暖调）：R 通道显著增强，B 通道显著衰减。"""
    gray = Image.fromarray(np.full((16, 16, 3), 128, dtype=np.uint8), "RGB")
    out = _disp(_render(gray, {"temperature": 100}))
    r_mean = out[:, :, 0].mean()
    g_mean = out[:, :, 1].mean()
    b_mean = out[:, :, 2].mean()
    assert r_mean > g_mean > b_mean


def test_white_balance_tint_modulates_green():
    """色调调节：正 tint 偏洋红（G 下降），负 tint 偏绿（G 上升）。"""
    gray = Image.fromarray(np.full((16, 16, 3), 128, dtype=np.uint8), "RGB")
    magenta = _disp(_render(gray, {"tint": 100}))
    green = _disp(_render(gray, {"tint": -100}))
    assert magenta[:, :, 1].mean() < green[:, :, 1].mean()

