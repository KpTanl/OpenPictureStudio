from __future__ import annotations

import hashlib
from pathlib import Path

from ..contracts import WorkflowGraph
from .project_store import atomic_write, check_cancelled, read_bytes


class RevisionStore:
    def __init__(self, root: str):
        self.root = Path(root)

    def write_workflow(self, workflow_hash: str, payload: dict, *, is_cancelled=lambda: False) -> str:
        check_cancelled(is_cancelled)
        data = WorkflowGraph.from_dict(payload).canonical_json().encode("utf-8")
        if hashlib.sha256(data).hexdigest() != workflow_hash:
            raise ValueError("工作流哈希不匹配")
        path = self.root / f"{workflow_hash}.json"
        if path.exists():
            if read_bytes(path, is_cancelled) != data:
                raise ValueError("已有工作流快照损坏")
        else:
            atomic_write(path, data, is_cancelled=is_cancelled)
        return str(path)
