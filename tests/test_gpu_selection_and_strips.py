"""GPU 路由选择、分条带行块规划与显存估算、降噪边距、美颜分组逻辑。"""

from types import SimpleNamespace

import pytest

from src.application.gpu_backend import GpuBackend, GpuStatus
from src.application.gpu_beauty import GpuBeauty
from src.application.gpu_color import filter_input_settings, packing, working_memory
from src.application.gpu_denoise import GpuDenoise
from src.application.gpu_strips import (
    BLOCK_BYTES,
    MINIMUM_ROWS,
    STRIP_PIXEL_BYTES,
    StripPlan,
    capable,
)
from src.application.image_compute_settings import ImageComputeSettings, MIB
from src.application.image_resources import ComputeBudget, ImageResourcePolicy, ImageResourcePool
from src.application.shared_stages import SharedStageStore
from src.core.contracts.execution import CPU_BACKEND_IDENTITY, ExecutionRequirement, ExecutionTarget
from src.core.contracts.gpu import GpuExecutionError, GpuResident, ImageOutputSpec, display_request, recover_cpu
from src.core.contracts.image_display import PreviewDisplaySpec
from src.core.contracts.source import PixelSpecification
from src.core.contracts.tools import ToolReference
from src.core.contracts.workflow import WorkflowGraph, WorkflowNode
from src.core.image_resize import ResizeOptions
from src.core.photo_engine import RenderSource
from src.core.source_input import SourceInputSpec
from src.core.tools.color import COLOR_PARAMETERS
from src.core.tools.geometry import CROP_PARAMETERS, LASSO_PARAMETERS, SHAPE_PARAMETERS

GIB = 1024 ** 3


class _FakeDevice:
    def __init__(self, **overrides):
        self._info = {
            "lost": False,
            "allocated_bytes": 64 * MIB,
            "idle_bytes": 0,
            "process_budget": 8 * GIB,
            "process_usage": 64 * MIB,
            "max_binding_size": 256 * MIB,
            "max_buffer_size": 256 * MIB,
            "max_workgroups": 65535,
            "uploaded_bytes": 0,
            "readback_bytes": 0,
            "completed_event": 0,
            "driver": "555.0",
        }
        self._info.update(overrides)
        self.reserved = self._info["allocated_bytes"]

    def info(self):
        return dict(self._info)

    def reserved_bytes(self):
        return self.reserved

    def trim(self):
        pass

    def configure(self, *args):
        pass

    def close(self):
        pass


def _make_backend(monkeypatch, *, engine="auto", state="ready", reason="", budget_bytes=4 * GIB,
                  color_ready=True, geometry_ready=True, **device_overrides):
    compute = ComputeBudget(2, 4, operator_threads=2)
    resources = ImageResourcePool(ImageResourcePolicy())
    stages = SharedStageStore(resources, compute)
    backend = GpuBackend(compute, resources, stages=stages, settings=ImageComputeSettings(engine=engine))
    monkeypatch.setattr(backend, "start", lambda: None)
    device = _FakeDevice(**device_overrides)
    backend._device = device
    backend._color_ready = color_ready
    backend._geometry_ready = geometry_ready
    backend._status = GpuStatus(
        state=state,
        tier="A" if state == "ready" else "C",
        name="Test GPU" if state == "ready" else "",
        backend="D3D12 / dxc",
        device="dxgi:0",
        reason=reason,
        revision="wgpu27-dx12-strips-7:dxc",
        generation=1,
        budget_bytes=budget_bytes,
        cpu_ms=4.0,
        gpu_ms=2.0,
    )

    def cleanup():
        backend._device = None
        backend.close()
        stages.close()
        resources.close()
        compute.close()

    return backend, device, cleanup


def _source(width=2048, height=1536, *, is_proxy=False, content_identity=("file", "abc")):
    return SimpleNamespace(
        pixels=PixelSpecification((height, width, 3)),
        alpha=None,
        orig_size=(width, height),
        is_proxy=is_proxy,
        source_path="sample.png",
        content_identity=content_identity,
        patches=(),
        gpu_image=None,
        input_spec=None,
        backend_identity=CPU_BACKEND_IDENTITY,
    )


