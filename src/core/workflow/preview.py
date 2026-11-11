from ..contracts.result import CoreOperationError
from .validator import validate_structure


def output_size(graph, source_size, describe, *, order=None):
    if order is None:
        checked = validate_structure(graph)
        if not checked.success:
            raise CoreOperationError(checked.error)
        order = checked.data
    size = tuple(source_size)
    for node_id in order:
        size = describe(graph.node(node_id), size)
        if size is None:
            return None
        size = tuple(size)
    return size


def input_limit(graph, source_size, display_spec, describe):
    if display_spec.max_dimension is None:
        return None
    checked = validate_structure(graph)
    if not checked.success:
        raise CoreOperationError(checked.error)
    full = output_size(graph, source_size, describe, order=checked.data)
    if full is None:
        return None
    target = display_spec.size_for(*full)
    longest = max(source_size)

    def sufficient(limit):
        ratio = min(1, limit / longest)
        size = tuple(max(1, round(side * ratio)) for side in source_size)
        result = output_size(graph, size, describe, order=checked.data)
        return result is not None and all(actual >= needed for actual, needed in zip(result, target))

    low, high = min(longest, max(target)), longest
    while low < high:
        middle = (low + high) // 2
        if sufficient(middle):
            high = middle
        else:
            low = middle + 1
    return low if low < longest else None
