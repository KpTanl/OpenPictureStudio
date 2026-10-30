from __future__ import annotations

import logging
import math
from concurrent.futures import CancelledError
from contextlib import nullcontext
from dataclasses import replace
from time import monotonic

import numpy as np

from . import photo_engine as engine
from .looks import look_stages
from .tools.color import validate_legacy_params
from .local_masks import (LOCAL_KEYS as KEYS, EFFECTIVE_RANGES, MASK_BAND_ROWS as BAND_ROWS,
                          mask_weight as weight, prepare_masks)


LOGGER = logging.getLogger(__name__)
ONE = np.float32(1)
LUMA = np.array((0.2126, 0.7152, 0.0722), dtype=np.float32)


def check_cancelled(cancelled):
    if cancelled():
        raise CancelledError()


def prepare(params):
    global_params = {**params, "process_version": 2, "masks": None}
    validate_legacy_params(global_params)
    if params.get("process_version") == 4:
        from .mask_components import prepare_components
        return global_params, prepare_components(params.get("masks"))
    return global_params, prepare_masks(params.get("masks"))


def effective_globals(params):
    result = dict(params)
    brightness = float(params.get("brightness", 1))
    if brightness > 0 and brightness != 1:
        result["exposure"] = float(params.get("exposure", 0)) + math.log2(brightness)
    sharpness = float(params.get("sharpness", 1))
    if sharpness != 1:
        result["texture"] = float(params.get("texture", 0)) + round((sharpness - 1) * 250)
    result.update(brightness=1, sharpness=1)
    return result


class Fields:
    def __init__(self, params, masks, shape, reference_size, cancelled):
        self.params, self.masks, self.shape = effective_globals(params), masks, shape
        self.reference_size, self.cancelled = reference_size, cancelled

    def active(self, *keys):
        return any(self.params.get(key, 1 if key in ("contrast", "saturation") else 0)
                   != (1 if key in ("contrast", "saturation") else 0)
                   or any(mask.delta[KEYS.index(key)] for mask in self.masks) for key in keys)

    def values(self, keys, start, stop, *, clipped=None):
        check_cancelled(self.cancelled)
        result = np.zeros((len(keys), stop - start, self.shape[1]), dtype=np.float32)
        indices = tuple(KEYS.index(key) for key in keys)
        for mask in self.masks:
            if not any(mask.delta[index] for index in indices):
                continue
            check_cancelled(self.cancelled)
            weights = weight(mask, self.shape, start, stop, reference_size=self.reference_size)
            for target, index in zip(result, indices):
                if mask.delta[index]:
                    target += weights * np.float32(mask.delta[index])
        for target, key in zip(result, keys):
            target += np.float32(self.params.get(key, 1 if key in ("contrast", "saturation") else 0))
            if clipped is not None and (np.any(target < EFFECTIVE_RANGES[key][0]) or np.any(target > EFFECTIVE_RANGES[key][1])):
                clipped.add(key)
            np.clip(target, *EFFECTIVE_RANGES[key], out=target)
        return result

    def bands(self, keys):
        for start in range(0, self.shape[0], BAND_ROWS):
            stop = min(start + BAND_ROWS, self.shape[0])
            yield start, stop, self.values(keys, start, stop)


def _exposure(rgb, ev):
    scale = np.exp2(ev.astype(np.float64)).astype(np.float32)[..., None]
    original = rgb * scale
    high, low = np.clip(rgb, 0, 1).max(axis=-1, keepdims=True), np.clip(rgb, 0, 1).min(axis=-1, keepdims=True)
    white = np.sqrt(np.maximum(scale, ONE))
    coefficient = (white - ONE) / (np.float32(0.25) * (white - np.float32(0.75)))

    def shoulder(value):
        y = value * scale
        distance = np.maximum(y - np.float32(0.75), np.float32(0))
        denominator = ONE + distance * np.where(scale > ONE, coefficient, np.float32(0))
        return np.minimum(np.where(y <= np.float32(0.75), y, np.float32(0.75) + distance / denominator), ONE)

    span = high - low
    ratio = np.divide(np.clip(rgb, 0, 1) - low, span, out=np.zeros_like(rgb), where=span > 0)
    adjusted = shoulder(low) + (shoulder(high) - shoulder(low)) * ratio
    return np.where(scale > ONE, adjusted, original)


