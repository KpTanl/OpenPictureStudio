"""Phase 2.5 — 默认 RAW 比色根因修正（E1）回归锚。

真机实测：旧默认路 `inv(cam_xyz)` 把蓝/绿/橙整片旋成品红、中性相机
像素→R≈2.9/G≈0.06/B≈3.4（ColorMatrix 单独取逆不是合法的 camera→XYZ）。
E1 = libraw 权威 `output_color=XYZ` → 我方 `_XYZ_D65_TO_SRGB`。本模块钉死
「品红 bug 不复发」的**离线**反命题 + libraw 路的 OPS 观感门控 + 真机 χ² 桥
（无 RAW 时 skip，精确 sign-off 仍走 RawTherapee 手动对拍）。

与 test_dcp_color_science.test_matrix_fallback_neutral_in_gray_out（中性→灰）
互补：此处专攻 hue 分布健全（有蓝/绿/橙、无品红堆积）。
"""

import glob
import os

import numpy as np
import pytest

from src.core import dcp
from src.core.dcp import matrix_fallback_to_linear_srgb
from src.core.photo_engine import (
    _dcp_opt_in,
    decode_raw_native,
    linear_to_srgb,
)

try:
    import rawpy  # type: ignore
except ImportError:
    rawpy = None

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_RAW_GLOBS = ("*.cr2", "*.cr3", "*.nef", "*.arw", "*.dng", "*.raf", "*.orf", "*.pef", "*.rw2")


def _find_raw_fixture():
    env = os.environ.get("OPS_RAW_FIXTURE")
    if env and os.path.isfile(env):
        return env
    for pat in _RAW_GLOBS:
        hits = glob.glob(os.path.join(_FIXTURE_DIR, pat))
        if hits:
            return hits[0]
    return None


# sRGB(linear)→XYZ(D65) = _XYZ_D65_TO_SRGB 的逆：用来从「期望的 sRGB 颜色」
# 反构 libraw 会交来的 XYZ，喂进 E1 尾段验证 hue 不被旋。
_SRGB_TO_XYZ_D65 = np.linalg.inv(dcp._XYZ_D65_TO_SRGB)


def _xyz_for(srgb_lin):
    """把期望 linear sRGB 颜色反构成 libraw XYZ(D65) 输入（1x1x3）。"""
    s = np.asarray(srgb_lin, dtype=np.float64)
    xyz = s @ _SRGB_TO_XYZ_D65.T
    return xyz.astype(np.float32).reshape(1, 1, 3)


def _is_magenta(rgb):
    """品红特征：R、B 高而 G 被压（旧路 bug 把蓝/绿旋成此态）。"""
    r, g, b = rgb
    return (r > g + 0.05) and (b > g + 0.05)


@pytest.mark.parametrize(
    "name,srgb_lin,argmax_ch",
    [
        ("blue", (0.06, 0.06, 0.70), 2),
        ("green", (0.06, 0.55, 0.06), 1),
        ("orange", (0.80, 0.32, 0.04), 0),
    ],
)
def test_e1_preserves_hue_no_magenta_rotation(name, srgb_lin, argmax_ch):
    """蓝/绿/橙过 E1 尾段后 hue 不旋（主通道仍是原主通道、绝不变品红）——
    旧路「蓝/绿/橙→品红」的直接反命题。"""
    xyz = _xyz_for(srgb_lin)
    out = matrix_fallback_to_linear_srgb(xyz, np.ones(3), None).reshape(3)
    # E1 尾段是精确线性变换 ⇒ 逐位还原期望颜色
    np.testing.assert_allclose(out, np.asarray(srgb_lin, np.float32), atol=1e-4)
    assert int(np.argmax(out)) == argmax_ch, f"{name} 主通道被旋"
    assert not _is_magenta(out), f"{name} 被旋成品红（旧路 bug 复发）"


def test_e1_hue_distribution_has_blue_green_orange_not_magenta():
    """一组覆盖蓝/绿/橙/中性的像素过 E1：主通道分布健全，品红占比≈0。"""
    palette = np.array([
        [0.06, 0.06, 0.70],   # blue
        [0.06, 0.55, 0.06],   # green
        [0.80, 0.32, 0.04],   # orange
        [0.30, 0.30, 0.30],   # neutral
    ], dtype=np.float64)
    xyz = (palette @ _SRGB_TO_XYZ_D65.T).astype(np.float32)[None, :, :]  # 1x4x3
    out = matrix_fallback_to_linear_srgb(xyz, np.ones(3), None).reshape(4, 3)
    chans = {int(np.argmax(out[i])) for i in range(3)}        # 前三非中性
    assert chans == {0, 1, 2}, "蓝/绿/橙主通道未各自保留"
    magenta = sum(_is_magenta(px) for px in out)
    assert magenta == 0, f"品红堆积 {magenta}/4（旧路 bug）"
    # 中性仍中性（spread≈0），不被旋
    assert (out[3].max() - out[3].min()) < 2e-3


