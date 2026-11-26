"""
调色辅助模块 — 提供直方图统计、曲线 LUT 生成、自动参数分析与内置滤镜预设表。
"""

from __future__ import annotations

import math
import logging
from typing import Dict, Tuple

import numpy as np
from PIL import Image, ImageChops

# 直方图统计的取样规模：图像像素数超过它时按等间隔取样
HISTOGRAM_SAMPLE_PIXELS = 200_000

logger = logging.getLogger(__name__)


class ColorAdjuster:
    """提供曲线 LUT、直方图统计与自动参数分析的色彩辅助工具。"""

    # ------------------------------------------------------------------ Curves adjustment

    @staticmethod
    def _monotone_cubic_hermite(points: list[Tuple[int, int]]) -> list[int]:
        """Fritsch-Carlson 单调三次 Hermite 插值，从控制点生成 256 值 LUT。

        保证单调性，避免过冲振荡。
        """
        pts = sorted(points, key=lambda p: p[0])
        if len(pts) < 2:
            return list(range(256))

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        n = len(pts)

        deltas = []
        for i in range(n - 1):
            dx = xs[i + 1] - xs[i]
            if dx == 0:
                deltas.append(0.0)
            else:
                deltas.append((ys[i + 1] - ys[i]) / dx)

        m = [0.0] * n
        m[0] = deltas[0] if n > 1 else 0.0
        m[n - 1] = deltas[n - 2] if n > 1 else 0.0
        for i in range(1, n - 1):
            if deltas[i - 1] * deltas[i] <= 0:
                m[i] = 0.0
            else:
                m[i] = (deltas[i - 1] + deltas[i]) / 2.0

        for i in range(n - 1):
            if abs(deltas[i]) < 1e-12:
                m[i] = 0.0
                m[i + 1] = 0.0
            else:
                alpha_i = m[i] / deltas[i]
                beta_i = m[i + 1] / deltas[i]
                if alpha_i < 0:
                    m[i] = 0.0
                if beta_i < 0:
                    m[i + 1] = 0.0
                tau = alpha_i * alpha_i + beta_i * beta_i
                if tau > 9.0:
                    scale = 3.0 / math.sqrt(tau)
                    m[i] = scale * alpha_i * deltas[i]
                    m[i + 1] = scale * beta_i * deltas[i]

        lut = [0] * 256
        seg = 0
        for i in range(256):
            x = i
            while seg < n - 2 and xs[seg + 1] < x:
                seg += 1
            x0 = xs[seg]
            x1 = xs[seg + 1]
            y0 = ys[seg]
            y1 = ys[seg + 1]
            dx = x1 - x0
            if dx < 1e-12:
                lut[i] = max(0, min(255, y0))
                continue
            t = (x - x0) / dx
            t2 = t * t
            t3 = t2 * t
            h00 = 2 * t3 - 3 * t2 + 1
            h10 = t3 - 2 * t2 + t
            h01 = -2 * t3 + 3 * t2
            h11 = t3 - t2
            val = h00 * y0 + h10 * dx * m[seg] + h01 * y1 + h11 * dx * m[seg + 1]
            lut[i] = max(0, min(255, int(round(val))))
        return lut

    @staticmethod
    def _build_curve_lut(points: list[Tuple[int, int]]) -> list[int]:
        """从控制点列表生成 256 值曲线 LUT。"""
        if not points or (len(points) == 2 and points[0] == (0, 0) and points[1] == (255, 255)):
            return list(range(256))
        return ColorAdjuster._monotone_cubic_hermite(points)

    # ------------------------------------------------------------------ Histogram

    @staticmethod
    def compute_histogram(image: Image.Image) -> dict:
        """计算图片的 R/G/B/亮度直方图数据，用于 UI 绘制。

        返回:
            {"r": list[int], "g": list[int], "b": list[int],
             "luma": list[int], "max_count": int}
        """
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        img_rgb = image.convert("RGB") if image.mode == "RGBA" else image

        w, h = img_rgb.size
        if w * h > HISTOGRAM_SAMPLE_PIXELS:
            step = int(math.ceil(math.sqrt((w * h) / HISTOGRAM_SAMPLE_PIXELS)))
            arr = np.asarray(img_rgb)[::step, ::step]
        else:
            arr = np.asarray(img_rgb)

        r_flat = arr[..., 0].reshape(-1)
        g_flat = arr[..., 1].reshape(-1)
        b_flat = arr[..., 2].reshape(-1)
        luma_flat = np.clip(
            np.rint(0.2126 * r_flat.astype(np.float32) + 0.7152 * g_flat.astype(np.float32) + 0.0722 * b_flat.astype(np.float32)),
            0,
            255,
        ).astype(np.uint8)

        r_hist = np.bincount(r_flat, minlength=256).tolist()
        g_hist = np.bincount(g_flat, minlength=256).tolist()
        b_hist = np.bincount(b_flat, minlength=256).tolist()
        luma = np.bincount(luma_flat, minlength=256).tolist()

        max_count = max(max(r_hist), max(g_hist), max(b_hist), max(luma), 1)
        return {"r": r_hist, "g": g_hist, "b": b_hist, "luma": luma, "max_count": max_count}

    # ------------------------------------------------------------------ Auto

    def analyze_auto_params(self, image: Image.Image) -> Dict[str, float]:
        """对外暴露自动分析接口，返回 UI 滑块原始值字典（直接回写滑块）。"""
        return self._analyze_auto_params(image)

    @staticmethod
    def _auto_white_balance(sample: Image.Image, visibility_mask: Image.Image | None = None,
                            visible_count: int | None = None) -> Tuple[int, int]:
        """从近白高光像素估算白平衡偏移，返回 (temperature, tint) UI 滑块值。

        用 white-patch 假设而非灰世界：画面里最亮的非过曝中性面（白裙/灰墙
        受光面）颜色最接近光源真实色。旧实现采样 luma∈[40,220]，把开放阴影
        也算进来——阴影被蓝天补光本就偏蓝，灰世界据此把它当色偏、反向加暖，
        于是中性日光人像被整体推黄。现在只取高光白片、用中位数稳健估计
        （少量漏过饱和度掩膜的彩色像素拽不动中位数），并在白参考不足或色偏
        微弱时返回 0/0——对齐 Lightroom Auto 无把握时保持"原照设置"。
        """
        if sample.mode != "RGB":
            sample = sample.convert("RGB")

        arr = np.asarray(sample, dtype=np.float32)
        r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        mx = np.maximum(np.maximum(r, g), b)
        mn = np.minimum(np.minimum(r, g), b)
        sat = np.where(mx > 1e-3, (mx - mn) / np.maximum(mx, 1e-3), 0.0)

        # white-patch：亮但不过曝、低饱和的中性受光面
        cand = (luma >= 210.0) & (luma <= 245.0) & (sat <= 0.12)
        if visibility_mask is not None:
            cand &= np.asarray(visibility_mask, dtype=np.uint8) > 0

        total = visible_count if visible_count is not None else arr.shape[0] * arr.shape[1]
        n = int(cand.sum())
        if n < max(64, int(total * 0.02)):
            return (0, 0)  # 无可信白参考 → 不动，保持原片

        px = arr[cand]
        med_r = float(np.median(px[:, 0]))
        med_g = float(np.median(px[:, 1]))
        med_b = float(np.median(px[:, 2]))
        if (med_r + med_g + med_b) < 30:
            return (0, 0)

        rb_diff = med_r - med_b
        g_diff = med_g - (med_r + med_b) / 2.0

        # adjust_temperature 在 ±100 时 R/B 各偏移 ±25 → 总差异 50；故 ±50 R-B 偏差对应满量程
        # adjust_tint 在 ±100 时 G 偏移 ±15、R/B 反向偏移 ±10 → "G 相对中性"差异 ±25 对应满量程
        temp_raw = -rb_diff / 50.0 * 100.0
        tint_raw = g_diff / 25.0 * 100.0

        def _soft_deadzone(v: float, dz: float) -> float:
            """平滑死区：|v|<dz 归零，其余从 0 起连续上升（无硬阶跃）。"""
            if v > dz:
                return v - dz
            if v < -dz:
                return v + dz
            return 0.0

        temperature = _soft_deadzone(temp_raw * 0.5, 8.0)
        tint = _soft_deadzone(tint_raw * 0.5, 8.0)
        # 加暖方向额外保守：蓝阴影/冷光人像被误判加暖是经典翻车点。
        if temperature > 0:
            temperature *= 0.6

        temperature = max(-60, min(60, int(round(temperature))))
        tint = max(-60, min(60, int(round(tint))))
        return (temperature, tint)

    @staticmethod
    def _center_weighted_luma_hist(luma: Image.Image, mask: Image.Image | None = None,
                                   zones: int = 8, edge_weight: float = 0.3) -> list[float]:
        """中心加权亮度直方图（相机"中央重点测光"思路）。

        人像/主体多居中，纯全局中位数会被大面积暗背景（如本场景里
        店铺内部那种阴影区）带偏，导致自动曝光误判欠曝、提亮过度而爆白。
        这里用 radial_gradient 把画面切成 ``zones`` 个同心环带，权重从
        中心 1.0 线性衰减到边缘 ``edge_weight``，逐环带取直方图后加权累加。
        """
        grad = Image.radial_gradient("L").resize(luma.size)
        hist = [0.0] * 256
        step = 256.0 / zones
        for k in range(zones):
            lo = int(k * step)
            hi = 256 if k == zones - 1 else int((k + 1) * step)
            ring = grad.point(lambda v, lo=lo, hi=hi: 255 if lo <= v < hi else 0)
            if mask is not None:
                ring = ImageChops.multiply(ring, mask)
            ring_hist = luma.histogram(mask=ring)
            t = (k + 0.5) / zones
            w = 1.0 - (1.0 - edge_weight) * t
            for i in range(256):
                hist[i] += ring_hist[i] * w
        return hist

    @staticmethod
    def _analyze_auto_params(image: Image.Image) -> Dict[str, float]:
        """智能 Auto：白平衡 + 场景识别 + 全局调整，返回 UI 滑块原始值字典。

        在缩略图（≤512px）上做统计以提速，然后按场景特征自适应:
          - 白平衡: 受限灰世界估算 temperature/tint
          - 曝光: 把亮度中位数推到 18% 灰 ≈ 118
          - 白/黑色阶: 把 p99/p1 推到目标，扩展动态范围
          - 高光/阴影: 检测裁切并回收
          - 对比度: 动态范围窄时增强
          - 鲜艳度/饱和度: 偏灰图加 vibrance（对皮肤友好）
          - 去朦胧/清晰度: IQR + 动态范围窄 = 雾蒙场景，加 dehaze + clarity
        """
        if image.mode not in ("RGB", "RGBA", "L"):
            image = image.convert("RGB")

        sample = image
        if max(sample.width, sample.height) > 512:
            sample = sample.copy()
            sample.thumbnail((512, 512), Image.Resampling.LANCZOS)

        if sample.mode == "RGBA":
            alpha = sample.split()[3]
            visibility_mask = alpha.point(lambda v: 255 if v >= 8 else 0)
            sample_rgb = sample.convert("RGB")
        else:
            visibility_mask = None
            sample_rgb = sample.convert("RGB") if sample.mode != "RGB" else sample

        luma_img = sample_rgb.convert("L")
        luma_hist = luma_img.histogram(mask=visibility_mask)
        pixel_count = max(1, sum(luma_hist))
        if pixel_count <= 1:
            return {
                "temperature": 0, "tint": 0, "exposure_amt": 0, "contrast_amt": 0,
                "highlights": 0, "shadows": 0, "whites": 0, "blacks": 0,
                "texture_amt": 0, "clarity_amt": 0, "dehaze_amt": 0,
                "vibrance_amt": 0, "saturation_amt": 0, "curves": None,
            }

        def _percentile(pct: float) -> int:
            target = pixel_count * pct
            cum = 0
            for i, c in enumerate(luma_hist):
                cum += c
                if cum >= target:
                    return i
            return 255

        p1, p5, p25 = _percentile(0.01), _percentile(0.05), _percentile(0.25)
        p75, p95, p99 = _percentile(0.75), _percentile(0.95), _percentile(0.99)

        # 中心加权测光：曝光用的中位数按主体（画面中心权重高）统计，
        # 避免"大面积暗背景 + 居中亮主体"场景被全局中位数误判为欠曝、
        # 进而过度提亮。p1/p95/p99 等仍用全局，色阶与高光余量按整图算。
        cw_hist = ColorAdjuster._center_weighted_luma_hist(luma_img, visibility_mask)
        cw_total = sum(cw_hist) or 1.0
        cw_cum, median = 0.0, 255
        for _i, _c in enumerate(cw_hist):
            cw_cum += _c
            if cw_cum >= cw_total * 0.50:
                median = _i
                break

        dynamic_range = p95 - p5
        iqr = p75 - p25

        # 亮度百分位无法发现少量高饱和/高亮通道（例如灯、星点、红色主体），
        # 额外检查 RGB 最大通道的 p99，给曝光和白场共用更可靠的高光余量。
        rgb_arr = np.asarray(sample_rgb, dtype=np.float32)
        channel_max = np.max(rgb_arr, axis=-1)
        if visibility_mask is not None:
            visible = np.asarray(visibility_mask, dtype=np.uint8) > 0
            channel_max = channel_max[visible]
        channel_max_p99 = float(np.percentile(channel_max, 99.0)) if channel_max.size else 0.0

        # p5~p95 + IQR 的双重约束继续作为纯色/平图保护；小主体构图不应
        # 因少量极值被当成高反差场景，避免误触发负曝光和强对比。
        is_flat = dynamic_range < 6 and iqr < 3
        temperature, tint = ColorAdjuster._auto_white_balance(sample_rgb, visibility_mask, pixel_count)

        if is_flat:
            return {
                # 若存在可信白参考，平图仍可校正明显色偏；其余影调保持原片。
                "temperature": temperature,
                "tint": tint,
                "exposure_amt": 0,
                "contrast_amt": 0,
                "highlights": 0,
                "shadows": 0,
                "whites": 0,
                "blacks": 0,
                "texture_amt": 0,
                "clarity_amt": 0,
                "dehaze_amt": 0,
                "vibrance_amt": 0,
                "saturation_amt": 0,
                "curves": None,
            }

        # 计算顺序：先定高光/阴影（压缩动态范围的两个动作），
        # 再让曝光/黑色/对比度针对它们做"反向补偿"——这是 Lightroom
        # 不发灰的关键：压扁的同时把通透感找回来。

        # ---- 高光裁切回收 ----
        near_white_ratio = sum(luma_hist[245:256]) / pixel_count
        if near_white_ratio > 0.02:
            # Auto 只做温和回收，避免把本来有意保留的高光压成灰。
            highlights = -int(round(min(45, near_white_ratio * 720)))
        else:
            highlights = 0

        # ---- 阴影压死提亮 ----
        # 仅按 near-black 占比会把大面积黑背景误判成"需要提亮的阴影"。
        # 用 p1→p25 的跨度确认暗部确实有可恢复层次，并设置较低软上限。
        near_black_ratio = sum(luma_hist[:12]) / pixel_count
        shadow_span = max(0, p25 - p1)
        if near_black_ratio > 0.04 and shadow_span >= 8:
            span_confidence = min(1.0, shadow_span / 32.0)
            shadows = int(round(min(36, near_black_ratio * 300 * span_confidence)))
        else:
            shadows = 0

        # ---- 曝光 ----
        # 中心加权中位数只做轻量校正。固定推到 118 会让低调/高反差照片
        # 失去氛围，因此根据高光位置把目标收敛到 100~112。
        if p95 >= 238:
            target_median = 100
        elif p95 >= 220:
            target_median = 104
        elif dynamic_range < 90:
            target_median = 112
        else:
            target_median = 108
        if channel_max_p99 >= 250.0:
            target_median = min(target_median, 104)
        desired_ev = math.log2(target_median / max(1, median))
        # 极端高调/低调照片保留原片氛围，只做半量校正；整体 EV 软上限
        # 收到 ±0.65，避免 Auto 变成一次强曝光推拉。
        if median < 72:
            desired_ev *= 0.55
        elif median > 184:
            desired_ev *= 0.45
        desired_ev = max(-0.65, min(0.65, desired_ev))
        if desired_ev > 0:
            luma_headroom_ev = math.log2(238 / max(1, p99))
            channel_headroom_ev = math.log2(250 / max(1.0, channel_max_p99))
            headroom_ev = min(luma_headroom_ev, channel_headroom_ev)
            # 高光已接近上限时不再靠曝光硬推；高光回收只允许极小余量。
            if headroom_ev <= 0:
                desired_ev = 0.0
            else:
                desired_ev = min(desired_ev, headroom_ev + (0.06 if highlights < 0 else 0.0))
        exposure_amt = int(round(desired_ev * 100)) if abs(desired_ev) >= 0.04 else 0

        # ---- 白色色阶 ----
        # 先估算曝光后的 p99，再补白场，避免与 exposure 重复提亮。
        white_target = 248
        predicted_p99 = min(
            255.0,
            max(float(p99), channel_max_p99) * (2.0 ** max(0.0, desired_ev)),
        )
        white_gap = white_target - predicted_p99
        if dynamic_range >= 24 and white_gap > 8:
            whites = int(round(min(28, white_gap * 0.35)))
        elif p99 > 252:
            whites = -int(round(min(28, (p99 - 252) * 1.1)))
        else:
            whites = 0
        whites = max(-100, min(100, whites))

        # ---- 黑色色阶（含"反向补偿"）----
        # 1) 黑场被抬高 → 压回；纯黑已到位则不动（旧实现这里会反向抬黑，
        #    与提阴影叠加把暗部压成灰）。
        black_target = 5
        if dynamic_range >= 24 and p1 > black_target + 3 and p25 < 150:
            blacks = -int(round(min(32, (p1 - black_target) * 0.55)))
        else:
            blacks = 0
        # 2) 关键补偿：提阴影会压扁暗部层次，按比例下压黑场把深度拉回，
        #    对齐 Lightroom（阴影 +48 → 黑色 ≈ -10）。
        blacks += -int(round(shadows * 0.12))
        blacks = max(-40, min(40, blacks))

        # ---- 对比度（含"反向补偿"）----
        if dynamic_range < 180:
            contrast_amt = int(round((180 - dynamic_range) / 180.0 * 24))
        else:
            contrast_amt = 0
        # 压缩动态范围（压高光 + 提阴影）会发闷，补一点对比把反差找回，
        # 对齐 Lightroom（高光 -55 + 阴影 +48 → 对比度 ≈ +6）。
        compression = max(0, -highlights) + max(0, shadows)
        contrast_amt += int(round(compression * 0.03))
        if median < 65 or median > 190:
            contrast_amt = min(contrast_amt, 12)
        contrast_amt = max(-20, min(32, contrast_amt))

        # ---- 鲜艳度 / 饱和度 ----
        sat_hist = sample_rgb.convert("HSV").split()[1].histogram(mask=visibility_mask)
        avg_sat = sum(i * v for i, v in enumerate(sat_hist)) / pixel_count / 255.0
        if median < 65 or median > 190:
            # 极端低调/高调的低饱和噪声不做颜色放大，避免夜景出现彩色噪点。
            vibrance_amt = 0
            saturation_amt = 0
        elif avg_sat < 0.16:
            # 偏灰: 只给轻量 vibrance，避免把噪声和彩色边缘一起放大。
            vibrance_amt = int(round(min(20, max(8, (0.20 - avg_sat) * 120))))
            saturation_amt = 0
        elif avg_sat < 0.30:
            vibrance_amt = 8
            saturation_amt = 0
        elif avg_sat < 0.42:
            vibrance_amt = 4
            saturation_amt = 0
        else:
            # 已经鲜艳的照片保持原色，不再强行套"流行色"。
            vibrance_amt = 0
            saturation_amt = 0

        # ---- 去朦胧 / 清晰度: 雾蒙场景检测 ----
        # 仅凭 IQR/动态范围会把高饱和插画、渐变和夜景误判为雾蒙。
        is_hazy = (
            avg_sat < 0.32
            and 25 <= iqr < 52
            and 45 <= dynamic_range < 145
            and near_black_ratio < 0.18
        )
        if is_hazy:
            dehaze_amt = 15
            clarity_amt = 8
        else:
            dehaze_amt = 0
            clarity_amt = 0

        return {
            "temperature":    temperature,
            "tint":           tint,
            "exposure_amt":   exposure_amt,
            "contrast_amt":   contrast_amt,
            "highlights":     highlights,
            "shadows":        shadows,
            "whites":         whites,
            "blacks":         blacks,
            "texture_amt":    0,
            "clarity_amt":    clarity_amt,
            "dehaze_amt":     dehaze_amt,
            "vibrance_amt":   vibrance_amt,
            "saturation_amt": saturation_amt,
            "curves":         None,
        }


