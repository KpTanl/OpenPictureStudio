from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import uuid
import zipfile
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path, PurePosixPath
from threading import Condition, Event

from ..core.contracts.inference import (CPU, DIRECTML, Advice, RuntimeDescriptor, RuntimeFile, RuntimeSnapshot)
from ..core.contracts.runtime_libraries import RuntimeLibrarySnapshot
from .model_download import check_cancelled, check_space, download_file, verify_file
from .runtime_bootstrap import (MANIFEST, Activation, check_installation, managed_directory, python_tag,
                                read_preferences, validate_managed, write_preferences)
from .runtime_libraries import environment_library
from .listeners import ListenerRegistry

logger = logging.getLogger(__name__)

# 显存分档来自单机实测，属于初始值；标称 4 GB、2 GB 的显卡实际上报值略低。
_RECOMMENDED_VIDEO_MEMORY = int(3.5 * 1024 ** 3)
_MINIMUM_VIDEO_MEMORY = int(1.5 * 1024 ** 3)
_LOW_SYSTEM_MEMORY = int(7.5 * 1024 ** 3)


def runtime_advice(profile, snapshots, status, *, supported=True):
    """由本机配置、运行环境状态和实际后端推导建议，至多两条。"""
    advice = []
    gpu = next((item for item in snapshots if item.descriptor.backend == DIRECTML), None)
    if gpu is not None and gpu.state == "removing":
        advice.append(Advice("runtime.advice.restart_remove", "restart"))
    elif gpu is not None and gpu.state == "installed" and gpu.enabled != gpu.active:
        advice.append(Advice("runtime.advice.restart_enable" if gpu.enabled else "runtime.advice.restart_disable", "restart"))
    elif status.backend == CPU and status.reason:
        advice.append(Advice(status.reason))
    elif status.backend != DIRECTML and status.preference != CPU and (gpu is None or gpu.state != "installed"):
        if profile is None:
            pass
        elif profile.error:
            advice.append(Advice("runtime.advice.unknown"))
        else:
            usable = [item for item in profile.adapters if item.directx12]
            best = max(usable, key=lambda item: item.dedicated_bytes, default=None)
            params = (("name", best.name), ("memory", f"{best.dedicated_bytes / 1024 ** 3:.1f}")) if best else ()
            action = "install" if supported and gpu is not None else ""
            if best is None:
                advice.append(Advice("runtime.advice.no_gpu"))
            elif not supported:
                advice.append(Advice("runtime.advice.python", "", params))
            elif best.dedicated_bytes >= _RECOMMENDED_VIDEO_MEMORY:
                advice.append(Advice("runtime.advice.install", action, params))
            elif best.dedicated_bytes >= _MINIMUM_VIDEO_MEMORY:
                advice.append(Advice("runtime.advice.install_small", action, params))
            else:
                advice.append(Advice("runtime.advice.integrated", action, params))
    if profile is not None and not profile.error and 0 < profile.memory_bytes < _LOW_SYSTEM_MEMORY:
        advice.append(Advice("runtime.advice.low_memory"))
    return tuple(advice[:2])


