from __future__ import annotations

import logging
from dataclasses import replace

from ..core.contracts import WorkflowGraph
from .core_api import CoreAPI
from .result_types import CoreOperationError, CoreResult, ErrorCode
from .services import DisplayFrame, PreviewResult
from ..core.contracts.image_display import PreviewDisplaySpec

STUDIO_PREVIEW_MAX_DIMENSION = 1600
logger = logging.getLogger(__name__)


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

    def render_preview(self, session, *, cached_preview=None, **options):
        if cached_preview is not None and not session.read_only:
            result = self._cached_preview(session, *cached_preview, **options)
            if result is not None:
                return result
        return self._render(session, display=True, **options)

    def _cached_preview(self, session, key, frame, *, source_identity=None, source_preview=None,
                        is_cancelled=lambda: False, max_dimension=STUDIO_PREVIEW_MAX_DIMENSION, **options):
        from .studio_preview_cache import preview_variant
        from ..core.contracts.execution import dependency_cancellation
        tools, sources = self.core.tools, self.core.image_sources
        spec = PreviewDisplaySpec(max_dimension)
        if (is_cancelled() or frame.display_spec != spec or frame.backend_identity != key.backend_identity
                or not key.matches_file() or not tools.cacheable_workflow(session.workflow)
                or not self.core.matches_edit_state(session.core_id, session.state_version, source_identity)
                or self.core.source_preview(session.core_id) != source_preview):
            return None
        snapshot = tools.snapshot(is_cancelled=is_cancelled)
        if not key.variant.startswith(preview_variant(session.workflow, snapshot.generation, spec, frame.backend_identity)):
            return None
        checked = tools.validate(session.workflow)
        if not checked.success:
            raise CoreOperationError(checked.error)
        token = dependency_cancellation.set(is_cancelled)
        try:
            for node in session.workflow.nodes:
                definition = snapshot.registry.get(node.node_type, node.version)
                if definition.execution_requirements is not None:
                    definition.execution_requirements(node.params, node.node_id)
        finally:
            dependency_cancellation.reset(token)
        if (is_cancelled() or snapshot.is_cancelled() or not sources.matches_file(source_identity)
                or not self.core.matches_edit_state(session.core_id, session.state_version, source_identity)
                or self.core.source_preview(session.core_id) != source_preview):
            return None
        logger.debug("预览帧命中 session=%s workflow=%s size=%sx%s",
            session.core_id, session.workflow.workflow_hash()[:12], *frame.size)
        return PreviewResult(None, session.core_id, session.workflow.workflow_hash(), *frame.size,
            display=DisplayFrame(frame.rgba, frame.size, frame.backend_identity), full_size=frame.full_size,
            effect_status=frame.effect_status, input_size=frame.input_size, input_is_proxy=frame.input_is_proxy,
            display_spec=frame.display_spec, backend_identity=frame.backend_identity)

    def render_region(self, session, bounds, *, source_identity, source_preview, is_cancelled=lambda: False):
        from .services import DetailDisplay, PreviewResult
        from ..core.photo_engine import display_rgba8
        tools, sources = self.core.tools, self.core.image_sources
        snapshot = tools.snapshot()
        def cancelled():
            return (is_cancelled() or snapshot.is_cancelled()
                or not self.core.matches_edit_state(session.core_id, session.state_version, source_identity)
                or self.core.source_preview(session.core_id) != source_preview)
        with sources.borrow_linear(source_identity.path, identity=source_identity, is_cancelled=cancelled) as lease:
            with sources.compute.request_context(cancelled, priority=1, role="foreground",
                    threads=sources.policy.interactive_threads):
                result = snapshot.executor.execute_region(lease.value, session.workflow, bounds, is_cancelled=cancelled)
                if not result.success:
                    raise CoreOperationError(result.error)
                region = result.data
                source = region.source
                size = source.pixels.shape[1::-1]
                with tools.execution.output_operation(source, "region.display", cancelled,
                        threads=sources.policy.interactive_threads, memory_bytes=source.pixels.nbytes*4):
                    detail = DetailDisplay(sources.resources, size,
                        lambda: DisplayFrame.from_array(display_rgba8(source, cancelled=cancelled)), cancelled,
                        bounds=region.bounds, full_size=region.full_size)
                if cancelled() or not sources.matches_file(source_identity):
                    detail.close()
                    return None
                return PreviewResult(None, session.core_id, session.workflow.workflow_hash(), *size,
                    detail=detail, full_size=region.full_size, effect_status=source.effect_status,
                    input_size=lease.value.pixels.shape[1::-1], input_is_proxy=False)

    def render_original(self, session_id, workflow, *, proxy_input, is_cancelled, max_dimension=None):
        """改前画面。max_dimension 是改后画面所用的显示上限，两边取同样的输入，画质才对得上。"""
        result = self.core.preview(session_id, workflow, update_draft=False, allow_intermediate=True,
            max_dimension=(max_dimension or STUDIO_PREVIEW_MAX_DIMENSION) if proxy_input else None,
            is_cancelled=is_cancelled, priority=2)
        if not result.success:
            raise CoreOperationError(result.error)
        return result.data

    def _render(self, session, *, display, use_proxy=True, editing_tool=None, is_cancelled=lambda: False,
                allow_intermediate=False, source_identity=None, source_preview=None, sampling_request=None,
                priority=0, keep_full_display=False, max_dimension=STUDIO_PREVIEW_MAX_DIMENSION,
                source_is_cancelled=None, display_limit=None, display_region=None):
        """use_proxy 为假时按完整输入计算；此时给了 display_limit，就只取这么大的显示帧，不取整幅。

        display_region 为 ((left, top, right, bottom), (宽, 高))：输出是这个尺寸时显示帧只取这一块。"""
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
                                               max_dimension=max_dimension if use_proxy else None,
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
            max_dimension=max_dimension if use_proxy else display_limit,
            full_input=not use_proxy and display_limit is not None,
            allow_intermediate=allow_intermediate, display=display,
            priority=priority, keep_full_display=keep_full_display,
            expected_state=session.state_version if not allow_intermediate else None,
            source_is_cancelled=source_is_cancelled,
            **({"sampling_request": sampling_request} if sampling_request is not None else {}),
            **({"display_region": display_region} if display_region is not None else {}),
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
