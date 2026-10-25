from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageCapabilities:
    colorspace: str = "linear_srgb"
    dtype: str = "float32"
    alpha: str = "preserve"
    geometry: str = "preserve"
    intermediate_precision: str = "float32"


@dataclass(frozen=True)
class ToolReference:
    tool_id: str
    params_version: int
    implementation_id: str
    implementation_version: str
    resources: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_dict(cls, data):
        fields = {"tool_id", "params_version", "implementation_id", "implementation_version", "resources", "settings"}
        if not isinstance(data, dict) or set(data) != fields:
            raise ValueError("工具引用字段无效")
        if any(not isinstance(data[key], str) or not data[key] for key in
               ("tool_id", "implementation_id", "implementation_version")):
            raise ValueError("工具身份无效")
        if type(data["params_version"]) is not int or data["params_version"] < 1:
            raise ValueError("参数版本无效")
        if not isinstance(data["settings"], dict) or not isinstance(data["resources"], list):
            raise ValueError("工具参数或资源无效")
        resources = []
        for resource in data["resources"]:
            if (not isinstance(resource, dict) or set(resource) != {"resource_id", "hash"}
                    or not isinstance(resource["resource_id"], str) or not resource["resource_id"]
                    or not isinstance(resource["hash"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", resource["hash"])):
                raise ValueError("资源引用无效")
            resources.append((resource["resource_id"], resource["hash"]))
        if len({key for key, _ in resources}) != len(resources) or resources != sorted(resources):
            raise ValueError("资源身份重复或顺序无效")
        return cls(data["tool_id"], data["params_version"], data["implementation_id"],
                   data["implementation_version"], tuple(resources))


@dataclass(frozen=True)
class ToolDescriptor:
    tool_id: str
    module_version: str
    api_version: int
    params_version: int
    implementation_id: str
    implementation_version: str
    image: ImageCapabilities = ImageCapabilities()
    resources: tuple[tuple[str, str], ...] = ()

    @property
    def reference(self):
        return ToolReference(self.tool_id, self.params_version, self.implementation_id,
                             self.implementation_version, self.resources)
