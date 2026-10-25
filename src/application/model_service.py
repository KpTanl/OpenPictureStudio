from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
import zipfile
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Condition, Event

from ..core.contracts.models import ModelDescriptor, ModelFile, ModelKey, ModelLease, ModelSnapshot
from .listeners import ListenerRegistry
from .model_download import check_cancelled, check_space, download_file, prepare_file, verify_file
from .runtime_libraries import environment_library

logger = logging.getLogger(__name__)


class ModelService:
    def __init__(self, root, catalog_path, *, external_roots=(), bundled_provider=None, compute_budget=None, resource_budget=None,
                 runtime_query=None):
        self.root = Path(root).absolute()
        self._catalog = self._read_catalog(Path(catalog_path))
        self._condition = Condition()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-model")
        self._states = {item.key: ModelSnapshot(item, "missing") for item in self._catalog}
        self._defaults = {}
        self._leases = {}
        self._tasks = {}
        self._listeners = ListenerRegistry("模型状态订阅失败")
        self._retire = None
        self._references = None
        self._closed = False
        self._bundled_provider = bundled_provider
        self._compute_budget = compute_budget
        self._resource_budget = resource_budget
        self._runtime_query = runtime_query or environment_library
        self._external_roots = (self.root, *(Path(path) for path in external_roots))
        self._pool.submit(self._discover)

    @staticmethod
    def _read_catalog(path):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["schema_version"] != 1:
            raise ValueError("不支持的模型目录版本")
        models = []
        for item in document["models"]:
            key = ModelKey(item["id"], item["revision"])
            if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in (".", "..")
                       for part in (key.model_id, key.revision)):
                raise ValueError("无效模型身份")
            files = tuple(ModelFile(**file) for file in item["files"])
            for file in files:
                if (Path(file.name).name != file.name or file.name in (".", "..")
                        or not re.fullmatch(r"[a-f0-9]{64}", file.digest)
                        or not re.fullmatch(r"[a-f0-9]{64}", file.sha256)
                        or not file.url.startswith("https://") or min(file.size, file.disk_size) < 1):
                    raise ValueError("无效模型文件合同")
            runtime = item.get("runtime", "mediapipe" if item["adapter"] == "face_landmarker" else "onnxruntime")
            if runtime not in ("", "onnxruntime", "mediapipe", "cv2"):
                raise ValueError("无效模型运行库")
            models.append(ModelDescriptor(key, item["name"], item["capability"], item["adapter"], files,
                item["source"], item["license"], item.get("scale", 1), item.get("tile", 256),
                item.get("overlap", 32), item.get("alignment", 1), tuple(item.get("legacy_names", ())), runtime))
        if len({item.key for item in models}) != len(models):
            raise ValueError("重复模型版本")
        return tuple(models)

    def subscribe(self, listener):
        """返回取消订阅的函数；它返回后该订阅者不再被回调。"""
        return self._listeners.subscribe(listener)

    def _update(self, key, **changes):
        with self._condition:
            self._states[key] = replace(self._states[key], **changes)
            self._condition.notify_all()
        self._listeners.notify(key)

    def list(self, capability=None):
        with self._condition:
            return tuple(replace(snapshot, default=self.default(snapshot.descriptor.capability) == key.token) for key, snapshot in self._states.items()
                         if capability is None or snapshot.descriptor.capability == capability)

    def snapshot(self, key):
        with self._condition:
            return self._states[key]

    def default(self, capability):
        with self._condition:
            token = self._defaults.get(capability)
        if token:
            return token
        return next((model.key.token for model in self._catalog if model.capability == capability), "")

    def set_default(self, key):
        with self._condition:
            item = self._states[key].descriptor
            if item.adapter in ("bundled", "legacy") or self._closed:
                return
            self._defaults[item.capability] = key.token
        self._pool.submit(self._save_defaults)
        self._update(key, error="")

    def _save_defaults(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._condition:
                payload = {"schema_version": 1, "defaults": dict(self._defaults)}
            temp = self.root / ("preferences-" + uuid.uuid4().hex + ".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.root / "preferences.json")
        except OSError:
            logger.exception("模型默认选择保存失败")
            with self._condition:
                keys = tuple(key for key, snapshot in self._states.items()
                             if self._defaults.get(snapshot.descriptor.capability) == key.token)
            for key in keys:
                self._update(key, error="models.error.preferences")

    def _directory(self, key):
        return self.root / "managed" / key.model_id / key.revision

    def _discover(self):
        try:
            prefs = self.root / "preferences.json"
            if prefs.exists():
                document = json.loads(prefs.read_text(encoding="utf-8"))
                if document["schema_version"] != 1:
                    raise ValueError("模型配置版本无效")
                with self._condition:
                    self._defaults.update({cap: token for cap, token in document["defaults"].items()
                                           if cap not in self._defaults and any(item.key.token == token and item.capability == cap
                                                  for item in self._catalog)})
        except (OSError, ValueError, KeyError, TypeError):
            logger.exception("模型默认选择读取失败")
        for item in self._catalog:
            if self._closed:
                return
            with self._condition:
                if item.key in self._tasks:
                    continue
            self._update(item.key, state="discovering")
            candidates = [(self._directory(item.key), True)]
            candidates.extend((root, False) for root in self._external_roots)
            found = None
            corrupt = False
            for directory, managed in candidates:
                paths = tuple((file.name, str(directory / file.name)) for file in item.files)
                if not managed and len(item.files) == 1:
                    names = (item.files[0].name, *item.legacy_names)
                    paths = next((tuple(((item.files[0].name, str(directory / name)),))
                                  for name in names if (directory / name).is_file()), paths)
                if not all(Path(path).is_file() for _, path in paths):
                    continue
                try:
                    for file, (_, path) in zip(item.files, paths):
                        verify_file(Path(path), file.disk_size, file.digest, lambda: self._closed)
                    found = (paths, managed)
                    break
                except (OSError, ValueError):
                    corrupt = corrupt or managed
                except CancelledError:
                    return
            if found:
                self._update(item.key, state="installed", paths=found[0], managed=found[1],
                             installed_bytes=sum(file.disk_size for file in item.files))
            else:
                self._update(item.key, state="corrupt" if corrupt else "missing")

            self._update(item.key, temporary_bytes=self._temporary_bytes(item.key))

        self._discover_external()
        if self._bundled_provider is not None and not self._closed:
            try:
                for snapshot in self._bundled_provider():
                    with self._condition:
                        self._states[snapshot.descriptor.key] = snapshot
                    self._update(snapshot.descriptor.key)
            except (OSError, ImportError):
                logger.exception("内置模型资源读取失败")

    def _discover_external(self):
        import hashlib
        known = {file.digest for item in self.list() for file in item.descriptor.files}
        for directory in dict.fromkeys(self._external_roots):
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if self._closed:
                    return
                if not path.is_file() or path.suffix not in (".onnx", ".task") or path.stat().st_size < 1024:
                    continue
                try:
                    before = path.stat()
                    hasher = hashlib.sha256()
                    with path.open("rb") as stream:
                        while chunk := stream.read(1 << 20):
                            check_cancelled(lambda: self._closed)
                            hasher.update(chunk)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise OSError("旧模型资源在读取期间发生变化")
                    digest = hasher.hexdigest()
                    if digest in known:
                        continue
                    size = path.stat().st_size
                    key = ModelKey("external-" + digest[:16], "sha256-" + digest[:16])
                    descriptor = ModelDescriptor(key, path.name, "legacy_resource", "legacy", (
                        ModelFile(path.name, size, digest, ""),), str(path), "")
                    snapshot = ModelSnapshot(descriptor, "installed", installed_bytes=size,
                                             paths=((path.name, str(path)),))
                    with self._condition:
                        self._states[key] = snapshot
                    known.add(digest)
                    self._update(key)
                except CancelledError:
                    return
                except OSError:
                    logger.exception("旧模型资源读取失败：%s", path)

    def runtime_available(self, descriptor):
        return not descriptor.runtime or self.runtime_status(descriptor).available

    def runtime_status(self, descriptor):
        return self._runtime_query(descriptor.runtime)

    def install(self, key, *, import_path=None):
        with self._condition:
            if self._closed or key in self._tasks:
                return
            current = self._states[key]
            if current.descriptor.adapter in ("bundled", "legacy"):
                return
            if current.state == "installed" and current.managed:
                return
            cancel = Event()
            self._tasks[key] = cancel
        self._update(key, state="queued", error="")
        self._pool.submit(self._install, key, cancel, import_path)

    def _install(self, key, cancel, import_path):
        stage = None
        backup = None
        partials = None
        original = self.snapshot(key)
        item = original.descriptor
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            check_space(self.root, item.download_size + sum(file.disk_size for file in item.files))
            partials = self.root / ".downloads" / key.model_id / key.revision
            partials.mkdir(parents=True, exist_ok=True)
            self._validate_download_path(partials)
            stage = partials / ("staging-" + uuid.uuid4().hex)
            stage.mkdir()
            downloaded = 0
            for file in item.files:
                check_cancelled(cancel.is_set)
                if import_path:
                    source = Path(import_path)
                    source = source / file.name if source.is_dir() else source
                    self._update(key, state="verifying")
                    if source.stat().st_size == file.disk_size:
                        verify_file(source, file.disk_size, file.digest, cancel.is_set)
                        with source.open("rb") as stream, (stage / file.name).open("wb") as output:
                            while chunk := stream.read(1 << 20):
                                check_cancelled(cancel.is_set)
                                output.write(chunk)
                    else:
                        prepare_file(file, source, stage / file.name, cancel.is_set)
                else:
                    source = partials / (file.sha256 + ".part")
                    self._update(key, state="downloading")
                    download_file(file, source, cancel.is_set,
                                  lambda size, base=downloaded: self._update(key, downloaded=base + size))
                    self._update(key, state="verifying")
                    prepare_file(file, source, stage / file.name, cancel.is_set)
                downloaded += file.size
            with self._compute_budget.borrow(cancel.is_set, priority=1) if self._compute_budget else nullcontext():
                with self._resource_budget.working_memory(sum(file.disk_size for file in item.files) * 2 + (64 << 20), cancel.is_set) if self._resource_budget else nullcontext():
                    self._validate_structure(item, stage)
            check_cancelled(cancel.is_set)
            target = self._directory(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._validate_managed_path(target)
            if target.exists():
                self._validate_managed_path(target)
                backup = target.with_name(target.name + ".repair-" + uuid.uuid4().hex)
                os.replace(target, backup)
            with self._condition:
                check_cancelled(cancel.is_set)
                if self._closed:
                    raise CancelledError()
                os.replace(stage, target)
                stage = None
            self._update(key, state="installed", downloaded=downloaded, managed=True, error="",
                         paths=tuple((file.name, str(target / file.name)) for file in item.files),
                         installed_bytes=sum(file.disk_size for file in item.files))
            if not import_path:
                for file in item.files:
                    try:
                        (partials / (file.sha256 + ".part")).unlink(missing_ok=True)
                    except OSError:
                        logger.warning("模型临时下载清理失败：%s", key.token, exc_info=True)
        except CancelledError:
            self._update(key, state="installed" if original.paths else "cancelled", error="")
        except Exception as error:
            logger.exception("模型安装失败：%s", key.token)
            if partials is not None and not import_path and isinstance(error, ValueError) and str(error) in ("models.error.checksum", "models.error.size"):
                for file in item.files:
                    try:
                        (partials / (file.sha256 + ".part")).unlink(missing_ok=True)
                    except OSError:
                        logger.warning("损坏下载清理失败：%s", key.token, exc_info=True)
            self._update(key, state="installed" if original.paths else "failed",
                         error=str(error) if str(error).startswith("models.") else "models.error.install")
        finally:
            try:
                if backup is not None:
                    target = self._directory(key)
                    if stage is not None and not target.exists():
                        os.replace(backup, target)
                    elif backup.exists():
                        self._validate_managed_path(backup)
                        shutil.rmtree(backup)
                if stage is not None:
                    self._validate_download_path(stage.parent)
                    shutil.rmtree(stage)
            except (OSError, ValueError):
                logger.exception("模型安装收尾失败：%s", key.token)
                self._update(key, error="models.error.cleanup")
            finally:
                with self._condition:
                    self._tasks.pop(key, None)
                self._update(key, temporary_bytes=self._temporary_bytes(key))

    def _temporary_bytes(self, key):
        directory = self.root / ".downloads" / key.model_id / key.revision
        if not directory.exists():
            return 0
        try:
            self._validate_download_path(directory)
            return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
        except (OSError, ValueError):
            logger.exception("模型临时资源读取失败：%s", key.token)
            return 0

    def clear_temporary(self, key):
        with self._condition:
            if self._closed or key in self._tasks:
                return
            current = self._states[key]
            if current.descriptor.adapter in ("bundled", "legacy"):
                return
            cancel = Event()
            self._tasks[key] = cancel
        self._update(key, state="cleaning", error="")
        self._pool.submit(self._clear_temporary, key, current, cancel)

    def _clear_temporary(self, key, previous, cancel):
        try:
            directory = self.root / ".downloads" / key.model_id / key.revision
            if directory.exists():
                self._validate_download_path(directory)
                parts = {file.sha256 + ".part" for file in previous.descriptor.files}
                for path in directory.iterdir():
                    check_cancelled(cancel.is_set)
                    if path.name in parts and path.is_file():
                        path.unlink()
                    elif re.fullmatch(r"staging-[a-f0-9]{32}", path.name) and path.is_dir():
                        if any(child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction())
                               for child in path.rglob("*")):
                            raise ValueError("models.error.path")
                        shutil.rmtree(path)
            self._update(key, state=previous.state, error="")
        except CancelledError:
            self._update(key, state=previous.state)
        except (OSError, ValueError):
            logger.exception("模型临时资源清理失败：%s", key.token)
            self._update(key, state=previous.state, error="models.error.cleanup")
        finally:
            with self._condition:
                self._tasks.pop(key, None)
            self._update(key, temporary_bytes=self._temporary_bytes(key))

    def bind_references(self, provider):
        with self._condition:
            self._references = provider

    def reference_count(self, key):
        with self._condition:
            provider = self._references
            digests = frozenset(file.digest for file in self._states[key].descriptor.files)
        return provider(digests) if provider is not None else 0

    @staticmethod
    def _validate_structure(item, directory):
        if item.adapter == "face_landmarker":
            with zipfile.ZipFile(directory / item.files[0].name) as archive:
                if "face_landmarks_detector.tflite" not in archive.namelist():
                    raise ValueError("models.error.structure")
            return
        if item.adapter == "yunet":
            import cv2
            import numpy as np
            try:
                locator = cv2.FaceDetectorYN.create(str(directory / item.files[0].name), "", (64, 64))
                locator.detect(np.zeros((64, 64, 3), np.uint8))
            except cv2.error as error:
                raise ValueError("models.error.structure") from error
            return
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        session = ort.InferenceSession(str(directory / item.files[0].name), options,
                                       providers=["CPUExecutionProvider"])
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if len(inputs) != 1 or not outputs or inputs[0].type != "tensor(float)" or len(inputs[0].shape) != 4:
            raise ValueError("models.error.structure")
        del session

    def cancel(self, key):
        with self._condition:
            if key in self._tasks:
                self._tasks[key].set()
                self._condition.notify_all()

    def bind_retirement(self, retire):
        with self._condition:
            self._retire = retire

    @contextmanager
    def borrow(self, key):
        with self._condition:
            current = self._states[key]
            if not current.usable:
                raise FileNotFoundError(key.token)
            self._leases[key] = self._leases.get(key, 0) + 1
        try:
            yield ModelLease(current.descriptor, current.paths)
        finally:
            with self._condition:
                self._leases[key] -= 1
                self._condition.notify_all()

    def resolve(self, token):
        key = ModelKey.parse(token)
        current = self.snapshot(key)
        if not current.usable:
            raise FileNotFoundError(token)
        return current.descriptor, dict(current.paths)

    def resolve_digest(self, digest):
        with self._condition:
            return next(((snapshot.descriptor.key, path) for snapshot in self._states.values()
                         for file in snapshot.descriptor.files
                         for path in (dict(snapshot.paths).get(file.name, str(self._directory(snapshot.descriptor.key) / file.name)),)
                         if file.digest == digest), None)

    def describe(self, token):
        key = ModelKey.parse(token)
        current = self.snapshot(key)
        paths = dict(current.paths) or {file.name: str(self._directory(key) / file.name)
                                       for file in current.descriptor.files}
        return current.descriptor, paths

    def uninstall(self, key):
        with self._condition:
            current = self._states[key]
            if not current.managed or key in self._tasks or current.state != "installed":
                return
            cancel = Event()
            self._tasks[key] = cancel
        self._update(key, state="waiting", error="")
        if self._retire:
            self._retire(frozenset(file.digest for file in current.descriptor.files))
        self._pool.submit(self._uninstall, key, cancel)

    def _uninstall(self, key, cancel):
        try:
            item = self.snapshot(key).descriptor
            if self._retire:
                self._retire(frozenset(file.digest for file in item.files))
            with self._condition:
                while self._leases.get(key, 0):
                    check_cancelled(cancel.is_set)
                    self._condition.wait(0.1)
                check_cancelled(cancel.is_set)
            self._update(key, state="uninstalling")
            target = self._directory(key)
            self._validate_managed_path(target)
            shutil.rmtree(target)
            self._update(key, state="missing", paths=(), installed_bytes=0, managed=False)
        except CancelledError:
            self._update(key, state="installed")
        except Exception:
            logger.exception("模型卸载失败：%s", key.token)
            self._update(key, state="installed" if all(Path(path).is_file() for _, path in self.snapshot(key).paths) else "corrupt",
                         error="models.error.uninstall")
        finally:
            with self._condition:
                self._tasks.pop(key, None)

    def _validate_download_path(self, target):
        root = (self.root / ".downloads").resolve()
        if not target.resolve().is_relative_to(root):
            raise ValueError("models.error.path")
        for parent in (target, *target.parents):
            if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                raise ValueError("models.error.path")
            if parent == self.root:
                break
        if any(path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) for path in target.iterdir()):
            raise ValueError("models.error.path")

    def _validate_managed_path(self, target):
        root = (self.root / "managed").resolve()
        if not target.resolve().is_relative_to(root) or target.resolve() == root:
            raise ValueError("models.error.path")
        for parent in (target, *target.parents):
            if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                raise ValueError("models.error.path")
            if parent == self.root:
                break
        if any(path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
               for path in target.rglob("*")):
            raise ValueError("models.error.path")

    def close(self):
        with self._condition:
            self._closed = True
            for cancel in self._tasks.values():
                cancel.set()
            self._listeners.clear()
            self._references = None
            self._condition.notify_all()
        self._pool.shutdown(wait=False)
