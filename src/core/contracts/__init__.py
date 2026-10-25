"""Core contracts: serializable, std-lib-only data structures shared by the
Application layer and the UI.

These types intentionally contain no pixel buffers, no Qt objects and no engine
internals. They are the stable vocabulary exchanged across the Core API boundary.
"""

from .source import RenderSource, SourceBinding
from .workflow import WorkflowNode, WorkflowGraph
from .artifact import ArtifactRef
from .revision import RevisionRef

__all__ = [
    "RenderSource",
    "SourceBinding",
    "WorkflowNode",
    "WorkflowGraph",
    "ArtifactRef",
    "RevisionRef",
]
