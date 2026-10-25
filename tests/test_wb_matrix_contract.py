"""WB↔矩阵契约对拍（白平衡只施加一次）。

默认 RAW 路 = E1（libraw 权威 camera→XYZ(D65) + libraw 一次性 WB）；
旧的「WB 不在 rawpy 烘焙」契约**只适用于冻结 DCP opt-in 路**。

两部分：
  1. **离线契约断言**（无需 RAW/RT）：分别锁死
     - for_dcp=False（默认 E1）：output_color=XYZ、use_camera_wb=True；
     - for_dcp=True （冻结 DCP）：output_color=raw、use_camera_wb=False、
       user_wb 全 1（WB 留 DCP 矩阵链一次性施加）。
     共有：gamma=(1,1)、no_auto_bright=True、16-bit（预览仅 8-bit/half）。
  2. **默认路 RT 对拍 dumper**（需本机环境）：导出默认 E1 解码 +
     元数据，供与 RawTherapee「同条件」对拍——这是**默认路自己的比色
     sign-off**，DCP 路的对拍结果不能代替它。
     无 fixture 时 skip 并打印手动步骤。

设置 fixture：环境变量 OPS_RAW_FIXTURE 指向一个 RAW 文件，或放
tests/fixtures/ 下任一 .cr2/.nef/.arw/.dng/.raf/.orf/.pef/.rw2。
"""

import glob
import json
import os

import numpy as np
import pytest

from src.core.photo_engine import (
    RawDecodeMeta,
    decode_raw_native,
    _raw_postprocess_kwargs,
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


# --------------------------------------------------------- 1. 离线契约断言
def test_postprocess_kwargs_default_e1_path():
    """默认 E1：libraw 一次性做 WB + 权威 camera→XYZ；旧「WB 不烘焙」
    契约**不**再约束默认路（WB 只做一次，没有双重白平衡）。"""
    k = _raw_postprocess_kwargs(for_preview=False)            # for_dcp 默认 False
    assert k["gamma"] == (1, 1), "必须线性输出（关 gamma）"
    assert k["no_auto_bright"] is True, "关自动提亮以保高光余量"
    assert k["use_camera_wb"] is True, "E1：libraw 一次性做 WB（= as-shot）"
    assert "user_wb" not in k, "E1 不强制单位增益（WB 交 libraw）"
    assert k["half_size"] is False


def test_postprocess_kwargs_frozen_dcp_path_keeps_wb_contract():
    """旧「WB 不在 rawpy 烘焙」契约仅约束冻结 DCP opt-in 路。"""
    k = _raw_postprocess_kwargs(for_preview=False, for_dcp=True)
    assert k["gamma"] == (1, 1)
    assert k["no_auto_bright"] is True
    assert k["use_camera_wb"] is False, "冻结 DCP 路：WB 不在 rawpy 做（留矩阵链）"
    assert tuple(k["user_wb"]) == (1.0, 1.0, 1.0, 1.0), "显式单位增益，杜绝隐式 WB"


@pytest.mark.skipif(rawpy is None, reason="rawpy 未安装")
def test_postprocess_kwargs_color_and_depth():
    e1 = _raw_postprocess_kwargs(for_preview=False)
    e1_prev = _raw_postprocess_kwargs(for_preview=True)
    frozen = _raw_postprocess_kwargs(for_preview=False, for_dcp=True)
    assert e1["output_color"] == rawpy.ColorSpace.XYZ, "默认 E1：libraw 权威 camera→XYZ"
    assert frozen["output_color"] == rawpy.ColorSpace.raw, "冻结 DCP：相机原生喂 ForwardMatrix"
    assert e1["output_bps"] == 16, "导出走 16-bit"
    # 预览路径降尺寸但保持 16-bit 高位深，保留真实高光 Headroom
    assert e1_prev["output_bps"] == 16 and e1_prev["half_size"] is True
    assert e1_prev["no_auto_bright"] is True and e1_prev["use_camera_wb"] is True


# --------------------------------------- 2. RT 数值对拍 dumper（需用户环境）
def test_decode_raw_native_dump_for_rawtherapee_comparison(tmp_path):
    fixture = _find_raw_fixture()
    if rawpy is None:
        pytest.skip("rawpy 未安装：默认路 RT 对拍需 rawpy + RawTherapee + RAW 样张")
    if fixture is None:
        pytest.skip(
            "未找到 RAW fixture。默认 E1 路自己的比色 sign-off 需本机环境：\n"
            "  1) 置 OPS_RAW_FIXTURE=<raw路径> 或放 tests/fixtures/<raw>\n"
            "  2) 重跑本测试 → 生成 *_native.npy（默认 E1 解码 = XYZ(D65)）+ *_meta.json\n"
            "  3) RawTherapee 同条件（Camera/as-shot WB、线性、无 tone）导出对拍\n"
            "  4) 合格标准：通道增益 ±1%、ΔE2000 均值/中位/P95 ≤ 2.3/1.8/6.0、24×/48×/96× 池化 ΔE 单调递减"
        )

    pixels, meta = decode_raw_native(fixture, for_preview=False)   # 默认 E1（for_dcp=False）

    # 基本不变量（契约的客观部分，真机 RT 对拍是后续手动步骤）
    assert pixels.dtype == np.float32
    assert pixels.ndim == 3 and pixels.shape[2] == 3
    assert 0.0 <= float(pixels.min()) and float(pixels.max()) <= 1.0 + 1e-6
    assert isinstance(meta, RawDecodeMeta)
    assert len(meta.camera_whitebalance) >= 3
    # 默认 E1：libraw 一次性做 WB（无双重）→ use_camera_wb=True
    assert meta.postprocess_kwargs["use_camera_wb"] is True

    out_dir = tmp_path
    base = os.path.splitext(os.path.basename(fixture))[0]
    # 16-bit TIFF，PIL 单通道 I;16 拼不了 RGB → 存 npy（无损）+ 8-bit 预览 TIFF
    np.save(out_dir / f"{base}_native.npy", pixels)
    preview = (np.clip(pixels, 0, 1) * 255).round().astype(np.uint8)
    from PIL import Image

    Image.fromarray(preview, "RGB").save(out_dir / f"{base}_native_preview.tiff")
    with open(out_dir / f"{base}_meta.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "camera_model": meta.camera_model,
                "camera_whitebalance": meta.camera_whitebalance,
                "daylight_whitebalance": meta.daylight_whitebalance,
                "black_level": meta.black_level,
                "white_level": meta.white_level,
                "postprocess_kwargs": {
                    k: (list(v) if isinstance(v, tuple) else v)
                    for k, v in meta.postprocess_kwargs.items()
                    if k != "output_color"
                },
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        f"\n[默认 E1 dump] {meta.camera_model}\n"
        f"  pixels(XYZ→sRGB 前的 libraw XYZ(D65)): {out_dir / (base + '_native.npy')}\n"
        f"  meta:   {out_dir / (base + '_meta.json')}\n"
        f"  → RawTherapee 同条件对拍，合格标准：通道增益 ±1%、ΔE2000 均值/中位/P95 ≤ 2.3/1.8/6.0、24×/48×/96× 池化 ΔE 单调递减"
    )
