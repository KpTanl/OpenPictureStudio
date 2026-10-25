"""OPS 观感两层结构：RAW 出片底子 + 三类格式共用的 OPS 风格（core/looks.py）。"""

import numpy as np
import pytest

import src.core.photo_engine as engine
from src.core.looks import OPS_STYLE, look_stages
from src.core.photo_engine import RenderPipeline, RenderSource, _apply_look, _REC709_LUMA


def _lab(d):
    a = np.where(d <= 0.04045, d / 12.92, ((d + 0.055) / 1.055) ** 2.4).reshape(-1, 3)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = a @ m.T / np.array([0.95047, 1.0, 1.08883])
    e, k = 216.0 / 24389.0, 24389.0 / 27.0
    f = np.where(xyz > e, np.cbrt(xyz), (k * xyz + 16.0) / 116.0)
    return np.stack([116.0 * f[:, 1] - 16.0, 500.0 * (f[:, 0] - f[:, 1]), 200.0 * (f[:, 1] - f[:, 2])], 1)


def _source(display, pipeline):
    lin = engine.srgb_to_linear(np.asarray(display, np.float32))
    return RenderSource(pixels=lin, alpha=None, source_format="raw" if pipeline == "libraw" else "srgb8",
                        source_path="x", is_proxy=False, orig_size=lin.shape[1::-1], color_pipeline=pipeline)


def _display8(src, params):
    out = RenderPipeline().render(src, params)
    return np.round(np.clip(engine.linear_to_srgb(out.pixels), 0.0, 1.0) * 255.0).astype(np.int16)


def test_raw_and_other_formats_share_one_style():
    """调风格只改 OPS_STYLE：RAW 与已出片格式经过的是同一个风格层对象。"""
    raw = look_stages("ops_v2", "libraw")
    other = look_stages("ops_v2", "srgb-direct")
    assert raw[-1] is OPS_STYLE and other == (OPS_STYLE,)


def test_raw_two_layers_stay_close_to_legacy_look():
    """两层合成复原旧版单层 RAW 观感：自然亮度与中等饱和下差异很小。"""
    rng = np.random.default_rng(5)
    lum = rng.uniform(0.02, 0.7, (200, 200, 1)).astype(np.float32)
    chroma = rng.normal(0.0, 0.05, (200, 200, 3)).astype(np.float32)
    d = np.clip(lum + chroma - (chroma @ _REC709_LUMA)[..., None], 0.0, 1.0)
    de = np.sqrt(np.sum((_lab(_apply_look(d, look_stages("ops_base", "libraw")))
                         - _lab(_apply_look(d, look_stages("ops_v2", "libraw")))) ** 2, axis=1))
    assert de.mean() < 1.0 and np.percentile(de, 95) < 2.5
    gray = np.linspace(0.0, 1.0, 256, dtype=np.float32)[None, :, None].repeat(3, 2)
    diff = (np.round(_apply_look(gray, look_stages("ops_base", "libraw")) * 255)
            - np.round(_apply_look(gray, look_stages("ops_v2", "libraw")) * 255))
    assert np.abs(diff).max() <= 5


def test_other_formats_are_not_rendered_twice():
    """已出片的 JPG、PNG 只走风格层：中灰、肤色、天空不再被 RAW 出片提亮。"""
    colors = np.array([[[128, 128, 128], [200, 150, 125], [110, 160, 215]]], np.float32) / 255.0
    src = _source(colors, "srgb-direct")
    look = _display8(src, {"look": "ops_v2", "process_version": 2})[0]
    original = np.round(colors[0] * 255).astype(np.int16)
    assert np.abs(look[0] - original[0]).max() <= 10
    assert look[1].max() < 240
    assert np.abs(look - original).max() <= 25


@pytest.mark.parametrize("pipeline,look", [("libraw", "ops_v2"), ("srgb-direct", "ops_v2"), ("libraw", "ops_base")])
@pytest.mark.parametrize("extra", [{}, {"exposure": 0.4, "contrast": 1.2, "saturation": 1.1, "shadows": 20}])
def test_native_and_python_paths_agree(monkeypatch, pipeline, look, extra):
    if not engine._HAS_RUST_ACCEL or not getattr(engine.ops_accel, "LOOK_STAGES", False):
        pytest.skip("原生模块不支持观感层参数")
    rng = np.random.default_rng(11)
    display = rng.random((48, 64, 3), dtype=np.float32)
    display[0, :] = np.linspace(0.0, 1.0, 64, dtype=np.float32)[:, None]
    src = _source(display, pipeline)
    params = {"look": look, "process_version": 2, **extra}
    native = _display8(src, params)
    monkeypatch.setattr(engine, "_HAS_RUST_ACCEL", False)
    python = _display8(src, params)
    assert np.abs(native - python).max() <= 1
