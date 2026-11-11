import copy
from concurrent.futures import CancelledError
from dataclasses import dataclass, replace
from time import monotonic

from ..contracts.result import CoreOperationError, CoreResult, ErrorCode
from ..contracts.workflow import WorkflowGraph
from ..contracts.execution import CPU_BACKEND_IDENTITY
from ..execution_memo import ResourceOwner
from .validator import validate
from .executor import _node_fingerprint, logger


@dataclass(frozen=True)
class RegionResult:
    source: object
    bounds: tuple
    full_size: tuple


def crop_region(source, bounds, cancelled=lambda: False):
    pixels, alpha = source.read_region(bounds, cancelled)
    return replace(source, pixels=pixels.copy(), alpha=None if alpha is None else alpha.copy(),
                   patches=(), patch_base_identity=(), execution_ownership=None, gpu_image=None,
                   backend_identity=CPU_BACKEND_IDENTITY)


def execute_region(executor, value, graph, bounds, cancelled):
    graph = copy.deepcopy(graph)
    checked = validate(graph, executor.registry)
    if not checked.success:
        return checked
    order = checked.data
    if value.is_proxy:
        return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible", details={"requires_full_input": True})
    if (not isinstance(bounds, tuple) or len(bounds) != 4 or any(type(item) is not int for item in bounds)
            or min(bounds[:2]) < 0 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]):
        return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.preview.invalid")
    if executor.coordinator is None:
        return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.preview.invalid")
    abort = lambda: cancelled() or not executor.is_current()
    full, sizes = {}, {}
    try:
        with executor.coordinator.request(value, graph, executor.registry, executor.generation, abort,
                requires_cpu=True, order=order) as execution:
            prefixes, reusable_prefixes = [], []
            for node_id in order:
                node = graph.node(node_id)
                spec = executor.registry.get(node.node_type, node.version)
                prefix = prefixes[-1] if prefixes else ""
                reusable = spec.cacheable is None or spec.cacheable(node.params)
                if not reusable:
                    prefix += execution.request_id
                reusable_prefixes.append(reusable and (reusable_prefixes[-1] if reusable_prefixes else True))
                prefixes.append(_node_fingerprint(prefix, node))

            def complete(position):
                if position not in full:
                    if abort():
                        raise CancelledError()
                    if position < 0:
                        source = value.resolve(abort) if hasattr(value, "resolve") else value
                        full[position] = source.materialize(abort)
                    else:
                        nodes = tuple(graph.node(node_id) for node_id in order[:position+1])
                        ids = {node.node_id for node in nodes}
                        prefix = WorkflowGraph(nodes=nodes, edges=tuple(edge for edge in graph.edges
                            if edge[0] in ids and edge[1] in ids))
                        result = executor.execute(value, prefix, is_cancelled=abort)
                        if not result.success:
                            raise CoreOperationError(result.error)
                        full[position] = result.data
                return full[position]

            def size(position):
                if position not in sizes:
                    if position < 0:
                        sizes[position] = value.pixels.shape[1::-1]
                    else:
                        node = graph.node(order[position])
                        spec = executor.registry.get(node.node_type, node.version)
                        dimensions = spec.output_size(node.params, size(position-1)) if spec.output_size else None
                        sizes[position] = tuple(dimensions) if dimensions is not None else complete(position).pixels.shape[1::-1]
                return sizes[position]

            def extract(position, region, abandoned):
                source = complete(position)
                memory = (region[2]-region[0])*(region[3]-region[1])*32
                with executor.coordinator.operation("region.read", abandoned, memory_bytes=memory):
                    return executor.coordinator.protect(crop_region(source, region, abandoned))

            def evaluate(position, region, abandoned):
                if abandoned():
                    raise CancelledError()
                if position < 0:
                    return extract(-1, region, abandoned)
                node = graph.node(order[position])
                spec = executor.registry.get(node.node_type, node.version)
                incoming_size, output_size = size(position-1), size(position)
                required = spec.region_input(node.params, region, incoming_size) if spec.region_input else None
                if required is None or spec.execute_region is None:
                    return extract(position, region, abandoned)
                key = executor.coordinator.results.key("region.checkpoint", (execution.content_identity,
                    execution.target, execution.requirements, execution.backend_identity,
                    prefixes[position], region, incoming_size, output_size))
                def calculate(abort_stage):
                    incoming = evaluate(position-1, tuple(required), abort_stage)
                    context = {"task_id": execution.request_id, "node_id": node.node_id, "is_cancelled": abort_stage,
                        "upstream_identity": (execution.content_identity, prefixes[position-1] if position else ""),
                        "upstream_dependencies": tuple(item for item in execution.requirements
                            if item.node_id in order[:position]),
                        "cacheable_input": reusable_prefixes[position-1] if position else True,
                        "input_bounds": tuple(required), "output_bounds": region,
                        "full_input_size": incoming_size, "full_output_size": output_size}
                    started = monotonic()
                    result = spec.execute_region(incoming, copy.deepcopy(node.params), context=context)
                    if isinstance(result, CoreResult):
                        if not result.success:
                            raise CoreOperationError(result.error)
                        result = result.data
                    if result.pixels.shape[1::-1] != (region[2]-region[0], region[3]-region[1]):
                        raise ValueError("区域输出尺寸无效")
                    if abort_stage():
                        raise CancelledError()
                    logger.debug("区域执行 node=%s bounds=%s input=%s full_size=%s duration_ms=%.1f",
                        node.node_id, region, required, output_size, (monotonic()-started)*1000)
                    return replace(result, orig_size=output_size)
                if not reusable_prefixes[position] or not executor.coordinator.memo_enabled:
                    return calculate(abandoned)
                lease = executor.coordinator.results.acquire(key, calculate, abandoned,
                    execution.priority, optional=True, retain=executor.coordinator.memo_enabled)
                return replace(lease.value, execution_ownership=ResourceOwner((lease,)))

            full_size = size(len(order)-1)
            left, top, right, bottom = bounds
            right, bottom = min(right, full_size[0]), min(bottom, full_size[1])
            if left >= right or top >= bottom:
                return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.preview.invalid")
            region = left, top, right, bottom
            rendered = evaluate(len(order)-1, region, abort)
            if abort():
                raise CancelledError()
            return CoreResult.ok(RegionResult(rendered, region, full_size))
    except CancelledError:
        return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
    except CoreOperationError as error:
        return CoreResult.from_exception(error)
    except Exception as error:
        return CoreResult.from_exception(error, message_key="error.preview.failed")
