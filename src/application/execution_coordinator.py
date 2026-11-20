from __future__ import annotations

import logging
import uuid
from concurrent.futures import CancelledError
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace
from time import monotonic

from ..core.contracts.execution import (CPU_BACKEND_IDENTITY, ExecutionContext, ExecutionRequirement,
                                        ExecutionStage, ExecutionTarget, current_execution, current_admission, dependency_cancellation)
from ..core.execution_memo import ResourceOwner
from ..utils.log_levels import TRACE

logger = logging.getLogger(__name__)


class ExecutionCoordinator:
    def __init__(self, compute, executors, resources=None):
        self._owns_resources = resources is None
        if resources is None:
            from .image_resources import ImageResourcePool, ImageResourcePolicy
            resources = ImageResourcePool(ImageResourcePolicy())
        self.compute, self.executors, self.resources = compute, executors, resources
        from .shared_stages import SharedStageStore
        self.results = SharedStageStore(resources, compute)
        self.memo_enabled = True
        from .gpu_backend import GpuBackend
        self.gpu = GpuBackend(compute, resources, stages=self.results)

    def protect(self, value):
        admission = current_admission.get()
        if admission is None or not hasattr(value, "execution_ownership"):
            return value
        budget, working = admission
        size = budget.additional_bytes(value)
        if not size:
            return value
        held = []
        try:
            if working is not None:
                credit = working.split(size)
                held.append(credit)
                size -= credit._size
            if size:
                held.append(budget.try_hold_memory(size))
            return replace(value, execution_ownership=ResourceOwner(held, value.execution_ownership))
        except BaseException:
            for hold in held:
                hold.close()
            raise

    def close(self):
        self.gpu.close()
        self.results.close()
        if self._owns_resources:
            self.resources.close()

    def demand(self, key, tool_id, ready, cancelled, *, digests=frozenset()):
        execution = current_execution.get()
        priority = execution.priority if execution is not None else self.compute.scheduling()[0]
        return self.executors.request(key, tool_id, priority=priority, digests=digests,
                                      ready=ready, cancelled=cancelled)

    def operation(self, stage, cancelled, *, threads=1, memory_bytes=0):
        return self.borrow(("host", stage), stage, lambda hold: ExitStack(), cancelled,
                           reusable=False, threads=threads, memory_bytes=memory_bytes, stage=stage)

    @contextmanager
    def output_operation(self, source, stage, cancelled, *, threads=1, memory_bytes=0):
        token = None
        if source.gpu_image is not None:
            from ..core.contracts.gpu import display_target
            spec = display_target.get()
            size = spec.size_for(source.pixels.shape[1], source.pixels.shape[0]) if spec is not None else tuple(reversed(source.pixels.shape[:2]))
            threads, memory_bytes = 1, min(memory_bytes, size[0] * size[1] * 8)
            priority, role = self.compute.scheduling()
            context = ExecutionContext(uuid.uuid4().hex, tuple(source.content_identity), source.gpu_image.family[1],
                ExecutionTarget(tuple(reversed(source.pixels.shape[:2])), source.orig_size, source.is_proxy,
                    source.colorspace, str(source.pixels.dtype), source.alpha is not None), (), 0,
                *source.backend_identity, 0, priority, role, cancelled, "gpu_output", stage, source.source_path)
            token = current_execution.set(context)
        try:
            with self.operation(stage, cancelled, threads=threads, memory_bytes=memory_bytes) as allocation:
                yield allocation
        finally:
            if token is not None:
                current_execution.reset(token)

    @contextmanager
    def materialized(self, source, cancelled, *, resources=None):
        if not source.patches:
            yield source
            return
        budget = self.resources if resources is None else resources
        size = source.pixels.nbytes + (source.alpha.nbytes if source.alpha is not None else 0)
        with self.compute.suspend(cancelled), budget.working_memory(size, cancelled):
            with self.operation("input.compose", cancelled):
                combined = source.materialize(cancelled)
            yield combined

    @contextmanager
    def request(self, source, graph, registry, generation, cancelled, *, requires_cpu=False, attempt=0, order=None,
                planned_stages=None):
        from .deferred_source import DeferredSource
        if isinstance(source, DeferredSource):
            source = source.metadata
        parent = current_execution.get()
        priority, role = self.compute.scheduling()
        if parent is not None:
            priority, role = parent.priority, parent.role
        priority = getattr(cancelled, "priority", priority)
        dependency_token = dependency_cancellation.set(cancelled)
        try:
            requirements = tuple(registry.get(node.node_type, node.version).execution_requirements(node.params, node.node_id)
                                 for node in graph.nodes
                                 if registry.get(node.node_type, node.version).execution_requirements is not None)
        finally:
            dependency_cancellation.reset(dependency_token)
        target = ExecutionTarget(tuple(reversed(source.pixels.shape[:2])), tuple(source.orig_size),
                                 source.is_proxy, source.colorspace, str(source.pixels.dtype), source.alpha is not None,
                                 source.white_point, source.gamut, source.color_pipeline)
        from ..core.contracts.gpu import display_target, cpu_recovery
        spec = display_target.get()
        if spec is not None:
            from ..core.contracts.gpu import ImageOutputSpec
            target = replace(target, output_adapter=(f"linear-f32:{spec.max_dimension}" if isinstance(spec, ImageOutputSpec)
                                                       else f"rgba8:{spec.max_dimension}"))
        force_cpu = requires_cpu or cpu_recovery.get() is not None or parent is not None and parent.force_cpu
        output_boundary = parent is None or parent.output_boundary
        stages = (planned_stages if planned_stages is not None and not force_cpu else
            self._plan(source, graph, registry, target, requirements, order, force_cpu, output_boundary))
        if stages[0].input_adapter:
            target = replace(target, input_adapter="integer:" + repr(stages[0].input_adapter))
        backend = next((stage.backend_identity for stage in stages if stage.backend_identity[0] == "gpu"), CPU_BACKEND_IDENTITY)
        reason = "cpu_recovery" if cpu_recovery.get() is not None else "cpu_required" if force_cpu else "staged"
        context = ExecutionContext(uuid.uuid4().hex, tuple(source.content_identity), graph.workflow_hash(), target,
                                   requirements, generation, *backend, max(attempt, int(cpu_recovery.get() is not None)),
                                   priority, role, cancelled, reason, source_path=source.source_path,
                                   stages=stages, force_cpu=force_cpu, output_boundary=output_boundary)
        token = current_execution.set(context)
        started = monotonic()
        try:
            with self.compute.suspend(cancelled):
                logger.log(TRACE, "执行请求 request=%s engine=%s device=%s attempt=%s role=%s target=%s dependencies=%s reason=%s",
                           context.request_id, context.engine, context.device, context.attempt, context.role,
                           target, len(requirements), context.selection_reason)
                logger.debug("执行计划 request=%s stages=%s", context.request_id,
                    tuple((stage.node_id, stage.backend_identity, stage.preparation_backend, stage.reason) for stage in stages))
                yield context
        finally:
            current_execution.reset(token)
            logger.log(TRACE, "执行请求结束 request=%s engine=%s attempt=%s cancelled=%s duration_ms=%.1f",
                       context.request_id, context.engine, context.attempt, cancelled(),
                       (monotonic() - started) * 1000)

    def _plan(self, source, graph, registry, target, requirements, order, force_cpu, output_boundary=True, strips=True):
        from ..core.contracts.source import PixelSpecification
        from ..core.contracts.workflow import WorkflowGraph
        from ..core.contracts.gpu import display_target, ImageOutputSpec
        from ..core.workflow.executor import geometry_prefix
        required = {item.node_id: item for item in requirements}
        incoming = source
        previous = CPU_BACKEND_IDENTITY if force_cpu else source.backend_identity
        stages = []
        nodes = tuple(graph.node(node_id) for node_id in order) if order is not None else tuple(graph.nodes)
        for index, node in enumerate(nodes):
            node_id = node.node_id
            item = required.get(node_id)
            if node.node_type in ("source", "export"):
                stages.append(ExecutionStage(node_id, previous,
                    "cpu_required" if force_cpu and node.node_type == "source" else "passthrough"))
                continue
            segment = WorkflowGraph(nodes=(node,), edges=())
            backend, reason = ((CPU_BACKEND_IDENTITY, "cpu_required") if force_cpu else
                self.gpu.select(incoming, segment, replace(target, size=incoming.pixels.shape[1::-1]),
                    (item,) if item is not None else (), (node_id,),
                    workflow_identity=graph.workflow_hash(), strips=strips))
            if item is None:
                backend, reason = CPU_BACKEND_IDENTITY, "capability_missing"
            preparation = (CPU_BACKEND_IDENTITY if item is not None and
                           any(dep[0] == "preparation" for dep in item.dependencies) else ())
            preparation_stages = ()
            if preparation:
                width, height = source.orig_size
                full = replace(source, pixels=PixelSpecification((height, width, 3)),
                    alpha=None if source.alpha is None else PixelSpecification((height, width)),
                    is_proxy=False, patches=(), gpu_image=None, backend_identity=CPU_BACKEND_IDENTITY)
                if backend[0] == "gpu" and self.gpu.source_capable(full):
                    preparation = backend
                    full = replace(full, backend_identity=backend)
                prefix_graph = geometry_prefix(nodes[:index], registry)
                ids = {part.node_id for part in prefix_graph.nodes}
                prefix_requirements = tuple(item for item in requirements if item.node_id in ids)
                preparation_stages = self._plan(full, prefix_graph, registry,
                    replace(target, size=(width, height), proxy=False), prefix_requirements,
                    tuple(part.node_id for part in prefix_graph.nodes), force_cpu, False, strips)
            stages.append(ExecutionStage(node_id, backend, reason, preparation, preparation_stages))
            previous = backend
            spec = registry.get(node.node_type, node.version)
            size = spec.output_size(node.params, incoming.pixels.shape[1::-1]) if spec.output_size is not None else None
            width, height = size or incoming.pixels.shape[1::-1]
            incoming = replace(incoming, pixels=PixelSpecification((height, width, 3)),
                alpha=None if incoming.alpha is None else PixelSpecification((height, width)),
                patches=(), gpu_image=None, backend_identity=backend)
        spec = display_target.get()
        output_gpu = output_boundary and spec is not None and (not isinstance(spec, ImageOutputSpec) or
            spec.resize is not None and (spec.resize.width is not None or spec.resize.height is not None))
        backend, reason = ((CPU_BACKEND_IDENTITY, "cpu_required") if force_cpu else
            (previous, "passthrough") if not output_gpu else
            self.gpu.select(incoming, WorkflowGraph(nodes=(), edges=()),
                replace(target, size=incoming.pixels.shape[1::-1]), (), (),
                workflow_identity=graph.workflow_hash(), strips=strips))
        stages.append(ExecutionStage(None, backend, reason))
        first = next((stage for stage in stages if stage.reason != "passthrough"), stages[-1])
        adapter = not force_cpu and first.backend_identity[0] == "gpu" and self.gpu.source_capable(source)
        if any(stage.reason == "gpu_strips" for stage in stages):
            # 分条从常驻的整数源逐条转出，整条链都得在 GPU 上逐条算
            from .gpu_strips import capable, source_capable
            if not (all(stage.backend_identity[0] == "gpu" for stage in stages if stage.reason != "passthrough")
                    and capable(source, graph, order, spec) and source_capable(self.gpu, source)):
                return self._plan(source, graph, registry, target, requirements, order, force_cpu, output_boundary,
                                  strips=False)
            adapter = True
        if adapter:
            stages[0] = replace(stages[0], input_adapter=source.input_spec)
            for index, stage in enumerate(stages):
                if stage.node_id is None or graph.node(stage.node_id).node_type not in ("source", "export"):
                    break
                stages[index] = replace(stage, backend_identity=first.backend_identity)
        return tuple(stages)

    def resolve(self, source, context, cancelled):
        from .deferred_source import DeferredSource
        if not isinstance(source, DeferredSource):
            return source
        if context.target.input_adapter.startswith("integer:"):
            first = next(stage for stage in context.stages if stage.backend_identity[0] == "gpu")
            active = replace(context, engine=first.backend_identity[0], backend_revision=first.backend_identity[1],
                device=first.backend_identity[2], stage_id="source.input", is_cancelled=cancelled)
            strips = any(stage.reason == "gpu_strips" for stage in context.stages)
            cached = (self.gpu.cached_strips if strips else self.gpu.cached_source)(source.metadata, active, cancelled)
            if cached is not None:
                return cached
            def prepare(source_input, metadata, abort):
                token = current_execution.set(active)
                try:
                    return (self.gpu.prepare_strips if strips else self.gpu.prepare_source)(source_input, metadata, active, abort)
                finally:
                    current_execution.reset(token)
            with self.preparation(cancelled):
                return source.resolve(cancelled, prepare)
        with self.preparation(cancelled):
            return source.resolve(cancelled)

    @contextmanager
    def preparation(self, cancelled):
        context = current_execution.get()
        if context is None:
            yield
            return
        token = current_execution.set(replace(context, engine=CPU_BACKEND_IDENTITY[0],
            backend_revision=CPU_BACKEND_IDENTITY[1], device=CPU_BACKEND_IDENTITY[2],
            is_cancelled=cancelled, stage_id=context.stage_id + ":prepare", output_boundary=False))
        try:
            with self.compute.suspend(cancelled):
                yield
        finally:
            current_execution.reset(token)

    @contextmanager
    def stage(self, source, context, stage, cancelled, *, graph=None, requirement=None):
        identity = source.result_identity or (source.gpu_image.resident.identity if source.gpu_image is not None
            else ("source", source.content_identity, source.backend_identity, source.pixels.shape, source.orig_size))
        target = replace(context.target, size=source.pixels.shape[1::-1], reference_size=source.orig_size,
            proxy=source.is_proxy, alpha=source.alpha is not None, colorspace=source.colorspace,
            dtype=str(source.pixels.dtype), white_point=source.white_point, gamut=source.gamut,
            color_pipeline=source.color_pipeline)
        active = replace(context, engine=stage.backend_identity[0], backend_revision=stage.backend_identity[1],
            device=stage.backend_identity[2], stage_id=stage.node_id or "output", selection_reason=stage.reason,
            input_identity=identity, target=target, is_cancelled=cancelled)
        token = current_execution.set(active)
        try:
            with ExitStack() as transfers:
                if stage.reason != "passthrough":
                    if active.engine == "cpu" and source.gpu_image is not None:
                        logger.debug("执行交接 request=%s stage=%s direction=gpu_to_cpu input=%s", context.request_id, stage.node_id, identity)
                        source = source.materialize(cancelled)
                    elif active.engine == "gpu":
                        if source.patches:
                            with self.preparation(cancelled):
                                source = transfers.enter_context(self.materialized(source, cancelled))
                        if graph is not None:
                            selected, reason = self.gpu.select(source, graph, target,
                                (requirement,) if requirement is not None else (),
                                tuple(node.node_id for node in graph.nodes),
                                workflow_identity=context.workflow_identity)
                            from .gpu_strips import is_strip
                            if selected != stage.backend_identity or reason == "gpu_strips" and not is_strip(source):
                                from ..core.contracts.gpu import GpuExecutionError
                                raise GpuExecutionError(reason, "GPU stage admission changed")
                        transfers.enter_context(self.gpu.frame(stage.backend_identity, cancelled))
                        if source.gpu_image is None:
                            logger.debug("执行交接 request=%s stage=%s direction=cpu_to_gpu input=%s", context.request_id, stage.node_id, identity)
                        source = self.begin(source, active)
                logger.log(TRACE, "执行阶段 request=%s stage=%s engine=%s device=%s reason=%s",
                    context.request_id, stage.node_id, active.engine, active.device, stage.reason)
                yield source, active
        finally:
            current_execution.reset(token)

    def complete(self, source, context):
        from ..core.contracts.workflow import WorkflowGraph
        stage = context.stages[-1]
        with self.stage(source, context, stage, context.is_cancelled,
                        graph=WorkflowGraph(nodes=(), edges=())) as (output, _):
            return output

    def begin(self, source, context):
        from ..core.contracts.gpu import DevicePixels
        if source.gpu_image is not None:
            if source.backend_identity != context.backend_identity:
                from ..core.contracts.gpu import GpuExecutionError
                raise GpuExecutionError("device_changed", "GPU input belongs to another engine")
            return source
        artifact = self.gpu.upload(source, context)
        height, width = artifact.resident.carrier.size[1], artifact.resident.carrier.size[0]
        return replace(source, pixels=DevicePixels((height, width, 3)),
            alpha=None if source.alpha is None else DevicePixels((height, width)),
            gpu_image=artifact, backend_identity=context.backend_identity, execution_ownership=None)

    @contextmanager
    def borrow(self, key, tool_id, factory, cancelled, *, reusable=True, digests=frozenset(), threads=None,
               memory_bytes=0, executor_memory=0, resources=None, stage="execute", demand=None):
        context = current_execution.get()
        priority, role = self.compute.scheduling()
        if context is not None:
            priority, role = context.priority, context.role
        priority = getattr(cancelled, "priority", priority)
        if role not in ("operator", "foreground"):
            role = "operator"
        budget = self.resources if resources is None else resources
        reservation, working, creation = None, None, None
        executor = None
        failed = True
        started = monotonic()

        def rollback():
            nonlocal reservation, working, creation
            if creation is not None:
                creation.close()
                creation = None
            if working is not None:
                working.close()
                working = None
            if reservation is not None:
                reservation.close()
                reservation = None

        def reserve_memory(size, retained=False):
            try:
                return budget.try_working_memory(size, retained=retained)
            except MemoryError:
                self.executors.trim_idle()
                return budget.try_working_memory(size, retained=retained)

        def admit():
            nonlocal reservation, working, creation
            if cancelled():
                raise CancelledError()
            if reusable:
                reservation = self.executors.try_reserve(ticket)
                if reservation is None:
                    return False
            try:
                if budget is not None:
                    if executor_memory and (not reusable or reservation.created):
                        creation = reserve_memory(executor_memory, retained=True)
                        if creation is None:
                            rollback()
                            return False
                    if memory_bytes:
                        working = reserve_memory(memory_bytes)
                        if working is None:
                            rollback()
                            return False
                return True
            except BaseException:
                rollback()
                raise

        token = None
        if context is None:
            context = ExecutionContext(uuid.uuid4().hex, (), "", ExecutionTarget(None, None, None,
                None, None, None, white_point=None, gamut=None, alpha_mode=None,
                input_adapter="legacy", output_adapter="legacy"), (ExecutionRequirement(stage, (tool_id,)),),
                0, *CPU_BACKEND_IDENTITY, 0, priority, role, cancelled, "cpu_adapter", f"{tool_id}:{stage}")
            token = current_execution.set(context)
        else:
            token = current_execution.set(replace(context, priority=priority, stage_id=f"{tool_id}:{stage}"))
        def finish():
            nonlocal reservation, executor
            try:
                if not reusable and executor is not None:
                    closing, executor = executor, None
                    closing.close()
            finally:
                if reservation is not None:
                    reservation.close(failed=failed)
                    reservation = None
                rollback()
        try:
            with self.compute.suspend(cancelled), ExitStack() as cleanup:
                cleanup.callback(finish)
                request = (nullcontext(demand) if demand is not None else
                           self.executors.request(key, tool_id, priority=priority, digests=digests, cancelled=cancelled)
                           if reusable else nullcontext())
                with request as ticket:
                    with self.compute.borrow(cancelled, priority=priority, role=role, threads=threads,
                                             minimum_threads=threads or 1, admit=admit) as compute, ExitStack() as active:
                        active.callback(finish)
                        admission_token = current_admission.set((budget, working))
                        active.callback(current_admission.reset, admission_token)
                        admitted = monotonic()
                        def create():
                            return factory(creation)
                        executor = reservation.initialize(create) if reusable else create()
                        creation = None
                        if cancelled() or reservation is not None and reservation.entry.retired:
                            raise CancelledError()
                        logger.log(TRACE, "执行阶段准入 request=%s stage=%s tool=%s engine=%s device=%s attempt=%s threads=%s working_bytes=%s wait_ms=%.1f",
                                   context.request_id if context else "", stage, tool_id, context.engine, context.device, context.attempt if context else 0,
                                   compute.threads, memory_bytes, (admitted - started) * 1000)
                        try:
                            yield executor, compute
                            if cancelled() or reservation is not None and reservation.entry.retired:
                                raise CancelledError()
                            failed = False
                        finally:
                            logger.log(logging.DEBUG if failed else TRACE,
                                       "执行阶段完成 request=%s stage=%s tool=%s engine=%s device=%s event=host_return duration_ms=%.1f failed=%s",
                                       context.request_id if context else "", stage, tool_id, context.engine, context.device,
                                       (monotonic() - admitted) * 1000, failed)
        finally:
            if token is not None:
                current_execution.reset(token)
