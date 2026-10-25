"""参考图的只读参数拟合；工作流构造和显示画面测量由宿主注入。"""

from __future__ import annotations

import copy

import numpy as np

from ..core.mixer_sampling import MIXER_CHANNELS
from ..core.photo_engine import curve_lut
from ..core.tools.color import COLOR_UI_PARAMETERS, curve_channels, mixer_values
from .photo_analysis import ANALYSIS_EDGE, MEASUREMENT_SPEC, display_lab


FIT_DEFAULT_STRENGTH = 60
FIT_PERTURBATION = 20
FIT_MAX_CHANGE = 40
FIT_CONDITION_LIMIT = 1000
FIT_HUE_SHARE = .02
FIT_RESPONSE_FLOORS = {"L": .2, "C": .2, "hab": .5}
FIT_CURVE_ERROR = 1.5
# 与目标比较的水平量测和验证用测量规格 1 的尺寸：分位、彩度与色相统计随尺寸变化，不同尺寸相减会带进偏差。
# 小图只用来求参数的响应（同一尺寸上两次渲染之差）。
FIT_VALIDATION_EDGE = ANALYSIS_EDGE
FIT_RESPONSE_EDGE = 256
_NEUTRAL = [[0, 0], [255, 255]]
_TONE_POINTS = ("p1", "p5", "p10", "p25", "p50", "p75", "p90", "p95", "p99")


def _angle(value):
    return (value + 180) % 360 - 180


def _inverse_distribution(histogram):
    counts = np.asarray(histogram, dtype=np.float64)
    bins = np.flatnonzero(counts)
    cumulative = np.cumsum(counts)
    quantiles = (cumulative[bins] - counts[bins] / 2) / cumulative[-1]
    return quantiles, bins / 255


def simplify_curve(mapping, focus=None):
    """把 256 项单调映射精简成不超过 6 个控制点。

    focus 是有像素的编码值区间 (lo, hi)：误差只在其中要求不超过 FIT_CURVE_ERROR，两端先作控制点；
    区间外几乎没有像素，只要求单调地连到 0 与 255。"""
    lo, hi = focus if focus else (0, 255)
    indices = sorted({0, 255, *(index for index in (lo, hi) if 0 < index < 255)})
    grid = np.linspace(0, 255, 1024)
    inside = (grid >= lo) & (grid <= hi)
    while True:
        points = [[index, int(round(mapping[index]))] for index in indices]
        error = np.abs(curve_lut(points) * 255 - np.interp(grid, np.arange(256), mapping))
        error = np.where(inside, error, 0.0)
        if float(error.max()) <= FIT_CURVE_ERROR:
            return points
        if len(indices) >= 6:
            return None
        available = np.array([index for index in range(1, 255) if index not in indices])
        sampled = np.interp(available, grid, error)
        indices = sorted([*indices, int(available[np.argmax(sampled)])])


