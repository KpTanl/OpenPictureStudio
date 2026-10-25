from __future__ import annotations

import copy
import hashlib
import logging
import uuid
from concurrent.futures import CancelledError
from dataclasses import replace
from time import monotonic

from ..contracts.result import CoreResult, ErrorCode, memory_error_key
from .validator import dependencies, validate

logger = logging.getLogger(__name__)


def input_fingerprint(nodes):
    prefix = ""
    for node in nodes:
        prefix = hashlib.sha256((prefix + str(node.to_dict())).encode("utf-8")).hexdigest()
    return prefix


class WorkflowExecutor:
    def __init__(self, registry):
        self.registry = registry

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

    def execute(self, value, graph, *, is_cancelled=lambda: False, sampling=None, input_preparer=None):
        graph = copy.deepcopy(graph)
        checked = validate(graph, self.registry)
        if not checked.success:
            return checked
        if sampling is not None:
            capability = self.sampling_capability(graph, sampling.request)
            if not capability.success:
                return capability
        parents = dependencies(graph)
        values = {}
        context = {"task_id": uuid.uuid4().hex, "is_cancelled": is_cancelled, "progress": lambda value: None}
        upstream = {}
        origin = getattr(value, "content_identity", ())
        node = None
        try:
            for node_id in checked.data:
                if is_cancelled():
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                node = graph.node(node_id)
                spec = self.registry.get(node.node_type, node.version)
                parent = next(iter(parents[node_id]), None)
                incoming = values[parent] if parent else value
                if (sampling is not None and not sampling.active and sampling.request.target_node is None
                        and sampling.request.before_node == node_id):
                    sampling.capture_neutral(incoming)
                params = copy.deepcopy(node.params)
                started = monotonic()
                prefix = upstream.get(parent, "")
                node_context = {**context, "node_id": node_id, "upstream_identity": (origin, prefix),
                                "input_preparer": input_preparer}
                if sampling is not None and (sampling.request.target_node == node_id
                        or sampling.active and spec.sampling_behavior(params) == "transport"):
                    node_context = {**node_context, "sampling": sampling}
                output = (spec.execute(incoming, params, context=node_context) if spec.accepts_context
                          else spec.execute(incoming, params))
                logger.debug("工作流节点 task=%s node=%s tool=%s duration_ms=%.1f",
                             context["task_id"], node.node_id,
                             node.params.get("tool_id", node.params.get("tool", node.node_type)),
                             (monotonic() - started) * 1000)
                if isinstance(output, CoreResult):
                    if not output.success:
                        return self._node_error(output, node)
                    output = output.data
                if hasattr(incoming, "effect_status") and hasattr(output, "effect_status"):
                    if incoming.effect_status and not output.effect_status:
                        output = replace(output, effect_status=incoming.effect_status)
                upstream[node_id] = hashlib.sha256((prefix + str(node.to_dict())).encode("utf-8")).hexdigest()
                values[node_id] = output
                if sampling is not None and sampling.request.target_node == node_id and not sampling.active:
                    return CoreResult.fail(ErrorCode.INTERNAL_ERROR, "error.tool.image_contract")
                if parent is not None:
                    del values[parent]
                del incoming, output
            if is_cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            result = values[checked.data[-1]] if checked.data else value
            if sampling is not None and not sampling.active:
                sampling.capture_neutral(result)
            return CoreResult.ok(result)
        except Exception as exc:
            if isinstance(exc, CancelledError):
                result = CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            elif isinstance(exc, MemoryError):
                result = CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(exc))
            else:
                result = CoreResult.from_exception(exc, message_key="error.workflow.execution_failed")
            return self._node_error(result, node) if node is not None else result

    @staticmethod
    def _node_error(result, node):
        details = {"node_id": node.node_id, "tool_id": node.params.get("tool_id", node.params.get("tool", node.node_type)),
                   **result.error.details}
        return CoreResult(success=False, error=replace(result.error, details=details))
