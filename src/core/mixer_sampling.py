from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

import numpy as np
from PIL import Image


MIXER_CENTERS = {
    "red": 0.0,
    "orange": 30.0,
    "yellow": 60.0,
    "green": 120.0,
    "cyan": 180.0,
    "blue": 240.0,
    "purple": 275.0,
    "magenta": 305.0,
}
MIXER_WIDTHS = {
    "red": 35.0,
    "orange": 30.0,
    "yellow": 35.0,
    "green": 50.0,
    "cyan": 45.0,
    "blue": 45.0,
    "purple": 35.0,
    "magenta": 45.0,
}
MIXER_CHANNELS = tuple(MIXER_CENTERS)
NO_SAMPLE = np.uint8(255)
COVERAGE_THRESHOLD = np.float32(1e-8)
_SRGB_OETF_THRESHOLD = np.float32(0.0031308)


def mixer_weights(hue: np.ndarray, saturation: np.ndarray, channels=MIXER_CHANNELS):
    """编码域 HSL 八色权重；只对传入的通道归一化，顺序与混合器一致。"""
    c0, c1 = np.float32(0.02), np.float32(0.08)
    t_c = np.clip((saturation - c0) / (c1 - c0), 0.0, 1.0)
    s_mask = t_c * t_c * (np.float32(3.0) - np.float32(2.0) * t_c)
    raw_weights = {}
    total_raw_weight = np.zeros_like(hue)
    for channel in channels:
        diff = np.abs((hue - MIXER_CENTERS[channel] + 180.0) % 360.0 - 180.0)
        width = MIXER_WIDTHS[channel]
        in_range = diff < width
        weight = np.zeros_like(diff)
        weight[in_range] = (np.cos(np.pi * 0.5 * (diff[in_range] / width)) ** 2) * s_mask[in_range]
        raw_weights[channel] = weight
        total_raw_weight += weight
    norm_factor = np.float32(1.0) / np.maximum(np.float32(1.0), total_raw_weight)
    for channel, weight in raw_weights.items():
        yield channel, weight * norm_factor


def linear_to_srgb(x: np.ndarray, *, out=None) -> np.ndarray:
    """sRGB OETF；输出缓冲与输入互不别名，调用方负责出口裁剪。"""
    x = np.asarray(x, dtype=np.float32)
    if out is None:
        out = np.empty_like(x)
    np.maximum(x, np.float32(0.0), out=out)
    np.power(out, np.float32(1.0 / 2.4), out=out)
    np.multiply(out, np.float32(1.055), out=out)
    np.subtract(out, np.float32(0.055), out=out)
    np.multiply(x, np.float32(12.92), out=out, where=x <= _SRGB_OETF_THRESHOLD)
    return out


