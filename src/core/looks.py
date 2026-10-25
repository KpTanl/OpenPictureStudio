"""OPS 观感的唯一定义：各层的曲线与饱和表，以及每类源要经过哪几层。

两层结构：默认 libraw RAW 先经“出片底子”做成与相机直出同等的起点，再与已出片的
JPG、PNG 等共用同一层 OPS 风格。调风格只改 ``OPS_STYLE``；``RAW_BASE`` 是技术标定，
不承载风格。

每层在 display sRGB 上施加两步：等增益保色相的影调曲线（33 点，均匀 x∈[0,1]，单调，
f(0)=0），再按色相分 12 段（每 30°，自红起）缩放 chroma。强度在 0—1 之间与该步的
输入凸组合，两项都为 0 的层逐位跳过。
"""

from __future__ import annotations

from dataclasses import dataclass

from .tools.color import COLOR_LOOK_ALIASES, LEGACY_OPS_LOOK, OPS_LOOK, canonical_look

TONE_LUT_POINTS = 33
HUE_SAT_BINS = 12


@dataclass(frozen=True)
class LookStage:
    tone_lut: tuple[float, ...]
    hue_sat: tuple[float, ...]
    tone_strength: float = 1.0
    sat_strength: float = 1.0

    def __post_init__(self):
        if len(self.tone_lut) != TONE_LUT_POINTS or len(self.hue_sat) != HUE_SAT_BINS:
            raise ValueError("观感层的曲线须为 33 点、饱和表须为 12 段")

    @property
    def active(self) -> bool:
        return self.tone_strength > 0 or self.sat_strength > 0


# 旧版单层观感（ops_base）：RAW 裸图 → Adobe Color 一次拟合，出片与风格合在一起。
# 只服务 2026-10-13 之前保存的数据，常数不再修改。
LEGACY_OPS = LookStage(
    tone_lut=(
        0.000000, 0.035000, 0.070000, 0.112000, 0.158000, 0.210000, 0.268000,
        0.328000, 0.388000, 0.448000, 0.508000, 0.565000, 0.618000, 0.668000,
        0.714000, 0.756000, 0.795000, 0.830000, 0.860000, 0.885000, 0.904000,
        0.920000, 0.932000, 0.942000, 0.951000, 0.960000, 0.968000, 0.975000,
        0.982000, 0.988000, 0.993000, 0.997000, 1.000000,
    ),
    hue_sat=(
        1.274036, 1.152161, 1.172899, 1.233715, 1.164381, 1.067396,
        1.065992, 1.029065, 0.908224, 0.880727, 1.109801, 1.332270,
    ),
)


# 以下两层于 2026-10-15 标定，标定集为 14 张 NEF 与各自内嵌的全尺寸 JPEG。
# 出片底子：引擎默认 RAW 裸图 → 相机内嵌 JPEG。
# OPS 风格：底子之后的 RAW 与相机 JPEG 同时对准旧版单层观感施于 RAW 的画面，
# 即把现行风格从出片里剥离出来；两层合成与旧版单层 ΔE76 均值 0.30、p95 1.04。
RAW_BASE = LookStage(
    tone_lut=(
        0.000000, 0.028776, 0.058681, 0.099109, 0.149874, 0.214056, 0.287524,
        0.350161, 0.406273, 0.468511, 0.529423, 0.581885, 0.627987, 0.675842,
        0.725933, 0.770519, 0.810645, 0.844910, 0.868395, 0.884974, 0.899075,
        0.912308, 0.925132, 0.935985, 0.944123, 0.951108, 0.958092, 0.965077,
        0.972062, 0.979046, 0.986031, 0.993015, 1.000000,
    ),
    hue_sat=(
        1.418728, 1.273823, 1.320443, 1.467065, 1.441712, 1.263508,
        1.194026, 1.180358, 1.064055, 1.026049, 1.270783, 1.498943,
    ),
)

# 三类格式共用；调 OPS 风格只改这里。
OPS_STYLE = LookStage(
    tone_lut=(
        0.000000, 0.038905, 0.074576, 0.106927, 0.136370, 0.163811, 0.190540,
        0.216696, 0.241683, 0.266605, 0.293424, 0.323120, 0.355184, 0.387078,
        0.417583, 0.447846, 0.478391, 0.509556, 0.542654, 0.577985, 0.613684,
        0.646648, 0.675919, 0.704031, 0.732775, 0.762260, 0.792462, 0.823401,
        0.857654, 0.896875, 0.935382, 0.968825, 1.000000,
    ),
    hue_sat=(
        0.894406, 0.893831, 0.874973, 0.847233, 0.836090, 0.855437,
        0.876442, 0.869928, 0.851659, 0.852230, 0.868625, 0.883536,
    ),
)


_STAGES: dict[str, dict[str, tuple[LookStage, ...]]] = {
    LEGACY_OPS_LOOK: {"libraw": (LEGACY_OPS,)},
    OPS_LOOK: {"libraw": (RAW_BASE, OPS_STYLE), "srgb-direct": (OPS_STYLE,)},
}

LOOKS = frozenset(_STAGES) | {"none"} | COLOR_LOOK_ALIASES.keys()


def look_stages(look: str, color_pipeline: str) -> tuple[LookStage, ...]:
    """这张图要依次经过的观感层；DCP 与其他源不施加 OPS 观感。"""
    return tuple(stage for stage in _STAGES.get(canonical_look(look), {}).get(color_pipeline, ()) if stage.active)
