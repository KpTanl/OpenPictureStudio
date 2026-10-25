from __future__ import annotations

from dataclasses import dataclass, replace
import logging

from ..core.contracts.tools import ToolReference
from ..core.contracts.workflow import WorkflowNode
from ..core.tools.effect_migration import reference_params
from ..core.tools.managed_models import MANAGED_BEAUTY_PARAMETERS


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BeautyAnalysisNotice:
    status: str
    skipped: int


def beauty_analysis_outcome(job, cancelled):
    from .result_types import CoreOperationError, ErrorCode
    try:
        return job(cancelled)
    except CoreOperationError as error:
        status = error.error.message_key.removeprefix("beauty.detail.")
        if (error.error.code != ErrorCode.INVALID_INPUT or error.error.details.get("tool_id") != "beauty"
                or status not in ("no_face", "face_too_small", "insufficient_landmarks")):
            raise
        skipped = error.error.details.get("skipped", 0)
        logger.info("美颜分析 status=%s processed=0 skipped=%s", status, skipped)
        return BeautyAnalysisNotice(status, skipped)


@dataclass(frozen=True)
class BeautyAnalysisTask:
    core: object
    session_id: str
    workflow: object
    version: int
    source_identity: object
    tool_generation: int
    preset: bool

    def is_current(self):
        return self._state_current() and self.core.image_sources.matches_file(self.source_identity)

    def _state_current(self):
        return (self.core.matches_edit_state(self.session_id, self.version, self.source_identity)
                and self.core.tools.is_current(self.tool_generation))

    @property
    def commit_stamp(self):
        return self.version, self.tool_generation, self.source_identity

    def __call__(self, cancelled):
        from .result_types import CoreOperationError
        result = self.core.analyze_beauty(self.session_id, self.workflow, expected_version=self.version,
            is_cancelled=lambda: cancelled() or not self._state_current(), preset=self.preset)
        if not result.success:
            raise CoreOperationError(result.error)
        return result.data


def query_face_regions(detector, image, params, cancelled, *, photo=None, workflow=None):
    query = getattr(detector, "query", None)
    return (query(image, params, cancelled, photo=photo, workflow=workflow) if query is not None
            else detector(image, params, cancelled))


class BeautyFaceQuery:
    def __init__(self, core):
        self.core = core

    def __call__(self, image, params, cancelled):
        settings = {**MANAGED_BEAUTY_PARAMETERS.defaults(3),
                    "face_detection_model": self.core.models.default("face_detection"), **params}
        result = self.core.tools.analyze("beauty", image, settings, is_cancelled=cancelled, purpose="regions")
        return result.data if result.success else None

    def query(self, image, params, cancelled, *, photo=None, workflow=None):
        if photo is None:
            return self(image, params, cancelled)
        workflow = workflow or photo.workflow
        checked = self.core.tools.validate(workflow)
        if not checked.success:
            return None
        nodes = tuple(workflow.node(node_id) for node_id in checked.data if workflow.node(node_id).node_type != "export")
        ids = {node.node_id for node in nodes}
        workflow = replace(workflow, nodes=nodes, edges=tuple(edge for edge in workflow.edges
                                                            if edge[0] in ids and edge[1] in ids))
        original = next((node for node in workflow.nodes if node.node_type == "tool"
                         and node.params.get("tool_id") == "beauty"), None)
        if original is None:
            original = next((node for node in photo.workflow.nodes if node.node_type == "tool"
                             and node.params.get("tool_id") == "beauty"), None)
        reference = ToolReference.from_dict(original.params) if original is not None else None
        params = original.params["settings"] if original is not None else params
        if reference is not None and reference.params_version < 3:
            result = self.core.tools.analyze("beauty", image, params, reference=reference,
                                             is_cancelled=cancelled, purpose="regions")
            return result.data if result.success else None
        settings = {**MANAGED_BEAUTY_PARAMETERS.defaults(3),
                    "face_detection_model": self.core.models.default("face_detection"), **params}
        settings["skin_smooth"] = max(1, settings["skin_smooth"])
        bound = self.core.tools.effect_reference("beauty", settings, reference=reference)
        if not bound.success:
            return None
        node_id = "beauty_face_query"
        existing = {node.node_id for node in workflow.nodes}
        while node_id in existing:
            node_id += "_"
        node = WorkflowNode(node_id, "tool", reference_params(bound.data, settings),
                            inputs=(workflow.nodes[-1].node_id,) if workflow.nodes else ())
        query_workflow = replace(workflow, nodes=(*workflow.nodes, node))
        result = self.core.analyze_beauty(photo.session_id, query_workflow, expected_version=photo.state_version,
            is_cancelled=cancelled, purpose="regions", target_node=node_id)
        return result.data if result.success else None
