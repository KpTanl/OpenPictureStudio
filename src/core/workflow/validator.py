from __future__ import annotations

import json
from collections import deque

from ..contracts import WorkflowGraph
from ..contracts.result import CoreResult, ErrorCode
from .registry import NodeRegistry


def dependencies(graph):
    parents = {node.node_id: set(node.inputs) for node in graph.nodes}
    for source, target in graph.edges:
        parents[target].add(source)
    return parents


def validate_structure(graph: WorkflowGraph):
    if not isinstance(graph, WorkflowGraph) or type(graph.schema_version) is not int or graph.schema_version != 1:
        return CoreResult.fail(ErrorCode.WORKFLOW_INVALID, "error.workflow.invalid")
    try:
        json.dumps(graph.to_dict(), allow_nan=False)
        ids = [node.node_id for node in graph.nodes]
        if any(not isinstance(key, str) or not key for key in ids) or len(ids) != len(set(ids)):
            raise ValueError("节点标识无效或重复")
        known = set(ids)
        for node in graph.nodes:
            if not isinstance(node.node_type, str) or not node.node_type or type(node.version) is not int or node.version < 1:
                raise ValueError("节点类型或版本无效")
            if not isinstance(node.params, dict):
                raise ValueError("节点参数必须为对象")
            _validate_json(node.params)
            if not isinstance(node.inputs, (tuple, list)) or any(not isinstance(key, str) for key in node.inputs):
                raise ValueError("节点输入无效")
            if any(dep not in known for dep in node.inputs):
                raise ValueError("节点输入不存在")
        for source, target in graph.edges:
            if source not in known or target not in known:
                raise ValueError("连线引用不存在的节点")
        parents = dependencies(graph)
        if any(len(inputs) > 1 for inputs in parents.values()):
            return CoreResult.fail(ErrorCode.PORT_TYPE_MISMATCH, "error.workflow.single_input_required")
        outgoing = {key: [] for key in ids}
        indegree = {key: len(inputs) for key, inputs in parents.items()}
        for target, inputs in parents.items():
            for source in inputs:
                outgoing[source].append(target)
        queue = deque(key for key in ids if not indegree[key])
        order = []
        while queue:
            current = queue.popleft()
            order.append(current)
            for target in outgoing[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if len(order) != len(ids):
            return CoreResult.fail(ErrorCode.CYCLE_DETECTED, "error.workflow.cycle")
        if ids and (sum(not parents[key] for key in ids) != 1 or
                    sum(not outgoing[key] for key in ids) != 1):
            raise ValueError("工作流必须为单输入单输出链")
        for node in graph.nodes:
            if node.node_type == "source" and parents[node.node_id]:
                raise ValueError("源节点不得有输入")
            if node.node_type == "export" and outgoing[node.node_id]:
                raise ValueError("导出节点必须为末端")
        return CoreResult.ok(tuple(order))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return CoreResult.from_exception(exc, code=ErrorCode.WORKFLOW_INVALID,
                                         message_key="error.workflow.invalid")


def _validate_json(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON 字段名必须为字符串")
        for item in value.values():
            _validate_json(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_json(item)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("参数必须可序列化")


def inspect_workflow(graph: WorkflowGraph, registry: NodeRegistry):
    structure = validate_structure(graph)
    if not structure.success:
        return structure
    issues = []
    for node in graph.nodes:
        spec = registry.get(node.node_type, node.version)
        if spec is None:
            code = ErrorCode.TOOL_INCOMPATIBLE if node.node_type in registry else ErrorCode.NODE_NOT_REGISTERED
            issue = CoreResult.fail(code, "error.tool.incompatible" if node.node_type in registry
                                   else "error.workflow.node_not_registered")
        elif spec.execute is None:
            issue = CoreResult.fail(ErrorCode.TOOL_MISSING, "error.tool.missing")
        else:
            try:
                if spec.inspect_params:
                    issue = spec.inspect_params(node.params)
                    if not isinstance(issue, CoreResult):
                        issue = CoreResult.fail(ErrorCode.INTERNAL_ERROR, "error.workflow.params")
                else:
                    if spec.validate_params:
                        spec.validate_params(node.params)
                    issue = CoreResult.ok(None)
            except (ValueError, TypeError, KeyError):
                issue = CoreResult.fail(ErrorCode.PARAMETER_INVALID, "error.workflow.params")
        if not issue.success:
            error = issue.error.to_dict()
            error["details"] = {**error["details"], "node_id": node.node_id}
            issues.append(error)
    return CoreResult.ok({"can_execute": not issues, "issues": issues, "order": structure.data})


def validate(graph: WorkflowGraph, registry: NodeRegistry):
    inspected = inspect_workflow(graph, registry)
    if not inspected.success:
        return inspected
    if inspected.data["issues"]:
        issue = inspected.data["issues"][0]
        return CoreResult.fail(issue["code"], issue["message_key"], details=issue["details"])
    return CoreResult.ok(inspected.data["order"])


validate_for_execution = validate
