import copy
import logging
import threading
from concurrent.futures import CancelledError

import numpy as np
import pytest

from src.core import photo_engine as engine
from src.core.mixer_sampling import MixerSampling, MixerSamplingRequest


LINEAR = {
    "type": "linear", "p0": [0.5, 0.2], "p1": [0.5, 0.8],
    "params": {"exposure": 0.5, "saturation": 1.2},
}
RADIAL = {
    "type": "radial", "center": [0.4, 0.55], "radius": [0.4, 0.35],
    "angle": 37, "feather": 0.6,
    "params": {"exposure": -0.3, "temperature": 15, "tint": -9, "saturation": 1.3},
}


@pytest.fixture
def source():
    height, width = 129, 193
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    red = xs / np.float32(width - 1)
    rgb = np.stack((red, ys / np.float32(height - 1), 1 - red), axis=-1)
    pixels = engine.srgb_to_linear(rgb)
    alpha = red.copy()
    pixels.setflags(write=False)
    alpha.setflags(write=False)
    return engine.RenderSource(pixels, alpha, "srgb16", "mask-fixture.tif",
                               False, (width, height), "srgb-direct")


def select_backend(monkeypatch, native):
    if native and not hasattr(engine, "ops_accel"):
        pytest.skip("原生模块不可用")
    monkeypatch.setattr(engine, "_HAS_RUST_ACCEL", native)


@pytest.fixture(params=(False, True), ids=("python", "native"))
def backend(request, monkeypatch):
    select_backend(monkeypatch, request.param)
    return request.param


@pytest.mark.parametrize("process_version", (1, 2), ids=("pv1", "pv2"))
@pytest.mark.parametrize("masks", ([LINEAR], [RADIAL], [LINEAR, RADIAL]),
                         ids=("linear", "radial", "overlap"))
@pytest.mark.parametrize("global_params", ({"contrast": 1.1}, {"saturation": 3.0}),
                         ids=("contrast", "saturation"))
@pytest.mark.parametrize("threads", (1, 4), ids=("single", "multi"))
def test_native_and_python_mask_parity(source, monkeypatch, caplog,
                                      process_version, masks, global_params, threads):
    caplog.set_level(logging.DEBUG, logger=engine.__name__)
    params = {**global_params, "look": "none", "process_version": process_version, "masks": masks}
    original = source.pixels.copy()
    select_backend(monkeypatch, True)
    native = engine.render_source(source, params, threads=threads)
    assert any("调色计算 backend=rust" in record.getMessage() for record in caplog.records)
    select_backend(monkeypatch, False)
    fallback = engine.render_source(source, params, threads=threads)
    np.testing.assert_allclose(engine.linear_to_srgb(native.pixels),
                               engine.linear_to_srgb(fallback.pixels), atol=1 / 255, rtol=0)
    np.testing.assert_array_equal(source.pixels, original)
    assert native.alpha is fallback.alpha is source.alpha
    assert native.pixels.dtype == fallback.pixels.dtype == np.float32


@pytest.mark.parametrize("process_version", (1, 2), ids=("pv1", "pv2"))
@pytest.mark.parametrize("global_params", ({}, {"saturation": 3.0}),
                         ids=("neutral", "saturation"))
def test_disabled_masks_equal_no_masks(source, backend, process_version, global_params):
    params = {**global_params, "look": "none", "process_version": process_version}
    expected = engine.render_source(source, params, threads=4)
    disabled = [{**LINEAR, "enabled": False}, {**RADIAL, "enabled": False}]
    for masks in ([], disabled):
        result = engine.render_source(source, {**params, "masks": masks}, threads=4)
        np.testing.assert_array_equal(result.pixels, expected.pixels)
        assert result.alpha is source.alpha
        if not global_params:
            assert result is source


@pytest.mark.parametrize("process_version", (1, 2), ids=("pv1", "pv2"))
def test_disabled_masks_do_not_change_enabled_masks(source, backend, process_version):
    params = {"saturation": 3.0, "look": "none", "process_version": process_version,
              "masks": [LINEAR]}
    expected = engine.render_source(source, params, threads=4)
    mixed = [{**RADIAL, "enabled": False}, LINEAR, {**LINEAR, "enabled": False}]
    actual = engine.render_source(source, {**params, "masks": mixed}, threads=4)
    np.testing.assert_array_equal(actual.pixels, expected.pixels)


@pytest.mark.parametrize("process_version", (1, 2), ids=("pv1", "pv2"))
def test_mixer_sampling_is_independent_of_masks(source, backend, process_version):
    params = {"saturation": 3.0, "look": "none", "process_version": process_version,
              "curves": {"rgb": [[0, 0], [120, 135], [255, 255]]},
              "color_mixer": {"red": {"hue": 20, "sat": -30}}}
    captured = []
    for masks in ([], [LINEAR, RADIAL]):
        sampling = MixerSampling(MixerSamplingRequest())
        try:
            engine.render_source(source, {**params, "masks": masks}, threads=4, sampling=sampling)
            assert sampling.backend == ("rust" if backend else "python")
            captured.append((sampling.field.rgb.copy(), sampling.field.coverage.copy()))
        finally:
            sampling.close()
    for unmasked, masked in zip(captured[0], captured[1]):
        np.testing.assert_array_equal(unmasked, masked)


@pytest.mark.parametrize("immediate", (True, False), ids=("immediate", "weight"))
def test_cancelled_mask_render_has_no_result(source, backend, monkeypatch, immediate):
    cancelled = threading.Event()
    if immediate:
        cancelled.set()
    else:
        weight = engine._compute_mask_array

        def cancel_after_weight(*args, **kwargs):
            result = weight(*args, **kwargs)
            cancelled.set()
            return result

        monkeypatch.setattr(engine, "_compute_mask_array", cancel_after_weight)
    with pytest.raises(CancelledError):
        engine.render_source(source, {"saturation": 3.0, "look": "none", "process_version": 2,
                                      "masks": [LINEAR]}, threads=4, cancelled=cancelled.is_set)
    assert not source.pixels.flags.writeable and not source.alpha.flags.writeable


@pytest.mark.parametrize("process_version", (1, 2), ids=("pv1", "pv2"))
def test_native_failure_uses_corrected_fallback(source, monkeypatch, process_version):
    params = {"saturation": 3.0, "look": "none", "process_version": process_version,
              "masks": copy.deepcopy([LINEAR, RADIAL])}
    select_backend(monkeypatch, False)
    expected = engine.render_source(source, params, threads=4)
    select_backend(monkeypatch, True)

    def fail(*args, **kwargs):
        raise RuntimeError("原生失败注入")

    monkeypatch.setattr(engine.ops_accel, "render_pipeline_fast", fail)
    result = engine.render_source(source, params, threads=4)
    np.testing.assert_array_equal(result.pixels, expected.pixels)
    assert result.alpha is source.alpha
