from __future__ import annotations

from dataclasses import dataclass, replace

from ..core.contracts.tool_resources import RequiredResource


@dataclass(frozen=True)
class ResourceReadiness:
    kind: str
    identity: str
    name: str
    state: str
    installed_bytes: int = 0
    download_size: int = 0
    source: str = ""
    license: str = ""
    token: str = ""
    digest: str = ""
    path: str = ""
    runtime: str = ""
    shared_with: tuple[str, ...] = ()
    error: str = ""
    downloaded: int = 0
    version: str = ""
    role: str = ""
    usable: bool = False
    installable: bool = False
    temporary_bytes: int = 0
    request_identity: tuple = ()


@dataclass(frozen=True)
class FeatureReadiness:
    feature_id: str
    state: str
    resources: tuple[ResourceReadiness, ...] = ()
    implementation_id: str = ""
    implementation_version: str = ""
    issue: str = ""


@dataclass(frozen=True)
class ToolReadiness:
    tool_id: str
    enabled: bool
    can_disable: bool
    state: str
    installed_bytes: int
    features: tuple[FeatureReadiness, ...]
    contextual: tuple[FeatureReadiness, ...] = ()


@dataclass(frozen=True)
class ResourceOverview:
    rows: tuple[ToolReadiness, ...]
    total_bytes: int
    model_bytes: int = 0
    runtime_bytes: int = 0


def _model_for(requirement, models):
    if requirement.model_token:
        return next((item for item in models if item.descriptor.key.token == requirement.model_token), None)
    if requirement.digest:
        return next((item for item in models
                     if any(file.digest == requirement.digest for file in item.descriptor.files)), None)
    return None


def _resources(requirement, models, libraries):
    model = _model_for(requirement, models)
    issue = requirement.issue
    request_identity = (requirement.role, requirement.capability, requirement.adapters,
        requirement.model_token, requirement.digest,
        requirement.path if not requirement.model_token and not requirement.digest else "",
        issue if issue in ("TOOL_INCOMPATIBLE", "TOOL_LEGACY_UNPINNED", "PARAMETER_INVALID") else "")
    if model is not None:
        descriptor = model.descriptor
        compatible = ((not requirement.capability or descriptor.capability == requirement.capability)
                      and (not requirement.adapters or descriptor.adapter in requirement.adapters))
        digest_matches = (not requirement.digest
                          or any(file.digest == requirement.digest for file in descriptor.files))
        if not compatible or not digest_matches:
            issue = issue or "TOOL_INCOMPATIBLE"
        digest = ",".join(file.digest for file in descriptor.files)
        identity = "model:" + digest
        state = "incompatible" if issue == "TOOL_INCOMPATIBLE" else model.state
        resource = ResourceReadiness("model", identity, descriptor.name, state,
            model.installed_bytes, descriptor.download_size, descriptor.source, descriptor.license,
            descriptor.key.token, digest, "\n".join(path for _, path in model.paths),
            requirement.runtime or descriptor.runtime, error=model.error or issue,
            downloaded=model.downloaded, version=descriptor.key.revision, role=requirement.role,
            usable=model.usable and not issue,
            installable=not model.usable and descriptor.adapter not in ("bundled", "legacy")
                and issue not in ("TOOL_INCOMPATIBLE", "PARAMETER_INVALID"),
            temporary_bytes=model.temporary_bytes, request_identity=request_identity)
    else:
        identity = "model:" + (requirement.digest or requirement.model_token or requirement.role)
        resource = ResourceReadiness("model", identity,
            requirement.model_token or requirement.path or requirement.role,
            "legacy_unpinned" if issue == "TOOL_LEGACY_UNPINNED" else "missing",
            token=requirement.model_token, digest=requirement.digest, path=requirement.path,
            runtime=requirement.runtime, error=issue, role=requirement.role, request_identity=request_identity)
    result = [resource]
    if resource.runtime:
        library = libraries.get(resource.runtime)
        if library is None:
            result.append(ResourceReadiness("runtime", "runtime:" + resource.runtime,
                resource.runtime, "missing", runtime=resource.runtime))
        else:
            result.append(ResourceReadiness("runtime", "runtime:" + (library.token or library.runtime),
                library.runtime, library.state, library.installed_bytes, source=library.source,
                token=library.token, runtime=library.runtime, error=library.error,
                version=library.version, role=requirement.role, usable=library.available,
                installable=bool(library.token) and library.state in ("missing", "corrupt", "update")))
    return tuple(result)


