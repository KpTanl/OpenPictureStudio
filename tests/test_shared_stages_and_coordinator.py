"""共享阶段并发取消/交付保护与统一执行计划降级路由。"""

from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from src.application.execution_coordinator import ExecutionCoordinator
from src.application.image_resources import ComputeBudget, ImageResourcePolicy, ImageResourcePool
from src.application.shared_stages import SharedStageStore
from src.core.contracts.execution import CPU_BACKEND_IDENTITY, ExecutionRequirement
from src.core.contracts.gpu import display_request, recover_cpu
from src.core.contracts.image_display import PreviewDisplaySpec
from src.core.contracts.source import PixelSpecification
from src.core.contracts.tools import ToolReference
from src.core.contracts.workflow import WorkflowGraph, WorkflowNode
from src.core.photo_engine import RenderSource
from src.core.source_input import SourceInputSpec
from src.core.tools.color import COLOR_PARAMETERS
from src.core.workflow import NodeRegistry, NodeSpec


def _make_store():
    compute = ComputeBudget(2, 4, operator_threads=2)
    resources = ImageResourcePool(ImageResourcePolicy())
    store = SharedStageStore(resources, compute)

    def cleanup():
        store.close()
        resources.close()
        compute.close()

    return store, resources, compute, cleanup


def test_shared_stage_concurrent_subscribers_share_single_producer():
    store, resources, _compute, cleanup = _make_store()
    try:
        key = store.key("stage.test", ("sample", 1))
        calls = 0
        entered = Event()
        release = Event()

        def load(abandoned):
            nonlocal calls
            calls += 1
            entered.set()
            assert release.wait(5)
            assert not abandoned()
            return np.full((8, 8, 3), 2.0, dtype=np.float32)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(store.acquire, key, load, lambda: False)
            assert entered.wait(5)
            fut_b = pool.submit(store.acquire, key, load, lambda: False)
            # 等第二个订阅者登记进 _entries[key].subscribers 再放行生产者
            deadline_event = Event()
            while not deadline_event.is_set():
                with store._condition:
                    entry = store._entries.get(key)
                    if entry is not None and len(entry.subscribers) >= 2:
                        break
                deadline_event.wait(0.005)
            release.set()
            lease_a = fut_a.result(timeout=5)
            lease_b = fut_b.result(timeout=5)
            try:
                assert calls == 1
                assert np.array_equal(lease_a.value, lease_b.value)
            finally:
                lease_a.close()
                lease_b.close()
    finally:
        cleanup()


def test_shared_stage_cancelling_one_subscriber_keeps_other_and_cancelling_all_discards():
    store, resources, _compute, cleanup = _make_store()
    try:
        key = store.key("stage.partial_cancel", ("sample", 2))
        entered = Event()
        release = Event()
        cancel_a = Event()
        cancel_b = Event()
        abandoned_states = []

        def load(abandoned):
            entered.set()
            assert release.wait(5)
            abandoned_states.append(abandoned())
            return np.ones((4, 4, 3), dtype=np.float32)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(store.acquire, key, load, cancel_a.is_set)
            assert entered.wait(5)
            fut_b = pool.submit(store.acquire, key, load, cancel_b.is_set)
            while True:
                with store._condition:
                    entry = store._entries.get(key)
                    if entry is not None and len(entry.subscribers) >= 2:
                        break
                Event().wait(0.005)
            cancel_b.set()
            release.set()
            lease_a = fut_a.result(timeout=5)
            try:
                assert abandoned_states == [False]
                assert lease_a.value.shape == (4, 4, 3)
            finally:
                lease_a.close()
            with pytest.raises(CancelledError):
                fut_b.result(timeout=5)

        # 全部订阅者取消后不再向资源池发布
        key_all = store.key("stage.all_cancel", ("sample", 3))
        all_entered = Event()
        all_release = Event()
        abort_1 = Event()
        abort_2 = Event()

        def load_aborted(abandoned):
            all_entered.set()
            assert all_release.wait(5)
            abandoned_states.append(abandoned())
            return np.ones((4, 4, 3), dtype=np.float32)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(store.acquire, key_all, load_aborted, abort_1.is_set)
            assert all_entered.wait(5)
            f2 = pool.submit(store.acquire, key_all, load_aborted, abort_2.is_set)
            while True:
                with store._condition:
                    entry = store._entries.get(key_all)
                    if entry is not None and len(entry.subscribers) >= 2:
                        break
                Event().wait(0.005)
            abort_1.set()
            abort_2.set()
            all_release.set()
            for future in (f1, f2):
                with pytest.raises(CancelledError):
                    future.result(timeout=5)
        assert abandoned_states[-1] is True
        assert not resources.contains(key_all)
    finally:
        cleanup()


