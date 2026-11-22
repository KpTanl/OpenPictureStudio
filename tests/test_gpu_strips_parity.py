"""真机 GPU 整幅与分条带逐像素一致回归测试（仅显式开启环境变量时执行）。"""

import os
from time import monotonic, sleep

import numpy as np
from PIL import Image
import pytest

from src.application.core_api import CoreAPI
from src.application.gpu_strips import StripPlan
from src.application.services import ExportRequest
from src.core.contracts import WorkflowGraph, WorkflowNode
from tests.support import wait_for_model_discovery

pytestmark = pytest.mark.skipif(
    os.environ.get("OPS_RUN_GPU_TESTS") != "1",
    reason="仅在设置 OPS_RUN_GPU_TESTS=1 的本机 GPU 环境下执行",
)


class _CappedDevice:
    """限制单块缓冲上限以在中小尺寸图上触发真实 gpu_strips，不改动生产代码。"""

    def __init__(self, wrapped, max_binding_size):
        self._wrapped = wrapped
        self._max_binding_size = max_binding_size

    def info(self):
        info = self._wrapped.info()
        info["max_binding_size"] = min(info["max_binding_size"], self._max_binding_size)
        return info

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def _synthetic_png(path, width, height, *, with_alpha):
    y, x = np.mgrid[:height, :width]
    r = np.clip(np.rint(127.5 + 110.0 * np.sin(x * 0.031 + y * 0.017)), 0, 255).astype(np.uint8)
    g = np.clip(np.rint((x * 255.0) / max(1, width - 1)), 0, 255).astype(np.uint8)
    b = np.clip(np.rint((y * 255.0) / max(1, height - 1)), 0, 255).astype(np.uint8)
    channels = [r, g, b]
    if with_alpha:
        a = np.clip(np.rint(40.0 + 200.0 * ((x + y) / max(1, width + height - 2))), 0, 255).astype(np.uint8)
        channels.append(a)
    Image.fromarray(np.stack(channels, axis=-1), "RGBA" if with_alpha else "RGB").save(path)


def _build_workflow(core, steps):
    nodes = [WorkflowNode("source", "source")]
    for index, (tool_id, settings) in enumerate(steps):
        reference = core.tools.effect_reference(tool_id, settings)
        assert reference.success, reference.error
        nodes.append(WorkflowNode(
            f"n{index}",
            "tool",
            reference.data.to_params(settings),
            inputs=(nodes[-1].node_id,),
        ))
    nodes.append(WorkflowNode("end", "export", inputs=(nodes[-1].node_id,)))
    return WorkflowGraph(
        nodes=tuple(nodes),
        edges=tuple((a.node_id, b.node_id) for a, b in zip(nodes, nodes[1:])),
    )


def _run_and_verify(core, gpu, sid, graph, out_png, *, expect_strips, monkeypatch):
    reasons = []
    strip_counts = []
    orig_select = gpu.select
    orig_chunks = StripPlan.chunks

    def tracked_select(*args, **kwargs):
        result = orig_select(*args, **kwargs)
        reasons.append(result[1])
        return result

    def tracked_chunks(first, last, rows):
        chunks = orig_chunks(first, last, rows)
        strip_counts.append(len(chunks))
        return chunks

    with monkeypatch.context() as mp:
        mp.setattr(gpu, "select", tracked_select)
        mp.setattr(StripPlan, "chunks", staticmethod(tracked_chunks))
        if expect_strips:
            real_device = gpu._device
            mp.setattr(gpu, "_device", _CappedDevice(real_device, 6 * 1024 * 1024))

        preview = core.preview(sid, graph, display=True, max_dimension=800, full_input=True)
        assert preview.success, preview.error
        assert preview.data.display.backend_identity[0] == "gpu"
        display_rgba = np.frombuffer(preview.data.display.rgba, dtype=np.uint8).reshape(
            preview.data.display.size[1], preview.data.display.size[0], 4
        ).copy()
        preview_reasons = list(reasons)
        preview_chunks = list(strip_counts)
        reasons.clear()
        strip_counts.clear()

        exported = core.export(sid, ExportRequest(str(out_png), "png", overwrite=True))
        assert exported.success, exported.error
        export_reasons = list(reasons)
        export_chunks = list(strip_counts)

    if expect_strips:
        assert "gpu_strips" in preview_reasons and max(preview_chunks, default=0) >= 2
        assert "gpu_strips" in export_reasons and max(export_chunks, default=0) >= 2
    else:
        assert "gpu_stage" in preview_reasons and "gpu_strips" not in preview_reasons
        assert "gpu_stage" in export_reasons and "gpu_strips" not in export_reasons

    with Image.open(out_png) as img:
        return display_rgba, np.asarray(img).copy()


@pytest.fixture
def gpu_core():
    core = CoreAPI(autosave_projects=False)
    try:
        wait_for_model_discovery(core.models)
        gpu = core.tools.execution.gpu
        gpu.start()
        deadline = monotonic() + 30.0
        while gpu.snapshot().state in ("idle", "probing") and monotonic() < deadline:
            sleep(0.05)
        state = gpu.snapshot()
        caps = gpu.capabilities()
        if state.state != "ready" or not caps["color"] or not caps["geometry"] or not caps["source_inputs"]:
            pytest.skip(f"当前环境无可用 GPU 加速能力: state={state.state} reason={state.reason}")
        yield core, gpu
    finally:
        core.tools.close()
        core.image_sources.close()