def _tone(rgb, fields):
    keys = ("shadows", "highlights", "blacks", "whites")
    if not fields.active(*keys):
        return rgb
    luma = rgb @ LUMA
    target = luma.copy()
    if fields.active("shadows", "highlights"):
        radius, subsample = engine.PRO_CALIB.localtone_kernel(min(fields.shape))
        check_cancelled(fields.cancelled)
        base = engine.fast_guided_filter(luma, luma, radius, 0.04 ** 2, subsample)
    else:
        base = None
    for start, stop, amounts in fields.bands(keys):
        current = luma[start:stop].copy()
        if base is not None:
            b = base[start:stop]
            current = b.copy()
            current += engine._zone_offset(b, 0, 128 / 255, 100, point_control=False, bright_anchored=False) * (amounts[0] / np.float32(100))
            current += engine._zone_offset(b, 128 / 255, 1, 100, point_control=False, bright_anchored=True) * (amounts[1] / np.float32(100))
            current += luma[start:stop] - b
        result = current.copy()
        result += engine._zone_offset(current, 0, 80 / 255, 100, point_control=True, bright_anchored=False) * (amounts[2] / np.float32(100))
        result += engine._zone_offset(current, 175 / 255, 1, 100, point_control=True, bright_anchored=True) * (amounts[3] / np.float32(100))
        target[start:stop] = np.clip(result, 0, 1)
    for start in range(0, fields.shape[0], BAND_ROWS):
        check_cancelled(fields.cancelled)
        stop = min(start + BAND_ROWS, fields.shape[0])
        rgb[start:stop] = engine._apply_equal_gain(rgb[start:stop], luma[start:stop], target[start:stop])
    return rgb


def _details(rgb, fields):
    if not fields.active("texture", "clarity", "dehaze"):
        return rgb
    luma = rgb @ LUMA
    target = luma.copy()
    fine = None
    cb = engine.PRO_CALIB
    for key, kernel, eps, strength in (("texture", cb.tex_kernel, cb.tex_eps, cb.tex_strength),
                                       ("clarity", cb.clarity_kernel, cb.clarity_eps, cb.clarity_strength),
                                       ("dehaze", cb.dehaze_kernel, cb.dehaze_eps, cb.dehaze_strength)):
        if key == "texture" and fields.active("texture", "clarity"):
            check_cancelled(fields.cancelled)
            radius, subsample = kernel(min(fields.shape))
            fine = engine.fast_guided_filter(luma, luma, radius, eps, subsample)
        if not fields.active(key):
            continue
        check_cancelled(fields.cancelled)
        if key == "texture":
            base = fine
        else:
            radius, subsample = kernel(min(fields.shape))
            base = engine.fast_guided_filter(luma, luma, radius, eps, subsample)
        for start, stop, (amount,) in fields.bands((key,)):
            high = fine[start:stop] if key == "clarity" else luma[start:stop]
            target[start:stop] += np.float32(strength) * (amount / np.float32(100)) * (high - base[start:stop])
        if key == "clarity":
            fine = None
        base = None
    np.clip(target, 0, 1, out=target)
    for start, stop, (dehaze,) in fields.bands(("dehaze",)):
        band = engine._apply_equal_gain(rgb[start:stop], luma[start:stop], target[start:stop])
        lum = (band @ LUMA)[..., None]
        factor = ONE + np.float32(cb.dehaze_sat) * np.maximum(dehaze, 0)[..., None] / np.float32(100)
        rgb[start:stop] = lum + factor * (band - lum)
    return rgb


