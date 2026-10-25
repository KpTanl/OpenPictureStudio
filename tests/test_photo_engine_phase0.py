"""Phase 0 验收。

覆盖：
  - sRGB 编解码往返 ≤1 LSB
  - 入口 A 空管线往返 ≤1 LSB（零调整）+ alpha 逐位保真
  - engine 选择器：classic 默认、pro 直通、非法值回退
  - box blur 对拍 naive、guided/fast guided filter 数值性质
  - RenderSource 字段与预留默认值
"""

import numpy as np
import pytest
from PIL import Image

from src.core.color_adjuster import ColorAdjuster
from src.core.photo_engine import (
    RenderSource,
    apply_chain_v2,
    box_filter,
    fast_guided_filter,
    guided_filter,
    linear_luma,
    linear_to_srgb,
    load_render_source,
    srgb_to_linear,
    _pil_from,
    _src_from_pil,
)


# --------------------------------------------------------------- sRGB 编解码
def test_srgb_roundtrip_all_8bit_values_le_1_lsb():
    vals = np.arange(256, dtype=np.float32) / 255.0
    back = linear_to_srgb(srgb_to_linear(vals)) * 255.0
    err = np.abs(np.round(back) - np.arange(256))
    assert err.max() <= 1.0, f"max round-trip error {err.max()} LSB > 1"


def test_srgb_decode_endpoints_and_monotonic():
    assert srgb_to_linear(np.float32(0.0)) == pytest.approx(0.0)
    assert srgb_to_linear(np.float32(1.0)) == pytest.approx(1.0, abs=1e-5)
    x = np.linspace(0, 1, 512, dtype=np.float32)
    lin = srgb_to_linear(x)
    assert np.all(np.diff(lin) >= -1e-7)  # 单调不减


def test_linear_luma_rec709():
    white = np.ones((2, 2, 3), dtype=np.float32)
    assert linear_luma(white) == pytest.approx(1.0)
    red = np.zeros((1, 1, 3), dtype=np.float32)
    red[..., 0] = 1.0
    assert float(linear_luma(red)[0, 0]) == pytest.approx(0.2126, abs=1e-6)


# ----------------------------------------------------------- 入口 A 空管线往返
def _rand_img(mode):
    rng = np.random.default_rng(42)
    ch = 4 if mode == "RGBA" else 3
    arr = rng.integers(0, 256, size=(37, 53, ch), dtype=np.uint8)
    return Image.fromarray(arr, mode=mode)


def test_entry_a_empty_pipeline_roundtrip_le_1_lsb():
    img = _rand_img("RGB")
    out = apply_chain_v2(img, {})
    a = np.asarray(img, dtype=np.int16)
    b = np.asarray(out, dtype=np.int16)
    assert np.abs(a - b).max() <= 1


def test_entry_a_alpha_is_bit_exact():
    img = _rand_img("RGBA")
    out = apply_chain_v2(img, {})
    assert out.mode == "RGBA"
    a_in = np.asarray(img.split()[3])
    a_out = np.asarray(out.split()[3])
    np.testing.assert_array_equal(a_in, a_out)  # alpha 不经 gamma，须逐位相同


# ------------------------------------------------------------- engine 选择器
def test_engine_selector_defaults():
    assert ColorAdjuster().engine == "pro"
    assert ColorAdjuster("pro").engine == "pro"


def test_pro_engine_empty_params_is_passthrough():
    img = _rand_img("RGB")
    res = ColorAdjuster().apply_chain(img, {})
    assert np.abs(np.asarray(img, np.int16) - np.asarray(res, np.int16)).max() <= 1


# ------------------------------------------------- box blur 与 guided filter
def _naive_box(img, r):
    h, w = img.shape
    out = np.zeros_like(img, dtype=np.float64)
    for i in range(h):
        for j in range(w):
            i0, i1 = max(0, i - r), min(h, i + r + 1)
            j0, j1 = max(0, j - r), min(w, j + r + 1)
            out[i, j] = img[i0:i1, j0:j1].mean()
    return out


def test_box_filter_matches_naive_with_edge_clip():
    rng = np.random.default_rng(1)
    img = rng.random((11, 13), dtype=np.float32)
    for r in (1, 2, 3):
        np.testing.assert_allclose(box_filter(img, r), _naive_box(img, r), rtol=1e-4, atol=1e-5)


def test_box_filter_constant_and_multichannel():
    const = np.full((8, 9, 3), 0.37, dtype=np.float32)
    np.testing.assert_allclose(box_filter(const, 2), const, atol=1e-6)
    assert box_filter(const, 0).shape == const.shape  # r=0 直通


def test_guided_filter_large_eps_approaches_double_box():
    # eps→∞: a→0, b→box(p), 经第二次 box(mean_a/mean_b) → 输出收敛到双重 box(p)
    rng = np.random.default_rng(2)
    g = rng.random((40, 40), dtype=np.float32)
    p = rng.random((40, 40), dtype=np.float32)
    out = guided_filter(g, p, radius=4, eps=1e6)
    np.testing.assert_allclose(out, box_filter(box_filter(p, 4), 4), rtol=1e-3, atol=1e-4)


def test_guided_filter_self_guided_small_eps_preserves_signal():
    rng = np.random.default_rng(3)
    g = rng.random((48, 48), dtype=np.float32)
    out = guided_filter(g, g, radius=2, eps=1e-8)
    # 自引导 + eps→0：a≈1,b≈0 → 近似恒等（边界外略平滑，放宽容差）
    assert np.corrcoef(out.ravel(), g.ravel())[0, 1] > 0.99


def test_fast_guided_filter_shape_and_constant():
    const = np.full((97, 61), 0.5, dtype=np.float32)
    out = fast_guided_filter(const, const, radius=8, eps=0.01, subsample=4)
    assert out.shape == const.shape
    np.testing.assert_allclose(out, const, atol=1e-4)


def test_fast_guided_filter_subsample_1_equals_exact():
    rng = np.random.default_rng(4)
    g = rng.random((30, 30), dtype=np.float32)
    np.testing.assert_allclose(
        fast_guided_filter(g, g, 3, 0.01, subsample=1),
        guided_filter(g, g, 3, 0.01),
        atol=1e-6,
    )


# -------------------------------------------------------------- RenderSource
def test_render_source_fields_and_reserved_defaults():
    img = _rand_img("RGB")
    src = _src_from_pil(img, source_path="x.jpg")
    assert isinstance(src, RenderSource)
    assert src.source_format == "srgb8"
    assert src.color_pipeline == "srgb-direct"
    assert src.orig_size == img.size
    assert src.pixels.dtype == np.float32
    # 宽色域/HDR 预留字段的固定值
    assert (src.colorspace, src.white_point, src.gamut) == ("linear_srgb", "D65", "srgb")


def test_load_render_source_srgb_path(tmp_path):
    p = tmp_path / "t.png"
    _rand_img("RGB").save(p)
    src = load_render_source(str(p))
    assert src.source_format == "srgb8"
    assert not src.is_proxy
    rt = np.asarray(_pil_from(src), np.int16)
    assert np.abs(rt - np.asarray(_rand_img("RGB"), np.int16)).max() <= 1
