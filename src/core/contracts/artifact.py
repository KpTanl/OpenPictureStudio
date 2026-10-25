"""Artifact contract.

An artifact is *any* produced output that must be referenced and reclaimed:
preview images, full-resolution renders, masks, AI outputs, histograms, LUTs.

Only the immutable reference is defined here. Bytes live in an ArtifactStore
(introduced in Phase 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LINEAR_ARTIFACT = "render-linear-v1"


@dataclass(frozen=True)
class ArtifactRef:
    """Immutable, content-addressed reference to a produced artifact."""

    hash: str
    kind: str = "render"                  # render | preview | mask | analysis | lut
    media_type: str = "image/png"
    producer_node: str = ""
    engine_version: str = ""
    source_hash: str = ""
    external_resource_hash: str = ""
    color_context: str = "linear_srgb"
    path: str = ""                        # optional local cache location

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash": self.hash,
            "kind": self.kind,
            "media_type": self.media_type,
            "producer_node": self.producer_node,
            "engine_version": self.engine_version,
            "source_hash": self.source_hash,
            "external_resource_hash": self.external_resource_hash,
            "color_context": self.color_context,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactRef":
        return cls(
            hash=str(data["hash"]),
            kind=str(data.get("kind", "render")),
            media_type=str(data.get("media_type", "image/png")),
            producer_node=str(data.get("producer_node", "")),
            engine_version=str(data.get("engine_version", "")),
            source_hash=str(data.get("source_hash", "")),
            external_resource_hash=str(data.get("external_resource_hash", "")),
            color_context=str(data.get("color_context", "linear_srgb")),
            path=str(data.get("path", "")),
        )