def _target(width=2048, height=1536, *, is_proxy=False, colorspace="linear_srgb", dtype="float32"):
    return ExecutionTarget((width, height), (width, height), is_proxy, colorspace, dtype, False)


def _color_workflow(**extra):
    settings = {**COLOR_PARAMETERS.defaults(7), "exposure": 0.4, **extra}
    ref = ToolReference("color_adjust", 7, "color_adjust.float", "7")
    graph = WorkflowGraph(nodes=(
        WorkflowNode("source", "source"),
        WorkflowNode("n1", "tool", ref.to_params(settings), inputs=("source",)),
        WorkflowNode("end", "export", inputs=("n1",)),
    ), edges=(("source", "n1"), ("n1", "end")))
    requirement = ExecutionRequirement("n1", ("color_adjust", 7, "color_adjust.float", "7"), (), ("cpu", "gpu"))
    return graph, (requirement,)


def test_gpu_select_admission_routes_and_preserves_cache_on_hard_budget(monkeypatch):
    backend, device, cleanup = _make_backend(monkeypatch)
    try:
        graph, reqs = _color_workflow()
        source = _source()
        target = _target()

        # 缺少 display_target
        assert backend.select(source, graph, target, reqs) == (CPU_BACKEND_IDENTITY, "output_adapter_missing")

        with display_request(PreviewDisplaySpec(1600)):
            assert backend.select(source, graph, target, reqs) == (backend._status.identity, "gpu_stage")

            # cpu_recovery 上下文强制回退
            with recover_cpu("test_recover"):
                assert backend.select(source, graph, target, reqs) == (CPU_BACKEND_IDENTITY, "cpu_recovery")

            # 缺内容身份、代理图遇模型节点、非标准色彩格式
            assert backend.select(_source(content_identity=()), graph, target, reqs) == (
                CPU_BACKEND_IDENTITY, "content_identity_missing"
            )
            model_req = (ExecutionRequirement("n1", ("ai_enhance", 3, "ai_enhance.neural", "1"), (), ("cpu", "gpu")),)
            assert backend.select(_source(is_proxy=True), graph, target, model_req) == (
                CPU_BACKEND_IDENTITY, "model_requires_full_input"
            )
            assert backend.select(source, graph, _target(colorspace="srgb"), reqs) == (
                CPU_BACKEND_IDENTITY, "target_format_missing"
            )

            # 单块缓冲不足时：允许分条则切 gpu_strips，不允许分条则回退 target_limit
            device._info["max_binding_size"] = 32 * MIB
            assert backend.select(source, graph, target, reqs, strips=True) == (
                backend._status.identity, "gpu_strips"
            )
            assert backend.select(source, graph, target, reqs, strips=False) == (
                CPU_BACKEND_IDENTITY, "target_limit"
            )
            with display_request(ImageOutputSpec(ResizeOptions(width=1024))):
                assert backend.select(source, graph, target, reqs, strips=True) == (
                    CPU_BACKEND_IDENTITY, "target_limit"
                )
            device._info["max_binding_size"] = 256 * MIB

            # 超出即使全清缓存也拿不到的总预算时，直接分条或回退且不清空驻留缓存
            dummy_carrier = SimpleNamespace(allocation_id=999, nbytes=1024)
            backend._cache[("keep-me",)] = SimpleNamespace(carrier=dummy_carrier, partial=False)
            backend._status = GpuStatus(**{**backend._status.__dict__, "budget_bytes": 180 * MIB})
            assert backend.select(source, graph, target, reqs, strips=True) == (
                backend._status.identity, "gpu_strips"
            )
            assert backend.select(source, graph, target, reqs, strips=False) == (
                CPU_BACKEND_IDENTITY, "target_budget"
            )
            assert ("keep-me",) in backend._cache

            # 设备丢失时更新状态并返回 device_lost
            device._info["lost"] = True
            assert backend.select(source, graph, target, reqs) == (CPU_BACKEND_IDENTITY, "device_lost")
            assert backend._status.state == "lost"
    finally:
        cleanup()


