from __future__ import annotations

from concurrent.futures import CancelledError

from ..core.contracts.result import CoreOperationError, CoreResult, ErrorCode


class CanonicalInputUnavailable(CoreOperationError):
    def __init__(self):
        super().__init__(CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible",
                                       details={"requires_full_input": True}).error)


class CanonicalInputPreparer:
    def __init__(self, snapshot, full_source=None, resources=None):
        self.snapshot = snapshot
        self.full_source = full_source
        self.resources = resources

    def prepare(self, incoming, context, reference, key, identity, prepare, cancelled):
        if not incoming.is_proxy:
            if not context["upstream_identity"][0]:
                return prepare(incoming, key, cancelled)
            return self._prepare(context["source"], context, reference, key, identity, prepare, cancelled)
        if self.full_source is None:
            raise CanonicalInputUnavailable()
        with self.full_source(cancelled) as lease:
            return self._prepare(lease.value, context, reference, key, identity, prepare, cancelled)

    def _prepare(self, source, context, reference, key, identity, prepare, cancelled):
        if source.is_proxy or source.content_identity != context["upstream_identity"][0]:
            raise CanonicalInputUnavailable()
        execution = context.get("execution")
        stage = next((item for item in execution.stages if item.node_id == context["node_id"]), None) if execution else None
        result = self.snapshot.executor.execute(source, context["preparation"], is_cancelled=cancelled, input_preparer=self,
            planned_stages=stage.preparation_stages if stage is not None and stage.preparation_stages else None)
        if not result.success:
            if result.error.code == ErrorCode.CANCELLED:
                raise CancelledError()
            raise CoreOperationError(result.error)
        canonical = result.data
        if canonical.is_proxy or identity(canonical, context, reference) != key:
            raise CanonicalInputUnavailable()
        return prepare(canonical, key, cancelled)