def _python(source, params, masks, stages, reference_size, cancelled, sampling):
    pixels = source.pixels
    if not masks:
        if not stages and engine.is_color_neutral(params, include_look=False):
            if sampling is not None:
                sampling.capture_neutral(source)
            return source
        rgb = engine._white_balance(pixels, int(params.get("temperature", 0)), int(params.get("tint", 0)))
        ev = float(params.get("exposure", 0))
        brightness = float(params.get("brightness", 1))
        if brightness > 0 and brightness != 1:
            ev += math.log2(brightness)
        rgb = engine._exposure(rgb, ev, 2)
        check_cancelled(cancelled)
        rgb = engine._apply_look(np.clip(engine.linear_to_srgb(rgb), 0, 1), stages)
        rgb = engine._local_tone(rgb, params)
        check_cancelled(cancelled)
        rgb = engine._apply_curves(engine._contrast(rgb, float(params.get("contrast", 1))), params.get("curves"))
        if sampling is not None:
            sampling.capture(rgb, source.alpha, "python")
        rgb = engine._color_mixer(rgb, params.get("color_mixer"))
        rgb = engine._saturation(engine._vibrance(rgb, int(params.get("vibrance", 0))), float(params.get("saturation", 1)))
        check_cancelled(cancelled)
        rgb = engine._detail_effects(rgb, params)
        check_cancelled(cancelled)
        return replace(source, pixels=engine.srgb_to_linear(engine._gamut_compress(rgb)))
    fields = Fields(params, masks, pixels.shape[:2], reference_size, cancelled)
    rgb = np.empty_like(pixels)
    for start, stop, (temperature, tint, exposure) in fields.bands(("temperature", "tint", "exposure")):
        band = pixels[start:stop].copy()
        red = np.exp2(temperature / np.float32(100) * np.float32(1.35))
        green = np.exp2(-tint / np.float32(100) * np.float32(0.8))
        blue = np.exp2(-temperature / np.float32(100) * np.float32(1.35))
        gains = np.stack((red, green, blue), axis=-1)
        gains /= np.maximum(gains @ LUMA, np.float32(1e-4))[..., None]
        band *= gains
        band = _exposure(band, exposure)
        rgb[start:stop] = engine._apply_look(np.clip(engine.linear_to_srgb(band), 0, 1), stages)
    rgb = _tone(rgb, fields)
    captured = np.empty_like(rgb) if sampling is not None else None
    for start, stop, (contrast, vibrance, saturation) in fields.bands(("contrast", "vibrance", "saturation")):
        band = rgb[start:stop]
        clipped = np.clip(band, 0, 1)
        exponent = np.maximum(contrast, np.float32(1e-3))[..., None]
        numerator = np.power(clipped, exponent)
        adjusted = numerator / (numerator + np.power(ONE - clipped, exponent) + np.float32(1e-12))
        band = np.where((np.abs(contrast - ONE) >= np.float32(1e-4))[..., None], adjusted, band)
        band = engine._apply_curves(band, params.get("curves"))
        if captured is not None:
            captured[start:stop] = band
        band = engine._color_mixer(band, params.get("color_mixer"))
        lum = (band @ LUMA)[..., None]
        chroma = np.clip(band.max(axis=-1) - band.min(axis=-1), 0, 1)
        t = np.clip((chroma - np.float32(0.03)) / np.float32(0.09), 0, 1)
        amount = vibrance / np.float32(100)
        gain = np.where(amount > 0, ONE + amount * np.float32(0.6) * t * t * (np.float32(3) - np.float32(2) * t) * (ONE - chroma),
                        ONE + amount * np.float32(0.75))
        band = lum + (band - lum) * gain[..., None]
        lum = (band @ LUMA)[..., None]
        rgb[start:stop] = lum + (band - lum) * saturation[..., None]
    if sampling is not None:
        check_cancelled(cancelled)
        sampling.capture(captured, source.alpha, "python")
        del captured
    rgb = _details(rgb, fields)
    for start in range(0, fields.shape[0], BAND_ROWS):
        check_cancelled(cancelled)
        stop = min(start + BAND_ROWS, fields.shape[0])
        rgb[start:stop] = engine.srgb_to_linear(engine._gamut_compress(rgb[start:stop]))
    return replace(source, pixels=rgb)


def _native():
    if not engine._HAS_RUST_ACCEL or not hasattr(engine.ops_accel, "render_local_pipeline"):
        raise ImportError("原生模块缺少处理版本 3 入口")
    return engine.ops_accel


