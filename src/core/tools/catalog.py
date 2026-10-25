from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, replace
from typing import Callable

from ..contracts.result import CoreResult, ErrorCode
from ..contracts.tools import ToolDescriptor, ToolReference
from ..contracts.tool_resources import RequiredResource, ResourceFeature


@dataclass(frozen=True)
class ParameterField:
    name: str
    kind: type | tuple[type, ...]
    default: object
    minimum: int | float | None = None
    maximum: int | float | None = None
    choices: tuple = ()


class ParameterContract:
    def __init__(self, fields=(), *, version=1, validate=None, neutral=None, allow_unknown=False):
        self.fields = tuple(fields)
        if type(version) is not int or version < 1:
            raise ValueError("参数版本无效")
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("参数字段重复")
        self.version = version
        self._validate = validate
        self._neutral = neutral
        self._allow_unknown = allow_unknown

    def defaults(self, params_version=1):
        if params_version != self.version:
            raise ValueError("不支持的参数版本")
        return {field.name: copy.deepcopy(field.default) for field in self.fields}

    def normalize(self, raw, params_version=1):
        if params_version != self.version:
            return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible")
        if not isinstance(raw, dict):
            return self._error("settings", "error.param.type")
        if any(not isinstance(key, str) for key in raw):
            return self._error("settings", "error.param.type")
        try:
            json.dumps(raw, allow_nan=False)
        except (ValueError, TypeError):
            return self._error("settings", "error.param.type")
        fields = {field.name: field for field in self.fields}
        if not self._allow_unknown:
            unknown = set(raw) - set(fields)
            if unknown:
                return self._error(f"settings.{sorted(unknown, key=str)[0]}", "error.param.unknown")
        normalized = self.defaults(params_version)
        normalized.update(copy.deepcopy(raw))
        for name, field in fields.items():
            value = normalized[name]
            kinds = field.kind if isinstance(field.kind, tuple) else (field.kind,)
            if type(value) not in kinds:
                return self._error(f"settings.{name}", "error.param.type")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if (type(value) is float and not math.isfinite(value) or field.minimum is not None and value < field.minimum
                        or field.maximum is not None and value > field.maximum):
                    return self._error(f"settings.{name}", "error.param.range")
            if field.choices and value not in field.choices:
                return self._error(f"settings.{name}", "error.param.range")
        if self._validate:
            try:
                self._validate(normalized)
            except (ValueError, TypeError, KeyError):
                return self._error("settings", "error.workflow.params")
        return CoreResult.ok(normalized)

    def is_neutral(self, validated_params, params_version=1):
        if params_version != self.version:
            raise ValueError("不支持的参数版本")
        return bool(self._neutral and self._neutral(validated_params))

    def migrate(self, raw, from_version, to_version):
        if from_version != to_version:
            return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible")
        return self.normalize(raw, to_version)

    @staticmethod
    def _error(path, key):
        return CoreResult.fail(ErrorCode.PARAMETER_INVALID, key, details={"field_path": path})


@dataclass(frozen=True)
class ToolDefinition:
    descriptor: ToolDescriptor
    parameters: ParameterContract
    create_executor: Callable | None
    legacy_resource_check: Callable | None = None
    supports_proxy: bool = False
    needs_preparation: Callable | None = None
    can_disable: bool = True
    reusable_executor: bool = False
    working_memory: Callable | None = None
    allow_resource_gaps_edit: bool = False
    minimum_threads: Callable | None = None
    implicit_resources: Callable | None = None
    resource_features: tuple[ResourceFeature, ...] = ()
    collect_resources: Callable | None = None
    sampling_behavior: str = "unsupported"
    prepare_input: Callable | None = None
    preparation_memory: Callable | None = None
    prepare_execution: Callable | None = None
    executor_memory: Callable | None = None

    def required_resources(self, validated_params) -> tuple[RequiredResource, ...]:
        return tuple(self.collect_resources(validated_params)) if self.collect_resources else ()

    def resource_references(self, validated_params):
        implicit = self.implicit_resources(validated_params) if self.implicit_resources else ()
        return tuple(sorted(set(self.descriptor.resources) | set(implicit)))


class ToolCatalog:
    def __init__(self, definitions=()):
        self._definitions = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition):
        descriptor = definition.descriptor
        reference = ToolReference.from_dict({
            "tool_id": descriptor.tool_id,
            "params_version": descriptor.params_version,
            "implementation_id": descriptor.implementation_id,
            "implementation_version": descriptor.implementation_version,
            "resources": [{"resource_id": key, "hash": digest} for key, digest in descriptor.resources],
            "settings": {},
        })
        if reference in self._definitions:
            raise ValueError("工具实现重复注册")
        if type(descriptor.api_version) is not int or descriptor.api_version != 1:
            raise ValueError("不支持的工具接口版本")
        image = descriptor.image
        if (image.colorspace != "linear_srgb" or image.dtype != "float32"
                or image.alpha not in ("preserve", "transform")
                or image.geometry not in ("preserve", "resize", "tool_defined")
                or image.intermediate_precision not in ("float32", "srgb8")):
            raise ValueError("不支持的图像契约")
        if definition.parameters.version != definition.descriptor.params_version:
            raise ValueError("工具参数版本不一致")
        if definition.sampling_behavior not in ("capture", "preserve", "transport", "unsupported"):
            raise ValueError("取样行为无效")
        self._definitions[reference] = definition

    def resolve(self, reference: ToolReference):
        definition = self._definitions.get(reference)
        if definition is not None:
            return CoreResult.ok(definition)
        code = (ErrorCode.TOOL_INCOMPATIBLE if any(item.tool_id == reference.tool_id
                for item in self._definitions) else ErrorCode.TOOL_MISSING)
        return CoreResult.fail(code, "error.tool.incompatible" if code == ErrorCode.TOOL_INCOMPATIBLE
                               else "error.tool.missing", details={"tool_id": reference.tool_id})

    def descriptors(self):
        return tuple(item.descriptor for item in self._definitions.values())

    def needs_preparation(self, tool_id, settings):
        return any(item.descriptor.tool_id == tool_id and item.needs_preparation is not None
                   and item.needs_preparation(settings) for item in self._definitions.values())

    def can_disable(self, tool_id):
        return all(item.can_disable for item in self._definitions.values()
                   if item.descriptor.tool_id == tool_id)

    def copy(self):
        return ToolCatalog(replace(item, parameters=copy.deepcopy(item.parameters))
                           for item in self._definitions.values())
