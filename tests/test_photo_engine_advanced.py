"""高级调色算子单元测试：Color Mixer、局部渐变蒙版、16-bit TIFF 导出。"""

import os
import numpy as np
import pytest

from src.core.photo_engine import (
    RenderSource,
    RenderPipeline,
    _color_mixer,
    _compute_mask_array,
    _apply_masks,
    _REC709_LUMA,
    save_render_source,
)


def test_color_mixer_neutral():
    """中性参数与空参数下，_color_mixer 必须原样返回输入。"""
    rgb = np.random.rand(50, 50, 3).astype(np.float32)
    assert _color_mixer(rgb, None) is rgb
    assert _color_mixer(rgb, {}) is rgb
    neutral_params = {"blue": {"hue": 0, "sat": 0, "lum": 0}}
    assert _color_mixer(rgb, neutral_params) is rgb


def test_color_mixer_luminance_preservation():
    """测试标准 HSL 混合器在仅调节饱和度时，HSL 明度 L=(max+min)/2 保持恒定。"""
    sky = np.zeros((64, 64, 3), dtype=np.float32)
    sky[:] = [0.2, 0.5, 0.9]
    l_orig = (np.max(sky) + np.min(sky)) * 0.5

    params = {"blue": {"hue": 0, "sat": 50, "lum": 0}}
    out = _color_mixer(sky, params)
    l_out = (np.max(out) + np.min(out)) * 0.5

    max_l_diff = np.abs(l_orig - l_out)
    assert max_l_diff < 1e-4, f"HSL Lightness shifted by {max_l_diff} during saturation adjustment"
    assert out[0, 0, 2] >= sky[0, 0, 2]


def test_color_mixer_isolation():
    """测试蓝色通道的调节不会影响纯绿色区域。"""
    img = np.zeros((32, 64, 3), dtype=np.float32)
    img[:, :32] = [0.2, 0.5, 0.9]
    img[:, 32:] = [0.1, 0.8, 0.2]

    params = {"blue": {"hue": 30, "sat": -50, "lum": 20}}
    out = _color_mixer(img, params)

    green_diff = np.max(np.abs(img[:, 32:] - out[:, 32:]))
    assert green_diff < 1e-3, f"Green region was affected by blue mixer: {green_diff}"


def test_color_mixer_magenta_rose_responsiveness():
    """验证粉红/洋红玫瑰花瓣像素（Hue ~292-311 deg）在洋红通道下的显著响应。"""
    # 模拟真实玫瑰花瓣采样点
    rose_pixels = np.array([
        [[205/255.0, 159/255.0, 205/255.0],
         [149/255.0, 115/255.0, 154/255.0],
         [199/255.0, 134/255.0, 187/255.0]],
    ], dtype=np.float32)

    # 1. 色相拉满 +100：必须产生可见的暖红偏移（ΔRGB 显著）
    out_hue = _color_mixer(rose_pixels, {"magenta": {"hue": 100}})
    max_diff_hue = np.max(np.abs(out_hue - rose_pixels))
    assert max_diff_hue > 0.05, f"Magenta hue shift too weak: {max_diff_hue}"
    # 验证偏红特征：R 增加且 B 减少
    assert np.all(out_hue[..., 0] >= rose_pixels[..., 0] - 1e-4)

    # 2. 饱和度 -100：必须显著去色变为中性灰
    out_sat = _color_mixer(rose_pixels, {"magenta": {"sat": -100}})
    l_hsl = (np.max(rose_pixels, axis=-1, keepdims=True) + np.min(rose_pixels, axis=-1, keepdims=True)) * 0.5
    for i in range(3):
        # 去色后各通道应非常接近其 HSL 明度
        assert np.max(np.abs(out_sat[..., i] - l_hsl[..., 0])) < 0.05

    # 3. 明度 +100：必须显著提亮
    out_lum = _color_mixer(rose_pixels, {"magenta": {"lum": 100}})
    assert np.all(out_lum > rose_pixels)