def test_gpu_select_engine_cpu_and_unready_states(monkeypatch):
    cpu_backend, _dev, cleanup_cpu = _make_backend(monkeypatch, engine="cpu", state="disabled", reason="cpu_selected")
    idle_backend, _dev2, cleanup_idle = _make_backend(monkeypatch, engine="auto", state="probing", reason="probing")
    try:
        graph, reqs = _color_workflow()
        with display_request(PreviewDisplaySpec(1600)):
            assert cpu_backend.select(_source(), graph, _target(), reqs) == (CPU_BACKEND_IDENTITY, "cpu_selected")
            assert idle_backend.select(_source(), graph, _target(), reqs) == (CPU_BACKEND_IDENTITY, "probing")
    finally:
        cleanup_cpu()
        cleanup_idle()


def test_gpu_strips_capable_boundaries():
    color_settings = {
        **COLOR_PARAMETERS.defaults(7),
        "shadows": 30,
        "highlights": -20,
        "texture": 25,
        "clarity": 15,
        "dehaze": 20,
    }
    color_ref = ToolReference("color_adjust", 7, "color_adjust.float", "7")
    crop_ref = ToolReference("crop", 2, "crop.float", "2")
    crop_settings = {**CROP_PARAMETERS.defaults(2), "rotation_angle": 5.0, "crop_rect": [0.05, 0.05, 0.95, 0.95]}
    shape_ref = ToolReference("shape_crop", 2, "shape_crop.float", "2")
    lasso_ref = ToolReference("lasso", 2, "lasso.float", "2")
    denoise_ref = ToolReference("denoise", 2, "denoise.basic", "3")
    denoise_settings = {"method": "bilateral", "strength": 40, "color_strength": 20, "sharpen": 15}

    graph = WorkflowGraph(nodes=(
        WorkflowNode("source", "source"),
        WorkflowNode("c", "tool", color_ref.to_params(color_settings)),
        WorkflowNode("b", "tool", ToolReference("beauty", 3, "beauty.detail", "5-neutral").to_params({})),
        WorkflowNode("r", "tool", crop_ref.to_params(crop_settings)),
        WorkflowNode("s", "tool", shape_ref.to_params(SHAPE_PARAMETERS.defaults(2))),
        WorkflowNode("l", "tool", lasso_ref.to_params(LASSO_PARAMETERS.defaults(2))),
        WorkflowNode("d", "tool", denoise_ref.to_params(denoise_settings)),
        WorkflowNode("end", "export"),
    ))
    assert capable(_source(2048, 1536), graph) is True
    assert capable(_source(2048, 1536, is_proxy=True), graph) is False
    assert capable(_source(32769, 1024), graph) is False
    assert capable(_source(2048, 1536), graph, spec=ImageOutputSpec(ResizeOptions(width=800))) is False

    # 小图细档降采样倍数为 1 (< 2)，不能走引导滤波分条
    small_color_graph = WorkflowGraph(nodes=(
        WorkflowNode("c", "tool", color_ref.to_params({**COLOR_PARAMETERS.defaults(7), "texture": 20})),
    ))
    assert capable(_source(800, 512), small_color_graph) is False

    # AI 降噪与旧版局部蒙版带阴影高光不可分条
    ai_denoise_graph = WorkflowGraph(nodes=(
        WorkflowNode("d", "tool", ToolReference("denoise", 2, "denoise.ai", "2").to_params(
            {"method": "ai", "strength": 50, "color_strength": 0, "sharpen": 0}
        )),
    ))
    assert capable(_source(2048, 1536), ai_denoise_graph) is False

    legacy_mask_color = WorkflowGraph(nodes=(
        WorkflowNode("c", "tool", ToolReference("color_adjust", 4, "color_adjust.float", "4").to_params({
            "process_version": 2,
            "look": "none",
            "masks": [{"type": "linear", "enabled": True, "params": {"shadows": 20}}],
        })),
    ))
    assert capable(_source(2048, 1536), legacy_mask_color) is False


