"""形状裁切：输出尺寸、形状蒙版与透明通道。"""

import pytest
from PIL import Image

from src.core.shape_cropper import CropRegion, ShapeCropper


def _region(shape, rect=(0.1, 0.25, 0.5, 0.5), corner_radius=0.0):
    return CropRegion(id=1, rect=rect, shape=shape, corner_radius=corner_radius)


def test_to_pixel_rect_scales_normalized_rect():
    assert _region("rect").to_pixel_rect(100, 80) == (10, 20, 50, 40)


def test_rect_region_is_fully_opaque_and_keeps_pixels():
    source = Image.new("RGB", (100, 80), (10, 120, 230))
    result = ShapeCropper(source).apply_region(_region("rect"))
    assert result.mode == "RGBA" and result.size == (50, 40)
    assert result.getchannel("A").getextrema() == (255, 255)
    assert result.getpixel((25, 20))[:3] == (10, 120, 230)


@pytest.mark.parametrize("shape", ["circle", "ellipse"])
def test_round_shapes_cut_the_corners(shape):
    source = Image.new("RGB", (100, 80), "white")
    alpha = ShapeCropper(source).apply_region(_region(shape)).getchannel("A")
    width, height = alpha.size
    assert alpha.getpixel((width // 2, height // 2)) == 255
    for corner in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        assert alpha.getpixel(corner) == 0


def test_circle_uses_the_shorter_side():
    """圆形取选区短边为直径，长边方向的两端留空。"""
    source = Image.new("RGB", (100, 80), "white")
    alpha = ShapeCropper(source).apply_region(_region("circle")).getchannel("A")
    assert alpha.size == (50, 40)
    assert alpha.getpixel((0, 20)) == 0 and alpha.getpixel((49, 20)) == 0
    assert alpha.getpixel((25, 1)) == 255


def test_round_rect_only_rounds_the_corners():
    source = Image.new("RGB", (100, 80), "white")
    alpha = ShapeCropper(source).apply_region(_region("round_rect", corner_radius=12)).getchannel("A")
    assert alpha.getpixel((0, 0)) == 0
    assert alpha.getpixel((25, 0)) == 255 and alpha.getpixel((0, 20)) == 255
    assert alpha.getpixel((25, 20)) == 255


def test_round_rect_without_radius_equals_rect():
    source = Image.new("RGB", (100, 80), "white")
    cropper = ShapeCropper(source)
    assert (cropper.apply_region(_region("round_rect", corner_radius=0)).tobytes()
            == cropper.apply_region(_region("rect")).tobytes())


def test_existing_alpha_is_multiplied_by_the_shape_mask():
    source = Image.new("RGBA", (100, 80), (200, 100, 50, 128))
    alpha = ShapeCropper(source).apply_region(_region("circle")).getchannel("A")
    assert alpha.getpixel((25, 20)) == 128
    assert alpha.getpixel((0, 0)) == 0


def test_region_is_clamped_to_the_image():
    source = Image.new("RGB", (100, 80), "white")
    result = ShapeCropper(source).apply_region(_region("rect", rect=(0.8, 0.8, 0.5, 0.5)))
    assert result.size == (20, 16)
    assert result.getchannel("A").getextrema() == (255, 255)


def test_source_image_is_not_modified():
    source = Image.new("RGBA", (100, 80), (200, 100, 50, 128))
    before = source.tobytes()
    ShapeCropper(source).apply_region(_region("ellipse"))
    assert source.tobytes() == before