class ReferenceFitter:
    def __init__(self, build_workflow, measure, is_cancelled):
        self.build_workflow, self.measure, self.is_cancelled = build_workflow, measure, is_cancelled
        self.renders = 0

    def _render(self, values, edge=FIT_VALIDATION_EDGE):
        if self.is_cancelled():
            raise ValueError("AI_CANCELLED")
        result = self.measure(self.build_workflow(copy.deepcopy(values)), edge)
        self.renders += 1
        if self.is_cancelled():
            raise ValueError("AI_CANCELLED")
        if result.get("status") != "available":
            raise ValueError("AI_FIT_MEASUREMENT")
        return result

    def _locked(self, path):
        return any(path == key or path.startswith(key + ".") for key in self.preserve)

    def _skip(self, field, code):
        self.skipped.append({"field": field, "code": code})

    def _residuals(self, stats):
        result = {}
        if "tone" in self.dimensions:
            result["tone"] = {key: 255 * (stats["luma_percentiles"][key] - self.target["luma_percentiles"][key])
                              for key in _TONE_POINTS}
        if "split_tone" in self.dimensions:
            result["split_tone"] = {
                segment: {key: stats["split_tone"][segment][key] - self.target["split_tone"][segment][key]
                          for key in ("a", "b")}
                for segment in self.split_segments
                if stats["split_tone"][segment]["status"] != "missing"}
        if "hue" in self.dimensions:
            result["hue"] = {}
            for color in self.hue_colors:
                row, target = stats["hues"][color], self.target["hues"][color]
                if row["share"] >= FIT_HUE_SHARE and row.get("hab") is not None:
                    result["hue"][color] = {key: _angle(row[key] - target[key]) if key == "hab" else row[key] - target[key]
                                             for key in ("L", "C", "hab")}
        if "chroma" in self.dimensions:
            result["chroma"] = {key: stats["chroma_percentiles"][key] - self.target["chroma_percentiles"][key]
                                for key in ("p50", "p90")}
        return result

    @staticmethod
    def _errors(residuals):
        def numbers(value):
            return [number for item in value.values() for number in numbers(item)] if isinstance(value, dict) else [value]
        return {key: float(np.sqrt(np.mean(np.square(numbers(value)))))
                for key, value in residuals.items() if numbers(value)}

    def _validate(self, dimension, candidate):
        if candidate == self.values:
            self.steps.append({"dimension": dimension, "accepted": False, "code": "no_change"})
            return
        measured = self._render(candidate)
        before, after = self._residuals(self.stats), self._residuals(measured)
        old_errors, new_errors = self._errors(before), self._errors(after)
        missing = any(key not in after.get(dimension, {}) for key in before.get(dimension, {}))
        improved = (not missing and dimension in new_errors and dimension in old_errors
                    and new_errors[dimension] < old_errors[dimension] - 1e-6)
        stable = all(key in new_errors and new_errors[key] <= error + 1e-6
                     and set(after[key]) == set(before[key]) for key, error in old_errors.items() if key in self.protected)
        lightness_stable = (dimension != "split_tone" or all(
            abs(measured["luma_percentiles"][key] - self.stats["luma_percentiles"][key]) * 255 <= FIT_CURVE_ERROR
            for key in _TONE_POINTS))
        accepted = improved and stable and lightness_stable
        self.steps.append({"dimension": dimension, "accepted": accepted,
                           "code": "improved" if accepted else "lightness_changed" if not lightness_stable
                           else "regressed" if not stable else "not_improved",
                           "before": before, "after": after,
                           "before_error": old_errors, "after_error": new_errors})
        if accepted:
            self.values, self.stats = candidate, measured

    def _tone(self):
        if self._locked("curves.rgb"):
            self._skip("tone", "locked")
            return
        neutral = copy.deepcopy(self.values)
        neutral["curves"]["rgb"] = copy.deepcopy(_NEUTRAL)
        stats = self._render(neutral)
        percentiles = stats["luma_percentiles"]
        if percentiles["p99"] - percentiles["p1"] < .02:
            self._skip("tone", "narrow_luma_range")
            return
        probabilities, values = _inverse_distribution(stats["histogram"]["luma"])
        target_probabilities, targets = _inverse_distribution(self.target["histogram"]["luma"])
        grid = np.arange(256) / 255
        mapped = np.interp(np.interp(grid, values, probabilities), target_probabilities, targets)
        low, high = percentiles["p1"], percentiles["p99"]
        low_y, high_y = np.interp([low, high], grid, mapped)
        # p1—p99 之外按区间内的平均斜率连到两端；到不了 0 或 255 时端点夹住，折角只落在 p1、p99 两个控制点上
        slope = (high_y - low_y) / (high - low)
        start = float(np.clip(low_y - low * slope, 0.0, low_y))
        end = float(np.clip(high_y + (1 - high) * slope, high_y, 1.0))
        mapped = np.where(grid < low, np.interp(grid, [0.0, low], [start, low_y]), mapped)
        mapped = np.where(grid > high, np.interp(grid, [high, 1.0], [high_y, end]), mapped)
        existing = curve_lut(self.values["curves"]["rgb"], 256)
        mapping = np.maximum.accumulate(np.clip((existing * (1 - self.strength) + mapped * self.strength) * 255, 0, 255))
        points = simplify_curve(mapping, (int(round(low * 255)), int(round(high * 255))))
        if points is None:
            self._skip("tone", "curve_precision_limit")
            return
        candidate = copy.deepcopy(self.values)
        candidate["curves"]["rgb"] = points
        self._validate("tone", candidate)

    def _split_tone(self):
        channels = ("r", "g", "b")
        if any(self._locked("curves." + channel) for channel in channels):
            self._skip("split_tone", "locked")
            return
        if any(type(value) is not int for channel in channels for point in self.values["curves"][channel] for value in point):
            self._skip("split_tone", "non_integer_curve")
            return
        curves = {channel: {int(x): int(y) for x, y in self.values["curves"][channel]} for channel in channels}
        anchors = set()
        for segment, point in (("shadows", "p10"), ("midtones", "p50"), ("highlights", "p90")):
            field = "split_tone." + segment
            row, target = self.stats["split_tone"][segment], self.target["split_tone"][segment]
            if segment not in self.split_segments or "rgb" not in row:
                self._skip(field, "insufficient_neutral_samples")
                continue
            if row["L"] < 5:
                self._skip(field, "near_black")
                continue
            rgb = np.asarray(row["rgb"], dtype=np.float64)
            columns = []
            for channel in range(3):
                step = .002 if rgb[channel] <= .998 else -.002
                probe = rgb.copy()
                probe[channel] += step
                columns.append((display_lab(probe) - display_lab(rgb)) / step)
            jacobian = np.column_stack(columns)
            if np.linalg.cond(jacobian) > FIT_CONDITION_LIMIT:
                self._skip(field, "ill_conditioned")
                continue
            delta = np.linalg.solve(jacobian, [0, target["a"] - row["a"], target["b"] - row["b"]]) * self.strength
            x = int(round(self.stats["luma_percentiles"][point] * 255))
            if not 0 < x < 255:
                self._skip(field, "endpoint")
                continue
            if x in anchors:
                self._skip(field, "repeated_anchor")
                continue
            anchors.add(x)
            for channel, offset in zip(channels, delta):
                existing = curve_lut(self.values["curves"][channel], 256)[x] * 255
                value = existing + offset * 255
                curves[channel][x] = int(round(np.clip(value, 0, 255)))
                if not 0 <= value <= 255:
                    self._skip("curves." + channel, "at_upper_limit" if value > 255 else "at_lower_limit")
        if any(len(points) > 16 for points in curves.values()):
            self._skip("split_tone", "curve_point_limit")
            return
        candidate = copy.deepcopy(self.values)
        candidate["curves"].update({channel: [[x, y] for x, y in sorted(points.items())] for channel, points in curves.items()})
        self._validate("split_tone", candidate)

    def _bounded(self, field, current, delta):
        value = current + float(np.clip(delta, -FIT_MAX_CHANGE, FIT_MAX_CHANGE))
        if value > 100 or value < -100:
            self._skip(field, "at_upper_limit" if value > 100 else "at_lower_limit")
        return int(round(np.clip(value, -100, 100)))

    def _hue(self):
        baseline = self._render(self.values, FIT_RESPONSE_EDGE)
        candidate = copy.deepcopy(self.values)
        for color in self.colors:
            # 与目标的差取同一尺寸的 self.stats；小图 baseline 只作求响应的起点
            level, row, target = self.stats["hues"][color], baseline["hues"][color], self.target["hues"][color]
            if color not in self.hue_colors or level["share"] < FIT_HUE_SHARE or row["share"] < FIT_HUE_SHARE:
                self._skip("color_mixer." + color, "insufficient_color_share")
                continue
            for part, metric in (("hue", "hab"), ("sat", "C"), ("lum", "L")):
                field = "color_mixer." + color + "." + part
                if self._locked(field):
                    self._skip(field, "locked")
                    continue
                if level.get(metric) is None or row.get(metric) is None or target.get(metric) is None:
                    self._skip(field, "missing_hue_angle")
                    continue
                value = self.values["color_mixer"][color][part]
                step = FIT_PERTURBATION if value + FIT_PERTURBATION <= 100 else -FIT_PERTURBATION
                probe = copy.deepcopy(self.values)
                probe["color_mixer"][color][part] = value + step
                measured = self._render(probe, FIT_RESPONSE_EDGE)["hues"][color]
                if measured["share"] < FIT_HUE_SHARE or measured.get(metric) is None:
                    self._skip(field, "insufficient_color_share")
                    continue
                response = measured[metric] - row[metric]
                difference = target[metric] - level[metric]
                if metric == "hab":
                    response, difference = _angle(response), _angle(difference)
                if abs(response) < FIT_RESPONSE_FLOORS[metric]:
                    self._skip(field, "no_response")
                    continue
                candidate["color_mixer"][color][part] = self._bounded(field, value, difference * step / response * self.strength)
        self._validate("hue", candidate)

    def _chroma(self):
        keys = [key for key in ("vibrance_amt", "saturation_amt") if not self._locked(key)]
        if not keys:
            self._skip("chroma", "locked")
            return
        baseline = self._render(self.values, FIT_RESPONSE_EDGE)
        points = ("p50", "p90")
        # 响应在小图上取两次渲染之差；与目标的差取同一尺寸的 self.stats
        reference = np.array([baseline["chroma_percentiles"][point] for point in points])
        current = np.array([self.stats["chroma_percentiles"][point] for point in points])
        target = np.array([self.target["chroma_percentiles"][point] for point in points])
        columns = []
        for key in keys:
            value = self.values[key]
            step = FIT_PERTURBATION if value + FIT_PERTURBATION <= 100 else -FIT_PERTURBATION
            probe = copy.deepcopy(self.values)
            probe[key] = value + step
            stats = self._render(probe, FIT_RESPONSE_EDGE)
            response = np.array([stats["chroma_percentiles"][point] for point in points]) - reference
            if np.max(np.abs(response)) < FIT_RESPONSE_FLOORS["C"]:
                self._skip(key, "no_response")
                continue
            columns.append((key, response / step))
        if not columns:
            return
        matrix = np.column_stack([column for _key, column in columns])
        if np.linalg.cond(matrix) > FIT_CONDITION_LIMIT:
            self._skip("chroma", "ill_conditioned")
            return
        delta = np.linalg.lstsq(matrix, (target - current) * self.strength, rcond=None)[0]
        candidate = copy.deepcopy(self.values)
        for (key, _column), change in zip(columns, delta):
            candidate[key] = self._bounded(key, self.values[key], change)
        self._validate("chroma", candidate)

    def fit(self, current, target, *, dimensions, colors=MIXER_CHANNELS, strength=FIT_DEFAULT_STRENGTH, preserve=()):
        self.renders = 0
        self.target, self.dimensions, self.colors = target, dimensions, colors
        self.strength, self.preserve = strength / 100, preserve
        self.values = {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version), **copy.deepcopy(current),
                       "curves": curve_channels(current.get("curves")),
                       "color_mixer": mixer_values(current.get("color_mixer"))}
        self.steps, self.skipped = [], []
        self.protected = []
        if not strength:
            return {"status": "no_fittable_dimensions", "values": {}, "steps": [],
                    "skipped": [{"field": "strength", "code": "zero_strength"}], "renders": 0}
        if target.get("status") != "available" or target.get("spec") != MEASUREMENT_SPEC or "histogram" not in target:
            raise ValueError("AI_FIT_MEASUREMENT")
        initial = copy.deepcopy(self.values)
        self.stats = self._render(self.values)
        self.split_segments = [segment for segment in ("shadows", "midtones", "highlights")
                               if self.stats["split_tone"][segment]["status"] != "missing"
                               and target["split_tone"][segment]["status"] != "missing"]
        self.hue_colors = [color for color in colors if self.stats["hues"][color]["share"] >= FIT_HUE_SHARE
                          and target["hues"][color]["share"] >= FIT_HUE_SHARE
                          and self.stats["hues"][color].get("hab") is not None and target["hues"][color].get("hab") is not None]
        before = self._residuals(self.stats)
        for dimension, action in (("tone", self._tone), ("split_tone", self._split_tone),
                                  ("hue", self._hue), ("chroma", self._chroma)):
            if dimension in dimensions:
                action()
                if dimension in self._errors(self._residuals(self.stats)):
                    self.protected.append(dimension)
        values = {key: value for key, value in self.values.items() if value != initial[key]}
        for key in ("curves", "color_mixer"):
            if key in values:
                values[key] = {name: value for name, value in values[key].items() if value != initial[key][name]}
        if "color_mixer" in values:
            values["color_mixer"] = {color: {part: value for part, value in parts.items()
                                            if value != initial["color_mixer"][color][part]}
                                     for color, parts in values["color_mixer"].items()}
        return {"status": "suggested" if values else "no_fittable_dimensions", "values": values,
                "before": before, "after": self._residuals(self.stats), "steps": self.steps, "skipped": self.skipped,
                "renders": self.renders, "validation_edge": FIT_VALIDATION_EDGE, "response_edge": FIT_RESPONSE_EDGE,
                "approximation": "Levels and validation use the measurement size; edge-256 renders only estimate parameter "
                                 "responses, where spatial effects may differ. "
                                 "Reference statistics mix scene content, lighting and grading. Split tone is weak evidence."}