def test_strip_plan_extend_rows_chunks_spans_and_needed(monkeypatch):
    backend, device, cleanup = _make_backend(monkeypatch)
    try:
        resident = GpuResident(None, backend._status.identity, None, ("src",))
        metadata = SimpleNamespace(source_path="sample.png", orig_size=(2000, 1503), alpha=None)
        base_plan = StripPlan(
            backend,
            resident,
            SourceInputSpec((2000, 1503), 3, 8),
            (),
            metadata,
            (),
            backend._status.identity,
        )
        base_source = RenderSource(
            PixelSpecification((1503, 2000, 3)),
            None,
            "srgb8",
            "sample.png",
            False,
            (2000, 1503),
            "srgb-direct",
            gpu_image=base_plan.artifact(),
            backend_identity=backend._status.identity,
        )
        denoise_ref = ToolReference("denoise", 2, "denoise.basic", "3")
        denoise_settings = {"method": "bilateral", "strength": 40, "color_strength": 20, "sharpen": 0}
        extended_source = base_plan.extend(base_source, denoise_ref, denoise_settings, None, lambda: False)
        plan = extended_source.gpu_image.service
        assert isinstance(plan, StripPlan) and len(plan.steps) == 1

        # 中性几何节点直接透传原 source
        neutral_crop = ToolReference("crop", 2, "crop.float", "2")
        assert plan.extend(extended_source, neutral_crop, CROP_PARAMETERS.defaults(2), None, lambda: False) is extended_source

        halo = GpuDenoise.strip_halo(denoise_settings, 3)
        frame = SimpleNamespace(device=device, scratch={})
        expected_rows = min(
            BLOCK_BYTES // (2000 * 16) - 2 * halo,
            (4 * GIB - 64 * MIB) // (2000 * STRIP_PIXEL_BYTES),
        )
        assert plan.rows(frame) == expected_rows

        device._info["allocated_bytes"] = 4 * GIB - (MINIMUM_ROWS - 1) * 2000 * STRIP_PIXEL_BYTES
        with pytest.raises(GpuExecutionError) as exc:
            plan.rows(frame)
        assert exc.value.code == "target_budget"

        # 奇数高度分条覆盖完整，spans 输出等高，needed 映射不越界
        chunks = plan.chunks(0, 1503, 500)
        assert chunks[0][0] == 0 and chunks[-1][1] == 1503
        assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:]))
        spans = plan.spans(chunks, (800, 601))
        heights = {hi - lo for _f, _l, lo, hi in spans}
        assert len(heights) == 1
        assert all(0 <= lo < hi <= 1503 for _f, _l, lo, hi in spans)
        assert plan.needed((2000, 1503), 10, 80) == (10, 80)
        scaled_lo, scaled_hi = plan.needed((800, 601), 0, 100)
        assert 0 <= scaled_lo < scaled_hi <= 1503
    finally:
        cleanup()


