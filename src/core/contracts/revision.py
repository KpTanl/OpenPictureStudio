"""Revision contract.

A revision is an immutable node in the edit history. Revisions never mutate
their ancestors; undo/redo/checkout only move the current pointer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .artifact import ArtifactRef


@dataclass(frozen=True)
class RevisionRef:
    """Immutable revision node referencing a workflow hash and artifacts."""

    revision_id: str
    parent_id: str | None = None
    workflow_hash: str = ""
    artifact_refs: tuple[ArtifactRef, ...] = ()
    label: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "parent_id": self.parent_id,
            "workflow_hash": self.workflow_hash,
            "artifact_refs": [a.to_dict() for a in self.artifact_refs],
            "label": self.label,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RevisionRef":
        return cls(
            revision_id=str(data["revision_id"]),
            parent_id=data.get("parent_id"),
            workflow_hash=str(data.get("workflow_hash", "")),
            artifact_refs=tuple(
                ArtifactRef.from_dict(a) for a in data.get("artifact_refs", ())
            ),
            label=str(data.get("label", "")),
            created_at=float(data.get("created_at", 0.0)),
        )
