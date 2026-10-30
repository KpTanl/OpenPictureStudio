from ..core.contracts import WorkflowGraph, WorkflowNode
from ..core.contracts.result import SourceChangedError
from ..core.photo_engine import _pil_from
from .tool_runtime import ToolRuntime
from .result_types import CoreOperationError
from .services import PreviewService
from .image_source_service import SourcePreviewSpec


class PanelRenderService:
    def __init__(self, runtime=None):
        self.runtime = runtime or ToolRuntime()
        self._preview_service = PreviewService(runtime=self.runtime)

    def needs_preparation(self, tool_id, settings):
        return self.runtime.needs_preparation(tool_id, settings)

    def check_available(self, tool_id):
        return self.runtime.check_enabled(tool_id)

    def is_current(self, generation):
        return self.runtime.is_current(generation)

    def prepare(self, tool_id, settings, *, reference=None):
        checked = self.runtime.validate_tool_params(tool_id, settings, reference=reference)
        if not checked.success:
            raise CoreOperationError(checked.error)
        result = self.runtime.effect_reference(tool_id, settings, reference=reference)
        if not result.success:
            raise CoreOperationError(result.error)
        available = self.check_available(tool_id)
        if not available.success:
            raise CoreOperationError(available.error)
        return result.data

    def analyze(self, tool_id, image, settings, *, is_cancelled=lambda: False):
        result = self.runtime.analyze(tool_id, image, settings, is_cancelled=is_cancelled)
        if not result.success:
            raise CoreOperationError(result.error)
        return result.data

    def analyze_full(self, identity, tool_id, settings, *, source_service, is_cancelled=lambda: False, preset=False,
                     reference=None):
        with source_service.borrow_linear(identity.path, identity=identity, is_cancelled=is_cancelled) as lease:
            result = self.runtime.analyze(tool_id, lease.value, settings, is_cancelled=is_cancelled,
                context={"upstream_identity": (lease.value.content_identity, "")}, preset=preset, reference=reference)
            if not result.success:
                raise CoreOperationError(result.error)
            if is_cancelled() or not source_service.matches_file(identity):
                raise SourceChangedError()
            return result.data

    def render(self, identity, tool_id, settings, *, source_service, preview=False, is_cancelled=lambda: False):
        result = self.render_source(identity, tool_id, settings, source_service=source_service,
                                    preview=preview, is_cancelled=is_cancelled)
        with source_service.resources.working_memory(result.pixels.nbytes * 4, is_cancelled):
            with source_service.compute.borrow(is_cancelled, role="foreground",
                    threads=source_service.policy.interactive_threads) as compute:
                image = _pil_from(result, parallelism=compute.threads)
        if preview:
            image.thumbnail((1000, 1000))
        return image

    def render_source(self, identity, tool_id, settings, *, source_service, preview=False, is_cancelled=lambda: False):
        available = self.check_available(tool_id)
        if not available.success:
            raise CoreOperationError(available.error)
        reference = self.runtime.effect_reference(tool_id, settings)
        if not reference.success:
            raise CoreOperationError(reference.error)
        workflow = WorkflowGraph(nodes=(WorkflowNode(tool_id, "tool", reference.data.to_params(settings)),))
        limit = 1000 if preview and self.runtime.supports_proxy(workflow, canonical_inputs=True) else None
        with source_service.borrow_linear(identity.path, identity=identity, max_side=limit,
                                          is_cancelled=is_cancelled) as lease:
            snapshot = self.runtime.snapshot()
            minimum_threads = snapshot.required_threads(workflow)
            if lease.value.is_proxy and self.runtime.uses_prepared_inputs(workflow):
                minimum_threads = max(minimum_threads, source_service.policy.operator_threads)
            with source_service.resources.working_memory(lease.value.pixels.nbytes * 4, is_cancelled):
                with source_service.compute.borrow(is_cancelled, role="foreground",
                        threads=max(source_service.policy.interactive_threads, minimum_threads),
                        minimum_threads=minimum_threads):
                    result = self.runtime.execute_preview(snapshot, lease.value, workflow, is_cancelled=is_cancelled,
                        resources=source_service.resources,
                        full_source=lambda abort: source_service.borrow_linear(identity.path, identity=identity,
                                                                               is_cancelled=abort))
                    if not result.success:
                        raise CoreOperationError(result.error)
                    if is_cancelled() or not source_service.matches_file(identity):
                        raise SourceChangedError()
                    return result.data

    def render_sampled(self, identity, tool_id, settings, *, source_service, epoch,
                       preview=True, is_cancelled=lambda: False):
        from ..core.mixer_sampling import MixerSamplingRequest
        available = self.check_available(tool_id)
        if not available.success:
            raise CoreOperationError(available.error)
        reference = self.runtime.effect_reference(tool_id, settings)
        if not reference.success:
            raise CoreOperationError(reference.error)
        workflow = WorkflowGraph(nodes=(WorkflowNode(tool_id, "tool", reference.data.to_params(settings)),))
        request = MixerSamplingRequest(target_node=tool_id, epoch=epoch)
        snapshot = self.runtime.snapshot()
        capability = snapshot.sampling_capability(workflow, request)
        if not capability.success:
            raise CoreOperationError(capability.error)
        cancelled = lambda: is_cancelled() or snapshot.is_cancelled()
        limit = 1000 if preview and self.runtime.supports_proxy(workflow) else None
        packet = None
        retained = False
        try:
            with source_service.borrow_linear(identity.path, identity=identity, max_side=limit,
                                              is_cancelled=cancelled) as lease:
                minimum_threads = snapshot.required_threads(workflow)
                with source_service.resources.working_memory(lease.value.pixels.nbytes * 4, cancelled), \
                        source_service.compute.borrow(cancelled, role="foreground",
                            threads=max(source_service.policy.interactive_threads, minimum_threads),
                            minimum_threads=minimum_threads) as compute:
                    result = self._preview_service.render(lease.value, workflow, display=True,
                        max_dimension=1000 if preview else None, parallelism=compute.threads,
                        sampling_request=request, resources=source_service.resources, is_cancelled=cancelled)
                if not result.success:
                    raise CoreOperationError(result.error)
                packet = result.data
                if cancelled():
                    from concurrent.futures import CancelledError
                    raise CancelledError()
                if not source_service.matches_file(identity):
                    raise SourceChangedError()
                packet.sampling.source_identity = identity
                packet.sampling.source_preview = SourcePreviewSpec()
                retained = True
                return packet
        finally:
            if packet is not None and not retained:
                packet.close()
