from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelRequirement:
    role: str
    capability: str
    adapters: tuple[str, ...]
    selection_field: str = ""


@dataclass(frozen=True)
class ResourceFeature:
    feature_id: str
    models: tuple[ModelRequirement, ...] = ()


@dataclass(frozen=True)
class RequiredResource:
    feature_id: str
    role: str
    capability: str = ""
    adapters: tuple[str, ...] = ()
    model_token: str = ""
    digest: str = ""
    path: str = ""
    runtime: str = ""
    issue: str = ""


@dataclass(frozen=True)
class ToolResourceSnapshot:
    tool_id: str
    enabled: bool
    can_disable: bool
    implementation_id: str
    implementation_version: str
    resource_features: tuple[ResourceFeature, ...]
    required_resources: tuple[RequiredResource, ...] = ()
    contextual: bool = False
    issue: str = ""
    operation_request: bool = False
    selected_resources: tuple[RequiredResource, ...] = ()


@dataclass(frozen=True)
class ToolParameterRequest:
    tool_id: str
    settings: dict
    params_version: int = 0
    # 已有记录的精确实现版本；所需资源随它而定时（如美颜的效果版本）由调用方给出，空串表示新建
    implementation_version: str = ""
