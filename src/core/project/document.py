from __future__ import annotations

import copy
import math
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..contracts import WorkflowGraph
from ..contracts.artifact import LINEAR_ARTIFACT


def resolve_source_path(project_path, source_path):
    if os.path.isabs(source_path):
        return source_path
    return os.path.join(os.path.dirname(os.path.abspath(project_path)), source_path)


@dataclass
class ProjectDocument:
    source: dict[str, Any]
    current_revision: str
    revisions: list[dict[str, Any]] = field(default_factory=list)
    schema_version: int = 1
    workflows: dict[str, dict] = field(default_factory=dict)
    draft: dict = field(default_factory=dict)
    redo_targets: dict[str, str] = field(default_factory=dict)
    document_id: str = ""
    assets: dict[str, dict] = field(default_factory=dict)

    def to_dict(self):
        result = asdict(self)
        if self.schema_version < 3:
            result.pop("assets")
        return result

    def validate(self):
        from ..workflow.validator import validate_structure

        if type(self.schema_version) is not int or self.schema_version not in (1, 2, 3):
            raise ValueError("不支持的项目版本")
        if not isinstance(self.source, dict) or not isinstance(self.source.get("path"), str):
            raise ValueError("源文件引用无效")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source.get("file_hash", "")):
            raise ValueError("源文件哈希无效")
        if not isinstance(self.document_id, str) or not isinstance(self.current_revision, str):
            raise ValueError("项目标识无效")
        if not isinstance(self.revisions, list) or not self.revisions:
            raise ValueError("项目历史为空")
        parents = {}
        for revision in self.revisions:
            rid = revision["revision_id"]
            if not isinstance(rid, str) or not rid or rid in parents:
                raise ValueError("版本标识无效或重复")
            parents[rid] = revision["parent_id"]
            if not isinstance(revision.get("label", ""), str) or not math.isfinite(revision["created_at"]):
                raise ValueError("版本元数据无效")
            if not isinstance(revision["artifact_refs"], list):
                raise ValueError("产物引用无效")
            for artifact in revision["artifact_refs"]:
                if artifact.get("kind") == LINEAR_ARTIFACT and (
                    self.schema_version < 2 or artifact.get("media_type") != "image/tiff"
                    or artifact.get("color_context") != "linear_srgb"
                ):
                    raise ValueError("浮点产物需要项目版本 2 和线性 sRGB 契约")
                if not re.fullmatch(r"[0-9a-f]{64}", artifact["hash"]):
                    raise ValueError("产物哈希无效")
                if artifact["media_type"] not in ("image/png", "image/tiff"):
                    raise ValueError("不支持的产物格式")
                if not isinstance(artifact["path"], str) or not artifact["path"]:
                    raise ValueError("产物路径无效")
        if self.current_revision not in parents or set(self.workflows) != set(parents):
            raise ValueError("项目版本或工作流引用缺失")
        if sum(parent is None for parent in parents.values()) != 1:
            raise ValueError("项目必须有唯一根版本")
        complete = set()
        for rid in parents:
            visiting = set()
            cursor = rid
            while cursor is not None and cursor not in complete:
                if cursor not in parents or cursor in visiting:
                    raise ValueError("版本历史缺失或存在循环")
                visiting.add(cursor)
                cursor = parents[cursor]
            complete.update(visiting)
        if not isinstance(self.redo_targets, dict):
            raise ValueError("重做映射无效")
        for parent, child in self.redo_targets.items():
            if parent not in parents or child not in parents or parents[child] != parent:
                raise ValueError("重做目标不是子版本")
        for revision in self.revisions:
            workflow = WorkflowGraph.from_dict(self.workflows[revision["revision_id"]])
            if not validate_structure(workflow).success or workflow.workflow_hash() != revision["workflow_hash"]:
                raise ValueError("工作流快照无效")
        if not validate_structure(WorkflowGraph.from_dict(self.draft)).success:
            raise ValueError("草稿工作流无效")
        if not isinstance(self.assets, dict) or self.assets and self.schema_version < 3:
            raise ValueError("项目资产清单无效")
        for digest, asset in self.assets.items():
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or not isinstance(asset, dict) or set(asset) != {"type", "size", "path"}
                    or asset["type"] != "mask-gray16" or not isinstance(asset["path"], str)
                    or not asset["path"] or not isinstance(asset["size"], list) or len(asset["size"]) != 2
                    or any(type(n) is not int or not 1 <= n <= 1600 for n in asset["size"])):
                raise ValueError("项目资产清单无效")
        from ..mask_components import asset_references
        for graph in (*self.workflows.values(), self.draft):
            for node in WorkflowGraph.from_dict(graph).nodes:
                settings = node.params.get("settings", {}) if node.node_type == "tool" else node.params
                if node.node_type == "color_adjust" or node.node_type == "tool" and node.params.get("tool_id") == "color_adjust":
                    for digest, expected in asset_references(settings).items():
                        actual = self.assets.get(digest)
                        if actual is None or any(actual[key] != value for key, value in expected.items()):
                            from .assets import ProjectAssetError
                            raise ProjectAssetError(digest)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("项目必须为 JSON 对象")
        document = cls(**copy.deepcopy(data))
        document.validate()
        return document
