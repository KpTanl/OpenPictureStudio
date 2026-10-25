"""可序列化工作流节点、连线与规范化内容哈希。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _tuple_params(params: Any) -> Any:
    """Normalize params into JSON-serializable primitives."""
    if isinstance(params, dict):
        return {str(k): _tuple_params(v) for k, v in params.items()}
    if isinstance(params, (list, tuple)):
        return [_tuple_params(v) for v in params]
    return params


@dataclass(frozen=True)
class WorkflowNode:
    """A single processing node with normalized parameters."""

    node_id: str
    node_type: str
    params: dict[str, Any] = field(default_factory=dict)
    inputs: tuple[str, ...] = ()
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "params": _tuple_params(self.params),
            "inputs": list(self.inputs),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowNode":
        if (not isinstance(data, dict) or set(data) - {"node_id", "node_type", "params", "inputs", "version"}
                or not isinstance(data.get("params", {}), dict)
                or not isinstance(data.get("inputs", []), (list, tuple))):
            raise ValueError("节点结构无效")
        return cls(
            node_id=data["node_id"],
            node_type=data["node_type"],
            params=dict(data.get("params", {})),
            inputs=tuple(data.get("inputs", ())),
            version=data.get("version", 1),
        )


@dataclass(frozen=True)
class WorkflowGraph:
    """An ordered collection of nodes plus dependency edges.

    ``edges`` are ``(source_node_id, target_node_id)`` pairs. The executor
    derives processing order from these edges and node inputs.
    """

    nodes: tuple[WorkflowNode, ...] = ()
    edges: tuple[tuple[str, str], ...] = ()
    schema_version: int = 1

    def node(self, node_id: str) -> WorkflowNode | None:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [list(e) for e in self.edges],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowGraph":
        if (not isinstance(data, dict) or set(data) - {"nodes", "edges", "schema_version"}
                or not isinstance(data.get("nodes", []), (list, tuple))
                or not isinstance(data.get("edges", []), (list, tuple))):
            raise ValueError("工作流结构无效")
        if any(not isinstance(edge, (list, tuple)) or len(edge) != 2 for edge in data.get("edges", [])):
            raise ValueError("工作流连线无效")
        return cls(
            nodes=tuple(WorkflowNode.from_dict(n) for n in data.get("nodes", ())),
            edges=tuple((a, b) for a, b in data.get("edges", ())),
            schema_version=data.get("schema_version", 1),
        )

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def workflow_hash(self) -> str:
        """Stable content hash used for cache keys and artifact identity."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def single_node(
        cls,
        node_type: str,
        params: dict[str, Any],
        *,
        node_id: str | None = None,
        version: int = 1,
    ) -> "WorkflowGraph":
        """Convenience builder for the common one-node workflow."""
        return cls(
            nodes=(
                WorkflowNode(
                    node_id=node_id or node_type,
                    node_type=node_type,
                    params=dict(params),
                    version=version,
                ),
            )
        )
