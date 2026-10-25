"""引擎选择与入口 A 集成验收。

覆盖 load_render_pil 的引擎分支、apply_chain_v2 的 meta 透传与 base
curve 门控、A/B 安全（classic 逐位不变、meta 对 classic 无副作用）。

RAW 经 DCP 的真路径由 test_dcp_color_science（无 RAW 时 skip）覆盖；此处用
非 RAW 图隔离验证引擎选择/解码契约与门控，不依赖 rawpy。
"""

import numpy as np
import pytest
from dataclasses import replace
from PIL import Image

from src.core.color_adjuster import ColorAdjuster
from src.core.looks import look_stages
from src.core.raw_loader import load_image
from src.core.photo_engine import (
    RenderMeta,
    _src_from_pil,
    apply_chain_v2,
    load_render_pil,
)


@pytest.fixture
def png_path(tmp_path):
    p = tmp_path / "sample.png"
    a = np.random.default_rng(7).integers(0, 256, (40, 60, 3), np.uint8)
    Image.fromarray(a, "RGB").save(p)
    return str(p)


# --------------------------------------------------- load_render_pil 引擎
def test_load_render_pil_nonraw_srgb_direct(png_path):
    """非 RAW 图片加载：不经 DCP，等同 load_image 并附带 srgb-direct 渲染元数据。"""
    img, meta = load_render_pil(png_path)
    ref = load_image(png_path)
    assert np.array_equal(np.asarray(img), np.asarray(ref))
    assert meta.color_pipeline == "srgb-direct"
    assert meta.source_format == "srgb8"
    assert meta.note == "" and meta.dcp_has_tone_curve is False


# --------------------------------------------------- base curve 门控
def test_meta_gates_base_curve_for_raw_dcp_tonecurve(png_path):
    """RAW 经 DCP → 不施加 OPS 观感（防 double tone map）；默认 libraw /
    srgb-direct → 施加。门控由 meta 透传的 color_pipeline 驱动。"""
    base = _src_from_pil(Image.open(png_path).convert("RGB"))

    dcp_tc = replace(base, color_pipeline="dcp:Camera Standard",
                     dcp_has_tone_curve=True, source_format="raw")
    assert look_stages("ops_v2", dcp_tc.color_pipeline) == ()

    libraw = replace(base, color_pipeline="libraw", source_format="raw")
    assert look_stages("ops_v2", libraw.color_pipeline)

    assert look_stages("ops_v2", base.color_pipeline)  # srgb-direct


# --------------------------------------------------- apply_chain_v2 meta 透传
def test_apply_chain_v2_meta_neutral_roundtrip_le_1_lsb(png_path):
    """meta（RAW+DCP tone curve）透传下，零调整仍 ≤1 LSB（Phase1 base
    curve 为恒等占位 → 门控不改输出，仅验证路径走通且无回归）。"""
    img = Image.open(png_path).convert("RGB")
    meta = RenderMeta(color_pipeline="dcp:Test", note="",
                      dcp_has_tone_curve=True, source_format="raw")
    out = apply_chain_v2(img, {}, meta=meta)
    assert out.size == img.size and out.mode == "RGB"
    assert np.abs(np.asarray(img, np.int16) - np.asarray(out, np.int16)).max() <= 1


def test_apply_chain_v2_meta_none_unchanged_from_before(png_path):
    """meta=None（默认）行为与历史一致：入口 A 往返 ≤1 LSB。"""
    img = Image.open(png_path).convert("RGB")
    out = apply_chain_v2(img, {})
    assert np.abs(np.asarray(img, np.int16) - np.asarray(out, np.int16)).max() <= 1


# --------------------------------------------------- ColorAdjuster apply_chain meta 透传
def test_color_adjuster_apply_chain_accepts_meta(png_path):
    img = Image.open(png_path).convert("RGB")
    out = ColorAdjuster().apply_chain(
        img, {"exposure": 0.2, "shadows": 20},
        meta=RenderMeta(color_pipeline="dcp:X", dcp_has_tone_curve=True, source_format="raw"),
    )
    assert out.size == img.size and out.mode == "RGB"
    assert np.isfinite(np.asarray(out)).all()