def test_shared_stage_delivered_result_is_not_discarded_when_other_subscriber_cancels():
    store, resources, compute, cleanup = _make_store()
    try:
        key = store.key("stage.delivered", ("sample", 4))
        entered = Event()
        follower_waiting = Event()
        leader_delivered = Event()
        cancel_b = Event()
        abort_b = lambda: cancel_b.is_set()
        original_suspend = compute.suspend

        @contextmanager
        def gated_suspend(is_cancelled):
            with original_suspend(is_cancelled):
                yield
            if is_cancelled is abort_b:
                assert leader_delivered.wait(5)

        compute.suspend = gated_suspend

        def load(_abandoned):
            entered.set()
            assert follower_waiting.wait(5)
            return np.full((6, 6, 3), 3.0, dtype=np.float32)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(store.acquire, key, load, lambda: False)
            assert entered.wait(5)
            fut_b = pool.submit(store.acquire, key, load, abort_b)
            while True:
                with store._condition:
                    entry = store._entries.get(key)
                    if entry is not None and len(entry.subscribers) >= 2:
                        break
                Event().wait(0.005)
            follower_waiting.set()
            lease_a = fut_a.result(timeout=5)
            cancel_b.set()
            leader_delivered.set()
            try:
                with pytest.raises(CancelledError):
                    fut_b.result(timeout=5)
                assert resources.contains(key)
            finally:
                lease_a.close()
            reused = store.cached(key, lambda: False)
            assert reused is not None
            try:
                assert float(reused.value[0, 0, 0]) == 3.0
            finally:
                reused.close()
    finally:
        cleanup()


def test_shared_stage_error_propagates_and_close_wakes_waiters():
    store, _resources, _compute, cleanup = _make_store()
    try:
        key = store.key("stage.error", ("sample", 5))
        entered = Event()
        release = Event()
        attempts = 0

        def failing_load(_abandoned):
            nonlocal attempts
            attempts += 1
            entered.set()
            assert release.wait(5)
            raise RuntimeError("stage boom")

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(store.acquire, key, failing_load, lambda: False)
            assert entered.wait(5)
            f2 = pool.submit(store.acquire, key, failing_load, lambda: False)
            while True:
                with store._condition:
                    entry = store._entries.get(key)
                    if entry is not None and len(entry.subscribers) >= 2:
                        break
                Event().wait(0.005)
            release.set()
            for future in (f1, f2):
                with pytest.raises(RuntimeError, match="stage boom"):
                    future.result(timeout=5)

        # 错误后槽位已清理，可重新尝试
        retry = store.acquire(key, lambda _abandoned: np.zeros((2, 2, 3), dtype=np.float32), lambda: False)
        try:
            assert attempts == 1 and retry.value.shape == (2, 2, 3)
        finally:
            retry.close()

        # store.close() 唤醒等待者并抛出 CancelledError
        wait_key = store.key("stage.close", ("sample", 6))
        wait_entered = Event()
        wait_release = Event()

        def slow_load(_abandoned):
            wait_entered.set()
            wait_release.wait(5)
            return np.zeros((2, 2, 3), dtype=np.float32)

        with ThreadPoolExecutor(max_workers=2) as pool:
            leader = pool.submit(store.acquire, wait_key, slow_load, lambda: False)
            assert wait_entered.wait(5)
            follower = pool.submit(store.acquire, wait_key, slow_load, lambda: False)
            while True:
                with store._condition:
                    entry = store._entries.get(wait_key)
                    if entry is not None and len(entry.subscribers) >= 2:
                        break
                Event().wait(0.005)
            store.close()
            wait_release.set()
            for future in (leader, follower):
                with pytest.raises(CancelledError):
                    future.result(timeout=5)
    finally:
        cleanup()


