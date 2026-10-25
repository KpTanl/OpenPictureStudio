from __future__ import annotations

from concurrent.futures import CancelledError
from contextlib import nullcontext

from ..core.contracts import WorkflowGraph
from ..core.contracts.result import CoreOperationError, CoreResult, ErrorCode
from ..core.workflow.executor import input_fingerprint
from ..core.workflow.validator import validate


class CanonicalInputUnavailable(CoreOperationError):
    def __init__(self):
        super().__init__(CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible",
                                       details={"requires_full_input": True}).error)


class CanonicalInputPreparer:
    def __init__(self, snapshot, workflow, full_source, resources=None, temporary_workflow=None):
        self.snapshot = snapshot
        self.workflow = workflow
        self.full_source = full_source
        self.resources = resources
        self.temporary_workflow = temporary_workflow

    def _prefix(self, workflow, node_id):
        checked = validate(workflow, self.snapshot.registry)
        if not checked.success:
            raise CoreOperationError(checked.error)
        if node_id not in checked.data:
            raise CanonicalInputUnavailable()
        nodes = tuple(workflow.node(identity) for identity in checked.data[:checked.data.index(node_id)])
        ids = {node.node_id for node in nodes}
        return WorkflowGraph(nodes=nodes, edges=tuple(edge for edge in workflow.edges
                                                     if edge[0] in ids and edge[1] in ids))

    def temporary_key(self, incoming, context, reference, identity):
        if self.temporary_workflow is None:
            return None
        node_id = context["node_id"]
        if self.temporary_workflow.node(node_id) is None:
            return None
        current = self._prefix(self.workflow, node_id)
        previous = self._prefix(self.temporary_workflow, node_id)
        if len(current.nodes) != len(previous.nodes) or current.edges != previous.edges:
            return None
        target = self.workflow.node(node_id)
        old_target = self.temporary_workflow.node(node_id)
        if target.to_dict() != old_target.to_dict():
            return None
        changed = False
        for node, old_node in zip(current.nodes, previous.nodes):
            left, right = node.to_dict(), old_node.to_dict()
            if left == right:
                continue
            if (node.node_type != "tool" or old_node.node_type != "tool"
                    or node.params.get("tool_id") != "color_adjust"
                    or old_node.params.get("tool_id") != "color_adjust"):
                return None
            left["params"].pop("settings", None)
            right["params"].pop("settings", None)
            if left != right:
                return None
            changed = True
        if not changed:
            return None
        origin, _ = context["upstream_identity"]
        return identity(incoming, {**context, "upstream_identity": (origin, input_fingerprint(previous.nodes))}, reference)

    def prepare(self, incoming, context, reference, key, identity, prepare, cancelled):
        if self.full_source is None:
            raise CanonicalInputUnavailable()
        prefix = self._prefix(self.workflow, context["node_id"])
        with self.full_source(cancelled) as lease:
            source = lease.value
            if source.is_proxy or source.content_identity != context["upstream_identity"][0]:
                raise CanonicalInputUnavailable()
            memory = (self.resources.working_memory(source.pixels.nbytes * 4, cancelled)
                      if self.resources is not None else nullcontext())
            with memory:
                result = self.snapshot.executor.execute(source, prefix, is_cancelled=cancelled, input_preparer=self)
                if not result.success:
                    if result.error.code == ErrorCode.CANCELLED:
                        raise CancelledError()
                    raise CoreOperationError(result.error)
                canonical = result.data
                if canonical.is_proxy or identity(canonical, context, reference) != key:
                    raise CanonicalInputUnavailable()
                return prepare(canonical, key, cancelled)
