"""Core API: the single supported entry point for UI, Agent and CLI.

This module must stay importable without a Qt application (or Qt at all). It
owns runtime session state and delegates actual work to the application
services and persists project state through ProjectStore.
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import replace
from functools import wraps
from contextlib import contextmanager, nullcontext
from threading import RLock
from time import monotonic
from typing import Callable, Optional

from ..core.contracts import RenderSource, SourceBinding, RevisionRef, WorkflowGraph
from ..core.contracts.artifact import LINEAR_ARTIFACT
from concurrent.futures import CancelledError
from .image_source_service import ImageSourceService, SourcePreviewSpec
from .image_resources import ImageResourcePolicy
from .result_types import CoreOperationError, CoreResult, ErrorCode, SourceChangedError
from ..core.contracts.result import memory_error_key
from .project_requests import ProjectSaveSnapshot, record_project_stage
from .services import (
    CommitPolicy,
    CommitResult,
    DisplayFrame,
    DetailDisplay,
    ExportRequest,
    ExportResult,
    ExportService,
    ImageEngineAdapter,
    PreviewResult,
    PreviewService,
    ProjectSession,
    CommitService,
    hash_file,
)
from ..core.project import ProjectDocument, ProjectStore
from ..core.project.document import resolve_source_path
from ..core.project.project_store import ProjectChangedError
from ..core.project.assets import ProjectAssetError, validate_asset
from ..core.raw_loader import RawSupportUnavailableError, is_raw_file

logger = logging.getLogger(__name__)


def synchronized(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return invoke


class CoreAPI:
    """Orchestrates sources, sessions and the application services."""

    def __init__(
        self,
        engine: Optional[ImageEngineAdapter] = None,
        *,
        artifact_dir: str | None = None,
        preview_service: PreviewService | None = None,
        commit_service: CommitService | None = None,
        export_service: ExportService | None = None,
        autosave_projects: bool = True,
        pixel_cache_max_bytes: int = 512 * 1024 * 1024,
        pixel_cache_max_items: int = 4,
        tool_runtime=None,
        output_writer=None,
        image_source_service=None,
        model_service=None,
        inference_backend=None,
        runtime_query=None,
    ):
        self._lock = RLock()
        self._autosave_projects = autosave_projects
        self._preview_tokens = {}
        self._preview_generations = {}
        self._engine = engine or ImageEngineAdapter()
        self.image_sources = image_source_service or ImageSourceService(engine=self._engine,
            policy=ImageResourcePolicy(source_cache_bytes=pixel_cache_max_bytes,
                                       source_cache_items=pixel_cache_max_items))
        from .tool_runtime import ToolRuntime
        supplied = [getattr(service, "runtime", None) for service in
                    (preview_service, commit_service, export_service) if service is not None]
        self.tools = tool_runtime or next((runtime for runtime in supplied if runtime is not None), None) or ToolRuntime(
            self._engine, execution_threads=self.image_sources.policy.operator_threads,
            compute_budget=self.image_sources.compute, model_service=model_service,
            resource_budget=self.image_sources.resources, inference_backend=inference_backend,
            runtime_query=runtime_query)
        self.models = self.tools.models
        self.inference = self.tools.inference
        if any(runtime is not None and runtime is not self.tools for runtime in supplied):
            raise ValueError("应用服务必须共享同一工具运行环境")
        self._preview_service = preview_service or PreviewService(self._engine, runtime=self.tools,
            display_parallelism=self.image_sources.policy.operator_threads)
        self._commit_service = commit_service or CommitService(self._engine, artifact_dir, runtime=self.tools)
        from .output_writer import OutputWriter
        self._export_service = export_service or ExportService(self._engine, runtime=self.tools, writer=output_writer or OutputWriter())

        self._sources: dict[str, RenderSource | SourceBinding] = {}
        self._source_identities = {}
        self._source_previews = {}
        self._relocation_owners = {}
        self.project_source_resolver = lambda path, digest: None
        self.source_access_guard = lambda path: True
        self._sessions: dict[str, ProjectSession] = {}
        self._history: dict[str, list[RevisionRef]] = {}
        self._pointer: dict[str, int] = {}
        self._workflows: dict[str, dict[str, WorkflowGraph]] = {}
        self._redo_targets: dict[str, dict[str, str]] = {}
        self._edit_groups: dict[str, tuple[str, str]] = {}
        self._project_paths: dict[str, str] = {}
        self._project_hashes: dict[str, str] = {}
        self._loaded_projects: set[str] = set()
        self._project_versions = {}
        self._project_saved_versions = {}
        self._project_inspections = {}
        from .export_snapshots import FixedOutputService
        self._fixed_outputs = FixedOutputService(self.tools, self.image_sources, self._engine,
                                                self._export_service, self._fixed_output_target_guard)
        if self.models is not None:
            self.models.bind_references(self.model_reference_count)

    def model_reference_count(self, digests):
        with self._lock:
            workflows = tuple((session.draft_workflow, *self._workflows.get(session_id, {}).values())
                              for session_id, session in self._sessions.items())
        return (sum(any(not self.tools.workflow_resource_digests(workflow).isdisjoint(digests)
                        for workflow in graphs) for graphs in workflows)
                + self._fixed_outputs.reference_count(digests))

    def validate_workflow(self, workflow):
        return self.tools.validate(workflow)

    def inspect_workflow(self, workflow):
        return self.tools.inspect(workflow)

    def sampling_capability(self, workflow, request):
        return self.tools.sampling_capability(workflow, request)

    def set_tool_enabled(self, tool_id, enabled):
        return self.tools.set_enabled(tool_id, enabled)

    def list_tools(self):
        return self.tools.list_status()

    @synchronized
    def source_identity(self, session_id):
        session = self._sessions.get(session_id)
        return self._source_identities.get(session.active_source.source_id) if session is not None else None

    @synchronized
    def source_preview(self, session_id):
        session = self._sessions.get(session_id)
        return (self._source_previews.get(session.active_source.source_id, SourcePreviewSpec())
                if session is not None else None)

    @synchronized
    def source_size(self, session_id):
        session = self._sessions.get(session_id)
        source = session.active_source if session is not None else None
        return (source.width, source.height) if isinstance(source, RenderSource) else None

    @synchronized
    def begin_source_relocation(self, session_id, *, expected_source, expected_state, token):
        session = self._sessions.get(session_id)
        if (session is None or session.state_version != expected_state
                or session.active_source != expected_source or session_id in self._relocation_owners):
            return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "library.expired")
        if any(s.session_id != session_id and s.active_source.source_id == expected_source.source_id for s in self._sessions.values()):
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "library.target_occupied")
        self._relocation_owners[session_id] = token
        session.state_version += 1
        self._edit_groups.pop(session_id, None)
        return CoreResult.ok(session.state_version)

    @synchronized
    def relocate_source(self, session_id, identity, *, token, expected_state, expected_source):
        from .services import make_source_id
        session = self._sessions.get(session_id)
        if (self._relocation_owners.get(session_id) != token or session is None
                or session.state_version != expected_state or session.active_source != expected_source
                or identity.file_hash != expected_source.file_hash or not self.image_sources.matches_file(identity)):
            return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "library.expired")
        if any(s.session_id != session_id and os.path.normcase(s.active_source.path) == os.path.normcase(identity.path) for s in self._sessions.values()):
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "library.target_occupied")
        source_id = make_source_id(identity.path, identity.file_hash)
        source = replace(expected_source, source_id=source_id, path=identity.path)
        self._sources[source_id] = source
        self._source_identities[source_id] = identity
        spec = self._source_previews.get(expected_source.source_id, SourcePreviewSpec())
        self._source_previews[source_id] = SourcePreviewSpec(generation=spec.generation + 1)
        session.active_source = source
        session.state_version += 1
        return CoreResult.ok(session.state_version)

    @synchronized
    def rollback_source_relocation(self, session_id, source, identity, *, token, expected_state):
        session = self._sessions.get(session_id)
        if self._relocation_owners.get(session_id) != token or session is None or session.state_version != expected_state:
            return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "library.expired")
        self._sources[source.source_id] = source
        if identity is not None:
            self._source_identities[source.source_id] = identity
        session.active_source = copy.deepcopy(source)
        session.state_version += 1
        return CoreResult.ok(session.state_version)

    @synchronized
    def end_source_relocation(self, session_id, token):
        if self._relocation_owners.get(session_id) == token:
            self._relocation_owners.pop(session_id)

    def bind_source(self, path, *, is_cancelled=lambda: False):
        from .services import make_source_id
        if not self.source_access_guard(path):
            return CoreResult.fail(ErrorCode.CANCELLED, "library.recovery_required")
        try:
            identity = self.image_sources.identity(path, is_cancelled=is_cancelled)
            binding = SourceBinding(make_source_id(identity.path, identity.file_hash),
                                    identity.path, identity.file_hash, is_raw_file(identity.path))
            with self._lock:
                if is_cancelled() or not self.image_sources.matches_file(identity):
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                source = self._sources.setdefault(binding.source_id, binding)
                self._source_identities[binding.source_id] = identity
                self._source_previews.setdefault(binding.source_id, SourcePreviewSpec())
                return CoreResult.ok(copy.deepcopy(source))
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except SourceChangedError as error:
            return CoreResult.from_exception(error)
        except (OSError, ValueError, TypeError) as error:
            return CoreResult.from_exception(error, code=ErrorCode.SOURCE_NOT_FOUND,
                                             message_key="error.source.not_found")

    def create_bound_session(self, binding, *, initial_workflow=None):
        with self._lock:
            registered = self._sources.get(binding.source_id)
            if registered is None or (registered.path, registered.file_hash) != (binding.path, binding.file_hash):
                return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
        return self.create_session(binding.source_id, initial_workflow=initial_workflow)

    def ensure_source_ready(self, session_id, *, expected_source=None, expected_state=None,
                            is_cancelled=lambda: False):
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
            source, version = session.active_source, session.state_version
            identity = self.source_identity(session_id)
            if (is_cancelled() or expected_source is not None and identity != expected_source
                    or expected_state is not None and version != expected_state):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            if isinstance(source, RenderSource):
                return CoreResult.ok(copy.deepcopy(session))

        def cancelled():
            with self._lock:
                return (is_cancelled() or self._sessions.get(session_id) is not session
                        or session.state_version != version or session.active_source != source)

        decoded = self._decode_source(source.path, source.file_hash, identity=identity,
                                      max_side=1600, is_cancelled=cancelled)
        if not decoded.success:
            with self._lock:
                if (not is_cancelled() and self._sessions.get(session_id) is session
                        and session.state_version == version and isinstance(session.active_source, RenderSource)
                        and session.active_source.source_id == source.source_id):
                    return CoreResult.ok(copy.deepcopy(session))
            return decoded
        lease, identity = decoded.data
        try:
            complete = self._engine.to_contract_source(lease.value, file_hash=source.file_hash,
                                                       is_raw=source.is_raw)
            if complete.source_id != source.source_id:
                return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
            with self._lock:
                if cancelled() or not self.image_sources.matches_file(identity):
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                self._sources[source.source_id] = complete
                self._source_identities[source.source_id] = identity
                spec = self.source_preview(session_id)
                self._source_previews[source.source_id] = SourcePreviewSpec(generation=spec.generation + 1)
                for active in self._sessions.values():
                    if active.active_source.source_id == source.source_id:
                        active.active_source = complete
                return CoreResult.ok(copy.deepcopy(session))
        except Exception as error:
            return CoreResult.from_exception(error, code=ErrorCode.DECODE_FAILED,
                                             message_key="error.source.decode_failed")
        finally:
            lease.close()

    def complete_source_preview(self, session_id, identity, *, is_cancelled=lambda: False):
        ready = self.ensure_source_ready(session_id, expected_source=identity, is_cancelled=is_cancelled)
        if not ready.success:
            return ready
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or self.source_identity(session_id) != identity:
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            if not self.source_preview(session_id).half_size:
                return CoreResult.ok()
            source = session.active_source
        lease = None
        try:
            lease = self.image_sources.borrow_linear(identity.path, identity=identity, max_side=1600,
                                                      priority=2, is_cancelled=is_cancelled)
            if (lease.value.orig_size != (source.width, source.height)
                    or lease.value.color_pipeline != source.color_pipeline):
                return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
            with self._lock:
                if (is_cancelled() or self._sessions.get(session_id) is not session
                        or self.source_identity(session_id) != identity
                        or not self.image_sources.matches_file(identity)):
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                spec = self.source_preview(session_id)
                if spec.half_size:
                    self._source_previews[source.source_id] = SourcePreviewSpec(generation=spec.generation + 1)
                result = CoreResult.ok(lease)
                lease = None
                return result
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except Exception as error:
            return CoreResult.from_exception(error, code=ErrorCode.DECODE_FAILED,
                                             message_key="studio.full_source_failed")
        finally:
            if lease is not None:
                lease.close()

    def restore_disabled_tools(self, tool_ids):
        return self.tools.restore_disabled(tool_ids)

    def project_capabilities(self, session_id, *, verify_resources=True, is_cancelled=lambda: False):
        if is_cancelled():
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        generation = self.tools.generation
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
            workflow = copy.deepcopy(session.draft_workflow)
            version = session.state_version
            require_versions = session_id in self._loaded_projects
            cached = self._project_inspections.get(session_id)
            key = workflow.workflow_hash(), generation
            if not verify_resources and cached is not None and cached[0] == key:
                return copy.deepcopy(cached[1])
        def cancelled():
            if is_cancelled():
                return True
            with self._lock:
                return self._sessions.get(session_id) is not session or session.state_version != version

        try:
            if cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            generation, result = self.tools.inspect_with_generation(
                workflow, require_resource_versions=require_versions and verify_resources, is_cancelled=cancelled,
            )
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        key = workflow.workflow_hash(), generation
        with self._lock:
            if (self._sessions.get(session_id) is not session or session.state_version != version
                    or self.tools.generation != generation):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            if verify_resources:
                self._project_inspections[session_id] = key, copy.deepcopy(result)
        return result

    def _check_project_execution(self, session_id, *, verify_resources=True, is_cancelled=lambda: False,
                                 workflow=None):
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and (session_id in self._relocation_owners or not self.source_access_guard(session.active_source.path)):
                return CoreResult.fail(ErrorCode.CANCELLED, "library.recovery_required")
        if workflow is None:
            inspected = self.project_capabilities(session_id, verify_resources=verify_resources,
                                                   is_cancelled=is_cancelled)
        else:
            with self._lock:
                if session_id not in self._sessions:
                    return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
                require_versions = session_id in self._loaded_projects and verify_resources
            try:
                generation, inspected = self.tools.inspect_with_generation(
                    workflow, require_resource_versions=require_versions, is_cancelled=is_cancelled)
            except CancelledError:
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            if not self.tools.is_current(generation):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        if not inspected.success:
            return inspected
        if inspected.data["issues"]:
            issue = inspected.data["issues"][0]
            return CoreResult.fail(issue["code"], issue["message_key"], details=issue["details"])
        return CoreResult.ok(None)

    def _project_document(self, session_id):
        session = self._sessions[session_id]
        if not isinstance(session.active_source, RenderSource):
            raise ValueError("batch.source_not_ready")
        from .mask_assets import workflow_assets
        assets = {}
        for workflow in (session.draft_workflow, *self._workflows[session_id].values()):
            for digest, entry in workflow_assets(workflow).items():
                if digest in assets and any(assets[digest][key] != value for key, value in entry.items()):
                    raise ValueError("error.mask.asset")
                assets[digest] = {**entry, "path": str(self.tools.mask_assets.path(digest))}
        return ProjectDocument(
            schema_version=max(self._project_versions.get(session_id, 1),
                               3 if assets else 1,
                               2 if any(a.kind == LINEAR_ARTIFACT for r in self._history[session_id]
                                        for a in r.artifact_refs) else 1),
            source=session.active_source.to_dict(), document_id=session.document_id,
            current_revision=session.current_revision.revision_id if session.current_revision else "",
            revisions=[r.to_dict() for r in self._history[session_id]],
            workflows={key: value.to_dict() for key, value in self._workflows[session_id].items()},
            draft=session.draft_workflow.to_dict(), redo_targets=dict(self._redo_targets[session_id]),
            assets=assets,
        )

    def _prepare_project(self, path, doc, *, expected_hash=None, is_cancelled=lambda: False):
        from pathlib import Path
        target = Path(path).resolve()
        if target.suffix.lower() != ".opsproj" or target == Path(doc.source["path"]).resolve():
            raise ValueError("项目目标必须为独立的 .opsproj 文件")
        root = target.with_suffix(".opsdata")
        store = ProjectStore()
        from .mask_assets import workflow_assets
        for graph in (*doc.workflows.values(), doc.draft):
            for digest, entry in workflow_assets(WorkflowGraph.from_dict(graph)).items():
                doc.assets[digest] = {**entry, "path": str(self.tools.mask_assets.path(digest))}
        if doc.assets:
            doc.schema_version = 3
        copied = set()
        for digest, asset in doc.assets.items():
            relative = "assets/" + digest + ".png"
            validate_asset(asset["path"], digest, asset["size"], is_cancelled)
            store.copy_artifact(Path(asset["path"]), root / relative, digest, is_cancelled=is_cancelled)
            asset["path"] = relative
        for revision in doc.revisions:
            for artifact in revision["artifact_refs"]:
                original = Path(artifact["path"])
                relative = "artifacts/" + artifact["hash"] + original.suffix
                destination = root / relative
                key = (original, destination, artifact["hash"])
                if key not in copied:
                    store.copy_artifact(original, destination, artifact["hash"], is_cancelled=is_cancelled)
                    copied.add(key)
                artifact["path"] = relative
        prepared = store.prepare(str(target), doc, expected_hash=expected_hash, is_cancelled=is_cancelled)
        try:
            revisions = copy.deepcopy(doc.revisions)
            for revision in revisions:
                for artifact in revision["artifact_refs"]:
                    artifact["path"] = str(root / artifact["path"])
            return prepared, [RevisionRef.from_dict(item) for item in revisions], doc.schema_version
        except BaseException:
            prepared.close()
            raise

    def _publish_project(self, session_id, staged, reservation, *, revision_id=None, saved_version=None):
        from pathlib import Path
        prepared, history, version = staged
        if prepared.target != reservation[0] or Path(prepared.target).resolve() != reservation[0]:
            raise ProjectChangedError("项目目标在保存期间发生变化")
        ProjectStore().publish(prepared)
        self._project_paths[session_id] = str(prepared.target)
        from .storage_paths import register_project
        register_project(prepared.target)
        self._project_hashes[session_id] = prepared.file_hash
        self._project_versions[session_id] = version
        self._history[session_id] = history
        session = self._sessions[session_id]
        self._project_saved_versions[session_id] = session.state_version if saved_version is None else saved_version
        if revision_id is not None:
            self._pointer[session_id] = next(i for i, item in enumerate(history) if item.revision_id == revision_id)
        session.current_revision = history[self._pointer[session_id]]

    @synchronized
    def get_project_saved_state(self, session_id):
        session = self._sessions.get(session_id)
        if session is None:
            return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
        path = self._project_paths.get(session_id)
        if path is None:
            return CoreResult.ok(None)
        return CoreResult.ok({
            "path": path, "file_hash": self._project_hashes[session_id],
            "state_version": self._project_saved_versions[session_id],
            "source_id": session.active_source.source_id,
        })

    @synchronized
    def capture_project_save(self, session_id: str, path: str, *, expected_state=None):
        from pathlib import Path
        session = self._sessions.get(session_id)
        if session is None:
            return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
        if session_id in self._relocation_owners or not self.source_access_guard(session.active_source.path):
            return CoreResult.fail(ErrorCode.CANCELLED, "library.recovery_required")
        if not isinstance(session.active_source, RenderSource):
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "batch.source_not_ready")
        self._edit_groups.pop(session_id, None)
        if expected_state is not None and session.state_version != expected_state:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        try:
            target = Path(path).resolve()
            if target.suffix.lower() != ".opsproj" or target == Path(session.active_source.path).resolve():
                return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.project.save_failed")
            return CoreResult.ok(ProjectSaveSnapshot(
                session_id, session.state_version, session.active_source.source_id, str(target),
                self._project_document(session_id), self._project_paths.get(session_id),
                self._project_hashes.get(session_id),
            ))
        except (OSError, ValueError, TypeError) as exc:
            return CoreResult.from_exception(exc, code=ErrorCode.INVALID_INPUT,
                                             message_key="error.project.save_failed")

    def _project_snapshot_current(self, snapshot):
        session = self._sessions.get(snapshot.session_id)
        return (session is not None and session.state_version == snapshot.state_version
                and snapshot.session_id not in self._relocation_owners and self.source_access_guard(session.active_source.path)
                and session.active_source.source_id == snapshot.source_id
                and self._project_paths.get(snapshot.session_id) == snapshot.bound_path
                and self._project_hashes.get(snapshot.session_id) == snapshot.bound_hash)

    def save_project(self, session_id: str, path: str, *, expected_state=None,
                     cancellation=None, snapshot=None, request=None) -> CoreResult[str]:
        from .output_writer import OperationCancellation
        cancellation = cancellation or OperationCancellation()
        if snapshot is None:
            ready = self.ensure_source_ready(session_id, expected_state=expected_state,
                                             is_cancelled=cancellation.is_cancelled)
            if not ready.success:
                return ready
            captured = self.capture_project_save(session_id, path, expected_state=expected_state)
            if not captured.success:
                return captured
            snapshot = captured.data
        elif snapshot.session_id != session_id or snapshot.path != path:
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.project.save_failed")
        staged = None
        try:
            writer = self._export_service.writer
            with writer.reserve(snapshot.path) as reservation:
                def cancelled():
                    if cancellation.is_cancelled() or not writer.is_current(reservation):
                        return True
                    with self._lock:
                        return not self._project_snapshot_current(snapshot)

                if cancelled():
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                same_target = (snapshot.bound_path is not None
                               and os.path.normcase(snapshot.path) == os.path.normcase(snapshot.bound_path))
                expected_hash = snapshot.bound_hash if same_target else None
                started = monotonic()
                staged = self._prepare_project(snapshot.path, copy.deepcopy(snapshot.document),
                                               expected_hash=expected_hash, is_cancelled=cancelled)
                record_project_stage(request, "prepare", started)

                @contextmanager
                def publication():
                    with self._lock, cancellation.publication() as active:
                        yield active and self._project_snapshot_current(snapshot)

                started = monotonic()
                with writer.publication(reservation, publication) as valid:
                    if not valid:
                        return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                    self._publish_project(session_id, staged, reservation)
                    cancellation.mark_published()
                record_project_stage(request, "publication", started)
                return CoreResult.ok(path)
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except ProjectChangedError:
            return CoreResult.fail(ErrorCode.PROJECT_CHANGED, "error.project.changed")
        except ProjectAssetError as error:
            return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.mask.asset", details={"path": error.path})
        except Exception as exc:
            return CoreResult.from_exception(exc, code=ErrorCode.ARTIFACT_WRITE_FAILED,
                                             message_key="error.project.save_failed")
        finally:
            if staged is not None:
                staged[0].close()

    def _load_project_snapshot(self, path, source_path, is_cancelled):
        loaded = None
        explicit_source = source_path is not None
        try:
            loaded = ProjectStore().prepare_load(path, is_cancelled=is_cancelled)
            doc = loaded.document
            source_path = resolve_source_path(path, source_path or doc.source.get("path", ""))
            if not self.source_access_guard(source_path):
                loaded.close()
                return CoreResult.fail(ErrorCode.CANCELLED, "library.recovery_required")
            expected = doc.source.get("file_hash", "")
            if not os.path.isfile(source_path) and expected and not explicit_source:
                source_path = self.project_source_resolver(source_path, expected) or source_path
            if not os.path.isfile(source_path):
                loaded.close()
                return CoreResult.fail(ErrorCode.SOURCE_NOT_FOUND, "error.source.not_found", details={"path": source_path})
            expected = doc.source.get("file_hash", "")
            if expected and hash_file(source_path, is_cancelled=is_cancelled) != expected:
                relocated = None if explicit_source else self.project_source_resolver(source_path, expected)
                if (not relocated or not self.source_access_guard(relocated)
                        or hash_file(relocated, is_cancelled=is_cancelled) != expected):
                    loaded.close()
                    return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed",
                                           details={"path": source_path, "action": "relocate_source"})
                source_path = relocated
            doc.source["path"] = os.path.abspath(source_path)
            return CoreResult.ok(loaded)
        except CancelledError:
            if loaded is not None:
                loaded.close()
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except ProjectChangedError:
            if loaded is not None:
                loaded.close()
            return CoreResult.fail(ErrorCode.PROJECT_CHANGED, "error.project.changed")
        except ProjectAssetError as error:
            if loaded is not None:
                loaded.close()
            return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.mask.asset", details={"path": error.path})
        except FileNotFoundError:
            if loaded is not None:
                loaded.close()
            return CoreResult.fail(ErrorCode.SOURCE_NOT_FOUND, "error.project.not_found", details={"path": path})
        except Exception as exc:
            if loaded is not None:
                loaded.close()
            return CoreResult.from_exception(exc, code=ErrorCode.INVALID_INPUT, message_key="error.project.invalid")

    def load_project(self, path: str, *, source_path: str | None = None,
                     cancellation=None) -> CoreResult[ProjectDocument]:
        from .output_writer import OperationCancellation
        cancellation = cancellation or OperationCancellation()
        result = self._load_project_snapshot(path, source_path, cancellation.is_cancelled)
        if not result.success:
            return result
        loaded = result.data
        try:
            with self._export_service.writer.publication(None, cancellation.publication) as valid:
                if not valid:
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                ProjectStore().publish_load(loaded)
                cancellation.mark_published()
            return CoreResult.ok(loaded.document)
        except ProjectChangedError:
            return CoreResult.fail(ErrorCode.PROJECT_CHANGED, "error.project.changed")
        except Exception as exc:
            return CoreResult.from_exception(exc, code=ErrorCode.INVALID_INPUT, message_key="error.project.invalid")
        finally:
            loaded.close()

    def open_project(self, path: str, *, source_path: str | None = None,
                     cancellation=None, request=None) -> CoreResult[ProjectSession]:
        from .output_writer import OperationCancellation
        cancellation = cancellation or OperationCancellation()

        def cancelled():
            if cancellation.is_cancelled():
                return True
            if request is not None and request.session_id is not None:
                with self._lock:
                    session = self._sessions.get(request.session_id)
                    return (session is None or session.state_version != request.state_version
                            or session.active_source.source_id != request.source_id)
            return False

        started = monotonic()
        loaded = self._load_project_snapshot(path, source_path, cancelled)
        if not loaded.success:
            return loaded
        prepared = loaded.data
        doc = prepared.document
        opened_source = None
        registered = False
        try:
            root = prepared.target.with_suffix(".opsdata")
            revisions = copy.deepcopy(doc.revisions)
            workflows = {key: WorkflowGraph.from_dict(value) for key, value in doc.workflows.items()}
            for revision in revisions:
                if cancelled():
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                for artifact in revision["artifact_refs"]:
                    artifact["path"] = str((root / artifact["path"]).resolve())
            for digest, asset in doc.assets.items():
                self.tools.mask_assets.import_asset(root / asset["path"], digest, asset["size"], cancelled)
            draft = WorkflowGraph.from_dict(doc.draft)
            history = [RevisionRef.from_dict(r) for r in revisions]
            pointer = next(i for i, r in enumerate(history) if r.revision_id == doc.current_revision)
            opened = self.open_source(doc.source["path"], is_cancelled=cancelled)
            if not opened.success:
                return opened
            opened_source = opened.data
            if opened_source.file_hash != doc.source["file_hash"]:
                return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
            generation, inspected = self.tools.inspect_with_generation(
                draft, require_resource_versions=True, is_cancelled=cancelled,
            )
            inspection_key = draft.workflow_hash(), generation
            record_project_stage(request, "prepare", started)

            @contextmanager
            def publication():
                with self._lock, cancellation.publication() as active:
                    identity = self._source_identities.get(opened_source.source_id)
                    if active and identity is not None and not self.image_sources.matches_file(identity):
                        raise CoreOperationError(CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed").error)
                    yield active and identity is not None and not cancelled() and self.tools.generation == generation

            started = monotonic()
            with self._export_service.writer.publication(None, publication) as valid:
                if not valid:
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                ProjectStore().publish_load(prepared)
                created = self.create_session(opened_source)
                if not created.success:
                    return created
                sid = created.data.session_id
                session = self._sessions[sid]
                session.document_id = doc.document_id or session.document_id
                self._history[sid] = history
                self._workflows[sid] = workflows
                self._redo_targets[sid] = dict(doc.redo_targets)
                self._pointer[sid] = pointer
                session.current_revision = history[pointer]
                session.draft_workflow = draft
                self._project_paths[sid] = str(prepared.target)
                from .storage_paths import register_project
                register_project(prepared.target)
                self._project_hashes[sid] = prepared.file_hash
                self._loaded_projects.add(sid)
                self._project_versions[sid] = doc.schema_version
                self._project_saved_versions[sid] = session.state_version
                self._project_inspections[sid] = inspection_key, inspected
                cancellation.mark_published()
                registered = True
                result = CoreResult.ok(copy.deepcopy(session))
            record_project_stage(request, "publication", started)
            return result
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except ProjectChangedError:
            return CoreResult.fail(ErrorCode.PROJECT_CHANGED, "error.project.changed")
        except ProjectAssetError as error:
            return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.mask.asset", details={"path": error.path})
        except Exception as exc:
            return CoreResult.from_exception(exc, code=ErrorCode.INVALID_INPUT, message_key="error.project.invalid")
        finally:
            prepared.close()
            if opened_source is not None and not registered:
                self.release_source(opened_source.source_id)

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    def open_source(self, path: str, *, is_cancelled=None, identity=None,
                    initial_proxy=False) -> CoreResult[RenderSource]:
        if not path or not isinstance(path, str):
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.source.invalid_path")

        normalized = os.path.abspath(path)
        if not os.path.isfile(normalized):
            return CoreResult.fail(
                ErrorCode.SOURCE_NOT_FOUND,
                "error.source.not_found",
                details={"path": normalized},
            )

        try:
            if identity is not None and os.path.normcase(os.path.realpath(normalized)) != os.path.normcase(identity.path):
                return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
            from ..core.photo_engine import supports_raw_initial_proxy
            identity = identity or self.image_sources.identity(normalized, is_cancelled=is_cancelled or (lambda: False))
            lease = self.image_sources.borrow_cached_linear(identity) if initial_proxy else None
            if lease is None and initial_proxy:
                lease = self.image_sources.borrow_cached_proxy(identity,
                    is_cancelled=is_cancelled or (lambda: False))
            half_size = lease is None and initial_proxy and supports_raw_initial_proxy(normalized)
            if lease is None:
                decoded = self._decode_source(normalized, is_cancelled=is_cancelled, identity=identity,
                                              half_size=half_size)
                if not decoded.success:
                    return CoreResult(success=False, error=decoded.error)
                lease, identity = decoded.unwrap()
            try:
                source = self._engine.to_contract_source(
                    lease.value, file_hash=identity.file_hash, is_raw=is_raw_file(normalized)
                )
                with self.image_sources.borrow_linear(normalized, identity=identity, max_side=1600,
                                                      half_size=half_size,
                                                      is_cancelled=is_cancelled or (lambda: False)):
                    pass
            finally:
                lease.close()
            with self._lock:
                if is_cancelled is not None and is_cancelled():
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                self._sources[source.source_id] = source
                self._source_identities[source.source_id] = identity
                self._source_previews.setdefault(source.source_id, SourcePreviewSpec(half_size=half_size))
                spec = self._source_previews[source.source_id]
                if spec.half_size and not half_size:
                    self._source_previews[source.source_id] = SourcePreviewSpec(generation=spec.generation + 1)
            return CoreResult.ok(copy.deepcopy(source))
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as memory_error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(memory_error))
        except SourceChangedError as exc:
            return CoreResult.from_exception(exc)
        except Exception as exc:  # noqa: BLE001
            code = ErrorCode.DECODE_FAILED
            if isinstance(exc, RawSupportUnavailableError):
                code = ErrorCode.UNSUPPORTED_FORMAT
            return CoreResult.fail(
                code,
                "error.source.decode_failed",
                details={
                    "path": normalized,
                    "exception": type(exc).__name__,
                    "message": str(exc),
                },
            )

    @synchronized
    def release_source(self, source_id: str) -> CoreResult[None]:
        if not any(session.active_source.source_id == source_id for session in self._sessions.values()):
            self._sources.pop(source_id, None)
            self._source_identities.pop(source_id, None)
            self._source_previews.pop(source_id, None)
        return CoreResult.ok()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    def create_session(
        self, source: RenderSource | str, *, initial_workflow: WorkflowGraph | None = None
    ) -> CoreResult[ProjectSession]:
        initial_workflow = copy.deepcopy(initial_workflow) if initial_workflow is not None else WorkflowGraph()
        checked = self.tools.validate(initial_workflow, allow_model_gaps=True)
        if not checked.success:
            return checked
        if isinstance(source, str):
            with self._lock:
                contract = self._sources.get(source)
            if contract is None:
                return CoreResult.fail(
                    ErrorCode.SOURCE_NOT_FOUND,
                    "error.source.not_found",
                    details={"source_id": source},
                )
        elif isinstance(source, RenderSource):
            with self._lock:
                contract = self._sources.get(source.source_id)
            if contract is None:
                contract = source
                opened = self.open_source(contract.path)
                if not opened.success:
                    err = opened.error
                    assert err is not None
                    return CoreResult.fail(err.code, err.message_key, details=err.details)
                contract = opened.data
                assert contract is not None
        else:
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.source.invalid")

        import uuid

        with self._lock:
            if not self.source_access_guard(contract.path):
                return CoreResult.fail(ErrorCode.CANCELLED, "library.recovery_required")
            session = ProjectSession(
                session_id=uuid.uuid4().hex[:16],
                document_id=uuid.uuid4().hex[:16],
                active_source=contract,
                draft_workflow=initial_workflow,
            )
            self._sessions[session.session_id] = session
            baseline = RevisionRef(
                revision_id=f"source-{session.session_id}",
                workflow_hash=session.draft_workflow.workflow_hash(),
                label="source",
            )
            session.current_revision = baseline
            self._history[session.session_id] = [baseline]
            self._pointer[session.session_id] = 0
            self._workflows[session.session_id] = {baseline.revision_id: copy.deepcopy(initial_workflow)}
            self._redo_targets[session.session_id] = {}
            return CoreResult.ok(copy.deepcopy(session))

    @synchronized
    def close_session(self, session_id: str) -> CoreResult[None]:
        if session_id in self._relocation_owners:
            return CoreResult.fail(ErrorCode.CANCELLED, "library.busy")
        if session_id not in self._sessions:
            return CoreResult.fail(
                ErrorCode.SESSION_NOT_FOUND, "error.session.not_found"
            )
        self._preview_tokens.pop(session_id, None)
        self._preview_generations.pop(session_id, None)
        self._project_paths.pop(session_id, None)
        self._project_hashes.pop(session_id, None)
        self._loaded_projects.discard(session_id)
        self._project_versions.pop(session_id, None)
        self._project_saved_versions.pop(session_id, None)
        self._project_inspections.pop(session_id, None)
        source_id = self._sessions[session_id].active_source.source_id
        self._sessions.pop(session_id, None)
        if not any(item.active_source.source_id == source_id for item in self._sessions.values()):
            self._sources.pop(source_id, None)
            self._source_identities.pop(source_id, None)
            self._source_previews.pop(source_id, None)
        self._history.pop(session_id, None)
        self._pointer.pop(session_id, None)
        self._workflows.pop(session_id, None)
        self._redo_targets.pop(session_id, None)
        self._edit_groups.pop(session_id, None)
        return CoreResult.ok(None)

    @synchronized
    def get_session(self, session_id: str) -> CoreResult[ProjectSession]:
        session = self._sessions.get(session_id)
        if session is None:
            return CoreResult.fail(
                ErrorCode.SESSION_NOT_FOUND, "error.session.not_found"
            )
        return CoreResult.ok(copy.deepcopy(session))

    @synchronized
    def matches_edit_state(self, session_id, version, source_identity):
        session = self._sessions.get(session_id)
        return (session is not None and session.state_version == version
                and self._source_identities.get(session.active_source.source_id) == source_identity)

    @synchronized
    def baseline_workflow(self, session_id: str) -> CoreResult[WorkflowGraph]:
        if session_id not in self._sessions:
            return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
        revision = next(item for item in self._history[session_id] if item.parent_id is None)
        return CoreResult.ok(copy.deepcopy(self._workflows[session_id][revision.revision_id]))

    # ------------------------------------------------------------------
    # Preview / Commit / Export
    # ------------------------------------------------------------------
    @synchronized
    def edit_workflow(self, session_id, workflow, *, label="adjust", edit_group: str | None = None,
                      expected_version=None, expected_tools=None, expected_source=None, cancellation=None):
        if session_id in self._relocation_owners:
            return CoreResult.fail(ErrorCode.CANCELLED, "library.busy")
        if session_id in self._sessions and not self.source_access_guard(self._sessions[session_id].active_source.path):
            return CoreResult.fail(ErrorCode.CANCELLED, "library.recovery_required")
        if edit_group is not None and (not isinstance(edit_group, str) or not edit_group):
            return CoreResult.fail(ErrorCode.PARAMETER_INVALID, "error.workflow.params")
        checked = self.tools.validate(workflow, allow_model_gaps=True)
        if not checked.success:
            return checked
        session = self._sessions.get(session_id)
        if session is None:
            return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
        current = self._check_project_execution(session_id, verify_resources=False)
        if not current.success:
            from ..core.tools.enhancement import model_gaps_editable
            inspected = self.project_capabilities(session_id, verify_resources=False)
            if not inspected.success or not model_gaps_editable(session.draft_workflow, inspected.data["issues"]):
                return current
        with cancellation.publication() if cancellation is not None else nullcontext(True) as active:
            if (not active or expected_version is not None and session.state_version != expected_version
                    or expected_tools is not None and not self.tools.is_current(expected_tools)
                    or expected_source is not None and (self.source_identity(session_id) != expected_source
                        or not self.image_sources.matches_file(expected_source))):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            result = self._record_workflow_edit(session_id, workflow, label, edit_group=edit_group)
            if cancellation is not None:
                cancellation.mark_published()
            return result

    @synchronized
    def end_edit_group(self, session_id):
        if session_id not in self._sessions:
            return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
        self._edit_groups.pop(session_id, None)
        return CoreResult.ok()

    def _record_workflow_edit(self, session_id, workflow, label, *, edit_group=None):
        session = self._sessions[session_id]
        group = self._edit_groups.get(session_id)
        if edit_group is None or (group is not None and group[0] != edit_group):
            self._edit_groups.pop(session_id, None)
            group = None
        workflow = copy.deepcopy(workflow)
        if session.draft_workflow.workflow_hash() == workflow.workflow_hash():
            return CoreResult.ok(copy.deepcopy(session))
        import uuid
        parent = session.current_revision
        history = self._history[session_id]
        replacing = (group is not None and group[1] == parent.revision_id
                     and parent.parent_id is not None and not parent.artifact_refs
                     and not any(item.parent_id == parent.revision_id for item in history))
        parent_id = parent.parent_id if replacing else parent.revision_id
        revision = RevisionRef(revision_id=uuid.uuid4().hex[:16],
                               parent_id=parent_id, workflow_hash=workflow.workflow_hash(),
                               label=label)
        if replacing:
            history[self._pointer[session_id]] = revision
            self._workflows[session_id].pop(parent.revision_id)
            self._redo_targets[session_id].pop(parent.revision_id, None)
        else:
            history.append(revision)
            self._pointer[session_id] = len(history) - 1
        self._workflows[session_id][revision.revision_id] = workflow
        self._redo_targets[session_id][parent_id] = revision.revision_id
        if edit_group is not None:
            self._edit_groups[session_id] = edit_group, revision.revision_id
        session.current_revision = revision
        session.draft_workflow = copy.deepcopy(workflow)
        session.state_version += 1
        session.preview_state = {}
        return CoreResult.ok(copy.deepcopy(session))

    def upgrade_legacy_effects(self, session_id, *, expected_version, cancellation=None):
        from dataclasses import replace
        from ..core.tools.effect_migration import is_legacy_effect, migrated_settings, reference_params
        from ..core.workflow.validator import inspect_workflow
        from .output_writer import OperationCancellation

        cancellation = cancellation or OperationCancellation()
        self.end_edit_group(session_id)
        current = self.get_session(session_id)
        if not current.success:
            return current
        session = current.data
        if session.state_version != expected_version:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        nodes = []
        for node in session.draft_workflow.nodes:
            if cancellation.is_cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            if not is_legacy_effect(node):
                nodes.append(copy.deepcopy(node))
                continue
            tool_id = node.params.get("tool") or node.params.get("tool_id")
            settings = migrated_settings(tool_id, node.params["settings"],
                                         legacy_defaults=node.node_type == "studio_effect"
                                         or node.params.get("implementation_version") == "legacy-1")
            reference = self.tools.effect_reference(tool_id, settings, refresh=True)
            if not reference.success:
                return reference
            nodes.append(replace(node, node_type="tool", version=1,
                                 params=reference_params(reference.data, settings)))
        workflow = replace(session.draft_workflow, nodes=tuple(nodes))
        snapshot = self.tools.snapshot(require_resource_versions=True)
        checked = inspect_workflow(workflow, snapshot.registry)
        if not checked.success:
            return checked
        if checked.data["issues"]:
            issue = checked.data["issues"][0]
            return CoreResult.fail(issue["code"], issue["message_key"], details=issue["details"])
        with self._lock, self.tools.publication(snapshot) as available, cancellation.publication() as active:
            current = self._sessions.get(session_id)
            if (not available or not active or current is None
                    or current.state_version != expected_version):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            result = self._record_workflow_edit(session_id, workflow, "studio.effect_upgrade_title")
            cancellation.mark_published()
            return result

    def analyze_node_input(self, session_id, workflow, node_id, consumer, *, expected_version,
                           is_cancelled=lambda: False):
        from ..core.workflow.executor import input_fingerprint
        from ..core.workflow.validator import validate_structure
        checked = validate_structure(workflow)
        if not checked.success:
            return checked
        ordered = tuple(workflow.node(identity) for identity in checked.data)
        target = next((node for node in ordered if node.node_id == node_id), None)
        if target is None:
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.workflow.params")
        nodes = ordered[:ordered.index(target)]
        ids = {node.node_id for node in nodes}
        prefix = WorkflowGraph(nodes=nodes, edges=tuple(edge for edge in workflow.edges if edge[0] in ids and edge[1] in ids))
        resolved = self._resolve(session_id, is_cancelled=is_cancelled, expected_state=expected_version)
        if not resolved.success:
            return resolved
        session, lease, version, source_preview = resolved.data
        checked = self.tools.validate(prefix)
        if not checked.success:
            lease.close()
            return checked
        snapshot = self.tools.snapshot()
        cancelled = lambda: is_cancelled() or snapshot.is_cancelled()
        try:
            with lease, self.image_sources.resources.working_memory(lease.value.pixels.nbytes * 4, cancelled), \
                    self.image_sources.compute.borrow(cancelled, role="foreground",
                        threads=max(self.image_sources.policy.operator_threads, snapshot.required_threads(prefix))):
                rendered = snapshot.executor.execute(lease.value, prefix, is_cancelled=cancelled)
                if not rendered.success:
                    return rendered
                value = consumer(rendered.data, {"node_id": node_id, "upstream_identity":
                    (lease.value.content_identity, input_fingerprint(nodes))}, cancelled)
                with self._lock:
                    if (cancelled() or self._sessions.get(session_id) is not session or session.state_version != version
                            or self.source_preview(session_id) != source_preview
                            or not self.image_sources.matches_file(self.source_identity(session_id))):
                        return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                return CoreResult.ok(value)
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(error))
        except CoreOperationError as error:
            return CoreResult.from_exception(error)
        except (ValueError, OSError) as error:
            return CoreResult.from_exception(error, code=ErrorCode.TOOL_RESOURCE_MISSING, message_key="error.mask.asset")
        except ImportError as error:
            return CoreResult.from_exception(error, code=ErrorCode.TOOL_INCOMPATIBLE, message_key="models.runtime.missing")
        except RuntimeError as error:
            return CoreResult.from_exception(error, code=ErrorCode.MODEL_LOAD_FAILED, message_key="mask.generation_failed")
        except Exception as error:
            return CoreResult.from_exception(error, code=ErrorCode.INTERNAL_ERROR, message_key="mask.generation_failed")

    def analyze_beauty(self, session_id, workflow, *, expected_version, is_cancelled=lambda: False,
                       purpose="parameters", preset=False, target_node=None):
        from ..core.contracts.tools import ToolReference
        from ..core.workflow.executor import input_fingerprint
        target = next((node for node in workflow.nodes if node.node_type == "tool"
                       and node.params.get("tool_id") == "beauty"
                       and (target_node is None or node.node_id == target_node)), None)
        if target is None:
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.workflow.params")
        checked = self.tools.validate(workflow)
        if not checked.success:
            return checked
        ordered = tuple(workflow.node(node_id) for node_id in checked.data)
        position = ordered.index(target)
        nodes = ordered[:position]
        ids = {node.node_id for node in nodes}
        prefix = WorkflowGraph(nodes=nodes, edges=tuple(edge for edge in workflow.edges
                                                       if edge[0] in ids and edge[1] in ids))
        resolved = self._resolve(session_id, is_cancelled=is_cancelled, expected_state=expected_version)
        if not resolved.success:
            return resolved
        session, lease, version, source_preview = resolved.data
        snapshot = self.tools.snapshot()
        cancelled = lambda: is_cancelled() or snapshot.is_cancelled()
        try:
            with lease, self.image_sources.resources.working_memory(lease.value.pixels.nbytes * 4, cancelled), \
                    self.image_sources.compute.borrow(cancelled, role="foreground",
                        threads=max(self.image_sources.policy.operator_threads, snapshot.required_threads(prefix))):
                rendered = snapshot.executor.execute(lease.value, prefix, is_cancelled=cancelled)
                if not rendered.success:
                    return rendered
                result = self.tools.analyze("beauty", rendered.data, target.params["settings"],
                    reference=ToolReference.from_dict(target.params), is_cancelled=cancelled,
                    context={"node_id": target.node_id, "upstream_identity":
                             (lease.value.content_identity, input_fingerprint(nodes))}, purpose=purpose, preset=preset)
                with self._lock:
                    current = self._sessions.get(session_id)
                    if (cancelled() or current is not session or current.state_version != version
                            or self.source_preview(session_id) != source_preview
                            or not self.image_sources.matches_file(self.source_identity(session_id))):
                        return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                return result
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(error))
        except CoreOperationError as error:
            return CoreResult.from_exception(error)

    def upgrade_beauty_effect(self, session_id, node_id, *, expected_version, cancellation=None):
        from dataclasses import replace
        from ..core.tools.effect_migration import migrated_settings, reference_params, is_legacy_effect
        from ..core.tools.managed_models import MANAGED_BEAUTY_PARAMETERS
        from ..core.contracts.beauty import DETAIL_BEAUTY_PROFILES, is_current_detail_beauty
        from ..core.contracts.tools import ToolReference
        from .output_writer import OperationCancellation
        cancellation = cancellation or OperationCancellation()
        self.end_edit_group(session_id)
        current = self.get_session(session_id)
        if not current.success:
            return current
        session = current.data
        source_identity = self.source_identity(session_id)
        original = next((node for node in session.draft_workflow.nodes if node.node_id == node_id), None)
        if session.state_version != expected_version:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        if (original is None or (original.params.get("tool_id") or original.params.get("tool")) != "beauty"
                or is_current_detail_beauty(original.params.get("implementation_id", ""),
                                            original.params.get("implementation_version", ""))):
            return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible")
        raw = original.params.get("settings", {})
        settings = migrated_settings("beauty", raw, legacy_defaults=is_legacy_effect(original))
        settings["face_detection_model"] = raw.get("face_detection_model") or self.models.default("face_detection")
        settings = MANAGED_BEAUTY_PARAMETERS.normalize(settings, 3).unwrap()
        target = ToolReference("beauty", 3, "beauty.detail", f"{max(DETAIL_BEAUTY_PROFILES)}-neutral")
        reference = self.tools.effect_reference("beauty", settings, reference=target)
        if not reference.success:
            return reference
        workflow = replace(session.draft_workflow, nodes=tuple(
            replace(node, node_type="tool", version=1, params=reference_params(reference.data, settings))
            if node.node_id == node_id else node for node in session.draft_workflow.nodes))
        checked = self.tools.validate(workflow)
        if not checked.success:
            return checked
        snapshot = self.tools.snapshot()
        with self._lock, self.tools.publication(snapshot) as available, cancellation.publication() as active:
            current = self._sessions.get(session_id)
            if (not available or not active or current is None or current.state_version != expected_version
                    or self.source_identity(session_id) != source_identity
                    or not self.image_sources.matches_file(source_identity)):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            result = self._record_workflow_edit(session_id, workflow, "beauty.detail.upgrade")
            cancellation.mark_published()
            return result

    def upgrade_enhancement_effect(self, session_id, node_id, *, expected_version, cancellation=None):
        from dataclasses import replace
        from ..core.tools.enhancement import upgrade_settings
        from ..core.tools.effect_migration import reference_params
        from .output_writer import OperationCancellation
        cancellation = cancellation or OperationCancellation()
        self.end_edit_group(session_id)
        current = self.get_session(session_id)
        if not current.success:
            return current
        session = current.data
        if session.state_version != expected_version:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        original = next((node for node in session.draft_workflow.nodes if node.node_id == node_id), None)
        if original is None:
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.workflow.params")
        tool_id = original.params.get("tool_id") or original.params.get("tool")
        if tool_id not in ("denoise", "ai_enhance") or original.params.get("params_version") == 3:
            return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible")
        if tool_id == "denoise" and any(node is not original and
                (node.params.get("tool_id") or node.params.get("tool")) == "ai_enhance"
                for node in session.draft_workflow.nodes):
            return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "enhance.upgrade_conflict")
        defaults = {cap: self.models.default(cap) for cap in ("super_resolution", "denoise", "deblur")}
        settings = upgrade_settings(tool_id, original.params["settings"], defaults)
        reference = self.tools.effect_reference("ai_enhance", settings)
        if not reference.success:
            return reference
        workflow = replace(session.draft_workflow, nodes=tuple(
            replace(node, node_type="tool", version=1, params=reference_params(reference.data, settings))
            if node.node_id == node_id else node for node in session.draft_workflow.nodes))
        checked = self.tools.validate(workflow, allow_model_gaps=True)
        if not checked.success:
            return checked
        with self._lock, cancellation.publication() as active:
            current = self._sessions.get(session_id)
            if not active or current is None or current.state_version != expected_version:
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            result = self._record_workflow_edit(session_id, workflow, "menu.edit.ai_enhance")
            cancellation.mark_published()
            return result

    @synchronized
    def history_state(self, session_id, *, include_revisions=False):
        session = self._sessions.get(session_id)
        if session is None:
            return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
        current = session.current_revision
        state = {
            "can_undo": current.parent_id is not None,
            "can_redo": current.revision_id in self._redo_targets[session_id],
        }
        if include_revisions:
            state["revisions"] = copy.deepcopy(self._history[session_id])
        return CoreResult.ok(state)

    @synchronized
    def history_timeline(self, session_id):
        """当前这条历史线：从根修订到当前修订，再沿重做目标到最远可以重做的一步。

        返回 ``revisions``（自旧到新）与 ``current``（当前修订在其中的位置）；其后的各项是已撤销、可以重做的。
        """
        session = self._sessions.get(session_id)
        if session is None:
            return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
        revisions = {item.revision_id: item for item in self._history[session_id]}
        line = []
        cursor = session.current_revision
        while cursor is not None:
            line.append(cursor)
            cursor = revisions.get(cursor.parent_id)
        line.reverse()
        current = len(line) - 1
        targets = self._redo_targets[session_id]
        seen = {item.revision_id for item in line}
        following = targets.get(line[-1].revision_id)
        while following in revisions and following not in seen:
            line.append(revisions[following])
            seen.add(following)
            following = targets.get(following)
        return CoreResult.ok({"revisions": tuple(copy.deepcopy(line)), "current": current,
                              "state_version": session.state_version})

    @synchronized
    def revision_workflow(self, session_id, revision_id):
        """某个修订的工作流副本。"""
        if session_id not in self._sessions:
            return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
        workflow = self._workflows[session_id].get(revision_id)
        if workflow is None:
            return CoreResult.fail(ErrorCode.REVISION_NOT_FOUND, "error.revision.not_found",
                                   details={"revision_id": revision_id})
        return CoreResult.ok(copy.deepcopy(workflow))

    def preview(
        self, session_id, workflow, *, generation=0, compute_histogram=False,
        is_cancelled=None, update_draft=True, max_dimension=None, allow_intermediate=False,
        display=False, sampling_request=None, priority=0, keep_full_display=False, expected_state=None,
        temporary_workflow=None,
    ):
        """display=True 时结果的 image 为 None，display 为显示用 RGBA8 帧；其余契约相同。"""
        started = monotonic()
        if ((allow_intermediate and update_draft) or (display and compute_histogram)
                or sampling_request is not None and (update_draft or allow_intermediate)
                or keep_full_display and not display
                or temporary_workflow is not None and (not allow_intermediate or update_draft or not display
                                                       or sampling_request is not None or keep_full_display)):
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.preview.invalid")
        if max_dimension is not None:
            if type(max_dimension) is not int or max_dimension < 1:
                return CoreResult.fail(ErrorCode.PARAMETER_INVALID, "error.param.range")
        checked = self.validate_workflow(workflow)
        if not checked.success:
            return checked
        tool_snapshot = self.tools.snapshot()
        tool_generation = tool_snapshot.generation
        workflow = copy.deepcopy(workflow)
        if sampling_request is not None:
            capability = tool_snapshot.sampling_capability(workflow, sampling_request)
            if not capability.success:
                return capability
        prepared_at = monotonic()
        resolved = self._resolve(session_id, is_cancelled=is_cancelled,
                                 max_side=max_dimension if self.tools.supports_proxy(
                                     workflow, canonical_inputs=sampling_request is None) else None,
                                 allow_intermediate=allow_intermediate, priority=priority, expected_state=expected_state)
        if not resolved.success:
            return resolved
        session, source_lease, version, source_preview = resolved.data
        result = None
        retained = False
        try:
            resolved_at = monotonic()
            engine_source = source_lease.value
            if update_draft or workflow.nodes:
                current = self._check_project_execution(session_id, is_cancelled=is_cancelled or (lambda: False),
                                                        workflow=workflow if allow_intermediate else None)
                if not current.success:
                    return current
            with self._lock:
                source = copy.deepcopy(session.active_source)
                render_identity = self._source_identities.get(source.source_id)
                sampling_identity = self._source_identities.get(source.source_id) if sampling_request is not None else None
                if (self._sessions.get(session_id) is not session
                        or (not allow_intermediate and session.state_version != version)):
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                token = self._preview_tokens.get(session_id, 0)
                if update_draft:
                    if generation and generation < self._preview_generations.get(session_id, 0):
                        return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                    self._preview_generations[session_id] = generation
                    token += 1
                    self._preview_tokens[session_id] = token

            render_workflow = workflow

            def cancelled():
                if not self.tools.is_current(tool_generation) or (is_cancelled is not None and is_cancelled()):
                    return True
                with self._lock:
                    return (self._sessions.get(session_id) is not session
                            or session.active_source != source
                            or (sampling_request is not None
                                and self._source_identities.get(source.source_id) != sampling_identity)
                            or self.source_preview(session_id) != source_preview
                            or (not allow_intermediate and session.state_version != version)
                            or (update_draft and self._preview_tokens.get(session_id) != token))

            checked_at = monotonic()
            minimum_threads = tool_snapshot.required_threads(render_workflow)
            if engine_source.is_proxy and self.tools.uses_prepared_inputs(render_workflow):
                minimum_threads = max(minimum_threads, self.image_sources.policy.operator_threads)
            with self.image_sources.resources.working_memory(engine_source.pixels.nbytes * 4, cancelled):
                with self.image_sources.compute.borrow(cancelled, priority=priority, role="foreground",
                        threads=max(self.image_sources.policy.interactive_threads, minimum_threads),
                        minimum_threads=minimum_threads) as compute:
                    render_started = monotonic()
                    result = self._preview_service.render(
                        engine_source, render_workflow, generation=generation,
                        compute_histogram=compute_histogram, is_cancelled=cancelled,
                        display=display, max_dimension=max_dimension, parallelism=compute.threads,
                        full_source=lambda abort: self.image_sources.borrow_linear(source.path,
                            identity=render_identity, is_cancelled=abort, priority=priority),
                        temporary_workflow=temporary_workflow,
                        **({"sampling_request": sampling_request} if sampling_request is not None else {}),
                        **({"resources": self.image_sources.resources}
                           if sampling_request is not None or keep_full_display else {}),
                        **({"keep_full_display": not engine_source.is_proxy} if keep_full_display else {}),
                    )
            logger.debug("OPS 预览准备 session=%s state=%s input=%sx%s proxy=%s prepare_ms=%.1f source_ms=%.1f availability_ms=%.1f budget_wait_ms=%.1f render_ms=%.1f",
                         session_id, version, engine_source.pixels.shape[1], engine_source.pixels.shape[0],
                         engine_source.is_proxy, (prepared_at - started) * 1000,
                         (resolved_at - prepared_at) * 1000, (checked_at - resolved_at) * 1000,
                         (render_started - checked_at) * 1000, (monotonic() - render_started) * 1000)
            if result.success and result.data.image is not None:
                if max_dimension is not None:
                    result.data.image.thumbnail((max_dimension, max_dimension))
                    result.data.width, result.data.height = result.data.image.size
                if display:
                    with result.data.image as image:
                        result.data.display = DisplayFrame.from_image(image)
                    result.data.image = None
            if result.success and result.data.sampling is not None:
                if sampling_identity is None or not self.image_sources.matches_file(sampling_identity):
                    result.data.close()
                    return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
                result.data.sampling.source_identity = sampling_identity
                result.data.sampling.source_preview = source_preview
                result.data.sampling.state_version = version
            with self._lock, self.tools.publication(tool_snapshot) as current:
                if not current or cancelled():
                    if result.success:
                        result.data.close()
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                if result.success:
                    result.data.source_id = session.active_source.source_id
                    result.data.workflow_hash = workflow.workflow_hash()
                    if result.data.detail is not None:
                        result.data.detail.source = source_lease
                        source_lease = None
                    if update_draft:
                        session.draft_workflow = workflow
                        session.state_version += 1
                        session.preview_state = {"workflow_hash": result.data.workflow_hash,
                                                 "generation": generation}
            retained = True
            return result
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as memory_error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(memory_error))
        finally:
            if not retained and result is not None and result.success:
                result.data.close()
            if source_lease is not None:
                source_lease.close()

    def _operation_source(self, session_id, expected_state=None):
        with self._lock:
            if session_id in self._relocation_owners:
                return CoreResult.fail(ErrorCode.CANCELLED, "library.busy")
            session = self._sessions.get(session_id)
            if session is None:
                return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
            if not self.source_access_guard(session.active_source.path):
                return CoreResult.fail(ErrorCode.CANCELLED, "library.recovery_required")
            if expected_state is not None and session.state_version != expected_state:
                return CoreResult.fail(ErrorCode.STALE_CANDIDATE, "error.agent.stale_candidate")
            return CoreResult.ok((session, copy.deepcopy(session)))

    def _operation_pixels(self, source, is_cancelled=None):
        try:
            if hash_file(source.path, is_cancelled=is_cancelled or (lambda: False)) != source.file_hash:
                return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except OSError:
            return CoreResult.fail(ErrorCode.SOURCE_NOT_FOUND, "error.source.not_found")
        decoded = self._decode_source(source.path, source.file_hash, is_cancelled=is_cancelled)
        if not decoded.success:
            return decoded
        return CoreResult.ok(decoded.data[0])

    @staticmethod
    def _source_stamp(path):
        stat = os.stat(path)
        return stat.st_size, stat.st_mtime_ns, stat.st_ino

    def commit(self, session_id: str, workflow: WorkflowGraph, policy=CommitPolicy.PROJECT, *, label="",
               expected_state=None, is_cancelled: Callable[[], bool] | None = None,
               cancellation=None) -> CoreResult[CommitResult]:
        with self._lock:
            self._edit_groups.pop(session_id, None)
            project_path = self._project_paths.get(session_id) if self._autosave_projects else None
        try:
            reservation_context = self._export_service.writer.reserve(project_path) if project_path else nullcontext(None)
            with reservation_context as reservation:
                return self._commit(session_id, workflow, policy, label=label, expected_state=expected_state,
                                    is_cancelled=is_cancelled, cancellation=cancellation,
                                    project_reservation=reservation)
        except Exception as exc:
            return CoreResult.from_exception(exc, code=ErrorCode.ARTIFACT_WRITE_FAILED,
                                             message_key="error.commit.failed")

    def _commit(self, session_id, workflow, policy, *, label, expected_state, is_cancelled,
                cancellation, project_reservation):
        from .output_writer import OperationCancellation
        cancellation = cancellation or OperationCancellation()
        ready = self.ensure_source_ready(session_id, expected_state=expected_state,
            is_cancelled=lambda: cancellation.is_cancelled() or bool(is_cancelled and is_cancelled()))
        if not ready.success:
            return ready
        checked = self.validate_workflow(workflow)
        if not checked.success:
            return checked
        tool_snapshot = self.tools.snapshot()
        with self._lock:
            self._edit_groups.pop(session_id, None)
            captured = self._operation_source(session_id, expected_state)
        if not captured.success:
            return captured
        session, state = captured.data
        workflow = copy.deepcopy(workflow)
        current = self._check_project_execution(session_id, is_cancelled=cancellation.is_cancelled)
        if not current.success:
            return current
        with self._lock:
            project_path = self._project_paths.get(session_id)
            project_hash = self._project_hashes.get(session_id)
        if self._autosave_projects and project_path and (
            project_reservation is None or str(project_reservation[0]) != project_path
        ):
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")

        def session_current():
            return (self._sessions.get(session_id) is session
                    and session.state_version == state.state_version
                    and self._project_paths.get(session_id) == project_path
                    and self._project_hashes.get(session_id) == project_hash)

        def cancelled():
            if (project_reservation is not None
                    and not self._export_service.writer.is_current(project_reservation)):
                return True
            with self._lock:
                return (not session_current()
                        or tool_snapshot.is_cancelled() or cancellation.is_cancelled()
                        or bool(is_cancelled and is_cancelled()))

        try:
            source_stamp = self._source_stamp(state.active_source.path)
        except OSError:
            return CoreResult.fail(ErrorCode.SOURCE_NOT_FOUND, "error.source.not_found")
        pixels = self._operation_pixels(state.active_source, cancelled)
        if not pixels.success:
            return pixels
        try:
            with pixels.data as lease:
                with self.image_sources.resources.working_memory(lease.value.pixels.nbytes * 4, cancelled):
                    with self.image_sources.compute.borrow(cancelled, priority=1):
                        if cancelled():
                            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                        result = self._commit_service.commit(
                            lease.value, state.active_source, workflow, policy=policy,
                            parent=state.current_revision, label=label, is_cancelled=cancelled,
                        )
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as memory_error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(memory_error))
        if not result.success:
            return result
        published = False
        staged = None
        try:
            committed = result.data
            parent = state.current_revision
            if cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            with self._lock:
                if not session_current():
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                if self._autosave_projects and project_path:
                    doc = self._project_document(session_id)
                    doc.schema_version = max(2, doc.schema_version)
                    doc.current_revision = committed.revision.revision_id
                    doc.revisions.append(committed.revision.to_dict())
                    doc.workflows[committed.revision.revision_id] = workflow.to_dict()
                    doc.draft = workflow.to_dict()
                    if parent:
                        doc.redo_targets[parent.revision_id] = committed.revision.revision_id
            if self._autosave_projects and project_path:
                staged = self._prepare_project(project_path, doc, expected_hash=project_hash,
                                               is_cancelled=cancelled)

            @contextmanager
            def publication():
                with self._lock, self.tools.publication(tool_snapshot) as current, cancellation.publication() as active:
                    yield (current and active and session_current()
                           and not bool(is_cancelled and is_cancelled()))

            with self._export_service.writer.publication(project_reservation, publication) as valid:
                if not valid:
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                if self._source_stamp(state.active_source.path) != source_stamp:
                    return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
                if staged is not None:
                    self._publish_project(session_id, staged, project_reservation,
                                          revision_id=committed.revision.revision_id,
                                          saved_version=state.state_version + 1)
                    committed.revision = staged[1][-1]
                else:
                    self._history[session_id].append(committed.revision)
                self._workflows[session_id][committed.revision.revision_id] = copy.deepcopy(workflow)
                if parent:
                    self._redo_targets[session_id][parent.revision_id] = committed.revision.revision_id
                self._pointer[session_id] = len(self._history[session_id]) - 1
                session.current_revision = committed.revision
                session.draft_workflow = copy.deepcopy(workflow)
                session.state_version += 1
                session.preview_state = {}
                original_artifact = committed.artifact
                committed.artifact = session.current_revision.artifact_refs[0]
                cancellation.mark_published()
                published = True
            if staged is not None:
                self._commit_service.discard(original_artifact)
            return result
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except ProjectChangedError:
            return CoreResult.fail(ErrorCode.PROJECT_CHANGED, "error.project.changed")
        except Exception as exc:
            return CoreResult.from_exception(exc, code=ErrorCode.ARTIFACT_WRITE_FAILED,
                                             message_key="error.commit.failed")
        finally:
            if staged is not None:
                staged[0].close()
            if not published:
                self._commit_service.discard(result.data.artifact)

    def _export_target_allowed(self, session_id, target, allow_source_overwrite):
        from pathlib import Path
        session = self._sessions[session_id]
        source = Path(session.active_source.path).resolve()
        allow_source_overwrite = (allow_source_overwrite and session_id not in self._project_paths
                                  and not session.active_source.is_raw)
        protected = set()
        for sid, active in self._sessions.items():
            path = Path(active.active_source.path).resolve()
            if not (allow_source_overwrite and path == source and sid not in self._project_paths):
                protected.add(path)
        for history in self._history.values():
            protected.update(Path(a.path).resolve() for r in history for a in r.artifact_refs if a.path)
        for path in self._project_paths.values():
            project = Path(path).resolve()
            protected.add(project)
            if target.is_relative_to(project.with_suffix(".opsdata")):
                return False
        if target in protected:
            return False
        return not (target.exists() and any(path.exists() and os.path.samefile(target, path)
                                            for path in protected))

    @contextmanager
    def _fixed_output_target_guard(self, target, *, is_cancelled=lambda: False):
        captured = self.capture_export_protection()
        protection = self._fixed_outputs.current_protection(captured, is_cancelled)
        with self._lock:
            if is_cancelled():
                raise CancelledError()
            current = self.capture_export_protection()
            if captured.paths != current.paths or captured.directories != current.directories:
                raise CancelledError()
            yield protection.allows(target)

    def capture_export_snapshot(self, session_id, *, expected_state=None, protected_paths=(), protection=None):
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError("error.session.not_found")
            if expected_state is not None and session.state_version != expected_state:
                raise ValueError("error.agent.stale_candidate")
            path, source_hash = session.active_source.path, session.active_source.file_hash
            workflow = copy.deepcopy(session.draft_workflow)
            generation = self.tools.generation
            protection = protection or self.capture_export_protection(protected_paths)
        return self.capture_export_workflow(path, workflow, source_hash=source_hash,
            expected_tools=generation, protection=protection)

    @synchronized
    def capture_export_protection(self, protected_paths=()):
        from pathlib import Path
        from .export_snapshots import ExportProtection
        protected = [active.active_source.path for active in self._sessions.values()]
        protected.extend(protected_paths)
        protected.extend(self._project_paths.values())
        protected.extend(artifact.path for history in self._history.values() for revision in history
                         for artifact in revision.artifact_refs if artifact.path)
        directories = [str(Path(project).with_suffix(".opsdata")) for project in self._project_paths.values()]
        directories.append(str(self.tools.mask_assets.directory))
        return ExportProtection(tuple(dict.fromkeys(os.fspath(path) for path in protected)),
                                tuple(dict.fromkeys(directories)))

    def capture_export_workflow(self, path, workflow, *, source_hash="", expected_tools=None, protected_paths=(), protection=None,
                                is_cancelled=lambda: False):
        with self._lock:
            generation = self.tools.generation
            if expected_tools is not None and generation != expected_tools:
                raise ValueError("export_queue.render_changed")
            protection = protection or self.capture_export_protection(protected_paths)
            workflow = copy.deepcopy(workflow)
        snapshot = self._fixed_outputs.capture(path, workflow, source_hash=source_hash,
            protection=protection, tool_generation=generation, is_cancelled=is_cancelled)
        if is_cancelled():
            self._fixed_outputs.release(snapshot)
            raise CancelledError()
        if not self.tools.is_current(generation):
            self._fixed_outputs.release(snapshot)
            raise ValueError("export_queue.render_changed")
        return snapshot

    def prepare_export_snapshot(self, snapshot, *, is_cancelled=lambda: False):
        return self._fixed_outputs.prepare(snapshot, is_cancelled)

    def export_snapshot(self, snapshot, target_path, preset, *, cancellation=None):
        return self._fixed_outputs.export(snapshot, target_path, preset, cancellation=cancellation)

    def preview_export_snapshot(self, snapshot, preset, *, cancellation, max_dimension=1600, proxy=False, square=False):
        return self._fixed_outputs.preview(snapshot, preset, cancellation=cancellation, max_dimension=max_dimension,
                                           proxy=proxy, square=square)

    def release_export_snapshot(self, snapshot):
        self._fixed_outputs.release(snapshot)

    def retain_export_snapshot(self, snapshot):
        return self._fixed_outputs.retain(snapshot)

    def borrow_export_snapshot(self, snapshot):
        return self._fixed_outputs.borrow(snapshot)

    def export_render_contract(self, path):
        return self._fixed_outputs.render_contract(path)

    def export(self, session_id: str, request: ExportRequest, *, expected_state=None, cancellation=None,
               is_cancelled=None, allow_source_overwrite=False) -> CoreResult[ExportResult]:
        from pathlib import Path
        from .output_writer import OperationCancellation
        cancellation = cancellation or OperationCancellation()
        ready = self.ensure_source_ready(session_id, expected_state=expected_state,
            is_cancelled=lambda: cancellation.is_cancelled() or bool(is_cancelled and is_cancelled()))
        if not ready.success:
            return ready
        checked = self._export_service.validate_request(request)
        if not checked.success:
            return checked
        if cancellation.is_cancelled() or (is_cancelled is not None and is_cancelled()):
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        try:
            with self._export_service.writer.reserve(request.target_path) as reservation:
                captured = self._operation_source(session_id, expected_state)
                if not captured.success:
                    return captured
                session, state = captured.data
                tool_snapshot = self.tools.snapshot()
                execution = self._check_project_execution(session_id, is_cancelled=cancellation.is_cancelled)
                revision = state.current_revision
                artifact = (revision.artifact_refs[0] if revision and revision.artifact_refs
                            and revision.workflow_hash == state.draft_workflow.workflow_hash() else None)
                if not execution.success and artifact is None:
                    return execution
                with self._lock:
                    if not self._export_target_allowed(session_id, reservation[0], allow_source_overwrite):
                        return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.export.protected_target")

                def stamp():
                    return self._source_stamp(state.active_source.path)

                source_stamp = stamp()
                if (hash_file(state.active_source.path, is_cancelled=cancellation.is_cancelled) != state.active_source.file_hash
                        or stamp() != source_stamp):
                    return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")

                def cancelled():
                    with self._lock:
                        return (self._sessions.get(session_id) is not session
                                or session.state_version != state.state_version
                                or tool_snapshot.is_cancelled() or cancellation.is_cancelled()
                                or bool(is_cancelled and is_cancelled()))

                @contextmanager
                def publication():
                    with self._lock, self.tools.publication(tool_snapshot) as available, cancellation.publication() as active:
                        valid = available and active and not cancelled()
                        if valid and stamp() != source_stamp:
                            raise SourceChangedError()
                        if valid and Path(request.target_path).resolve() != reservation[0]:
                            raise ValueError("导出目标路径在任务期间发生变化")
                        if valid and not self._export_target_allowed(session_id, reservation[0], allow_source_overwrite):
                            raise ValueError("导出目标受保护")
                        yield valid

                ext = (request.ext or Path(request.target_path).suffix).lower().lstrip(".")
                needs_render = artifact is None or (artifact.kind != LINEAR_ARTIFACT
                                                    and execution.success and ext in ("tif", "tiff"))
                pixels = self._operation_pixels(state.active_source, cancelled) if needs_render else CoreResult.ok(None)
                if not pixels.success:
                    return pixels
                try:
                    estimate = (pixels.data.value.pixels.nbytes * 4 if needs_render
                                else state.active_source.width * state.active_source.height * 64)
                    with self.image_sources.resources.working_memory(estimate, cancelled):
                        with self.image_sources.compute.borrow(cancelled, priority=1):
                            return self._export_service.export(
                                pixels.data.value if needs_render else None, state.draft_workflow, request, artifact=artifact,
                                execution_allowed=execution.success, is_cancelled=cancelled,
                                publication=publication, reservation=reservation,
                                on_published=cancellation.mark_published,
                                preserve_alpha=allow_source_overwrite and reservation[0] == Path(state.active_source.path).resolve(),
                            )
                finally:
                    if needs_render:
                        pixels.data.close()
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as memory_error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(memory_error))
        except Exception as exc:
            return CoreResult.from_exception(exc, code=ErrorCode.ARTIFACT_WRITE_FAILED,
                                             message_key="error.export.failed")

    def export_grid(self, session_id, params, directory, prefix, extension, *, expected_state, cancellation):
        from .image_output_service import ImageOutputService
        ready = self.ensure_source_ready(session_id, expected_state=expected_state,
                                         is_cancelled=cancellation.is_cancelled)
        if not ready.success:
            return ready
        checked = self.tools.validate_tool_params("grid_crop", params)
        if not checked.success:
            return checked
        params = checked.data
        resolved = self._operation_source(session_id, expected_state)
        if not resolved.success:
            return resolved
        live, session = resolved.data
        allowed = self._check_project_execution(session_id)
        if not allowed.success:
            return allowed
        checked = self.tools.validate(session.draft_workflow)
        if not checked.success:
            return checked
        snapshot = self.tools.snapshot()
        try:
            stamp = self._source_stamp(session.active_source.path)
            def current():
                with self._lock:
                    return (self._sessions.get(session_id) is live and live.state_version == expected_state
                            and self.tools.check_enabled("grid_crop").success
                            and not snapshot.is_cancelled() and not cancellation.is_cancelled()
                            and self._source_stamp(session.active_source.path) == stamp)
            @contextmanager
            def publication():
                with self._lock, self.tools.publication(snapshot) as available, cancellation.publication() as active:
                    yield available and active and current()
            pixels = self._operation_pixels(session.active_source, lambda: not current())
            if not pixels.success:
                return pixels
            with pixels.data as lease:
                cancelled = lambda: not current()
                with self.image_sources.resources.working_memory(lease.value.pixels.nbytes * 4, cancelled):
                    with self.image_sources.compute.borrow(cancelled, priority=1):
                        rendered = snapshot.executor.execute(lease.value, session.draft_workflow,
                                                             is_cancelled=cancelled)
                        if not rendered.success:
                            return rendered
                        service = ImageOutputService(self._export_service.writer)
                        paths = service.grid(rendered.data, params, directory, prefix, extension,
                                             cancellation=cancellation, publication=publication)
                        return CoreResult.ok(paths)
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as memory_error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(memory_error))
        except Exception as error:
            return CoreResult.from_exception(error, message_key="error.export.failed")

    def history_preview(self, session_id, *, revision_id=None, max_dimension=1600, is_cancelled=None,
                        display=False, keep_full_display=False, priority=0, expected_state=None):
        ready = self.ensure_source_ready(session_id, expected_state=expected_state,
                                         is_cancelled=is_cancelled or (lambda: False))
        if not ready.success:
            return ready
        if max_dimension is not None and (type(max_dimension) is not int or max_dimension < 1):
            return CoreResult.fail(ErrorCode.PARAMETER_INVALID, "error.param.range")
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
            revision = session.current_revision if revision_id is None else next(
                (item for item in self._history[session_id] if item.revision_id == revision_id), None)
            if revision is None or not revision.artifact_refs:
                return CoreResult.fail(ErrorCode.REVISION_NOT_FOUND, "error.tool.history_missing")
            artifact = revision.artifact_refs[0]
            source_id = session.active_source.source_id
            estimate = session.active_source.width * session.active_source.height * 64

        def cancelled():
            if is_cancelled is not None and is_cancelled():
                return True
            with self._lock:
                return (self._sessions.get(session_id) is not session
                        or session.active_source.source_id != source_id
                        or expected_state is not None and session.state_version != expected_state)

        image, detail, retained = None, None, False
        try:
            from io import BytesIO
            from pathlib import Path
            from PIL import Image
            import hashlib
            with self.image_sources.resources.working_memory(estimate, cancelled):
                with self.image_sources.compute.borrow(cancelled, priority=priority):
                    payload = Path(artifact.path).read_bytes()
                    if hashlib.sha256(payload).hexdigest() != artifact.hash:
                        return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.project.invalid")
                    if artifact.kind == LINEAR_ARTIFACT:
                        from ..core.render_codec import read_linear
                        image = self._engine.source_to_pil(read_linear(BytesIO(payload)))
                    else:
                        with Image.open(BytesIO(payload)) as original:
                            image = original.convert("RGBA")
                    full_size = image.size
                    detail_error = None
                    if display and keep_full_display and (max_dimension is None or max(image.size) > max_dimension):
                        try:
                            detail = DetailDisplay(self.image_sources.resources, image.size,
                                lambda: DisplayFrame.from_image(image), cancelled)
                        except MemoryError as error:
                            if max_dimension is None:
                                raise
                            detail_error = CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(error)).error
                    if max_dimension:
                        image.thumbnail((max_dimension, max_dimension))
                    if cancelled():
                        image.close()
                        return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            frame = (detail.frame if detail is not None and detail.frame.size == image.size
                     else DisplayFrame.from_image(image)) if display else None
            result = PreviewResult(None if display else image, source_id, revision.workflow_hash,
                image.width, image.height, display=frame, detail=detail, full_size=full_size,
                detail_error=detail_error)
            if display:
                image.close()
            retained = True
            return CoreResult.ok(result)
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as memory_error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(memory_error))
        except ImportError:
            return CoreResult.fail(ErrorCode.UNSUPPORTED_FORMAT, "error.export.codec_unavailable")
        except (OSError, ValueError, TypeError):
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.project.invalid")
        finally:
            if not retained:
                if image is not None:
                    image.close()
                if detail is not None:
                    detail.close()

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    def undo(self, session_id: str) -> CoreResult[RevisionRef]:
        with self._lock:
            self._edit_groups.pop(session_id, None)
            session = self._sessions.get(session_id)
            if session is None:
                return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
            current = session.current_revision
            if current is None or current.parent_id is None:
                return CoreResult.fail(ErrorCode.REVISION_NOT_FOUND, "error.history.no_undo")
            version, parent = session.state_version, current.parent_id
        return self._checkout(session_id, parent, expected_state=version, keep_redo_path=True)

    def redo(self, session_id: str) -> CoreResult[RevisionRef]:
        with self._lock:
            self._edit_groups.pop(session_id, None)
            session = self._sessions.get(session_id)
            if session is None:
                return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
            current = session.current_revision
            target = self._redo_targets[session_id].get(current.revision_id) if current else None
            if target is None:
                return CoreResult.fail(ErrorCode.REVISION_NOT_FOUND, "error.history.no_redo")
            version = session.state_version
        return self._checkout(session_id, target, expected_state=version)

    def checkout(self, session_id: str, revision_id: str, *, expected_state=None,
                 cancellation=None, keep_redo_path=False) -> CoreResult[RevisionRef]:
        """把当前修订移到指定修订。

        ``keep_redo_path`` 为真且目标是当前修订的祖先时，沿途各步记为重做目标，效果等同连续撤销。
        """
        return self._checkout(session_id, revision_id, expected_state=expected_state,
                              cancellation=cancellation, keep_redo_path=keep_redo_path)

    def _checkout(self, session_id, revision_id, *, expected_state=None, cancellation=None, keep_redo_path=False):
        with self._lock:
            if session_id in self._relocation_owners:
                return CoreResult.fail(ErrorCode.CANCELLED, "library.busy")
        from .output_writer import OperationCancellation
        cancellation = cancellation or OperationCancellation()
        with self._lock:
            self._edit_groups.pop(session_id, None)
            session = self._sessions.get(session_id)
            if session is None:
                return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
            if not self.source_access_guard(session.active_source.path):
                return CoreResult.fail(ErrorCode.CANCELLED, "library.recovery_required")
            if expected_state is not None and session.state_version != expected_state:
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            index = next((i for i, r in enumerate(self._history[session_id]) if r.revision_id == revision_id), None)
            if index is None:
                return CoreResult.fail(ErrorCode.REVISION_NOT_FOUND, "error.revision.not_found",
                                       details={"revision_id": revision_id})
            version = session.state_version
            workflow = copy.deepcopy(self._workflows[session_id][revision_id])
            redo_path = {}
            if keep_redo_path:
                parents = {item.revision_id: item.parent_id for item in self._history[session_id]}
                cursor = session.current_revision.revision_id
                while cursor != revision_id:
                    parent = parents.get(cursor)
                    if parent is None:
                        # 目标不在当前修订的祖先里
                        redo_path = {}
                        break
                    redo_path[parent] = cursor
                    cursor = parent
            project_path = self._project_paths.get(session_id)
            project_hash = self._project_hashes.get(session_id)
            doc = self._project_document(session_id) if self._autosave_projects and project_path else None
        if doc is not None:
            doc.current_revision = revision_id
            doc.draft = workflow.to_dict()
            doc.redo_targets.update(redo_path)
        staged = None
        writer = self._export_service.writer
        try:
            with writer.reserve(project_path) if doc is not None else nullcontext(None) as reservation:
                def current():
                    return (self._sessions.get(session_id) is session and session.state_version == version
                            and self._project_paths.get(session_id) == project_path
                            and self._project_hashes.get(session_id) == project_hash)

                def cancelled():
                    if cancellation.is_cancelled() or (reservation is not None and not writer.is_current(reservation)):
                        return True
                    with self._lock:
                        return not current()

                if cancelled():
                    return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                if doc is not None:
                    staged = self._prepare_project(project_path, doc, expected_hash=project_hash,
                                                   is_cancelled=cancelled)

                @contextmanager
                def publication():
                    with self._lock, cancellation.publication() as active:
                        yield active and current()

                with writer.publication(reservation, publication) as valid:
                    if not valid:
                        return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                    if staged is not None:
                        self._publish_project(session_id, staged, reservation, revision_id=revision_id,
                                              saved_version=version + 1)
                    self._pointer[session_id] = index
                    session.current_revision = self._history[session_id][index]
                    session.draft_workflow = workflow
                    session.preview_state = {}
                    self._redo_targets[session_id].update(redo_path)
                    session.state_version += 1
                    cancellation.mark_published()
                    return CoreResult.ok(copy.deepcopy(session.current_revision))
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except ProjectChangedError:
            return CoreResult.fail(ErrorCode.PROJECT_CHANGED, "error.project.changed")
        except Exception as exc:
            return CoreResult.from_exception(exc, code=ErrorCode.ARTIFACT_WRITE_FAILED,
                                             message_key="error.project.save_failed")
        finally:
            if staged is not None:
                staged[0].close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _decode_source(
        self, path: str, expected_hash: str | None = None, *, is_cancelled=None, identity=None,
        max_side=None, half_size=False, priority=0,
    ) -> CoreResult[tuple]:
        if not os.path.isfile(path):
            return CoreResult.fail(ErrorCode.SOURCE_NOT_FOUND, "error.source.not_found")
        try:
            if is_cancelled is not None and is_cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            identity = identity or self.image_sources.identity(path, is_cancelled=is_cancelled or (lambda: False))
            if expected_hash is not None and identity.file_hash != expected_hash:
                return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
            if is_cancelled is not None and is_cancelled():
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            lease = self.image_sources.borrow_linear(path, identity=identity, max_side=max_side,
                                                     half_size=half_size,
                                                     priority=priority,
                                                     is_cancelled=is_cancelled or (lambda: False))
            return CoreResult.ok((lease, identity))
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except MemoryError as memory_error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(memory_error))
        except Exception as exc:
            code = ErrorCode.DECODE_FAILED
            if isinstance(exc, RawSupportUnavailableError):
                code = ErrorCode.UNSUPPORTED_FORMAT
            return CoreResult.from_exception(
                exc, code=code, message_key="error.source.decode_failed"
            )

    def _resolve(self, session_id: str, *, is_cancelled=None, max_side=None, allow_intermediate=False,
                 priority=0, expected_state=None):
        ready = self.ensure_source_ready(session_id, expected_state=expected_state,
                                         is_cancelled=is_cancelled or (lambda: False))
        if not ready.success:
            return ready
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return CoreResult.fail(ErrorCode.SESSION_NOT_FOUND, "error.session.not_found")
            version = session.state_version
            if expected_state is not None and version != expected_state:
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            source = copy.deepcopy(session.active_source)
            identity = self._source_identities.get(source.source_id)
            source_preview = self.source_preview(session_id)
        if is_cancelled is not None and is_cancelled():
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        decoded = self._decode_source(source.path, source.file_hash, is_cancelled=is_cancelled,
                                      identity=identity, max_side=max_side,
                                      half_size=max_side is not None and source_preview.half_size, priority=priority)
        if not decoded.success:
            return decoded
        lease, identity = decoded.unwrap()
        with self._lock:
            if (self._sessions.get(session_id) is not session
                    or (not allow_intermediate and session.state_version != version)
                    or self.source_preview(session_id) != source_preview
                    or session.active_source != source or (is_cancelled is not None and is_cancelled())):
                lease.close()
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            self._source_identities[source.source_id] = identity
        return CoreResult.ok((session, lease, version, source_preview))