def test_color_mixer_neutral_protection():
    """测试低饱和度中性灰/暗部区域受软死区保护，不会产生彩虹伪色。"""
    neutral_grey = np.full((16, 16, 3), 0.5, dtype=np.float32)
    out = _color_mixer(neutral_grey, {
        "magenta": {"hue": 100, "sat": 100},
        "red": {"hue": 100, "sat": 100},
        "blue": {"hue": 100, "sat": 100},
    })
    assert np.max(np.abs(out - neutral_grey)) < 1e-5


def test_color_mixer_all_eight_channels():
    """测试 8 个色彩通道分别对各自特征色块的精准调控。"""
    # 8 个典型纯色样本：红、橙、黄、绿、青、蓝、紫、洋红
    test_colors = {
        "red": np.array([[[0.9, 0.1, 0.1]]], dtype=np.float32),
        "orange": np.array([[[0.9, 0.5, 0.1]]], dtype=np.float32),
        "yellow": np.array([[[0.9, 0.9, 0.1]]], dtype=np.float32),
        "green": np.array([[[0.1, 0.9, 0.1]]], dtype=np.float32),
        "cyan": np.array([[[0.1, 0.9, 0.9]]], dtype=np.float32),
        "blue": np.array([[[0.1, 0.1, 0.9]]], dtype=np.float32),
        "purple": np.array([[[0.6, 0.1, 0.9]]], dtype=np.float32),
        "magenta": np.array([[[0.9, 0.1, 0.9]]], dtype=np.float32),
    }

    for ch_name, color_arr in test_colors.items():
        # 调节该通道饱和度 -100
        out_desat = _color_mixer(color_arr, {ch_name: {"sat": -100}})
        diff = np.max(np.abs(out_desat - color_arr))
        assert diff > 0.3, f"Channel {ch_name} did not respond to saturation adjustment: diff={diff}"


def test_linear_mask_geometry():
    """测试线性渐变蒙版在起点、终点和中间的几何过渡。"""
    m = _compute_mask_array(100, 100, {
        "type": "linear",
        "p0": (0.5, 0.2),
        "p1": (0.5, 0.8),
    })
    assert m[10, 50] == pytest.approx(1.0, abs=1e-3)
    assert m[50, 50] == pytest.approx(0.487, abs=0.05)
    assert m[90, 50] == pytest.approx(0.0, abs=1e-3)


def test_radial_mask_geometry():
    """测试径向渐变蒙版从中心向外的平滑衰减。"""
    m = _compute_mask_array(100, 100, {
        "type": "radial",
        "center": (0.5, 0.5),
        "radius": (0.3, 0.3),
        "feather": 0.5,
    })
    assert m[50, 50] == pytest.approx(1.0, abs=1e-3)
    assert m[5, 5] == pytest.approx(0.0, abs=1e-3)


def test_save_render_source_tiff_16(tmp_path):
    """测试 save_render_source 保存 16-bit TIFF 的位深与内容。"""
    pixels = np.full((32, 32, 3), 0.5, dtype=np.float32)
    src = RenderSource(
        pixels=pixels,
        alpha=None,
        source_format="raw",
        source_path="dummy.raw",
        is_proxy=False,
        orig_size=(32, 32),
        color_pipeline="libraw",
    )
    tif_path = str(tmp_path / "test_out.tiff")
    save_render_source(src, tif_path)
    assert os.path.exists(tif_path)

    import tifffile
    arr = tifffile.imread(tif_path)
    assert arr.shape == (32, 32, 3)
    assert arr.dtype == np.uint16
    assert np.max(arr) > 255


