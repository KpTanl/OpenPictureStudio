"""标准 HSL 色彩混合器与调色缺陷修复单元测试。"""

import os
import numpy as np
import pytest
from PIL import Image
from PyQt6.QtWidgets import QApplication

import src.core.photo_engine as photo_engine
from src.core.photo_engine import (
    _rgb_to_hsl,
    _hsl_to_rgb,
    _color_mixer,
    _src_from_pil,
    srgb_to_linear,
    linear_to_srgb,
    RenderPipeline,
    RenderSource,
    _COLOR_MIXER_CENTERS,
    _COLOR_MIXER_WIDTHS,
)
from src.core.color_adjuster import ColorAdjuster


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_hsl_roundtrip():
    """验证 RGB 与 HSL 之间的数学可逆性。"""
    rng = np.random.default_rng(42)
    rgb = rng.uniform(0.01, 0.99, size=(64, 64, 3)).astype(np.float32)
    
    H, S, L = _rgb_to_hsl(rgb)
    assert np.all(H >= 0.0) and np.all(H < 360.0)
    assert np.all(S >= 0.0) and np.all(S <= 1.0)
    assert np.all(L >= 0.0) and np.all(L <= 1.0)

    rgb_recon = _hsl_to_rgb(H, S, L)
    max_err = np.max(np.abs(rgb - rgb_recon))
    assert max_err < 1e-4, f"HSL roundtrip max error too large: {max_err}"


def test_hsl_boundary_convergence():
    """验证饱和度与明度向边界 (0/1) 收敛的数学性质。"""
    # 纯蓝 (0, 0, 1)
    blue = np.array([[[0.0, 0.0, 1.0]]], dtype=np.float32)
    
    # 1. 饱和度 -100 -> 完全去色为中性灰 (0.5, 0.5, 0.5)
    desat_blue = _color_mixer(blue, {"blue": {"sat": -100}})
    assert np.allclose(desat_blue, [[[0.5, 0.5, 0.5]]], atol=1e-3)

    # 2. 明度 +100 -> 真正收敛至纯白 (1.0, 1.0, 1.0)
    bright_blue = _color_mixer(blue, {"blue": {"lum": 100}})
    assert np.allclose(bright_blue, [[[1.0, 1.0, 1.0]]], atol=1e-3)

    # 3. 明度 -100 -> 真正收敛至纯黑 (0.0, 0.0, 0.0)
    dark_blue = _color_mixer(blue, {"blue": {"lum": -100}})
    assert np.allclose(dark_blue, [[[0.0, 0.0, 0.0]]], atol=1e-3)


def test_hsl_neutral_gray_protection():
    """验证纯中性灰受死区保护，单通道调色不产生彩虹伪色。"""
    neutral_gray = np.full((16, 16, 3), 0.45, dtype=np.float32)
    out = _color_mixer(neutral_gray, {
        "red": {"hue": 100, "sat": 100, "lum": 100},
        "orange": {"hue": 100, "sat": 100, "lum": 100},
        "blue": {"hue": 100, "sat": 100, "lum": 100},
        "magenta": {"hue": 100, "sat": 100, "lum": 100},
    })
    assert np.allclose(out, neutral_gray, atol=1e-5)


def test_hsl_eight_channels_response_and_cross_channel_isolation():
    """验证 8 个特征通道对各自特征色的精准调控，以及对非重叠通道的零串扰隔离性。"""
    # 8 个典型纯色样本
    center_colors = {}
    for ch_name, center in _COLOR_MIXER_CENTERS.items():
        H_arr = np.array([[[center]]], dtype=np.float32)
        S_arr = np.array([[[1.0]]], dtype=np.float32)
        L_arr = np.array([[[0.5]]], dtype=np.float32)
        center_colors[ch_name] = _hsl_to_rgb(H_arr, S_arr, L_arr)

    # 1. 响应测试：调节自身通道饱和度 -100，各通道精准变灰
    for ch_name, color_arr in center_colors.items():
        out_desat = _color_mixer(color_arr, {ch_name: {"sat": -100}})
        assert not np.allclose(out_desat, color_arr, atol=0.2), f"{ch_name} didn't respond to desaturation"
        assert np.allclose(out_desat, [[[0.5, 0.5, 0.5]]], atol=1e-3), f"{ch_name} desaturation did not produce neutral gray"

    # 2. 隔离性测试：调节蓝色通道（240°），纯红色（0°）与纯绿色（120°）必须 0 改变
    blue_param = {"blue": {"hue": 100, "sat": 100, "lum": 100}}
    assert np.allclose(_color_mixer(center_colors["red"], blue_param), center_colors["red"], atol=1e-6)
    assert np.allclose(_color_mixer(center_colors["green"], blue_param), center_colors["green"], atol=1e-6)
    assert np.allclose(_color_mixer(center_colors["yellow"], blue_param), center_colors["yellow"], atol=1e-6)

    # 3. 隔离性测试：调节红色通道（0°），纯蓝色（240°）与纯青色（180°）必须 0 改变
    red_param = {"red": {"hue": -100, "sat": -100, "lum": 100}}
    assert np.allclose(_color_mixer(center_colors["blue"], red_param), center_colors["blue"], atol=1e-6)
    assert np.allclose(_color_mixer(center_colors["cyan"], red_param), center_colors["cyan"], atol=1e-6)