class FilterPreset:
    """滤镜预设管理器，内置常用风格。"""

    PRESETS: Dict[str, Dict[str, float]] = {
        "grayscale": {
            "brightness": 1.0,
            "contrast": 1.1,
            "saturation": 0.0,
            "sharpness": 1.0,
            "exposure": 0,
            "temperature": 0,
            "tint": 0,
        },
        "vintage": {
            "brightness": 1.05,
            "contrast": 0.9,
            "saturation": 0.7,
            "sharpness": 0.8,
            "exposure": 0,
            "temperature": 30,
            "tint": 10,
        },
        "fresh": {
            "brightness": 1.1,
            "contrast": 1.05,
            "saturation": 1.2,
            "sharpness": 1.1,
            "exposure": 0,
            "temperature": -15,
            "tint": 0,
        },
        "warm": {
            "brightness": 1.05,
            "contrast": 1.0,
            "saturation": 1.1,
            "sharpness": 1.0,
            "exposure": 0,
            "temperature": 40,
            "tint": 5,
        },
        "cool": {
            "brightness": 1.0,
            "contrast": 1.05,
            "saturation": 1.05,
            "sharpness": 1.0,
            "exposure": 0,
            "temperature": -30,
            "tint": -5,
        },
        "soft": {
            "brightness": 1.05,
            "contrast": 0.9,
            "saturation": 0.95,
            "sharpness": 0.5,
            "exposure": 0,
            "temperature": 10,
            "tint": 0,
        },
        "vivid": {
            "brightness": 1.0,
            "contrast": 1.3,
            "saturation": 1.6,
            "sharpness": 1.3,
            "exposure": 0,
            "temperature": 0,
            "tint": 0,
        },
        "dramatic": {
            "brightness": 0.9,
            "contrast": 1.5,
            "saturation": 1.1,
            "sharpness": 1.3,
            "exposure": 0,
            "temperature": 10,
            "tint": 0,
        },
        "hdr": {
            "brightness": 1.1,
            "contrast": 1.4,
            "saturation": 1.3,
            "sharpness": 1.2,
            "exposure": 0,
            "temperature": 0,
            "tint": 0,
        },
        "film": {
            "brightness": 0.95,
            "contrast": 1.1,
            "saturation": 0.85,
            "sharpness": 1.0,
            "exposure": 0,
            "temperature": 15,
            "tint": 5,
        },
        "skin_beauty": {
            "brightness": 1.08,
            "contrast": 0.95,
            "saturation": 0.9,
            "sharpness": 0.7,
            "exposure": 0,
            "temperature": 10,
            "tint": 0,
        },
    }

    PRESET_LABELS: Dict[str, str] = {
        "grayscale": "Monochrome",
        "vintage": "Retro",
        "fresh": "Fresh",
        "warm": "Warm",
        "cool": "Cool",
        "soft": "Soft Light",
        "vivid": "Vivid",
        "dramatic": "Dramatic",
        "hdr": "HDR",
        "film": "Film",
        "skin_beauty": "Skin Beauty",
    }

    PRESET_I18N_KEYS: Dict[str, str] = {
        "grayscale": "color_adjust.preset.monochrome",
        "vintage": "color_adjust.preset.retro",
        "fresh": "color_adjust.preset.fresh",
        "warm": "color_adjust.preset.warm",
        "cool": "color_adjust.preset.cool",
        "soft": "color_adjust.preset.soft_light",
        "vivid": "color_adjust.preset.vivid",
        "dramatic": "color_adjust.preset.dramatic",
        "hdr": "color_adjust.preset.hdr",
        "film": "color_adjust.preset.film",
        "skin_beauty": "color_adjust.preset.skin_beauty",
    }

    _DEFAULT_PARAMS: Dict[str, float] = {
        "brightness": 1.0,
        "contrast": 1.0,
        "saturation": 1.0,
        "sharpness": 1.0,
        "exposure": 0,
        "temperature": 0,
        "tint": 0,
    }

    @classmethod
    def get_params(cls, preset_name: str) -> Dict[str, float]:
        """返回指定预设的参数字典（用于回写到 UI 滑块）。"""
        return cls.PRESETS.get(preset_name, {}).copy()

    @classmethod
    def preset_names(cls) -> list[str]:
        """返回所有预设名称列表。"""
        return list(cls.PRESETS.keys())