def test_render_pipeline_with_color_mixer_and_mask():
    """测试 RenderPipeline 完整渲染包含 Color Mixer 和 Mask 的 RenderSource。"""
    pixels = np.full((40, 40, 3), 0.3, dtype=np.float32)
    src = RenderSource(
        pixels=pixels,
        alpha=None,
        source_format="raw",
        source_path="test.raw",
        is_proxy=True,
        orig_size=(40, 40),
        color_pipeline="libraw",
    )
    params = {
        "exposure": 0.5,
        "color_mixer": {"blue": {"sat": 20}},
        "masks": [{
            "type": "linear",
            "p0": (0.0, 0.0),
            "p1": (1.0, 1.0),
            "enabled": True,
            "params": {"exposure": -0.5},
        }],
    }
    out = RenderPipeline().render(src, params)
    assert isinstance(out, RenderSource)
    assert out.pixels.shape == (40, 40, 3)
    assert np.all(np.isfinite(out.pixels))


def test_brightness_and_sharpness_effects():
    """验证 brightness 与 sharpness 在 Pro 引擎下有效改变像素且不被 is_color_neutral 忽略。"""
    from PIL import Image
    from src.core.color_adjuster import ColorAdjuster
    from src.core.tools.color import is_color_neutral

    assert not is_color_neutral({"brightness": 2.0})
    assert not is_color_neutral({"sharpness": 1.5})

    # 1. 亮度测试
    img = Image.new("RGB", (30, 30), (100, 100, 100))
    out_b = ColorAdjuster(engine="pro").apply_chain(img, {"brightness": 2.0})
    diff_b = np.abs(np.array(out_b, dtype=int) - np.array(img, dtype=int)).max()
    assert diff_b > 20, f"Brightness adjustment failed, max diff={diff_b}"

    # 2. 锐度测试
    rng = np.random.default_rng(42)
    noise_arr = rng.integers(50, 200, (60, 60, 3), dtype=np.uint8)
    noise_img = Image.fromarray(noise_arr, "RGB")
    out_s = ColorAdjuster(engine="pro").apply_chain(noise_img, {"sharpness": 1.5})
    diff_s = np.abs(np.array(out_s, dtype=int) - np.array(noise_img, dtype=int)).max()
    assert diff_s > 10, f"Sharpness adjustment failed, max diff={diff_s}"


def test_load_render_source_preview_proxy_scaling(tmp_path):
    """验证普通大图（如 2400x1600 JPEG）在 for_preview=True 时缩放为 1280px 且 is_proxy=True。"""
    from PIL import Image
    from src.core.photo_engine import load_render_source

    img_path = str(tmp_path / "large_photo.jpg")
    large_img = Image.new("RGB", (2400, 1600), (120, 150, 180))
    large_img.save(img_path, quality=90)

    # 预览模式：长边缩放到 1280
    proxy_src = load_render_source(img_path, for_preview=True)
    assert proxy_src.is_proxy is True
    assert proxy_src.orig_size == (2400, 1600)
    assert proxy_src.pixels.shape == (853, 1280, 3)

    # 全量模式：保留 2400x1600
    full_src = load_render_source(img_path, for_preview=False)
    assert full_src.is_proxy is False
    assert full_src.orig_size == (2400, 1600)
    assert full_src.pixels.shape == (1600, 2400, 3)


def test_rust_curves_lut_nan_robustness():
    """验证 Rust ops_accel 接收包含 NaN 的非法曲线 LUT 时安全清洗并输出有限值。"""
    from src.core.photo_engine import _HAS_RUST_ACCEL
    if not _HAS_RUST_ACCEL:
        pytest.skip("ops_accel not available")

    import ops_accel
    pixels = np.full((16, 16, 3), 0.5, dtype=np.float32)
    bad_params = {"curves_lut": {"rgb": [float("nan"), 1.0, float("inf"), -0.5]}}
    out = ops_accel.render_pipeline_fast(pixels, bad_params)
    assert np.all(np.isfinite(out))
    assert np.all(out >= 0.0) and np.all(out <= 1.0)
