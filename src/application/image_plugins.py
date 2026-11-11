from concurrent.futures import CancelledError
from dataclasses import dataclass, replace
from contextlib import ExitStack, contextmanager
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from threading import Lock
from time import monotonic, sleep
import uuid
from typing import Callable

import numpy as np

from ..core.contracts.image_plugins import IMAGE_PLUGIN_RESOURCE
from ..core.contracts.result import CoreOperationError, CoreResult, ErrorCode
from ..core.contracts.tools import ImageCapabilities, ToolDescriptor
from ..core.tools.catalog import ParameterContract, ParameterField, ToolDefinition
from .audit import AuditLog
from .plugin_service import PluginService, relative_file
from .storage_workspaces import ManagedWorkspace


@dataclass(frozen=True)
class ImagePluginAuxiliary:
    kind: str
    capability: str
    version: int
    identity: tuple
    reference_size: tuple
    data: object


@dataclass(frozen=True)
class AuxiliaryResolver:
    identity: tuple
    borrow: Callable
    resources: tuple = ()


class ImagePluginPackage:
    def __init__(self, directory, trusted_hash, auxiliaries=None):
        manifest, contents, digest = PluginService._snapshot(directory)
        if digest != trusted_hash:
            raise PermissionError("error.plugin.untrusted")
        if manifest.get("permissions", {}).get("network", False):
            raise PermissionError("error.plugin.network_disabled")
        if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", manifest["plugin_id"]):
            raise ValueError("插件标识需要发布者范围")
        self.manifest, self.digest = manifest, digest
        self.auxiliaries = dict(auxiliaries or {})
        self._lock = Lock()
        self._active = 0
        self._closed = False
        self._files = tuple(contents)
        self.workspace = ManagedWorkspace("plugins")
        self.root = Path(self.workspace.name) / "bundle"
        self.root.mkdir()
        try:
            with self.workspace.reserve(sum(map(len, contents.values()))+4096):
                for name, payload in contents.items():
                    target = relative_file(self.root, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                files = {name: hashlib.sha256(payload).hexdigest() for name, payload in contents.items()}
                self.resource_path = Path(self.workspace.name) / "bundle.json"
                self.resource_path.write_bytes(json.dumps(files, sort_keys=True).encode("utf-8"))
            self.audit = AuditLog(str(Path(self.workspace.name)/"audit.jsonl"), workspace=self.workspace)
            self._stamps = self._bundle_stamps()
            self.definitions = tuple(self._definition(node) for node in manifest["declared_nodes"])
        except BaseException:
            self.close()
            raise

    def _definition(self, node):
        modern = self.manifest["manifest_version"] == "2.0"
        timeout = node.get("timeout_sec", 30)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 300:
            raise ValueError("插件超时声明无效")
        if node.get("contract_version") != ("image/1" if modern else "3.4.0"):
            raise ValueError("插件图像接口版本不兼容")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", node["node_id"]):
            raise ValueError("插件节点标识无效")
        types = {"integer": int, "number": (int, float), "boolean": bool, "string": str, "array": list}
        fields = tuple(ParameterField(field["name"], types[field["type"]], field["default"],
            field.get("minimum"), field.get("maximum"), tuple(field.get("choices", ())))
            for field in node.get("parameters", ()))
        parameters = ParameterContract(fields, version=node.get("params_version", 1),
            allow_unknown=not modern and "parameters" not in node)
        if not parameters.normalize(parameters.defaults(parameters.version), parameters.version).success:
            raise ValueError("插件默认参数无效")
        policy = node.get("cache", "deterministic" if node["is_deterministic"] else "never")
        if policy not in ("deterministic", "seeded", "never") or policy == "deterministic" and not node["is_deterministic"]:
            raise ValueError("插件复用声明无效")
        seed = node.get("seed_parameter", "seed")
        if policy == "seeded" and not any(field.name == seed and field.kind is int for field in fields):
            raise ValueError("种子插件必须声明整数种子参数")
        if not isinstance(node.get("external_state", ""), str):
            raise ValueError("插件外部状态身份无效")
        if type(node.get("supports_proxy", False)) is not bool:
            raise ValueError("插件代理声明无效")
        image = ImageCapabilities(**({} if modern else {"intermediate_precision": "srgb8"}) | node.get("image", {}))
        if image.intermediate_precision != ("float32" if modern else "srgb8"):
            raise ValueError("插件图像精度与传输不匹配")
        auxiliary = node.get("auxiliary", [])
        if (not isinstance(auxiliary, list) or not modern and auxiliary
                or len({item["name"] for item in auxiliary}) != len(auxiliary)):
            raise ValueError("插件辅助产物声明无效")
        for item in auxiliary:
            if (not re.fullmatch(r"[a-z][a-z0-9_]*", item["name"])
                    or item["kind"] not in ("mask-f32", "faces-2d") or type(item["version"]) is not int
                    or item["version"] < 1 or item["capability"] not in self.auxiliaries):
                raise ValueError("插件辅助产物接口尚未注册")
        resources = [(IMAGE_PLUGIN_RESOURCE, self.digest)]
        for item in auxiliary:
            for role, digest, _ in self.auxiliaries[item["capability"]].resources:
                resources.append(("aux:"+item["name"]+":"+role, digest))
        output = node.get("output_size")
        if image.geometry != "preserve" and (not isinstance(output, list) or len(output) != 2
                or any(type(value) is not int or value < 1 for value in output)):
            raise ValueError("变换插件需要声明输出尺寸")
        output_size = lambda params, size: size if image.geometry == "preserve" else tuple(output)
        descriptor = ToolDescriptor(self.manifest["plugin_id"]+"."+node["node_id"], self.manifest["version"],
            1, parameters.version, self.manifest["plugin_id"]+"."+node["node_id"]+".image", self.manifest["version"],
            image, tuple(sorted(resources)))
        return ToolDefinition(descriptor, parameters, lambda resources: ImagePluginExecutor(self, node, output_size),
            execution_engines=("cpu",),
            supports_proxy=bool(node.get("supports_proxy", False)) and image.geometry == "preserve" and policy != "never",
            sampling_behavior="preserve" if image.geometry == "preserve" else "unsupported",
            working_memory=lambda source, params: source.pixels.nbytes*4+math.prod(output_size(params, source.pixels.shape[1::-1]))*64,
            cacheable=lambda params: policy != "never", output_size=output_size,
            prepare_execution=(lambda source, params, cancelled: self.prepare_auxiliary(node, source, params, cancelled)) if auxiliary else None,
            execution_dependencies=lambda params: self.dependencies(node))

    def dependencies(self, node):
        with self.borrow():
            return (("plugin_api", 1), ("plugin_state", node.get("external_state", "")),
                *(("plugin_auxiliary", item["name"], item["kind"], item["capability"], item["version"],
                    self.auxiliaries[item["capability"]].identity) for item in node.get("auxiliary", ())))

    @contextmanager
    def prepare_auxiliary(self, node, source, params, cancelled):
        with ExitStack() as leases:
            values = {}
            for item in node["auxiliary"]:
                resolver = self.auxiliaries[item["capability"]]
                value = leases.enter_context(resolver.borrow(source, params, cancelled))
                if (not isinstance(value, ImagePluginAuxiliary)
                        or (value.kind, value.capability, value.version) != (item["kind"], item["capability"], item["version"])
                        or value.reference_size != source.orig_size or not value.identity):
                    raise ValueError("插件辅助产物身份无效")
                data = value.data
                if (not isinstance(data, np.ndarray) or data.dtype != np.float32 or data.flags.writeable
                        or not np.isfinite(data).all() or item["kind"] == "mask-f32"
                        and (data.shape != source.pixels.shape[:2] or np.any(data < 0) or np.any(data > 1))
                        or item["kind"] == "faces-2d" and (data.ndim != 3 or data.shape[-1] != 2)):
                    raise ValueError("插件辅助产物缓冲无效")
                values[item["name"]] = value
            yield values

    def _bundle_stamps(self):
        stamps = []
        for name in self._files:
            stat = relative_file(self.root, name).stat()
            stamps.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        return tuple(stamps)

    @contextmanager
    def borrow(self):
        with self._lock:
            if self._closed:
                raise CancelledError()
            self._active += 1
        try:
            stamps = self._bundle_stamps()
            if stamps != self._stamps:
                if PluginService.inspect(self.root)[2] != self.digest:
                    raise CoreOperationError(CoreResult.fail(
                        ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing").error)
                self._stamps = stamps
            yield
        finally:
            with self._lock:
                self._active -= 1
                dispose = self._closed and not self._active
            if dispose:
                self.workspace.cleanup()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            dispose = not self._active
        if dispose:
            self.workspace.cleanup()


class ImagePluginExecutor:
    analyze = None

    def __init__(self, package, node, output_size):
        self.package, self.node, self.output_size = package, node, output_size
        self.timeout = float(node.get("timeout_sec", 30))

    def execute(self, source, params, context):
        cancelled = context["is_cancelled"]
        try:
            with self.package.borrow():
                if cancelled():
                    raise CancelledError()
                if self.package.manifest["manifest_version"] == "1.0":
                    return self._legacy(source, params, context)
                return CoreResult.ok(self._linear(source, params, context))
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except CoreOperationError as error:
            return CoreResult.from_exception(error)
        except Exception as error:
            return CoreResult.from_exception(error, message_key="error.plugin.failed")

    def _legacy(self, source, params, context):
        from ..core.artifact_store import ArtifactStore
        from ..core.photo_engine import _pil_from, _src_from_pil
        from PIL import Image
        workspace = ManagedWorkspace("plugins")
        with workspace as directory, workspace.reserve(source.pixels.nbytes*8):
            store = ArtifactStore(directory)
            service = PluginService(store, self.package.audit, trusted_hashes=(self.package.digest,))
            with _pil_from(source) as image, BytesIO() as stream:
                image.save(stream, format="PNG")
                artifact = store.put(stream.getvalue(), producer_node=context["node_id"])
            result = service.run(self.package.root, self.node["node_id"], [artifact], params,
                seed=params.get(self.node.get("seed_parameter", "seed")), timeout=self.timeout,
                is_cancelled=context["is_cancelled"])
            if not result.success:
                return result
            with Image.open(BytesIO(store.read(result.data))) as image:
                output = _src_from_pil(image)
            return CoreResult.ok(replace(source, pixels=output.pixels, alpha=output.alpha,
                orig_size=source.orig_size if self.node.get("image", {}).get("geometry", "preserve") == "preserve" else output.orig_size))

    @staticmethod
    def _load_array(path, shape, *, alpha=False):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > math.prod(shape)*4+4096:
            raise ValueError("插件缓冲大小无效")
        value = np.load(path, mmap_mode="r", allow_pickle=False)
        if not isinstance(value, np.memmap):
            if hasattr(value, "close"):
                value.close()
            raise ValueError("插件缓冲必须为 NPY 数组")
        try:
            if value.dtype != np.float32 or value.shape != shape:
                raise ValueError("插件缓冲规格无效")
            if not np.isfinite(value).all() or alpha and (np.any(value < 0) or np.any(value > 1)):
                raise ValueError("插件缓冲数值无效")
            return np.array(value, copy=True)
        finally:
            value._mmap.close()

    def _linear(self, source, params, context):
        cancelled = context["is_cancelled"]
        width, height = self.output_size(params, source.pixels.shape[1::-1])
        request_id = uuid.uuid4().hex
        workspace = ManagedWorkspace("plugins")
        result = None
        self.package.audit.append("plugin.request", request_id=request_id, protocol="image/1",
            package_hash=self.package.digest, node_id=self.node["node_id"], params=params)
        try:
            auxiliary_values = context.get("execution_input") or {}
            with workspace as directory, workspace.reserve(source.pixels.nbytes*2+width*height*16
                    +sum(value.data.nbytes for value in auxiliary_values.values())+3*1048576):
                work = Path(directory)
                np.save(work/"input.npy", source.pixels, allow_pickle=False)
                if source.alpha is not None:
                    np.save(work/"alpha.npy", source.alpha, allow_pickle=False)
                request = {"protocol": "image/1", "api_version": 1, "node_id": self.node["node_id"], "params": params,
                    "input": {"pixels": str(work/"input.npy"), "alpha": str(work/"alpha.npy") if source.alpha is not None else None,
                        "size": list(source.pixels.shape[1::-1]), "reference_size": list(source.orig_size),
                        "proxy": source.is_proxy, "colorspace": source.colorspace, "white_point": source.white_point,
                        "gamut": source.gamut, "alpha_mode": "straight", "dtype": "float32"},
                    "output": {"pixels": str(work/"output.npy"), "alpha": str(work/"output-alpha.npy"), "size": [width, height]}}
                request["auxiliary"] = {}
                for name, value in auxiliary_values.items():
                    path = work/("aux-"+name+".npy")
                    np.save(path, value.data, allow_pickle=False)
                    request["auxiliary"][name] = {"path": str(path), "kind": value.kind,
                        "capability": value.capability, "version": value.version, "identity": value.identity,
                        "reference_size": value.reference_size}
                entry = relative_file(self.package.root, self.package.manifest["entrypoint"])
                command = ([sys.executable, "--ops-plugin-runner", str(entry)] if getattr(sys, "frozen", False)
                           else [sys.executable, "-I", str(entry)])
                with (work/"request.json").open("w+", encoding="utf-8") as stdin, \
                        (work/"stdout.json").open("w+b") as stdout, (work/"stderr.txt").open("w+b") as stderr:
                    json.dump(request, stdin, allow_nan=False)
                    stdin.flush()
                    stdin.seek(0)
                    process = subprocess.Popen(command, cwd=work, stdin=stdin, stdout=stdout, stderr=stderr,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    started = monotonic()
                    try:
                        while process.poll() is None:
                            if cancelled():
                                raise CancelledError()
                            if monotonic()-started > self.timeout:
                                raise TimeoutError("插件执行超时")
                            if any((work/name).stat().st_size > 1048576 for name in ("stdout.json", "stderr.txt")):
                                raise ValueError("插件日志过大")
                            sleep(.05)
                        if process.returncode:
                            raise RuntimeError("插件进程异常退出")
                        stdout.seek(0)
                        response = json.loads(stdout.read(1048577))
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait()
                if cancelled():
                    raise CancelledError()
                if (response.get("protocol") != "image/1" or response.get("status") != "ok"
                        or type(response.get("alpha", False)) is not bool):
                    raise ValueError("插件响应协议无效")
                pixels = self._load_array(work/"output.npy", (height, width, 3))
                alpha = self._load_array(work/"output-alpha.npy", (height, width), alpha=True) if response.get("alpha", False) else None
                result = replace(source, pixels=pixels, alpha=alpha,
                    orig_size=source.orig_size if (width, height) == source.pixels.shape[1::-1] else (width, height),
                    patches=(), patch_base_identity=(), execution_ownership=None)
                return result
        finally:
            self.package.audit.append("plugin.result", request_id=request_id, success=result is not None)

    def close(self):
        pass