def test_auto_enhance_no_name_error():
    """验证自动增强直方图计算正常运行，不出现 ImageChops NameError。"""
    adj = ColorAdjuster()
    img = Image.new("RGB", (128, 128), (100, 120, 140))
    res = adj.auto_enhance(img)
    assert isinstance(res, Image.Image)
    assert res.size == img.size


def test_src_from_pil_multi_format_protection():
    """验证 _src_from_pil 对 RGBA, LA, P (含透明度), 16 位图的正确解析与精度保护。"""
    # 1. RGBA
    img_rgba = Image.new("RGBA", (20, 20), (255, 128, 64, 200))
    src_rgba = _src_from_pil(img_rgba)
    assert src_rgba.alpha is not None
    assert np.isclose(src_rgba.alpha[0, 0], 200.0 / 255.0, atol=1e-4)
    assert src_rgba.source_format == "srgb8"

    # 2. LA (Luminance + Alpha)
    img_la = Image.new("LA", (20, 20), (128, 180))
    src_la = _src_from_pil(img_la)
    assert src_la.alpha is not None
    assert np.isclose(src_la.alpha[0, 0], 180.0 / 255.0, atol=1e-4)
    assert src_la.source_format == "srgb8"

    # 3. P 模式且带 transparency info
    img_p_trans = Image.new("P", (20, 20))
    img_p_trans.putpalette([255, 0, 0,  0, 255, 0,  0, 0, 255] * 85)
    img_p_trans.info["transparency"] = 0
    src_p_trans = _src_from_pil(img_p_trans)
    assert src_p_trans.alpha is not None
    assert src_p_trans.source_format == "srgb8"

    # 4. P 模式无 transparency
    img_p_opaque = Image.new("P", (20, 20))
    img_p_opaque.putpalette([255, 0, 0] * 256)
    src_p_opaque = _src_from_pil(img_p_opaque)
    assert src_p_opaque.alpha is None

    # 5. 16 位灰度 (I;16)
    arr_16 = np.full((20, 20), 32768, dtype=np.uint16)
    img_16 = Image.fromarray(arr_16)
    src_16 = _src_from_pil(img_16)
    assert src_16.source_format == "srgb16"
    assert src_16.pixels.shape == (20, 20, 3)
    # 验证除以 65535 而不是 255
    assert np.max(src_16.pixels) <= 1.0
    assert np.isclose(srgb_to_linear(np.float32(32768.0 / 65535.0)), src_16.pixels[0, 0, 0], atol=1e-3)


def test_true_python_vs_rust_hsl_parity(monkeypatch):
    """真正独立对比纯 Python 实现与 Rust 加速实现的 HSL 调色输出一致性。"""
    try:
        import ops_accel
        print("ops_accel file in pytest:", ops_accel.__file__)
    except ImportError:
        pytest.skip("ops_accel module not compiled or available")

    # 创建包含 8 种主要色相以及渐变的多彩测试图 (sRGB [0, 1])
    h_vals = np.linspace(0, 360, 60, endpoint=False, dtype=np.float32)
    s_vals = np.linspace(0.1, 1.0, 40, dtype=np.float32)
    H_grid, S_grid = np.meshgrid(h_vals, s_vals)
    L_grid = np.full_like(H_grid, 0.5)

    test_img_srgb = _hsl_to_rgb(H_grid, S_grid, L_grid)
    test_img_linear = np.ascontiguousarray(srgb_to_linear(test_img_srgb), dtype=np.float32)

    mixer_params = {
        "red": {"hue": 25, "sat": -30, "lum": 15},
        "orange": {"hue": -15, "sat": 40, "lum": -10},
        "yellow": {"hue": 10, "sat": -50, "lum": 20},
        "green": {"hue": -20, "sat": 30, "lum": -15},
        "cyan": {"hue": 15, "sat": -40, "lum": 10},
        "blue": {"hue": -30, "sat": 50, "lum": -20},
        "purple": {"hue": 20, "sat": -25, "lum": 25},
        "magenta": {"hue": -10, "sat": 35, "lum": -30},
    }

    # 1. 纯 Python 回退路径（显式禁用 Rust 加速）
    monkeypatch.setattr(photo_engine, "_HAS_RUST_ACCEL", False)
    py_src = RenderSource(
        pixels=test_img_linear.copy(),
        alpha=None,
        source_format="srgb8",
        source_path="",
        orig_size=(60, 40),
        is_proxy=False,
        color_pipeline="srgb-direct",
    )
    py_out = RenderPipeline().render(py_src, {"color_mixer": mixer_params}).pixels

    # 2. 纯 Rust 加速执行路径
    rust_out = ops_accel.render_pipeline_fast(test_img_linear.copy(), {"color_mixer": mixer_params})

    # 验证独立双端输出最大绝对误差 <= 5e-4 (实测误差 ~1.8e-5)
    max_err = np.max(np.abs(py_out - rust_out))
    assert max_err < 5e-4, f"True Python and Rust HSL outputs diverged: max_err={max_err}"
