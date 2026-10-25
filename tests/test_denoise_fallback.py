"""Denoise fallback: missing AI model must not raise, must degrade to basic."""

from __future__ import annotations

from PIL import Image

from src.core.denoiser import DenoiseConfig, ImageDenoiser, is_ai_model_available


def test_is_ai_model_available():
    assert is_ai_model_available(None) is False
    assert is_ai_model_available("") is False
    assert is_ai_model_available("/definitely/not/here.onnx") is False


def test_ai_mode_falls_back_to_basic_when_model_missing(tmp_path):
    image = Image.new("RGB", (48, 32), color=(100, 120, 140))
    config = DenoiseConfig(
        mode="ai",
        basic_method="bilateral",
        model_path=str(tmp_path / "missing.onnx"),
        luma_strength=30,
        chroma_strength=20,
    )
    out = ImageDenoiser().denoise(image, config)
    assert out is not None
    assert out.size == image.size


def test_ai_mode_falls_back_when_model_path_unset():
    image = Image.new("RGB", (24, 24), color=(80, 90, 100))
    out = ImageDenoiser().denoise(image, DenoiseConfig(mode="ai", model_path=None))
    assert out.size == image.size