def rgb_to_hsl(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    r = rgb[..., 0]
    g = rgb[..., 1]
    b = rgb[..., 2]
    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    chroma = cmax - cmin
    lightness = (cmax + cmin) * np.float32(0.5)
    hue = np.zeros_like(lightness)
    nonzero_chroma = chroma > np.float32(1e-7)
    r_mask = nonzero_chroma & (cmax == r)
    hue[r_mask] = 60.0 * (((g[r_mask] - b[r_mask]) / chroma[r_mask]) % 6.0)
    g_mask = nonzero_chroma & (~r_mask) & (cmax == g)
    hue[g_mask] = 60.0 * (((b[g_mask] - r[g_mask]) / chroma[g_mask]) + 2.0)
    b_mask = nonzero_chroma & (~r_mask) & (~g_mask)
    hue[b_mask] = 60.0 * (((r[b_mask] - g[b_mask]) / chroma[b_mask]) + 4.0)
    hue = hue % np.float32(360.0)
    hue[hue < 0] += np.float32(360.0)
    saturation = np.zeros_like(lightness)
    denom = np.float32(1.0) - np.abs(np.float32(2.0) * lightness - np.float32(1.0))
    valid_s = nonzero_chroma & (denom > np.float32(1e-7))
    saturation[valid_s] = np.clip(chroma[valid_s] / denom[valid_s], 0.0, 1.0)
    return hue, saturation, lightness


def nearest_mixer_channel(hue_deg: float) -> int:
    return min(range(len(MIXER_CHANNELS)), key=lambda index: abs(
        (hue_deg - MIXER_CENTERS[MIXER_CHANNELS[index]] + 180.0) % 360.0 - 180.0))


def hue_of_rgb8(red: int, green: int, blue: int) -> float:
    if max(red, green, blue) - min(red, green, blue) < 8:
        return 0.0
    hue, _, _ = rgb_to_hsl(np.array([[[red, green, blue]]], dtype=np.float32) / np.float32(255))
    return float(hue[0, 0])


@dataclass(frozen=True)
class MixerSamplingRequest:
    target_node: str | None = None
    before_node: str | None = None
    epoch: int = 0


@dataclass
class MixerSamplingField:
    rgb: np.ndarray
    coverage: np.ndarray

    @property
    def size(self):
        return self.rgb.shape[1], self.rgb.shape[0]


class MixerSampling:
    def __init__(self, request: MixerSamplingRequest, *, reserve_memory=None, working_memory=None):
        self.request = request
        self.field: MixerSamplingField | None = None
        self.backend = ""
        self._reserve_memory = reserve_memory
        self._working_memory = working_memory
        self._hold = None

    @property
    def active(self):
        return self.field is not None

    def memory(self, size):
        return self._working_memory(size) if self._working_memory is not None else nullcontext()

    def _reserve(self, size):
        return self._reserve_memory(size) if self._reserve_memory is not None else None

    def _replace(self, rgb, coverage, hold):
        previous = self._hold
        self.field = MixerSamplingField(rgb, coverage)
        self._hold = hold
        if previous is not None:
            previous.close()

    def close(self):
        self.field = None
        hold, self._hold = self._hold, None
        if hold is not None:
            hold.close()

    def capture(self, rgb_srgb, alpha, backend):
        hold = self._reserve(rgb_srgb.shape[0] * rgb_srgb.shape[1] * 16)
        try:
            rgb = np.array(rgb_srgb, dtype=np.float32, copy=True)
            coverage = (np.ones(rgb.shape[:2], dtype=np.float32) if alpha is None
                        else np.array(alpha, dtype=np.float32, copy=True))
            self._replace(rgb, coverage, hold)
            self.backend = backend
        except BaseException:
            if hold is not None:
                hold.close()
            raise

    def capture_neutral(self, source):
        with self.memory(source.pixels.nbytes):
            rgb = linear_to_srgb(source.pixels)
            np.clip(rgb, 0.0, 1.0, out=rgb)
            self.capture(rgb, source.alpha, "neutral")

    def _spatial(self, operation, size, *, premultiply=True):
        if not self.active:
            return
        field = self.field
        width, height = size
        hold = self._reserve(width * height * 16)
        extra = (field.rgb.nbytes if premultiply else 0) + field.coverage.nbytes + width * height * 20
        try:
            with self.memory(extra):
                coverage = np.clip(np.asarray(operation(field.coverage), dtype=np.float32), 0, 1)
                weighted = field.rgb * field.coverage[..., None] if premultiply else field.rgb
                rgb = np.stack([np.asarray(operation(weighted[..., index]), dtype=np.float32)
                                for index in range(3)], axis=-1)
                if premultiply:
                    np.divide(rgb, coverage[..., None], out=rgb,
                              where=coverage[..., None] > COVERAGE_THRESHOLD)
                    rgb[coverage <= COVERAGE_THRESHOLD] = 0
                self._replace(rgb, coverage, hold)
        except BaseException:
            if hold is not None:
                hold.close()
            raise

    def resize(self, size, *, box=None, resample=Image.Resampling.LANCZOS, premultiply=True):
        if not self.active or (box is None and self.field.size == tuple(size)):
            return
        self._spatial(lambda channel: np.asarray(Image.fromarray(channel).resize(
            size, resample, box=box), dtype=np.float32), size, premultiply=premultiply)

    def rotate(self, angle, *, output_size, resample=Image.Resampling.BICUBIC,
               expand=True, premultiply=True):
        if not self.active or not angle:
            return
        quarter_turns = angle % 360
        if expand and quarter_turns in (90, 180, 270):
            self._spatial(lambda channel: np.rot90(channel, int(quarter_turns // 90)),
                          output_size, premultiply=False)
            return
        self._spatial(lambda channel: np.asarray(Image.fromarray(channel).rotate(
            angle, resample=resample, expand=expand), dtype=np.float32), output_size,
            premultiply=premultiply)

    def crop(self, bounds):
        if not self.active:
            return
        left, top, right, bottom = bounds
        hold = self._reserve((right - left) * (bottom - top) * 16)
        try:
            self._replace(self.field.rgb[top:bottom, left:right].copy(),
                          self.field.coverage[top:bottom, left:right].copy(), hold)
        except BaseException:
            if hold is not None:
                hold.close()
            raise

    def apply_mask(self, mask):
        if self.active:
            np.multiply(self.field.coverage, mask, out=self.field.coverage)
            np.clip(self.field.coverage, 0, 1, out=self.field.coverage)

    def transform_coverage(self, operation):
        if not self.active:
            return
        with self.memory(self.field.coverage.nbytes * 4):
            coverage = np.asarray(operation(self.field.coverage), dtype=np.float32)
            if coverage.shape != self.field.coverage.shape:
                raise ValueError("来源覆盖尺寸无效")
            np.clip(coverage, 0, 1, out=self.field.coverage)

    def pad(self, output_size, padding):
        if not self.active or not any(padding):
            return
        width, height = output_size
        left, top, _, _ = padding
        hold = self._reserve(width * height * 16)
        try:
            rgb = np.zeros((height, width, 3), dtype=np.float32)
            coverage = np.zeros((height, width), dtype=np.float32)
            source_width, source_height = self.field.size
            rgb[top:top + source_height, left:left + source_width] = self.field.rgb
            coverage[top:top + source_height, left:left + source_width] = self.field.coverage
            self._replace(rgb, coverage, hold)
        except BaseException:
            if hold is not None:
                hold.close()
            raise

    def beauty_detail(self, field, cancelled):
        if not self.active:
            return
        from .beauty_v2 import remap_once
        with self.memory(self.field.rgb.nbytes * 8 + field.shape[1] * min(64, field.shape[0]) * 224):
            rgb, coverage = remap_once(self.field.rgb, self.field.coverage, field, cancelled)
            hold = self._reserve(rgb.nbytes + coverage.nbytes)
            self._replace(rgb, coverage, hold)

    def to_channel_map(self, size):
        if not self.active:
            raise ValueError("混色器输入尚未采集")
        self.resize(size)
        width, height = self.field.size
        channels = np.empty((height, width), dtype=np.uint8)
        with self.memory(min(height, 64) * width * 96):
            for start in range(0, height, 64):
                stop = min(height, start + 64)
                rgb = self.field.rgb[start:stop]
                finite = np.all(np.isfinite(rgb), axis=-1)
                hue, saturation, _ = rgb_to_hsl(np.clip(rgb, 0.0, 1.0))
                best = np.full(hue.shape, np.float32(360))
                selected = channels[start:stop]
                selected.fill(0)
                for index, center in enumerate(MIXER_CENTERS.values()):
                    difference = np.abs((hue - center + np.float32(180)) % np.float32(360)
                                        - np.float32(180))
                    closer = difference < best
                    selected[closer] = index
                    np.copyto(best, difference, where=closer)
                coverage = self.field.coverage[start:stop]
                selected[~finite | ~np.isfinite(coverage) | (saturation <= np.float32(0.02))
                         | (coverage <= COVERAGE_THRESHOLD)] = NO_SAMPLE
        channels.setflags(write=False)
        return channels
