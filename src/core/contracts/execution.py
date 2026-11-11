from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable


CPU_BACKEND_IDENTITY = ("cpu", "cpu-5", "cpu")


@dataclass(frozen=True)
class ExecutionTarget:
    size: tuple[int, int] | None
    reference_size: tuple[int, int] | None
    proxy: bool | None
    colorspace: str | None
    dtype: str | None
    alpha: bool | None
    white_point: str | None = "D65"
    gamut: str | None = "srgb"
    color_pipeline: str = ""
    alpha_mode: str | None = "straight"
    input_adapter: str = "host-f32"
    output_adapter: str = "host-f32"


@dataclass(frozen=True)
class ExecutionRequirement:
    node_id: str
    implementation: tuple
    dependencies: tuple = ()
    engines: tuple[str, ...] = ("cpu",)


@dataclass(frozen=True)
class ExecutionStage:
    node_id: str | None
    backend_identity: tuple
    reason: str
    preparation_backend: tuple = ()
    preparation_stages: tuple[ExecutionStage, ...] = ()
    input_adapter: object = ()

    @property
    def identity(self):
        return self.backend_identity, self.preparation_backend, self.input_adapter, tuple(
            (stage.node_id, stage.identity) for stage in self.preparation_stages)


@dataclass(frozen=True)
class ExecutionContext:
    request_id: str
    content_identity: tuple
    workflow_identity: str
    target: ExecutionTarget
    requirements: tuple[ExecutionRequirement, ...]
    runtime_generation: int
    engine: str
    backend_revision: str
    device: str
    attempt: int
    priority: object
    role: str
    is_cancelled: Callable[[], bool]
    selection_reason: str = ""
    stage_id: str = ""
    source_path: str = ""
    stages: tuple[ExecutionStage, ...] = ()
    force_cpu: bool = False
    input_identity: tuple = ()
    output_boundary: bool = True

    @property
    def backend_identity(self):
        return self.engine, self.backend_revision, self.device

    @property
    def result_identity(self):
        return (self.content_identity, self.workflow_identity, self.target, self.requirements, self.stages,
                *self.backend_identity)


current_execution: ContextVar[ExecutionContext | None] = ContextVar("ops_execution", default=None)
current_admission = ContextVar("ops_admission", default=None)
dependency_cancellation = ContextVar("ops_dependency_cancellation", default=None)