def _feature_state(resources, issue=""):
    if issue:
        return {"TOOL_LEGACY_UNPINNED": "legacy_unpinned",
                "TOOL_DISABLED": "disabled"}.get(issue, "incompatible")
    models = tuple(item for item in resources if item.kind == "model")
    runtimes = tuple(item for item in resources if item.kind == "runtime")
    if any(item.state in ("incompatible", "legacy_unpinned") for item in models):
        return next(item.state for item in models if item.state in ("incompatible", "legacy_unpinned"))
    if any(item.state == "update" for item in runtimes):
        return "update"
    if any(item.state == "disabled" for item in runtimes):
        return "enable"
    if any(item.state not in ("available", "restart") for item in runtimes):
        return "runtime_missing"
    if any(not item.usable for item in models):
        return "model_missing"
    if any(item.state == "restart" for item in runtimes):
        return "restart"
    return "ready"


def _overview_requirement(feature, requirement, models):
    compatible = tuple(item for item in models
        if item.descriptor.capability == requirement.capability
        and item.descriptor.adapter in requirement.adapters)
    model = next((item for item in compatible if item.default), compatible[0] if compatible else None)
    return RequiredResource(feature.feature_id, requirement.role, requirement.capability,
        requirement.adapters, model.descriptor.key.token if model else "")


def project_tool_readiness(tool_snapshots, model_snapshots, library_snapshots, *, runtime_bytes=0):
    models = tuple(model_snapshots)
    libraries = {item.runtime: item for item in library_snapshots}
    current = tuple(item for item in tool_snapshots if not item.contextual)
    contexts = tuple(item for item in tool_snapshots if item.contextual)
    rows = []
    for tool in current:
        features = []
        selections = tuple(resource for context in contexts if context.tool_id == tool.tool_id
            and context.implementation_id == tool.implementation_id for resource in context.selected_resources)
        for feature in tool.resource_features:
            resources = tuple(resource for requirement in feature.models
                for resource in _resources(next((item for item in (*selections, *tool.required_resources)
                    if item.feature_id == feature.feature_id and item.role == requirement.role),
                    _overview_requirement(feature, requirement, models)), models, libraries))
            features.append(FeatureReadiness(feature.feature_id, _feature_state(resources), resources))
        contextual = []
        for context in contexts:
            if context.tool_id != tool.tool_id:
                continue
            groups = {}
            for requirement in context.required_resources:
                groups.setdefault(requirement.feature_id, []).extend(_resources(requirement, models, libraries))
            if not groups:
                groups["basic"] = []
            for feature_id, resources in groups.items():
                resources = tuple(resources)
                issue = context.issue or next((item.error for item in resources
                    if item.error in ("TOOL_LEGACY_UNPINNED", "TOOL_INCOMPATIBLE", "PARAMETER_INVALID")), "")
                contextual.append(FeatureReadiness(feature_id, _feature_state(resources, issue), resources,
                    context.implementation_id, context.implementation_version, issue))
                current_request = (context.implementation_id == tool.implementation_id
                    or context.operation_request and not any(feature.models for feature in tool.resource_features))
                if current_request and resources:
                    replacements = {(resource.kind, resource.role): resource for resource in resources}
                    for index, feature in enumerate(features):
                        projected = tuple(replacements.get((resource.kind, resource.role), resource)
                                          for resource in feature.resources)
                        if projected != feature.resources:
                            features[index] = replace(feature, resources=projected,
                                                      state=_feature_state(projected, issue))
                    requested = FeatureReadiness(feature_id, _feature_state(resources, issue), resources)
                    index = next((index for index, feature in enumerate(features)
                                  if feature.feature_id == feature_id), None)
                    if index is None:
                        features.append(requested)
                    else:
                        features[index] = requested
        states = tuple(item.state for item in features)
        if not tool.enabled:
            state = "disabled"
        elif all(not feature.resources for feature in features):
            state = "no_resources"
        elif all(value == "ready" for value in states):
            state = "ready"
        elif "ready" in states:
            state = "partial"
        else:
            state = next((value for value in ("incompatible", "legacy_unpinned", "update", "enable", "runtime_missing", "model_missing", "restart")
                          if value in states), "model_missing")
        unique = {resource.identity: resource for feature in (*features, *contextual) for resource in feature.resources}
        rows.append(ToolReadiness(tool.tool_id, tool.enabled, tool.can_disable, state,
                                 sum(item.installed_bytes + item.temporary_bytes for item in unique.values()),
                                 tuple(features), tuple(contextual)))
    owners = {}
    for row in rows:
        for feature in (*row.features, *row.contextual):
            for resource in feature.resources:
                owners.setdefault(resource.identity, set()).add(row.tool_id)
    def shared(feature):
        return replace(feature, resources=tuple(replace(resource,
            shared_with=tuple(sorted(owners[resource.identity]))) for resource in feature.resources))
    rows = tuple(replace(row, features=tuple(map(shared, row.features)),
                         contextual=tuple(map(shared, row.contextual))) for row in rows)
    unique_models = {tuple(file.digest for file in item.descriptor.files): item.installed_bytes + item.temporary_bytes
                     for item in models if item.installed_bytes or item.temporary_bytes}
    model_bytes = sum(unique_models.values())
    return ResourceOverview(rows, model_bytes + runtime_bytes, model_bytes, runtime_bytes)
