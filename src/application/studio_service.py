from __future__ import annotations

from dataclasses import replace

from ..core.contracts import WorkflowGraph
from .core_api import CoreAPI
from .result_types import CoreOperationError, CoreResult, ErrorCode
from .services import DisplayFrame

STUDIO_PREVIEW_MAX_DIMENSION = 1600


def comparison_workflow(workflow):
    nodes = []
    for node in workflow.nodes:
        if (node.node_type == "crop"
                or node.node_type == "tool" and node.params.get("tool_id") == "crop"):
            nodes.append(replace(node, inputs=(nodes[-1].node_id,) if nodes else ()))
    return WorkflowGraph(nodes=tuple(nodes))


class StudioRenderService:
    def __init__(self, core=None, *, output_writer=None):
        self.core = core or CoreAPI(autosave_projects=False, output_writer=output_writer)

    def render(self, session, **options):
        result = self._render(session, display=False, **options)
        return None if result is None else result.image

    def render_display(self, session, **options):
        """同 render，返回显示用 RGBA8 帧。"""
        result = self._render(session, display=True, **options)
        if result is None:
            return None
        if result.display is not None:
            return result.display
        with result.image as image:
            return DisplayFrame.from_image(image)

    def render_sampled(self, session, sampling_request, **options):
        return self._render(session, display=True, sampling_request=sampling_request, **options)

    def render_preview(self, session, **options):
        return self._render(session, display=True, **options)

    def render_original(self, session_id, workflow, *, proxy_input, is_cancelled):
        result = self.core.preview(session_id, workflow, update_draft=False, allow_intermediate=True,
            max_dimension=STUDIO_PREVIEW_MAX_DIMENSION if proxy_input else None,
            is_cancelled=is_cancelled, priority=2)
        if not result.success:
            raise CoreOperationError(result.error)
        return result.data

    def _render(self, session, *, display, use_proxy=True, editing_tool=None, is_cancelled=lambda: False,
                allow_intermediate=False, source_identity=None, source_preview=None, sampling_request=None,
                priority=0, keep_full_display=False, temporary_workflow=None):
        caller_cancelled = is_cancelled
        is_cancelled = lambda: caller_cancelled() or (
            source_identity is not None and self.core.source_identity(session.core_id) != source_identity) or (
            source_preview is not None and self.core.source_preview(session.core_id) != source_preview)
        if getattr(session, "read_only", False):
            if sampling_request is not None:
                raise CoreOperationError(CoreResult.fail(
                    ErrorCode.TOOL_INCOMPATIBLE, "color_adjust.mixer.unavailable").error)
            if is_cancelled():
                return None
            result = self.core.history_preview(session.core_id, revision_id=session.revision_id,
                                               max_dimension=STUDIO_PREVIEW_MAX_DIMENSION if use_proxy else None,
                                               is_cancelled=is_cancelled, display=display,
                                               priority=priority, keep_full_display=keep_full_display,
                                               expected_state=session.state_version if not allow_intermediate else None)
            if is_cancelled():
                if result.success:
                    result.data.close()
                return None
            if not result.success:
                if result.error.code in ("REVISION_NOT_FOUND", "CANCELLED"):
                    return None
                raise CoreOperationError(result.error)
            return result.data
        result = self.core.preview(
            session.core_id, session.workflow,
            update_draft=False, is_cancelled=is_cancelled,
            max_dimension=STUDIO_PREVIEW_MAX_DIMENSION if use_proxy else None,
            allow_intermediate=allow_intermediate, display=display,
            priority=priority, keep_full_display=keep_full_display,
            expected_state=session.state_version if not allow_intermediate else None,
            **({"temporary_workflow": temporary_workflow} if temporary_workflow is not None else {}),
            **({"sampling_request": sampling_request} if sampling_request is not None else {}),
        )
        if not result.success:
            if result.error.code == "CANCELLED" or is_cancelled():
                return None
            raise CoreOperationError(result.error)
        return result.data

    @staticmethod
    def histogram(frame):
        from .display_histogram import display_histogram

        return display_histogram(frame.size, frame.rgba)