def test_gpu_strips_parity_color_filters_and_masks(gpu_core, tmp_path, monkeypatch):
    core, gpu = gpu_core
    source_path = tmp_path / "rgb_1051x1027.png"
    _synthetic_png(source_path, 1051, 1027, with_alpha=False)

    color_settings = {
        "exposure": 0.35,
        "temperature": 18,
        "tint": -6,
        "contrast": 1.12,
        "saturation": 1.08,
        "vibrance": 12,
        "blacks": -10,
        "whites": 8,
        "shadows": 40,
        "highlights": -35,
        "texture": 30,
        "clarity": 25,
        "dehaze": 20,
        "curves": {"rgb": [[0, 0], [96, 80], [180, 200], [255, 255]]},
        "color_mixer": {"red": {"hue": 10, "sat": -20}, "blue": {"lum": 15, "sat": 10}},
        "look": "none",
        "process_version": 4,
        "masks": [
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "name": "linear",
                "enabled": True,
                "params": {"exposure": -0.5, "shadows": 25, "clarity": 20},
                "components": [{
                    "id": "00000000-0000-4000-8000-000000000002",
                    "name": "l",
                    "enabled": True,
                    "op": "add",
                    "type": "linear",
                    "p0": [0.5, 0.1],
                    "p1": [0.5, 0.65],
                }],
            },
            {
                "id": "00000000-0000-4000-8000-000000000003",
                "name": "radial",
                "enabled": True,
                "params": {"exposure": 0.4, "dehaze": 20, "texture": -15},
                "components": [
                    {
                        "id": "00000000-0000-4000-8000-000000000004",
                        "name": "r",
                        "enabled": True,
                        "op": "add",
                        "type": "radial",
                        "center": [0.45, 0.5],
                        "radius": [0.25, 0.18],
                        "angle": 20,
                        "feather": 0.6,
                    },
                    {
                        "id": "00000000-0000-4000-8000-000000000005",
                        "name": "b",
                        "enabled": True,
                        "op": "subtract",
                        "type": "brush",
                        "strokes": [{
                            "points": [[0.2, 0.3], [0.45, 0.55], [0.7, 0.4]],
                            "radius": 0.04,
                            "hardness": 0.5,
                            "opacity": 0.8,
                        }],
                    },
                ],
            },
        ],
    }
    graph = _build_workflow(core, [("color_adjust", color_settings)])
    sid = core.create_session(core.open_source(str(source_path)).unwrap()).unwrap().session_id
    try:
        full_disp, full_exp = _run_and_verify(
            core, gpu, sid, graph, tmp_path / "full_color.png", expect_strips=False, monkeypatch=monkeypatch
        )
        strip_disp, strip_exp = _run_and_verify(
            core, gpu, sid, graph, tmp_path / "strip_color.png", expect_strips=True, monkeypatch=monkeypatch
        )
        assert np.array_equal(full_disp, strip_disp)
        assert np.array_equal(full_exp, strip_exp)
    finally:
        core.close_session(sid)


def test_gpu_strips_parity_rgba_rotate_shape_and_lasso(gpu_core, tmp_path, monkeypatch):
    core, gpu = gpu_core
    source_path = tmp_path / "rgba_1051x1027.png"
    _synthetic_png(source_path, 1051, 1027, with_alpha=True)

    graph = _build_workflow(core, [
        ("color_adjust", {"exposure": 0.25, "contrast": 1.1, "look": "none", "process_version": 4}),
        ("crop", {"crop_rect": [0.07, 0.113, 0.93, 0.871], "rotation_angle": 5.0, "aspect_ratio": None}),
        ("shape_crop", {"shape": "round_rect", "corner_radius": 24, "rect": [0.05, 0.09, 0.88, 0.83]}),
        ("lasso", {
            "feather": 8,
            "keep_inside": True,
            "draft_points": [],
            "regions": [
                {"points": [[0.1, 0.08], [0.85, 0.15], [0.9, 0.7], [0.5, 0.95], [0.12, 0.8]], "op": "add"},
                {"points": [[0.4, 0.4], [0.55, 0.42], [0.5, 0.6]], "op": "subtract"},
            ],
        }),
    ])
    sid = core.create_session(core.open_source(str(source_path)).unwrap()).unwrap().session_id
    try:
        full_disp, full_exp = _run_and_verify(
            core, gpu, sid, graph, tmp_path / "full_rgba.png", expect_strips=False, monkeypatch=monkeypatch
        )
        strip_disp, strip_exp = _run_and_verify(
            core, gpu, sid, graph, tmp_path / "strip_rgba.png", expect_strips=True, monkeypatch=monkeypatch
        )
        assert np.array_equal(full_disp, strip_disp)
        assert np.array_equal(full_exp, strip_exp)
    finally:
        core.close_session(sid)


def test_gpu_strips_parity_traditional_denoise(gpu_core, tmp_path, monkeypatch):
    core, gpu = gpu_core
    if not gpu.capabilities()["denoise"]:
        pytest.skip("当前显卡降噪自检未通过，降噪走 CPU")
    source_path = tmp_path / "rgb_denoise_1051x1027.png"
    _synthetic_png(source_path, 1051, 1027, with_alpha=False)

    graph = _build_workflow(core, [
        ("denoise", {"method": "bilateral", "strength": 45, "color_strength": 25, "sharpen": 20}),
    ])
    sid = core.create_session(core.open_source(str(source_path)).unwrap()).unwrap().session_id
    try:
        full_disp, full_exp = _run_and_verify(
            core, gpu, sid, graph, tmp_path / "full_denoise.png", expect_strips=False, monkeypatch=monkeypatch
        )
        strip_disp, strip_exp = _run_and_verify(
            core, gpu, sid, graph, tmp_path / "strip_denoise.png", expect_strips=True, monkeypatch=monkeypatch
        )
        assert np.array_equal(full_disp, strip_disp)
        assert np.array_equal(full_exp, strip_exp)
    finally:
        core.close_session(sid)