def test_gpu_color_memory_filter_inputs_denoise_halo_and_beauty_groups():
    # packing 按 128 MiB 块取整，working_memory 随蒙版和滤波单调递增
    assert packing(64 * MIB) == 1.0
    assert packing(128 * MIB) == 1.0
    assert packing(70 * MIB) > 1.0
    assert packing(128 * MIB + 1) == 1

    steps = {"n1": ((2000, 1500), (2000, 1500))}
    ref = ToolReference("color_adjust", 7, "color_adjust.float", "7")

    def graph_for(**overrides):
        return WorkflowGraph(nodes=(WorkflowNode("n1", "tool", ref.to_params({**COLOR_PARAMETERS.defaults(7), **overrides})),))

    mem_plain = working_memory(graph_for(exposure=0.5), steps)
    mem_tone = working_memory(graph_for(shadows=30), steps)
    mem_detail = working_memory(graph_for(shadows=30, texture=20, dehaze=25), steps)
    mem_masked = working_memory(
        graph_for(
            shadows=30,
            texture=20,
            dehaze=25,
            masks=[{
                "id": "00000000-0000-4000-8000-000000000001",
                "name": "m",
                "enabled": True,
                "params": {"clarity": 20},
                "components": [{
                    "id": "00000000-0000-4000-8000-000000000002",
                    "name": "c",
                    "enabled": True,
                    "op": "add",
                    "type": "linear",
                    "p0": [0.2, 0.2],
                    "p1": [0.8, 0.8],
                }],
            }],
        ),
        steps,
    )
    assert mem_plain < mem_tone < mem_detail < mem_masked

    # filter_input_settings：修改下游滑块不改变上游滤波输入标识
    base = {
        "exposure": 0.4,
        "temperature": 12,
        "shadows": 25,
        "curves": {"rgb": [[0, 0], [255, 255]]},
        "texture": 10,
        "clarity": 20,
        "dehaze": 15,
        "sharpness": 1.2,
        "masks": [{"id": "m1", "params": {"exposure": 0.2, "shadows": 10, "texture": 30}}],
    }
    tweaked_after_tone = {
        **base,
        "shadows": -30,
        "curves": {"rgb": [[0, 0], [128, 160], [255, 255]]},
        "dehaze": 60,
        "masks": [{"id": "m1", "params": {"exposure": 0.2, "shadows": -40, "texture": 80}}],
    }
    assert filter_input_settings(base, "tone") == filter_input_settings(tweaked_after_tone, "tone")
    assert filter_input_settings(base, "tone") != filter_input_settings({**base, "exposure": 0.9}, "tone")
    assert filter_input_settings(base, "detail") == filter_input_settings(
        {**base, "texture": 80, "clarity": -50, "dehaze": 90, "sharpness": 2.0}, "detail"
    )
    assert filter_input_settings(base, "detail") != filter_input_settings({**base, "shadows": -10}, "detail")

    # GpuDenoise.strip_halo（AI 降噪仅在强度或彩色强度非零时返回 None，双强度为 0 时正常返回边距）
    assert GpuDenoise.strip_halo({"method": "ai", "strength": 50, "color_strength": 0, "sharpen": 0}, 2) is None
    assert GpuDenoise.strip_halo({"method": "ai", "strength": 0, "color_strength": 40, "sharpen": 0}, 2) is None
    assert GpuDenoise.strip_halo({"method": "ai", "strength": 0, "color_strength": 0, "sharpen": 0}, 2) == 0
    assert GpuDenoise.strip_halo({"method": "ai", "strength": 0, "color_strength": 0, "sharpen": 20}, 2) == 1
    halo_no_sharp = GpuDenoise.strip_halo({"method": "bilateral", "strength": 40, "color_strength": 30, "sharpen": 0}, 3)
    halo_sharp = GpuDenoise.strip_halo({"method": "bilateral", "strength": 40, "color_strength": 30, "sharpen": 25}, 3)
    assert halo_no_sharp == 3 and halo_sharp == 4
    assert GpuDenoise.strip_halo({"method": "gaussian", "strength": 50, "color_strength": 0, "sharpen": 0}, 3) > 0
    assert GpuDenoise.strip_halo({"method": "median", "strength": 60, "color_strength": 40, "sharpen": 10}, 2) > 1

    # GpuBeauty.groups 连通合并与保序
    disjoint = [
        (0, SimpleNamespace(bounds=(0, 0, 50, 50)), (0, 0, 60, 60)),
        (1, SimpleNamespace(bounds=(200, 0, 250, 50)), (200, 0, 260, 60)),
    ]
    groups_disjoint = GpuBeauty.groups(disjoint)
    assert len(groups_disjoint) == 2
    assert groups_disjoint[0][0] == (0, 0, 60, 60) and [i for i, _, _ in groups_disjoint[0][1]] == [0]
    assert groups_disjoint[1][0] == (200, 0, 260, 60) and [i for i, _, _ in groups_disjoint[1][1]] == [1]

    transitive = [
        (0, SimpleNamespace(bounds=(0, 0, 80, 80)), (0, 0, 100, 100)),
        (1, SimpleNamespace(bounds=(200, 0, 280, 80)), (200, 0, 300, 100)),
        (2, SimpleNamespace(bounds=(90, 10, 210, 90)), (95, 10, 205, 90)),
    ]
    groups_merged = GpuBeauty.groups(transitive)
    assert len(groups_merged) == 1
    assert groups_merged[0][0] == (0, 0, 300, 100)
    assert [i for i, _, _ in groups_merged[0][1]] == [0, 1, 2]
