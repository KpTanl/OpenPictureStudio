"""RenderSource contract.

A lightweight, serializable *descriptor* of a decoded source image. It is the
Core-facing identity of a source file (path + hash + geometry + color basis),
**not** the engine's pixel buffer.

Do not confuse this with ``src.core.photo_engine.RenderSource`` which carries
``pixels: np.ndarray``. The engine buffer never crosses the Core API boundary;
the UI and Agent only ever see this descriptor plus artifact references.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceBinding:
    source_id: str
    path: str
    file_hash: str
    is_raw: bool


@dataclass(frozen=True)
class RenderSource:
    """Immutable identity/descriptor of a decoded source."""

    source_id: str
    path: str
    file_hash: str = ""
    width: int = 0
    height: int = 0
    source_format: str = "srgb8"          # 'raw' | 'srgb8' | 'srgb16'
    color_pipeline: str = "srgb-direct"   # 'dcp:<name>' | 'libraw' | 'srgb-direct'
    note: str = ""
    is_raw: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "path": self.path,
            "file_hash": self.file_hash,
            "width": self.width,
            "height": self.height,
            "source_format": self.source_format,
            "color_pipeline": self.color_pipeline,
            "note": self.note,
            "is_raw": self.is_raw,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RenderSource":
        return cls(
            source_id=str(data["source_id"]),
            path=str(data.get("path", "")),
            file_hash=str(data.get("file_hash", "")),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            source_format=str(data.get("source_format", "srgb8")),
            color_pipeline=str(data.get("color_pipeline", "srgb-direct")),
            note=str(data.get("note", "")),
            is_raw=bool(data.get("is_raw", False)),
            metadata=dict(data.get("metadata", {})),
        )
