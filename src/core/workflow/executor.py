from __future__ import annotations

import copy
import hashlib
import logging
import uuid
from concurrent.futures import CancelledError
from dataclasses import replace
from contextlib import nullcontext
from time import monotonic

from ..contracts.result import CoreResult, ErrorCode, memory_error_key
from ..contracts.gpu import GpuExecutionError, cpu_recovery, recover_cpu
from ..contracts.workflow import _canonical_json
from .validator import validate

logger = logging.getLogger(__name__)


def _node_fingerprint(prefix, node):
    return hashlib.sha256((prefix + _canonical_json(node.to_dict())).encode("utf-8")).hexdigest()


def input_fingerprint(nodes):
    prefix = ""
    for node in nodes:
        prefix = _node_fingerprint(prefix, node)
    return prefix


class WorkflowExecutor:
    def __init__(self, registry, *, coordinator=None, generation=0, is_current=lambda: True):
        self.registry = registry
        self.coordinator, self.generation = coordinator, generation
        self.is_current = is_current

    def execute_region(self, value, graph, bounds, *, is_cancelled=lambda: False):
        from .regions import execute_region
        return execute_region(self, value, graph, bounds, is_cancelled)

    def preview_input_limit(self, size, graph, spec):
        from .preview import input_limit
        def describe(node, incoming):
            definition = self.registry.get(node.node_type, node.version)
            return definition.output_size(node.params, incoming) if definition.output_size is not None else None
        return input_limit(graph, size, spec, describe)

    def sampling_capability(self, graph, request):
        checked = validate(graph, self.registry)
        if not checked.success:
            return checked
        order = checked.data
        if (request.target_node is not None and request.before_node is not None
                or type(request.epoch) is not int or request.epoch < 0):
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.preview.invalid")
        if request.target_node is not None:
            if request.target_node not in order:
                return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.preview.invalid")
            start = order.index(request.target_node)
            node = graph.node(request.target_node)
            spec = self.registry.get(node.node_type, node.version)
            if spec.sampling_behavior is None or spec.sampling_behavior(node.params) != "capture":
                return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "color_adjust.mixer.unavailable")
            start += 1
        else:
            if request.before_node is not None and request.before_node not in order:
                return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.preview.invalid")
            start = order.index(request.before_node) if request.before_node is not None else len(order)
        for node_id in order[start:]:
            node = graph.node(node_id)
            spec = self.registry.get(node.node_type, node.version)
            if spec.sampling_behavior is None or spec.sampling_behavior(node.params) == "unsupported":
                return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "color_adjust.mixer.unavailable",
                                       details={"node_id": node_id})
        return CoreResult.ok()

    def execute(self, value, graph, *, is_cancelled=lambda: False, sampling=None, input_preparer=None,
                planned_stages=None):
        aborted = is_cancelled
        is_cancelled = lambda: not self.is_current() or aborted()
        if hasattr(aborted, "priority"):
            is_cancelled.priority = aborted.priority
        if is_cancelled():
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        graph = copy.deepcopy(graph)
        from ..contracts.execution import dependency_cancellation
        dependency_token = dependency_cancellation.set(is_cancelled)
        try:
            checked = validate(graph, self.registry)
        finally:
            dependency_cancellation.reset(dependency_token)
        if not checked.success:
            return checked
        if sampling is not None:
            capability = self.sampling_capability(graph, sampling.request)
            if not capability.success:
                return capability
        requires_cpu = sampling is not None
        request = (self.coordinator.request(value, graph, self.registry, self.generation, is_cancelled,
                    requires_cpu=requires_cpu, order=checked.data, planned_stages=planned_stages)
                   if self.coordinator is not None else nullcontext())
        try:
            with request as execution:
                return self._execute(value, graph, checked.data, is_cancelled, sampling, input_preparer, execution)
        except Exception as exc:
            if isinstance(exc, GpuExecutionError) and cpu_recovery.get() is None and not is_cancelled():
                from ..contracts.execution import current_execution
                parent = current_execution.get()
                if parent is not None and parent.stages:
                    raise
                logger.debug("GPU 执行恢复 CPU code=%s detail=%s", exc.code, str(exc))
                exc.__traceback__ = exc.__cause__ = exc.__context__ = None
                with recover_cpu(exc.code):
                    return self.execute(value, graph, is_cancelled=aborted, sampling=sampling, input_preparer=input_preparer)
            if isinstance(exc, CancelledError):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            if isinstance(exc, MemoryError):
                return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(exc))
            return CoreResult.from_exception(exc, message_key="error.workflow.execution_failed")

    def _execute(self, value, graph, order, is_cancelled, sampling, input_preparer, execution):
        from ..contracts.result import CoreOperationError
        from ..execution_memo import ResourceOwner
        context = {"task_id": execution.request_id if execution else uuid.uuid4().hex,
                   "progress": lambda value: None, "execution": execution}
        origin = getattr(value, "content_identity", ())
        approximate = input_preparer is not None and input_preparer.temporary_workflow is not None
        cacheable = (execution is not None and self.coordinator.memo_enabled and origin
                     and sampling is None and not approximate)
        prefixes, reusable_prefixes = [], []
        for identity in order:
            node = graph.node(identity)
            spec = self.registry.get(node.node_type, node.version)
            prefix = prefixes[-1] if prefixes else ""
            reusable = spec.cacheable is None or spec.cacheable(node.params)
            if not reusable:
                prefix += context["task_id"]
            reusable_prefixes.append(reusable and (reusable_prefixes[-1] if reusable_prefixes else True))
            prefixes.append(_node_fingerprint(prefix, node))
        requirements = {item.node_id: item for item in execution.requirements} if execution else {}
        stages = {stage.node_id: stage for stage in execution.stages} if execution else {}
        root_identity = getattr(value, "result_identity", ())
        if not root_identity and getattr(value, "gpu_image", None) is not None:
            root_identity = value.gpu_image.resident.identity
        dependencies = tuple((requirements.get(identity), stages[identity].identity)
                             for identity in order) if execution else ()

        def evaluate(position, cancelled):
            if cancelled():
                raise CancelledError()
            if position < 0:
                source = (self.coordinator.resolve(value, execution, cancelled) if execution is not None
                    else value.resolve(cancelled) if hasattr(value, "resolve") else value)
                return source
            node = graph.node(order[position])
            spec = self.registry.get(node.node_type, node.version)
            prefix = prefixes[position - 1] if position else ""
            stage = stages.get(node.node_id)
            key = (self.coordinator.results.key("checkpoint", (origin, root_identity, execution.target,
                dependencies[:position + 1], prefixes[position])) if execution else ())
            def load(abandoned):
                incoming = evaluate(position - 1, abandoned)
                params = copy.deepcopy(node.params)
                node_context = {**context, "node_id": node.node_id, "is_cancelled": abandoned,
                    "upstream_identity": (origin, prefix), "input_preparer": input_preparer,
                    "upstream_dependencies": (("input", root_identity), *dependencies[:position]),
                    "cacheable_input": reusable_prefixes[position-1] if position else True,
                    "approximate": approximate}
                if sampling is not None and (sampling.request.target_node == node.node_id
                        or sampling.active and spec.sampling_behavior(params) == "transport"):
                    node_context["sampling"] = sampling
                started = monotonic()
                from ..contracts.workflow import WorkflowGraph
                admission = (self.coordinator.stage(incoming, execution, stage, abandoned,
                    graph=WorkflowGraph(nodes=(node,), edges=()), requirement=requirements.get(node.node_id))
                    if execution else nullcontext((incoming, None)))
                with admission as (stage_input, active):
                    node_context["execution"] = active
                    if (sampling is not None and not sampling.active and sampling.request.target_node is None
                            and sampling.request.before_node == node.node_id):
                        sampling.capture_neutral(stage_input)
                    output = (spec.execute(stage_input, params, context=node_context) if spec.accepts_context
                              else spec.execute(stage_input, params))
                logger.debug("工作流节点 task=%s node=%s tool=%s duration_ms=%.1f", context["task_id"], node.node_id,
                             node.params.get("tool_id", node.node_type), (monotonic() - started) * 1000)
                if isinstance(output, CoreResult):
                    if not output.success:
                        raise CoreOperationError(self._node_error(output, node).error)
                    output = output.data
                if hasattr(incoming, "effect_status") and hasattr(output, "effect_status"):
                    if incoming.effect_status and not output.effect_status:
                        output = replace(output, effect_status=incoming.effect_status)
                if execution is not None and stage.reason != "passthrough":
                    output = replace(output, backend_identity=stage.backend_identity, result_identity=key)
                if sampling is not None and sampling.request.target_node == node.node_id and not sampling.active:
                    raise CoreOperationError(CoreResult.fail(ErrorCode.INTERNAL_ERROR, "error.tool.image_contract").error)
                if abandoned():
                    raise CancelledError()
                return output
            if not cacheable or not reusable_prefixes[position] or node.node_type in ("source", "export"):
                return load(cancelled)
            lease = self.coordinator.results.acquire(key, load, cancelled,
                getattr(cancelled, "priority", execution.priority), retain=stage.backend_identity[0] != "gpu",
                optional=stage.backend_identity[0] == "gpu")
            return replace(lease.value, execution_ownership=ResourceOwner((lease,)))
        try:
            result = evaluate(len(order) - 1, is_cancelled)
            if is_cancelled():
                raise CancelledError()
            if execution is not None and self.coordinator is not None:
                result = self.coordinator.complete(result, execution)
            if sampling is not None and not sampling.active:
                sampling.capture_neutral(result)
            return CoreResult.ok(result)
        except CoreOperationError as error:
            return CoreResult(success=False, error=error.error)
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(error))
        except GpuExecutionError:
            raise
        except Exception as error:
            return CoreResult.from_exception(error, message_key="error.workflow.execution_failed")

    @staticmethod
    def _node_error(result, node):
        details = {"node_id": node.node_id, "tool_id": node.params.get("tool_id", node.params.get("tool", node.node_type)),
                   **result.error.details}
        return CoreResult(success=False, error=replace(result.error, details=details))