def test_execution_coordinator_request_cpu_strip_fallback_and_input_adapter(monkeypatch):
    compute = ComputeBudget(2, 4, operator_threads=2)
    resources = ImageResourcePool(ImageResourcePolicy())
    coordinator = ExecutionCoordinator(compute, executors=SimpleNamespace(close=lambda: None), resources=resources)
    try:
        gpu_id = ("gpu", "wgpu27-dx12-strips-7:dxc", "dxgi:0:1")

        def requirements_for(params, node_id):
            if params.get("tool_id") == "color_adjust":
                return ExecutionRequirement(node_id, ("color_adjust", 7, "color_adjust.float", "7"), (), ("cpu", "gpu"))
            return ExecutionRequirement(node_id, ("custom", 1, "custom", "1"), (), ("cpu",))

        registry = NodeRegistry()
        registry.register(NodeSpec("source"))
        registry.register(NodeSpec("export"))
        registry.register(NodeSpec("tool", execution_requirements=requirements_for))

        color_settings = {**COLOR_PARAMETERS.defaults(7), "texture": 20}
        ref = ToolReference("color_adjust", 7, "color_adjust.float", "7")

        def make_source(width, height):
            return RenderSource(
                PixelSpecification((height, width, 3)),
                None,
                "srgb8",
                "photo.png",
                False,
                (width, height),
                "srgb-direct",
                content_identity=("file", "xyz"),
                input_spec=SourceInputSpec((width, height), 3, 8),
            )

        graph_single = WorkflowGraph(nodes=(
            WorkflowNode("source", "source"),
            WorkflowNode("n1", "tool", ref.to_params(color_settings), inputs=("source",)),
            WorkflowNode("end", "export", inputs=("n1",)),
        ), edges=(("source", "n1"), ("n1", "end")))

        # 场景 1：requires_cpu=True 与 recover_cpu 上下文下整链走 CPU
        with display_request(PreviewDisplaySpec(1600)):
            with coordinator.request(make_source(2048, 1536), graph_single, registry, 1, lambda: False,
                                     requires_cpu=True) as cpu_ctx:
                assert cpu_ctx.engine == "cpu" and cpu_ctx.selection_reason == "cpu_required"
                assert all(stage.backend_identity == CPU_BACKEND_IDENTITY for stage in cpu_ctx.stages)
                assert cpu_ctx.stages[0].reason == "cpu_required"

            with recover_cpu("test_recover"), coordinator.request(
                make_source(2048, 1536), graph_single, registry, 1, lambda: False
            ) as recovered_ctx:
                assert recovered_ctx.engine == "cpu" and recovered_ctx.selection_reason == "cpu_recovery"
                assert all(stage.backend_identity == CPU_BACKEND_IDENTITY for stage in recovered_ctx.stages)

        # 场景 2：首节点返回 gpu_strips，但后续节点不具备 GPU 能力时自动用 strips=False 重规划
        graph_mixed = WorkflowGraph(nodes=(
            WorkflowNode("source", "source"),
            WorkflowNode("n1", "tool", ref.to_params(color_settings), inputs=("source",)),
            WorkflowNode("n2", "tool", {"tool_id": "custom"}, inputs=("n1",)),
            WorkflowNode("end", "export", inputs=("n2",)),
        ), edges=(("source", "n1"), ("n1", "n2"), ("n2", "end")))

        select_calls = []

        def fake_select(src, segment, tgt, reqs, order=None, *, workflow_identity=None, strips=True):
            node_ids = tuple(node.node_id for node in segment.nodes)
            select_calls.append((node_ids, strips))
            if node_ids == ("n1",):
                return (gpu_id, "gpu_strips") if strips else (CPU_BACKEND_IDENTITY, "target_budget")
            return CPU_BACKEND_IDENTITY, "capability_missing"

        monkeypatch.setattr(coordinator.gpu, "select", fake_select)
        monkeypatch.setattr(coordinator.gpu, "source_capable", lambda src: True)
        monkeypatch.setattr("src.application.gpu_strips.source_capable", lambda backend, src: True)

        with display_request(PreviewDisplaySpec(1600)):
            with coordinator.request(make_source(2048, 1536), graph_mixed, registry, 2, lambda: False) as mixed_ctx:
                assert any(ids == ("n1",) and not s for ids, s in select_calls)
                assert all(stage.reason != "gpu_strips" for stage in mixed_ctx.stages)
                assert mixed_ctx.stages[1].backend_identity == CPU_BACKEND_IDENTITY
                assert mixed_ctx.stages[1].reason == "target_budget"

        # 场景 3：整链返回 gpu_strips，但小图细档不满足 gpu_strips.capable() 时也自动重规划为 strips=False
        monkeypatch.setattr(
            coordinator.gpu,
            "select",
            lambda src, seg, tgt, reqs, order=None, *, workflow_identity=None, strips=True: (
                (gpu_id, "gpu_strips") if strips else (CPU_BACKEND_IDENTITY, "target_budget")
            ),
        )
        with display_request(PreviewDisplaySpec(1600)):
            with coordinator.request(make_source(800, 512), graph_single, registry, 3, lambda: False) as small_ctx:
                assert all(stage.reason != "gpu_strips" for stage in small_ctx.stages)

        # 场景 4：全链支持分条且 source_capable=True 时挂载整数 input_adapter，source passthrough 继承 GPU 身份
        with display_request(PreviewDisplaySpec(1600)):
            src_big = make_source(2048, 1536)
            with coordinator.request(src_big, graph_single, registry, 4, lambda: False) as strip_ctx:
                assert strip_ctx.stages[0].node_id == "source"
                assert strip_ctx.stages[0].input_adapter == src_big.input_spec
                assert strip_ctx.stages[0].backend_identity == gpu_id
                assert strip_ctx.stages[1].reason == "gpu_strips"
                assert strip_ctx.target.input_adapter.startswith("integer:")
    finally:
        coordinator.close()
        resources.close()
        compute.close()
