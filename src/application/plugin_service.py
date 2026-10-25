from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from .result_types import CoreResult


def relative_file(root, name):
    path = Path(name)
    if path.is_absolute() or not path.parts or ".." in path.parts or ":" in name:
        raise ValueError("插件路径必须为根目录下的相对路径")
    candidate = root / path
    for parent in [candidate, *candidate.parents]:
        if parent == root.parent:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("插件路径不允许重解析点")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("插件路径越界")
    return resolved


class PluginService:
    def __init__(self, artifact_store, audit, *, trusted_hashes=()):
        self._store = artifact_store
        self._audit = audit
        self._trusted = frozenset(trusted_hashes)

    @staticmethod
    def _snapshot(directory):
        root = Path(directory).absolute()
        if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
            raise ValueError("插件目录不允许重解析点")
        root = root.resolve()
        manifest_bytes = relative_file(root, "plugin.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest.get("manifest_version") != "1.0" or manifest.get("runtime") != "subprocess_python":
            raise ValueError("不支持的插件协议")
        if not isinstance(manifest.get("plugin_id"), str) or not manifest["plugin_id"] or not isinstance(manifest.get("version"), str) or not manifest["version"]:
            raise ValueError("缺少插件标识或版本")
        permissions = manifest.get("permissions", {})
        if not isinstance(permissions, dict) or type(permissions.get("network", False)) is not bool:
            raise ValueError("插件网络权限声明无效")
        nodes = manifest.get("declared_nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("插件必须声明节点")
        node_ids = set()
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("node_id"), str) or not node["node_id"]:
                raise ValueError("插件节点标识无效")
            if node["node_id"] in node_ids or type(node.get("is_deterministic")) is not bool:
                raise ValueError("插件节点重复或确定性声明无效")
            node_ids.add(node["node_id"])
        entry = relative_file(root, manifest["entrypoint"])
        contents = {"plugin.json": manifest_bytes, manifest["entrypoint"]: entry.read_bytes()}
        files = {name: hashlib.sha256(payload).hexdigest() for name, payload in contents.items()}
        for name, expected in manifest.get("resources", {}).items():
            payload = relative_file(root, name).read_bytes()
            actual = hashlib.sha256(payload).hexdigest()
            if actual != expected:
                raise ValueError("资源哈希不匹配")
            files[name] = actual
            contents[name] = payload
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        return manifest, contents, digest

    @staticmethod
    def inspect(directory):
        manifest, contents, digest = PluginService._snapshot(directory)
        return manifest, Path(directory).resolve() / manifest["entrypoint"], digest

    def run(self, directory, node_id, inputs, params, *, seed=None, timeout=30,
            is_cancelled=lambda: False):
        request_id = os.urandom(16).hex()
        process = None
        try:
            self._audit.append("plugin.request", request_id=request_id, node_id=node_id, params=params,
                               inputs=[item.to_dict() for item in inputs], seed=seed,
                               runtime=sys.version, executable_hash=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest())
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            return CoreResult.from_exception(exc, code="AUDIT_WRITE_FAILED", message_key="error.audit.failed")
        result = CoreResult.fail("PLUGIN_CRASH", "error.plugin.failed")
        try:
            manifest, contents, resource_hash = self._snapshot(directory)
            self._audit.append("plugin.identity", request_id=request_id, resource_hash=resource_hash,
                               plugin_id=manifest["plugin_id"], version=manifest["version"])
            if resource_hash not in self._trusted:
                result = CoreResult.fail("PERMISSION_DENIED", "error.plugin.untrusted")
                return result
            if manifest.get("permissions", {}).get("network", False):
                result = CoreResult.fail("PERMISSION_DENIED", "error.plugin.network_disabled")
                return result
            node = next((item for item in manifest["declared_nodes"] if item["node_id"] == node_id), None)
            if node is None or node.get("contract_version") != "3.4.0":
                raise ValueError("插件节点契约不兼容")
            if not node.get("is_deterministic", False) and type(seed) is not int:
                raise ValueError("非确定性节点需要整数种子")
            timeout = min(float(timeout), float(manifest.get("resource_limits", {}).get("timeout_sec", 30)))
            if not 0 < timeout <= 300:
                raise ValueError("超时范围无效")
            if is_cancelled():
                result = CoreResult.fail("CANCELLED", "error.cancelled")
                return result
            from .storage_workspaces import ManagedWorkspace
            workspace = ManagedWorkspace("plugins")
            estimated = sum(len(payload) for payload in contents.values()) + sum(Path(item.path).stat().st_size for item in inputs) + 66 * 1024 ** 2
            with workspace as folder, workspace.reserve(estimated):
                work = Path(folder)
                bundle = work / "bundle"
                bundle.mkdir()
                for name, payload in contents.items():
                    target = relative_file(bundle, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                entry = relative_file(bundle, manifest["entrypoint"])
                refs = []
                for index, artifact in enumerate(inputs):
                    payload = self._store.read(artifact)
                    extension = {"image/png": ".png", "image/tiff": ".tiff"}[artifact.media_type]
                    target = work / f"input-{index}{extension}"
                    target.write_bytes(payload)
                    refs.append(replace(artifact, path=str(target)).to_dict())
                request = {"protocol": "1.0", "node_id": node_id, "inputs": refs,
                           "params": params, "seed": seed, "output_path": str(work / "output.png")}
                with (work / "request.json").open("w+", encoding="utf-8") as stdin, \
                        (work / "stdout.json").open("w+b") as stdout, \
                        (work / "stderr.txt").open("w+b") as stderr:
                    json.dump(request, stdin, allow_nan=False)
                    stdin.flush()
                    stdin.seek(0)
                    command = ([sys.executable, "--ops-plugin-runner", str(entry)]
                               if getattr(sys, "frozen", False)
                               else [sys.executable, "-I", str(entry)])
                    process = subprocess.Popen(command, cwd=work,
                                               stdin=stdin, stdout=stdout, stderr=stderr,
                                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    try:
                        deadline = time.monotonic() + timeout
                        while process.poll() is None:
                            cancelled = is_cancelled()
                            if cancelled or time.monotonic() >= deadline:
                                code = "CANCELLED" if cancelled else "TIMEOUT"
                                result = CoreResult.fail(code, "error.plugin." + code.lower())
                                return result
                            if any((work / name).stat().st_size > 1048576 for name in ("stdout.json", "stderr.txt")):
                                raise ValueError("插件日志过大")
                            time.sleep(0.02)
                        if process.returncode:
                            raise RuntimeError("插件进程异常退出")
                        if any((work / name).stat().st_size > 1048576 for name in ("stdout.json", "stderr.txt")):
                            raise ValueError("插件日志过大")
                        stdout.seek(0)
                        response = json.loads(stdout.read(1048577))
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait()
                if response.get("protocol") != "1.0" or response.get("status") != "ok":
                    raise ValueError("插件响应协议无效")
                output = work / "output.png"
                if output.is_symlink() or not output.is_file() or output.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError("插件产物无效或过大")
                from PIL import Image
                from io import BytesIO
                output_bytes = output.read_bytes()
                with Image.open(BytesIO(output_bytes)) as image:
                    if image.format != "PNG":
                        raise ValueError("插件产物必须为 PNG")
                    image.verify()
                if is_cancelled():
                    result = CoreResult.fail("CANCELLED", "error.cancelled")
                    return result
                artifact = self._store.put(output_bytes, producer_node=node_id)
                artifact = replace(artifact, external_resource_hash=resource_hash,
                                   engine_version=manifest["version"])
                result = CoreResult.ok(artifact)
                return result
        except Exception as exc:
            result = CoreResult.from_exception(exc, code="PLUGIN_CRASH", message_key=str(exc) if str(exc).startswith("storage.") else "error.plugin.failed")
            return result
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            try:
                self._audit.append("plugin.result", request_id=request_id, success=result.success,
                                   error=None if result.success else result.error.code,
                                   artifact=None if not result.success else result.data.to_dict())
            except (OSError, ValueError, TypeError) as exc:
                return CoreResult.from_exception(exc, code="AUDIT_WRITE_FAILED", message_key="error.audit.failed")
