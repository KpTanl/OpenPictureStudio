from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

@dataclass(frozen=True)
class NodeSpec:
    node_type: str
    version: int = 1
    execute: Callable[[Any, dict[str, Any]], Any] | None = None
    validate_params: Callable[[dict[str, Any]], None] | None = None
    inspect_params: Callable | None = None
    accepts_context: bool = False
    required_threads: Callable | None = None
    sampling_behavior: Callable | None = None

class NodeRegistry:
    def __init__(self) -> None:
        self._items: dict[tuple[str, int], NodeSpec] = {}

    def register(self, spec: NodeSpec) -> None:
        if not isinstance(spec.node_type, str) or not spec.node_type or type(spec.version) is not int or spec.version < 1:
            raise ValueError("节点类型或版本无效")
        key = (spec.node_type, spec.version)
        if key in self._items:
            raise ValueError("节点版本重复注册")
        self._items[key] = spec

    def get(self, node_type: str, version: int = 1) -> NodeSpec | None:
        return self._items.get((node_type, version))

    def types(self) -> tuple[str, ...]:
        return tuple(sorted({kind for kind, _ in self._items}))

    def __contains__(self, node_type: str) -> bool:
        return node_type in self.types()