def working_memory(source, params, *, threads=1):
    global_params, masks = prepare(params)
    height, width = source.pixels.shape[:2]
    if not masks:
        return 0
    spatial = global_params.get("sharpness", 1) != 1 or any(global_params.get(key, 0) or any(mask.delta[KEYS.index(key)] for mask in masks)
                  for key in ("shadows", "highlights", "blacks", "whites", "texture", "clarity", "dehaze"))
    bands = min(max(1, threads), math.ceil(height / BAND_ROWS))
    return (width * min(height, BAND_ROWS) * 4 * 32 * bands + (height * width * 4 * 24 if spatial else 0)
            + (height * width * 4 * len(masks) if params.get("process_version") == 4 else 0))


def render(source, params, *, backend="native", threads=1, reference_size=None,
           cancelled=lambda: False, sampling=None, prepared_masks=None):
    check_cancelled(cancelled)
    if backend not in ("python", "native") or type(threads) is not int or threads < 1:
        raise ValueError("调色后端或线程额度无效")
    if reference_size is not None and (len(reference_size) != 2 or any(type(value) is not int or value < 1 for value in reference_size)):
        raise ValueError("完整参考尺寸无效")
    reference_size = reference_size or source.orig_size
    component_mode = params.get("process_version") == 4
    params, masks = prepare(params)
    if component_mode and masks:
        if prepared_masks is None:
            from .mask_components import composite_weight
            assembled = []
            for mask in masks:
                if any("asset" in component for component in mask.components):
                    raise ValueError("error.mask.asset")
                raster = np.empty(source.pixels.shape[:2], np.float32)
                for start in range(0, raster.shape[0], BAND_ROWS):
                    stop = min(start + BAND_ROWS, raster.shape[0])
                    raster[start:stop] = composite_weight(mask, raster.shape, start, stop, reference_size, {}, cancelled)
                assembled.append(replace(mask, raster=raster))
            masks = tuple(assembled)
        else:
            masks = prepared_masks
    stages = look_stages(params.get("look", "none"), source.color_pipeline)
    started = monotonic()
    if backend == "native":
        try:
            native = _native()
            if component_mode and (getattr(native, "LOCAL_COMPONENT_MASKS", 0) != 1 or not hasattr(native, "render_component_pipeline")):
                raise ImportError("原生模块缺少处理版本 4 入口")
        except (ImportError, OSError):
            version = 4 if component_mode else 3
            report = ("python", version, "原生模块缺少处理版本 %s 入口" % version)
            if report not in engine._RENDER_BACKENDS_REPORTED:
                engine._RENDER_BACKENDS_REPORTED.add(report)
                LOGGER.info("调色 backend=%s process_version=%s reason=%s", *report)
            backend = "python"
    if backend == "native":
        if not masks:
            result = engine.render_source(source, params, threads=threads, cancelled=cancelled, sampling=sampling)
        else:
            native_params = {**effective_globals(params), "look_stages": [(list(stage.tone_lut), list(stage.hue_sat), stage.tone_strength, stage.sat_strength) for stage in stages],
                             "curves_lut": {key: engine.curve_lut(points).tolist() for key, points in (params.get("curves") or {}).items()}}
            encoded = [(mask.identity, mask.kind, list(mask.geometry), list(mask.delta)) for mask in masks]
            try:
                with sampling.memory(source.pixels.nbytes) if sampling is not None else nullcontext():
                    render_native = native.render_component_pipeline if component_mode else native.render_local_pipeline
                    extra = ([mask.raster for mask in masks],) if component_mode else ()
                    output, captured = render_native(np.ascontiguousarray(source.pixels), native_params, encoded,
                                                     reference_size or (source.pixels.shape[1], source.pixels.shape[0]),
                                                     threads, cancelled, sampling is not None, *extra)
                    check_cancelled(cancelled)
                    if sampling is not None:
                        sampling.capture(captured, source.alpha, "rust")
                        del captured
            except InterruptedError as error:
                raise CancelledError() from error
            check_cancelled(cancelled)
            result = replace(source, pixels=output)
    else:
        with sampling.memory(source.pixels.nbytes) if sampling is not None else nullcontext():
            result = _python(source, params, masks, stages, reference_size, cancelled, sampling)
    check_cancelled(cancelled)
    LOGGER.debug("局部调色 backend=%s size=%sx%s masks=%s duration_ms=%.6f", backend,
                 source.pixels.shape[1], source.pixels.shape[0], len(masks), (monotonic() - started) * 1000)
    return result
