"""推理运行环境的启动激活。须在导入任何推理库之前调用，只依赖标准库。"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

PREFERENCES = "preferences.json"
MANIFEST = "manifest.json"
_TOKEN = re.compile(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Activation:
    token: str = ""
    path: str = ""
    reason: str = ""


def python_tag():
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def default_preferences():
    return {"schema_version": 1, "enabled": "", "pending_removal": [], "preference": "auto"}


def read_preferences(root):
    preferences = default_preferences()
    path = Path(root) / PREFERENCES
    if not path.is_file():
        return preferences
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["schema_version"] != 1:
            raise ValueError("运行环境配置版本无效")
        enabled, pending, preference = document["enabled"], document["pending_removal"], document["preference"]
        if (not isinstance(enabled, str) or enabled and not _TOKEN.fullmatch(enabled)
                or not isinstance(pending, list) or any(not isinstance(item, str) or not _TOKEN.fullmatch(item) for item in pending)
                or preference not in ("auto", "cpu")):
            raise ValueError("运行环境配置内容无效")
    except (OSError, ValueError, KeyError, TypeError):
        return {**preferences, "invalid": True}
    return {"schema_version": 1, "enabled": enabled, "pending_removal": list(dict.fromkeys(pending)), "preference": preference}


def write_preferences(root, preferences):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    payload = {key: preferences[key] for key in ("schema_version", "enabled", "pending_removal", "preference")}
    temporary = root / ("preferences-" + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, root / PREFERENCES)


def managed_directory(root, token):
    if not _TOKEN.fullmatch(token) or any(part in (".", "..") for part in token.split("@")):
        raise ValueError("runtime.error.path")
    runtime_id, version = token.split("@")
    return Path(root) / "managed" / runtime_id / version


def _is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def validate_managed(root, target):
    """目标须位于托管根目录内，路径和内容都不含重解析点。"""
    root, target = Path(root).absolute(), Path(target).absolute()
    base = (root / "managed").resolve()
    resolved = target.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise ValueError("runtime.error.path")
    for parent in (target, *target.parents):
        if _is_link(parent):
            raise ValueError("runtime.error.path")
        if parent == root:
            break
    if target.exists() and any(_is_link(path) for path in target.rglob("*")):
        raise ValueError("runtime.error.path")


def check_installation(directory):
    """轻量检查：清单、解释器标签和原生库文件的大小。返回空字符串表示可用。"""
    directory = Path(directory)
    try:
        manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
        if manifest["schema_version"] != 1:
            return "runtime.reason.manifest"
        if manifest["python"] != python_tag():
            return "runtime.reason.python"
        files = manifest["files"]
        package = manifest["package"]
        if not (directory / package / "__init__.py").is_file():
            return "runtime.reason.corrupt"
        for name, size in files.items():
            if name.endswith((".pyd", ".dll")) and (directory / name).stat().st_size != size:
                return "runtime.reason.corrupt"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "runtime.reason.corrupt"
    return ""


def activate(root, *, package="onnxruntime"):
    """完成待卸载，并把启用的运行环境置于模块搜索路径最前。"""
    root = Path(root)
    preferences = read_preferences(root)
    if preferences.get("invalid"):
        return Activation(reason="runtime.reason.preferences")
    changed = False
    for token in list(preferences["pending_removal"]):
        try:
            target = managed_directory(root, token)
            if target.exists():
                validate_managed(root, target)
                shutil.rmtree(target)
        except (OSError, ValueError):
            continue
        preferences["pending_removal"].remove(token)
        if preferences["enabled"] == token:
            preferences["enabled"] = ""
        changed = True
    if changed:
        try:
            write_preferences(root, preferences)
        except OSError:
            return Activation(reason="runtime.reason.preferences")
    token = preferences["enabled"]
    if not token:
        return Activation()
    if package in sys.modules:
        return Activation(reason="runtime.reason.loaded")
    try:
        directory = managed_directory(root, token)
        validate_managed(root, directory)
    except (OSError, ValueError):
        return Activation(reason="runtime.reason.corrupt")
    reason = check_installation(directory)
    if reason:
        return Activation(reason=reason)
    sys.path.insert(0, str(directory))
    return Activation(token, str(directory))