class RuntimeService:
    """推理运行环境目录、安装状态、启用偏好与激活状态的唯一所有者。"""

    def __init__(self, root, catalog_path, *, activation=None, inference=None, profile_provider=None):
        self.root = Path(root).absolute()
        self._activation = activation or Activation()
        self._inference = inference
        self._profile_provider = profile_provider
        self._condition = Condition()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-runtime")
        self._listeners = ListenerRegistry("运行环境状态订阅失败")
        self._tasks = {}
        self._closed = False
        self._profile = None
        self._profiling = False
        self.catalog_error = ""
        try:
            self._catalog = self._read_catalog(Path(catalog_path))
        except (OSError, ValueError, KeyError, TypeError):
            logger.exception("运行环境目录读取失败：%s", catalog_path)
            self._catalog = ()
            self.catalog_error = "runtime.error.catalog"
        self._preferences = read_preferences(self.root)
        if self._inference is not None:
            self._inference.set_preference(self._preferences["preference"])
        self._states = {item.token: self._discover(item) for item in self._catalog}

    @staticmethod
    def _read_catalog(path):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["schema_version"] != 1:
            raise ValueError("不支持的运行环境目录版本")
        runtimes = []
        for item in document["runtimes"]:
            files = tuple(RuntimeFile(**file) for file in item["files"])
            for file in files:
                if (Path(file.name).name != file.name or not file.name.endswith(".whl")
                        or not re.fullmatch(r"[a-f0-9]{64}", file.sha256) or not re.fullmatch(r"cp\d+", file.python)
                        or not file.url.startswith("https://") or type(file.size) is not int or file.size < 1):
                    raise ValueError("无效运行环境文件合同")
            descriptor = RuntimeDescriptor(item["id"], item["version"], item["name"], item["backend"], item["source"],
                                           item["license"], item["package"], item["installed_size"], files)
            managed_directory(path.parent, descriptor.token)
            if (not re.fullmatch(r"[A-Za-z0-9_]+", descriptor.package) or descriptor.backend not in (DIRECTML,)
                    or type(descriptor.installed_size) is not int or descriptor.installed_size < 1):
                raise ValueError("无效运行环境合同")
            runtimes.append(descriptor)
        if len({item.token for item in runtimes}) != len(runtimes):
            raise ValueError("重复运行环境版本")
        return tuple(runtimes)

    def _discover(self, item):
        directory = managed_directory(self.root, item.token)
        enabled = self._preferences["enabled"] == item.token
        active = self._activation.token == item.token
        if item.token in self._preferences["pending_removal"]:
            return RuntimeSnapshot(item, "removing", path=str(directory), active=active)
        if not directory.exists():
            return RuntimeSnapshot(item, "missing", enabled=enabled)
        reason = check_installation(directory)
        if reason:
            return RuntimeSnapshot(item, "corrupt", path=str(directory), enabled=enabled, error=reason)
        try:
            manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
            installed_bytes = sum(manifest["files"].values())
        except (OSError, ValueError, KeyError, TypeError):
            return RuntimeSnapshot(item, "corrupt", path=str(directory), enabled=enabled, error="runtime.reason.corrupt")
        return RuntimeSnapshot(item, "installed", installed_bytes=installed_bytes, path=str(directory),
                               active=active, enabled=enabled)

    def subscribe(self, listener):
        """返回取消订阅的函数；它返回后该订阅者不再被回调。"""
        return self._listeners.subscribe(listener)

    def _notify(self, token=None):
        self._listeners.notify(token)

    def _update(self, token, **changes):
        with self._condition:
            self._states[token] = replace(self._states[token], **changes)
        self._notify(token)

    def list(self):
        with self._condition:
            return tuple(self._states.values())

    def supported(self, token):
        with self._condition:
            return self._states[token].descriptor.file_for(python_tag()) is not None

    @property
    def activation(self):
        return self._activation

    def status(self):
        return self._inference.status() if self._inference is not None else None

    def library(self, name):
        environment = environment_library(name)
        with self._condition:
            candidates = tuple(item for item in self._states.values() if item.descriptor.package == name)
        active = next((item for item in candidates if item.active), None)
        if active is not None:
            version = getattr(sys.modules.get(name), "__version__", "") or active.descriptor.version
            return replace(environment, source="managed", token=active.descriptor.token,
                           version=str(version),
                           installed_bytes=active.installed_bytes, error=environment.error or active.error)
        if environment.available:
            return environment
        selected = next((item for item in candidates if item.enabled), None)
        if selected is None:
            selected = next((item for item in candidates if item.state in ("installed", "corrupt")), None)
        if selected is None:
            return environment
        state = "restart" if selected.state == "installed" and selected.enabled else (
            "disabled" if selected.state == "installed" else "corrupt")
        return RuntimeLibrarySnapshot(name, state, "managed", selected.descriptor.token,
                                      selected.descriptor.version, selected.installed_bytes, selected.error)

    def libraries(self, names):
        return tuple(self.library(name) for name in dict.fromkeys(names))

    @property
    def restart_required(self):
        with self._condition:
            return any(item.state == "removing" and item.active or item.state == "installed" and item.enabled != item.active
                       for item in self._states.values())

    # ---- 偏好 ----
    def preference(self):
        with self._condition:
            return self._preferences["preference"]

    def set_preference(self, preference):
        if preference not in ("auto", CPU):
            raise ValueError("推理偏好无效")
        with self._condition:
            previous = self._preferences["preference"]
            self._preferences["preference"] = preference
            if not self._save_preferences():
                self._preferences["preference"] = previous
                return False
        if self._inference is not None:
            self._inference.set_preference(preference)
        self._notify()
        return True

    def set_enabled(self, token, enabled):
        with self._condition:
            current = self._states[token]
            if current.state != "installed" or token in self._tasks:
                return False
            previous = self._preferences["enabled"]
            self._preferences["enabled"] = token if enabled else ""
            if not self._save_preferences():
                self._preferences["enabled"] = previous
                return False
            for key, item in self._states.items():
                self._states[key] = replace(item, enabled=enabled and key == token, error="")
        self._notify(token)
        return True

    def _save_preferences(self):
        try:
            write_preferences(self.root, self._preferences)
            return True
        except OSError:
            logger.exception("运行环境配置保存失败")
            return False

    # ---- 本机配置与建议 ----
    def profile(self):
        with self._condition:
            return self._profile

    def refresh_profile(self):
        with self._condition:
            if self._closed or self._profiling or self._profile_provider is None:
                return
            self._profiling = True
        self._pool.submit(self._detect_profile)

    def _detect_profile(self):
        try:
            profile = self._profile_provider()
            if self._inference is not None:
                self._inference.target()
        finally:
            with self._condition:
                self._profiling = False
        with self._condition:
            self._profile = profile
        self._notify()

    def advice(self):
        status = self.status()
        if status is None:
            return ()
        snapshots = self.list()
        supported = all(item.descriptor.file_for(python_tag()) is not None for item in snapshots)
        return runtime_advice(self.profile(), snapshots, status, supported=supported)

    # ---- 安装 ----
    def install(self, token, *, import_path=None):
        with self._condition:
            if self._closed or token in self._tasks:
                return
            current = self._states[token]
            if current.active or current.state in ("installed", "removing") or current.descriptor.file_for(python_tag()) is None:
                return
            cancel = Event()
            self._tasks[token] = cancel
        self._update(token, state="queued", downloaded=0, error="")
        self._pool.submit(self._install, token, cancel, import_path)

    def cancel(self, token):
        with self._condition:
            if token in self._tasks:
                self._tasks[token].set()

    def _install(self, token, cancel, import_path):
        item = self._states[token].descriptor
        file = item.file_for(python_tag())
        downloads = self.root / ".downloads"
        stage = None
        try:
            downloads.mkdir(parents=True, exist_ok=True)
            check_space(self.root, file.size + item.installed_size)
            check_cancelled(cancel.is_set)
            if import_path:
                source = Path(import_path)
                self._update(token, state="verifying")
            else:
                source = downloads / (file.sha256 + ".part")
                self._update(token, state="downloading")
                download_file(file, source, cancel.is_set, lambda size: self._update(token, downloaded=size))
                self._update(token, state="verifying")
            verify_file(source, file.size, file.sha256, cancel.is_set)
            stage = downloads / ("staging-" + uuid.uuid4().hex)
            files = self._extract(source, stage, item.package, cancel.is_set)
            manifest = {"schema_version": 1, "token": token, "python": file.python, "sha256": file.sha256,
                        "package": item.package, "files": files}
            (stage / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
            reason = check_installation(stage)
            if reason:
                raise ValueError(reason)
            target = managed_directory(self.root, token)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                validate_managed(self.root, target)
                shutil.rmtree(target)
            with self._condition:
                check_cancelled(cancel.is_set)
                if self._closed:
                    raise CancelledError()
                os.replace(stage, target)
                stage = None
                self._preferences["enabled"] = token
                if token in self._preferences["pending_removal"]:
                    self._preferences["pending_removal"].remove(token)
                saved = self._save_preferences()
                for key, state in self._states.items():
                    if key != token:
                        self._states[key] = replace(state, enabled=False)
            if not import_path:
                try:
                    source.unlink(missing_ok=True)
                except OSError:
                    logger.warning("运行环境临时下载清理失败：%s", token, exc_info=True)
            self._update(token, state="installed", installed_bytes=sum(files.values()), path=str(target),
                         enabled=saved, error="" if saved else "runtime.error.preferences")
        except CancelledError:
            self._update(token, state="missing", error="")
        except Exception as error:
            logger.exception("运行环境安装失败：%s", token)
            if not import_path and isinstance(error, ValueError) and str(error) in ("models.error.checksum", "models.error.size"):
                try:
                    (downloads / (file.sha256 + ".part")).unlink(missing_ok=True)
                except OSError:
                    logger.warning("损坏下载清理失败：%s", token, exc_info=True)
            key = str(error) if str(error).startswith(("models.error.", "runtime.")) else "runtime.error.install"
            self._update(token, state="failed", error=key)
        finally:
            if stage is not None and stage.exists():
                try:
                    shutil.rmtree(stage)
                except OSError:
                    logger.exception("运行环境安装收尾失败：%s", token)
            with self._condition:
                self._tasks.pop(token, None)
            self._notify(token)

    @staticmethod
    def _extract(source, stage, package, cancelled):
        files = {}
        stage.mkdir()
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                check_cancelled(cancelled)
                parts = PurePosixPath(info.filename).parts
                if info.is_dir() or not parts or parts[0] != package:
                    continue
                if (info.filename.startswith("/") or "\\" in info.filename or ":" in info.filename
                        or any(part in ("", ".", "..") for part in parts)):
                    raise ValueError("runtime.error.package")
                destination = stage.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info) as stream, destination.open("wb") as output:
                    while chunk := stream.read(1 << 20):
                        check_cancelled(cancelled)
                        output.write(chunk)
                        written += len(chunk)
                if written != info.file_size:
                    raise ValueError("runtime.error.package")
                files["/".join(parts)] = written
        if not files:
            raise ValueError("runtime.error.package")
        return files

    # ---- 卸载 ----
    def uninstall(self, token):
        with self._condition:
            current = self._states[token]
            if self._closed or token in self._tasks or current.state not in ("installed", "corrupt", "failed"):
                return
            if current.active:
                previous = dict(self._preferences, pending_removal=list(self._preferences["pending_removal"]))
                if token not in self._preferences["pending_removal"]:
                    self._preferences["pending_removal"].append(token)
                if self._preferences["enabled"] == token:
                    self._preferences["enabled"] = ""
                if not self._save_preferences():
                    self._preferences = previous
                    self._states[token] = replace(current, error="runtime.error.preferences")
                else:
                    self._states[token] = replace(current, state="removing", enabled=False, error="")
                pending = True
            else:
                self._tasks[token] = Event()
                pending = False
        if pending:
            self._notify(token)
            return
        self._update(token, state="uninstalling", error="")
        self._pool.submit(self._uninstall, token)

    def _uninstall(self, token):
        try:
            target = managed_directory(self.root, token)
            if target.exists():
                validate_managed(self.root, target)
                shutil.rmtree(target)
            with self._condition:
                if self._preferences["enabled"] == token:
                    self._preferences["enabled"] = ""
                    self._save_preferences()
            self._update(token, state="missing", installed_bytes=0, path="", enabled=False, error="")
        except (OSError, ValueError):
            logger.exception("运行环境卸载失败：%s", token)
            with self._condition:
                refreshed = self._discover(self._states[token].descriptor)
            self._update(token, state=refreshed.state, error="runtime.error.uninstall")
        finally:
            with self._condition:
                self._tasks.pop(token, None)

    def cancel_removal(self, token):
        """撤销“重启后卸载”。"""
        with self._condition:
            current = self._states[token]
            if current.state != "removing":
                return False
            self._preferences["pending_removal"].remove(token)
            self._preferences["enabled"] = token
            if not self._save_preferences():
                self._preferences["pending_removal"].append(token)
                self._preferences["enabled"] = ""
                return False
            self._states[token] = replace(current, state="installed", enabled=True, error="")
        self._notify(token)
        return True

    def close(self):
        with self._condition:
            self._closed = True
            for cancel in self._tasks.values():
                cancel.set()
            self._listeners.clear()
        self._pool.shutdown(wait=False)
