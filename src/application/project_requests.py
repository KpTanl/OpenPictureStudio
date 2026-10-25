from __future__ import annotations

import logging
from dataclasses import dataclass, field
from time import monotonic

from ..core.project import ProjectDocument
from .output_writer import OperationCancellation


@dataclass(frozen=True)
class ProjectSaveSnapshot:
    session_id: str
    state_version: int
    source_id: str
    path: str
    document: ProjectDocument
    bound_path: str | None
    bound_hash: str | None


@dataclass(frozen=True)
class ProjectRequest:
    request_id: int
    generation: int
    purpose: str
    path: str
    session_id: str | None = None
    source_id: str | None = None
    state_version: int | None = None
    source_path: str | None = None
    snapshot: ProjectSaveSnapshot | None = None
    cancellation: OperationCancellation = field(default_factory=OperationCancellation, compare=False)
    queued_at: float = field(default_factory=monotonic)


def record_project_stage(request, stage, started):
    logging.getLogger(__name__).debug(
        "项目任务：task_id=%s source_id=%s session_id=%s state_version=%s generation=%s "
        "purpose=%s stage=%s elapsed_ms=%.1f",
        request.request_id if request else None, request.source_id if request else None,
        request.session_id if request else None, request.state_version if request else None,
        request.generation if request else None, request.purpose if request else None,
        stage, (monotonic() - started) * 1000,
    )