def test_libraw_default_keeps_ops_base_s_gate_enabled():
    """默认 color_pipeline='libraw' 必须**启用** OPS 观感——
    若回归为禁用，OPS look 将永不生效。"""
    from src.core.looks import look_stages

    assert look_stages("ops_v2", "libraw")
    assert look_stages("ops_base", "libraw")


def test_dcp_opt_in_env_gate(monkeypatch):
    """_dcp_opt_in 仅由显式 OPS_DCP_PROFILE/_DIR 驱动（决定 decode 走 E1
    还是相机原生）；二者皆无 → False（默认 E1）。"""
    monkeypatch.delenv("OPS_DCP_PROFILE", raising=False)
    monkeypatch.delenv("OPS_DCP_DIR", raising=False)
    assert _dcp_opt_in() is False
    monkeypatch.setenv("OPS_DCP_PROFILE", "x.dcp")
    assert _dcp_opt_in() is True
    monkeypatch.delenv("OPS_DCP_PROFILE", raising=False)
    monkeypatch.setenv("OPS_DCP_DIR", "/some/dir")
    assert _dcp_opt_in() is True


# ----------------------- 真机 χ² 桥（无 RAW 时 skip；精确 sign-off 走 RT 手动对拍）
def test_e1_default_path_not_magenta_vs_libraw_reference():
    """有 RAW fixture 时：默认 E1 整链（decode→XYZ→sRGB→display）对 libraw
    自身 sRGB 参考——粗 χ² 不离谱 + 品红占比≈0。这是 CI 可跑的「bug 没复发」
    护栏；χ²≈0 的精确逐位 sign-off 仍由 RawTherapee 手动对拍。"""
    if rawpy is None:
        pytest.skip("rawpy 未安装：E1 真机桥需 rawpy + RAW 样张")
    fixture = _find_raw_fixture()
    if fixture is None:
        pytest.skip(
            "未找到 RAW fixture。E1 真机桥：置 OPS_RAW_FIXTURE=<raw> 或放 "
            "tests/fixtures/<raw> 后重跑（精确 χ²≈0 sign-off 走 RawTherapee 手动对拍）。"
        )

    with rawpy.imread(fixture) as raw:
        ref = raw.postprocess(
            use_camera_wb=True, output_color=rawpy.ColorSpace.sRGB,
            gamma=(2.4, 12.92), no_auto_bright=True, output_bps=16,
        ).astype(np.float32) / 65535.0

    # 默认 E1：decode(XYZ) → matrix_fallback(XYZ→linear sRGB) → display sRGB
    cam_rgb, meta = decode_raw_native(fixture, for_preview=False)   # for_dcp=False
    lin = matrix_fallback_to_linear_srgb(cam_rgb, np.asarray(meta.as_shot_neutral), None)
    e1 = np.clip(linear_to_srgb(np.clip(lin, 0.0, None)), 0.0, 1.0)

    # 决定性：两次 decode 完全一致（pro 确定性）
    cam2, _ = decode_raw_native(fixture, for_preview=False)
    np.testing.assert_array_equal(cam_rgb, cam2)

    # 粗对齐（非逐位；逐位是 RT 手动对拍）：均值/亮度量级接近、非整体旋色
    m = ~(np.isnan(e1).any(-1) | np.isnan(ref).any(-1))
    mae = float(np.abs(e1[m] - ref[m]).mean())
    assert mae < 0.06, f"E1 与 libraw 参考偏差过大 mae={mae:.4f}（疑比色回归）"

    # 同图 libraw 参考保留场景本身的品红，限制额外产生的偏色。
    px = e1[m].reshape(-1, 3)
    ref_px = ref[m].reshape(-1, 3)
    stride = max(1, len(px) // 20000)
    mag = np.mean([_is_magenta(p) for p in px[::stride]])
    ref_mag = np.mean([_is_magenta(p) for p in ref_px[::stride]])
    assert mag - ref_mag < 0.03, (
        f"额外品红占比 {mag - ref_mag:.2%}，E1={mag:.2%}，libraw={ref_mag:.2%}（疑偏色回归）"
    )
