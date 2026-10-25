from __future__ import annotations

import copy
import uuid
from threading import RLock

from .result_types import CoreResult, ErrorCode


class AgentService:
    def __init__(self, core, audit):
        self._core = core
        self._audit = audit
        self._candidates = {}
        self._previewed = set()
        self._committing = set()
        self._lock = RLock()

    def _record(self, event, **fields):
        try:
            self._audit.append(event, **fields)
        except (OSError, ValueError, TypeError) as exc:
            return CoreResult.from_exception(exc, code="AUDIT_WRITE_FAILED", message_key="error.audit.failed")
        return CoreResult.ok(None)

    def record_external_request(self, event, **fields):
        """Record an external model request without exposing the audit object."""
        return self._record(event, **fields)

    def query(self, session_id):
        result = self._core.get_session(session_id)
        if not result.success:
            return result
        session = result.data
        return CoreResult.ok({"workflow": session.draft_workflow.to_dict(),
                              "revision": session.current_revision.revision_id,
                              "state_version": session.state_version,
                              "preview": copy.deepcopy(session.preview_state),
                              "nodes": list(self._core.tools.snapshot().registry.types())})

    def propose(self, session_id, workflow, *, request="", model_version="", resource_hash="", expected_state=None,
                audit_context=None):
        with self._lock:
            if not model_version:
                return CoreResult.fail("MODEL_NOT_INSTALLED", "error.agent.model_missing")
            current = self.query(session_id)
            if not current.success:
                return current
            available = self._core.project_capabilities(session_id)
            if not available.success:
                return available
            if not available.data["can_execute"]:
                issue = available.data["issues"][0]
                return CoreResult.fail(issue["code"], issue["message_key"], details=issue["details"])
            if expected_state is not None and current.data["state_version"] != expected_state:
                return CoreResult.fail("STALE_CANDIDATE", "error.agent.stale_candidate")
            workflow = copy.deepcopy(workflow)
            checked = self._core.validate_workflow(workflow)
            if not checked.success:
                return checked
            candidate_id = uuid.uuid4().hex
            base = current.data
            content = {"request": request, "workflow": workflow.to_dict()} if audit_context is None else dict(audit_context)
            recorded = self._record("agent.propose", candidate_id=candidate_id, session_id=session_id,
                                    model_version=model_version, resource_hash=resource_hash, **content)
            if not recorded.success:
                return recorded
            self._candidates[candidate_id] = (session_id, workflow, base)
            return CoreResult.ok(copy.deepcopy({"candidate_id": candidate_id, "before": base["workflow"],
                                                "after": workflow.to_dict()}))

    def _resolve(self, candidate_id):
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.agent.candidate_missing")
        sid, workflow, base = candidate
        current = self.query(sid)
        if not current.success:
            return current
        if current.data["state_version"] != base["state_version"]:
            return CoreResult.fail("STALE_CANDIDATE", "error.agent.stale_candidate")
        return CoreResult.ok(candidate)

    def discard(self, candidate_id):
        with self._lock:
            self._candidates.pop(candidate_id, None)
            self._previewed.discard(candidate_id)

    def preview(self, candidate_id, *, is_cancelled=None, max_dimension=None):
        with self._lock:
            resolved = self._resolve(candidate_id)
            if not resolved.success:
                return resolved
            sid, workflow, base = resolved.data
        recorded = self._record("agent.preview.request", candidate_id=candidate_id)
        if not recorded.success:
            return recorded
        result = self._core.preview(sid, workflow, update_draft=False, is_cancelled=is_cancelled,
                                    max_dimension=max_dimension)
        with self._lock:
            if result.success and not self._resolve(candidate_id).success:
                result.data.close()
                result = CoreResult.fail("STALE_CANDIDATE", "error.agent.stale_candidate")
            if result.success:
                self._previewed.add(candidate_id)
        recorded = self._record("agent.preview.result", candidate_id=candidate_id,
                                success=result.success, error=None if result.success else result.error.code)
        if not recorded.success:
            if result.success:
                result.data.close()
            return recorded
        return result

    def commit(self, candidate_id, *, confirmed=False, is_cancelled=None, label=""):
        with self._lock:
            if confirmed is not True:
                return CoreResult.fail("PERMISSION_DENIED", "error.agent.confirmation_required")
            if candidate_id not in self._previewed:
                return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.agent.preview_required")
            if candidate_id in self._committing:
                return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.agent.confirmation_required")
            resolved = self._resolve(candidate_id)
            if not resolved.success:
                return resolved
            sid, workflow, base = resolved.data
            self._committing.add(candidate_id)
        try:
            recorded = self._record("agent.commit.request", candidate_id=candidate_id)
            if not recorded.success:
                return recorded
            result = self._core.commit(sid, workflow, label=label, expected_state=base["state_version"],
                                       is_cancelled=is_cancelled)
            with self._lock:
                if result.success:
                    self._candidates.pop(candidate_id, None)
                    self._previewed.discard(candidate_id)
            recorded = self._record("agent.commit.result", candidate_id=candidate_id, success=result.success,
                                    revision_id=result.data.revision.revision_id if result.success else None,
                                    error=None if result.success else result.error.code)
            if not recorded.success:
                recorded.error.details["operation_applied"] = result.success
                if result.success:
                    recorded.error.details["revision_id"] = result.data.revision.revision_id
                return recorded
            return result
        finally:
            with self._lock:
                self._committing.discard(candidate_id)
