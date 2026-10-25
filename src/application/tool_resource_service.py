from __future__ import annotations

from dataclasses import dataclass

from ..core.contracts.models import ModelKey
from ..core.contracts.result import CoreResult, ErrorCode
from ..core.contracts.tool_resources import ToolParameterRequest, ToolResourceSnapshot
from ..core.contracts.workflow import WorkflowGraph
from .runtime_libraries import environment_library
from .runtime_bootstrap import python_tag
from .tool_readiness import project_tool_readiness


@dataclass(frozen=True)
class ResourceInstallItem:
    kind: str
    token: str
    name: str
    version: str
    digest: str
    source: str
    license: str
    download_size: int
    requires_restart: bool = False


@dataclass(frozen=True)
class InstallationPlan:
    tool_id: str
    context_identity: tuple
    items: tuple[ResourceInstallItem, ...]
    resource_identity: tuple = ()


@dataclass(frozen=True)
class ResourceContext:
    workflow: WorkflowGraph | None
    requests: tuple[ToolParameterRequest, ...] = ()
    snapshots: tuple[ToolResourceSnapshot, ...] = ()


class ToolResourceService:
    def __init__(self, tools, models, runtime_service=None):
        self.tools = tools
        self.models = models
        self.runtimes = runtime_service

    def overview(self, workflow=None):
        context = workflow if isinstance(workflow, ResourceContext) else ResourceContext(workflow)
        tools = (*self.tools.resource_status(context.workflow, requests=context.requests), *context.snapshots)
        models = self.models.list()
        names = {item.descriptor.runtime for item in models if item.descriptor.runtime}
        names.update(resource.runtime for item in tools for resource in item.required_resources if resource.runtime)
        libraries = (self.runtimes.libraries(names) if self.runtimes is not None
                     else tuple(environment_library(name) for name in sorted(names)))
        runtime_bytes = (sum(item.installed_bytes for item in self.runtimes.list())
                         if self.runtimes is not None else 0)
        return project_tool_readiness(tools, models, libraries, runtime_bytes=runtime_bytes)

    @staticmethod
    def features_for_install(row):
        if row.contextual and (any(feature.resources for feature in row.contextual)
                               or any(feature.issue for feature in row.contextual)):
            return row.contextual
        return row.features

    @staticmethod
    def _resources(row):
        return tuple(resource for feature in ToolResourceService.features_for_install(row) for resource in feature.resources)

    def plan(self, tool_id, workflow=None, context_identity=()):
        row = next((item for item in self.overview(workflow).rows if item.tool_id == tool_id), None)
        if row is None:
            return InstallationPlan(tool_id, tuple(context_identity), ())
        resources = self._resources(row)
        items = {}
        for resource in resources:
            if resource.kind == "runtime" and resource.token and self.runtimes is not None:
                runtime = next((item for item in self.runtimes.list()
                                if item.descriptor.token == resource.token), None)
                file = runtime.descriptor.file_for(python_tag()) if runtime is not None else None
                if file is not None and not runtime.active and runtime.state not in ("installed", "removing"):
                    descriptor = runtime.descriptor
                    items[("runtime", descriptor.token)] = ResourceInstallItem("runtime", descriptor.token,
                        descriptor.name, descriptor.version, file.sha256, file.url,
                        descriptor.license, file.size, True)
                continue
            if resource.kind != "model" or not resource.token or resource.error in (
                    "TOOL_INCOMPATIBLE", "PARAMETER_INVALID"):
                continue
            try:
                descriptor, _ = self.models.describe(resource.token)
                snapshot = self.models.snapshot(descriptor.key)
            except (KeyError, ValueError):
                continue
            if descriptor.adapter in ("bundled", "legacy") or snapshot.usable:
                continue
            digest = ",".join(file.digest for file in descriptor.files)
            if resource.digest and resource.digest not in (digest, *(file.digest for file in descriptor.files)):
                continue
            items[("model", descriptor.key.token)] = ResourceInstallItem("model", descriptor.key.token,
                descriptor.name, descriptor.key.revision, digest, descriptor.source, descriptor.license,
                descriptor.download_size)
        identity = tuple(sorted((feature.feature_id, feature.implementation_id, feature.implementation_version,
            feature.issue, resource.request_identity)
            for feature in self.features_for_install(row) for resource in feature.resources
            if resource.kind == "model"))
        return InstallationPlan(tool_id, tuple(context_identity), tuple(items.values()), identity)

    def install(self, plan, workflow=None, current_identity=()):
        if tuple(current_identity) != plan.context_identity:
            return CoreResult.fail(ErrorCode.PROJECT_CHANGED, "tool_manager.context_changed")
        current = self.plan(plan.tool_id, workflow, current_identity)
        if current.resource_identity != plan.resource_identity:
            return CoreResult.fail(ErrorCode.PROJECT_CHANGED, "tool_manager.context_changed")
        pending = {(item.kind, item.token): item for item in current.items}
        for item in plan.items:
            latest = pending.get((item.kind, item.token))
            if latest is not None and latest != item:
                return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible")
        scheduled = []
        for item in plan.items:
            if (item.kind, item.token) not in pending:
                continue
            if item.kind == "model":
                self.models.install(ModelKey.parse(item.token))
            else:
                self.runtimes.install(item.token)
            scheduled.append(item.token)
        return CoreResult.ok(tuple(scheduled))

    def cancel(self, plan):
        for item in plan.items:
            if item.kind == "model":
                self.models.cancel(ModelKey.parse(item.token))
            elif self.runtimes is not None:
                self.runtimes.cancel(item.token)
