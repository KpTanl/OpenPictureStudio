from __future__ import annotations

from contextlib import ExitStack, closing
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid

from .storage_paths import (LOCATION_KEYS, TEMP_KEYS, bootstrap_root, data_root,
                            check_plain_path, file_lock, plain, read_settings, storage_path, temporary_root, update_settings)

_cancelled = ContextVar("storage_cancelled", default=lambda: False)


def check_cancelled():
    if _cancelled.get()():
        raise InterruptedError("storage.cancelled")


def path_key(path):
    return os.path.normcase(os.path.abspath(path))


def contains(parent, child):
    return Path(path_key(child)).is_relative_to(Path(path_key(parent)))


def stamp(path):
    info = Path(path).stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def files_under(root, *, exclude=(), skip=(), cancelled=lambda: False):
    root = Path(root).absolute()
    if not root.exists():
        return
    check_plain_path(root)
    if not plain(root):
        raise ValueError("storage.error.link")
    if root.is_file():
        yield root
        return
    pending = [root]
    while pending:
        if cancelled():
            raise InterruptedError("storage.cancelled")
        parent = pending.pop()
        for path in parent.iterdir():
            if cancelled():
                raise InterruptedError("storage.cancelled")
            if parent == root and any(fnmatch.fnmatch(path.name, pattern) for pattern in exclude):
                continue
            if any(contains(ignored, path) for ignored in skip):
                continue
            if not plain(path):
                raise ValueError("storage.error.link")
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                yield path


@dataclass
class StorageItem:
    key: str
    paths: tuple[Path, ...]
    exclude: tuple[str, ...] = ()
    size: int = 0
    files: int = 0
    errors: list[str] = field(default_factory=list)
    free: int | None = None
    refreshed: float = 0
    include: tuple[str, ...] = ()
    skip: tuple[Path, ...] = ()
    unique_size: int = 0


class StorageService:
    def __init__(self, *, sources=(), cancelled=lambda: False):
        self.sources = tuple(sources)
        self.cancelled = cancelled

    def items(self, *, include_external=True):
        from ..utils.model_paths import get_project_model_root, get_runtime_root
        local = data_root()
        from ..utils.user_paths import application_root, home_root
        roaming = application_root("APPDATA")
        models, runtimes = get_project_model_root(create=False), get_runtime_root()
        items = [StorageItem("thumbnails", (storage_path("thumbnails", local / "thumbnails"),)),
                 StorageItem("edit_cache", (storage_path("edit_cache", local / "edit_cache"),)),
                 StorageItem("drafts", (local / "drafts.json",)),
                 StorageItem("mask_assets", (local / "mask_assets",)),
                 StorageItem("library", (local / "library",), ("before-restore-*.sqlite3",)),
                 StorageItem("backups", (local / "library",), include=("before-restore-*.sqlite3",)),
                 StorageItem("exports", (local / "exports",)),
                 StorageItem("assistant", (local / "assistant",), ("attachments", "styles")),
                 StorageItem("attachments", (local / "assistant" / "attachments",)),
                 StorageItem("styles", (local / "assistant" / "styles",)),
                 StorageItem("wallpaper", (local / "wallpaper",)),
                 StorageItem("logs", (storage_path("logs", roaming / "logs"),)),
                 StorageItem("models", (models,), (".downloads",)),
                 StorageItem("model_downloads", (models / ".downloads",)),
                 StorageItem("runtimes", (runtimes,), (".downloads",)),
                 StorageItem("runtime_downloads", (runtimes / ".downloads",)),
                 StorageItem("presets", (home_root() / "presets",)),
                 StorageItem("other", (local,), (".storage",))]
        for kind in TEMP_KEYS:
            items.append(StorageItem(kind, (temporary_root(kind),)))
        legacy = []
        for prefix in ("ops-artifacts-*", "ops-studio-audit-*", "ops_tmp_*", "ops-plugin-*") if include_external else ():
            if self.cancelled():
                raise InterruptedError("storage.cancelled")
            legacy.extend(Path(tempfile.gettempdir()).glob(prefix))
        items.append(StorageItem("legacy_temp", tuple(legacy)))
        legacy_models = roaming / "models"
        if path_key(legacy_models) != path_key(models):
            items.append(StorageItem("legacy_models", (legacy_models,)))
        settings = read_settings()
        projects = tuple(Path(path) for path in settings["projects"])
        project_paths = tuple(path for project in projects for path in (project, project.with_suffix(".opsdata")))
        items.append(StorageItem("projects", project_paths))
        source_paths = {path_key(path): Path(path).absolute() for path in self.sources}
        output_paths = {}
        source_errors, output_errors = [], []
        if include_external and (local / "drafts.json").is_file():
            try:
                drafts = json.loads((local / "drafts.json").read_text(encoding="utf-8"))
                for path in drafts:
                    if self.cancelled():
                        raise InterruptedError("storage.cancelled")
                    if os.path.isabs(path):
                        source_paths[path_key(path)] = Path(path).absolute()
            except InterruptedError:
                raise
            except (OSError, ValueError, TypeError) as error:
                source_errors.append(str(local / "drafts.json") + ": " + str(error))
        for project in projects if include_external else ():
            if self.cancelled():
                raise InterruptedError("storage.cancelled")
            try:
                value = json.loads(project.read_text(encoding="utf-8"))
                from ..core.project.document import resolve_source_path
                path = resolve_source_path(project, value["source"]["path"])
                source_paths[path_key(path)] = Path(path).absolute()
            except (OSError, ValueError, KeyError) as error:
                source_errors.append(str(project) + ": " + str(error))
        database = local / "exports" / "exports.sqlite3"
        if database.is_file() and include_external:
            try:
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                    for (record,) in db.execute("SELECT record FROM completed_exports"):
                        if self.cancelled():
                            raise InterruptedError("storage.cancelled")
                        value = json.loads(record)
                        path = value.get("target_path") or value.get("path")
                        if path:
                            output_paths[path_key(path)] = Path(path).absolute()
                        if value.get("source_path"):
                            source_paths[path_key(value["source_path"])] = Path(value["source_path"]).absolute()
            except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
                output_errors.append(str(database) + ": " + str(error))
        database = local / "library" / "library.sqlite3"
        if database.is_file() and include_external:
            try:
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                    for current, previous in db.execute("SELECT current_path,last_known_path FROM photos"):
                        if self.cancelled():
                            raise InterruptedError("storage.cancelled")
                        path = current or previous
                        if path:
                            source_paths[path_key(path)] = Path(path).absolute()
            except sqlite3.Error as error:
                source_errors.append(str(database) + ": " + str(error))
        items.append(StorageItem("originals", tuple(source_paths.values()), errors=source_errors))
        items.append(StorageItem("outputs", tuple(output_paths.values()), errors=output_errors))
        items.append(StorageItem("settings", ()))
        items.append(StorageItem("configuration", (bootstrap_root(),)))
        for index, entry in enumerate(settings["retired"]):
            items.append(StorageItem("retired:" + str(index), (Path(entry["source"]),), (".storage",)))
        other = next(item for item in items if item.key == "other")
        items.remove(other)
        other.skip = tuple(root for item in items for root in item.paths if contains(local, root))
        items.append(other)
        return items

    def scan(self, cancelled=lambda: False):
        items = self.items()
        seen = set()
        for item in items:
            for root in item.paths:
                try:
                    for path in files_under(root, exclude=item.exclude, skip=item.skip, cancelled=cancelled):
                        if item.include and not any(fnmatch.fnmatch(path.name, pattern) for pattern in item.include):
                            continue
                        key = path_key(path)
                        size = path.stat().st_size
                        item.size += size
                        item.files += 1
                        if key not in seen:
                            item.unique_size += size
                            seen.add(key)
                except InterruptedError:
                    raise
                except (OSError, ValueError) as error:
                    item.errors.append(str(root) + ": " + str(error))
                if item.free is None:
                    try:
                        parent = root
                        while not parent.exists() and parent != parent.parent:
                            parent = parent.parent
                        item.free = shutil.disk_usage(parent).free
                    except OSError:
                        pass
            item.refreshed = time.time()
        return items

    def item(self, key):
        return next(item for item in self.items(include_external=key in ("originals", "outputs", "legacy_temp")) if item.key == key)

    def objects(self, key):
        local = data_root()
        if key == "drafts":
            path = local / "drafts.json"
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            return [{"name": photo, "id": photo, "hash": digest(params), "bytes": len(json.dumps(params).encode("utf-8"))}
                    for photo, params in data.items()]
        if key == "projects":
            result = []
            for name in read_settings()["projects"]:
                if self.cancelled():
                    raise InterruptedError("storage.cancelled")
                path = Path(name)
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    size = sum(file.stat().st_size for root in (path, path.with_suffix(".opsdata"))
                               for file in files_under(root, cancelled=self.cancelled))
                    result.append({"id": name, "name": name, "hash": digest(value), "revisions": len(value["revisions"]), "bytes": size})
                except InterruptedError:
                    raise
                except (OSError, ValueError, KeyError):
                    result.append({"id": name, "name": name, "unavailable": True})
            return result
        return self.clean_candidates(key)

    def locations(self, key):
        result = []
        for root in self.item(key).paths:
            size, errors = 0, []
            try:
                if not root.exists():
                    raise FileNotFoundError(str(root))
                for path in files_under(root, cancelled=self.cancelled):
                    size += path.stat().st_size
            except InterruptedError:
                raise
            except (OSError, ValueError) as error:
                errors.append(str(error))
            result.append({"name": str(root), "bytes": size, "errors": errors})
        return result

    def reference_documents(self):
        path = data_root() / "drafts.json"
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("storage.error.references")
            yield value
        for name in read_settings()["projects"]:
            if self.cancelled():
                raise InterruptedError("storage.cancelled")
            path = Path(name)
            if not path.exists():
                raise ValueError("storage.error.references")
            value = json.loads(path.read_text(encoding="utf-8"))
            from ..core.project.document import ProjectDocument, resolve_source_path
            document = ProjectDocument.from_dict(value)
            value["source"]["path"] = resolve_source_path(path, document.source["path"])
            yield value
        database = data_root() / "assistant" / "assistant.sqlite3"
        if database.is_file():
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ("conversations", "styles"):
                    if table in tables:
                        for (value,) in db.execute("SELECT value FROM " + table):
                            if self.cancelled():
                                raise InterruptedError("storage.cancelled")
                            value = json.loads(value)
                            if not isinstance(value, dict):
                                raise ValueError("storage.error.references")
                            yield value

    def reference_paths(self):
        paths = {path_key(path) for path in self.sources}
        def visit(value):
            if self.cancelled():
                raise InterruptedError("storage.cancelled")
            if isinstance(value, dict):
                for key, child in value.items():
                    if isinstance(key, str) and os.path.isabs(key):
                        paths.add(path_key(key))
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str) and os.path.isabs(value):
                paths.add(path_key(value))
        for value in self.reference_documents():
            visit(value)
        database = data_root() / "library" / "library.sqlite3"
        if database.is_file():
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                for current, previous in db.execute("SELECT current_path,last_known_path FROM photos"):
                    path = current or previous
                    if path:
                        paths.add(path_key(path))
        return paths

    def clean_candidates(self, key):
        item = self.item(key)
        result = []
        if key in TEMP_KEYS:
            references = self.reference_paths()
            for root in item.paths:
                if not root.exists():
                    continue
                for folder in root.glob("work-*"):
                    if self.cancelled():
                        raise InterruptedError("storage.cancelled")
                    try:
                        owner = json.loads((folder / "owner.json").read_text(encoding="utf-8"))
                        if owner.get("kind") != key or not plain(folder):
                            continue
                        if any(contains(folder, referenced) for referenced in references):
                            continue
                        with file_lock(folder / "owner.lock"):
                            paths = list(files_under(folder, cancelled=self.cancelled))
                            result.append({"id": str(folder), "name": str(folder), "token": owner["token"],
                                           "bytes": sum(path.stat().st_size for path in paths)})
                    except InterruptedError:
                        raise
                    except (OSError, ValueError, KeyError):
                        continue
            return result
        allowed = {"logs", "backups", "model_downloads", "runtime_downloads", "mask_assets", "wallpaper", "projects", "presets"}
        if key.startswith("retired:"):
            entry = read_settings()["retired"][int(key.split(":")[1])]
            active = [root for candidate in self.items(include_external=False) if not candidate.key.startswith("retired:") and candidate.key not in ("other", "legacy_models")
                      for root in candidate.paths]
            references = self.reference_paths()
            if os.name == "nt":
                from .windows_wallpaper import WindowsDesktopWallpaper
                with WindowsDesktopWallpaper() as desktop:
                    references.update(path_key(path) for _, path in desktop.get_wallpapers() if path)
            for candidate in self.items(include_external=False):
                if candidate.key == "legacy_models":
                    references.update(path_key(root / "face_landmarker.task") for root in candidate.paths)
            for relative, saved in entry["files"].items():
                if self.cancelled():
                    raise InterruptedError("storage.cancelled")
                if Path(relative).is_absolute() or ".." in Path(relative).parts:
                    raise ValueError("storage.error.references")
                path = Path(entry["source"]) / relative
                if any(contains(root, path) for root in active) or path_key(path) in references:
                    continue
                if path.is_file() and plain(path) and stamp(path) == saved:
                    check_plain_path(path)
                    result.append({"id": str(path), "name": str(path), "stamp": saved, "bytes": saved[2]})
            return result
        if key not in allowed:
            return result
        if key == "projects":
            references = self.reference_paths()
            for name in read_settings()["projects"]:
                project = Path(name)
                root = project.with_suffix(".opsdata")
                if (root / "journal" / "pending.json").exists():
                    raise ValueError("storage.error.references")
                value = json.loads(project.read_text(encoding="utf-8"))
                from ..core.project.document import ProjectDocument
                ProjectDocument.from_dict(value)
                referenced = {"workflows/" + revision["workflow_hash"] + ".json" for revision in value["revisions"]}
                referenced.update(asset["path"].replace("\\", "/") for asset in value.get("assets", {}).values())
                referenced.update(artifact["path"].replace("\\", "/") for revision in value["revisions"] for artifact in revision["artifact_refs"])
                if any(Path(name).is_absolute() or ".." in Path(name).parts for name in referenced):
                    raise ValueError("storage.error.references")
                for directory in ("workflows", "artifacts", "assets"):
                    for path in files_under(root / directory, cancelled=self.cancelled):
                        if path.relative_to(root).as_posix() not in referenced and path_key(path) not in references:
                            result.append({"id": str(path), "name": str(path), "stamp": stamp(path), "bytes": path.stat().st_size})
            return result
        active = set()
        if key == "wallpaper":
            from .windows_wallpaper import WindowsDesktopWallpaper
            with WindowsDesktopWallpaper() as desktop:
                active = {path_key(path) for _, path in desktop.get_wallpapers() if path}
        protected = self.protected_paths() if key == "mask_assets" else set()
        for root in item.paths:
            for path in files_under(root, cancelled=self.cancelled):
                if item.include and not any(fnmatch.fnmatch(path.name, pattern) for pattern in item.include):
                    continue
                if key == "logs" and path.name == "openpicturestudio.log":
                    continue
                if key == "mask_assets" and (path.suffix != ".png" or path.stem in protected):
                    continue
                if key == "wallpaper" and (path.suffix != ".bmp" or path_key(path) in active):
                    continue
                result.append({"id": str(path), "name": str(path), "stamp": stamp(path), "bytes": path.stat().st_size})
        return result

    def protected_paths(self):
        protected = set()
        def visit(value):
            if self.cancelled():
                raise InterruptedError("storage.cancelled")
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "asset" and isinstance(child, str):
                        protected.add(child)
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        for value in self.reference_documents():
            visit(value)
        return protected

    def migration(self, key, target):
        if key not in LOCATION_KEYS:
            raise ValueError("storage.error.location")
        source = data_root() if key == "data" else self.item(key).paths[0]
        target = Path(target).absolute()
        from ..utils.installation import check_storage_target
        check_storage_target(target)
        for parent in (target, *target.parents):
            if parent.exists() and not plain(parent):
                raise ValueError("storage.error.link")
        if contains(source, target) or contains(target, source) or contains(target, bootstrap_root()):
            raise ValueError("storage.error.overlap")
        for item in self.items(include_external=False):
            if item.key == key or item.key in ("originals", "outputs", "projects", "other"):
                continue
            for root in item.paths:
                if contains(target, root) or contains(root, target):
                    raise ValueError("storage.error.overlap")
        if target.exists():
            raise ValueError("storage.error.target")
        for name in read_settings()["projects"]:
            if contains(Path(name).with_suffix(".opsdata"), target):
                raise ValueError("storage.error.overlap")
        paths = list(files_under(source, exclude=(".storage",), cancelled=self.cancelled))
        size = sum(path.stat().st_size for path in paths)
        parent = target.parent
        while not parent.exists():
            parent = parent.parent
        free = shutil.disk_usage(parent).free
        if free < (0 if key in ("thumbnails", "edit_cache") else size) + 16 * 1024 ** 2:
            raise ValueError("storage.error.space")
        return {"operation": "migrate", "kind": key, "source": str(source), "target": str(target),
                "bytes": size, "files": len(paths), "free": free}

    def backup_plan(self, key, target, *, project=None):
        if key == "projects":
            if project not in read_settings()["projects"]:
                raise ValueError("storage.error.changed")
            item = StorageItem(key, (Path(project), Path(project).with_suffix(".opsdata")))
        else:
            item = self.item(key) if key != "data" else StorageItem("data", (data_root(),), (".storage",))
        target = Path(target).absolute()
        if not item.paths or target.exists():
            raise ValueError("storage.error.target")
        for parent in (target.parent, *target.parents):
            if parent.exists() and not plain(parent):
                raise ValueError("storage.error.link")
        size, count = 0, 0
        for root in item.paths:
            if contains(root, target) or contains(target, root):
                raise ValueError("storage.error.overlap")
            if not root.exists():
                raise ValueError("storage.error.changed")
            for path in files_under(root, exclude=item.exclude, skip=item.skip, cancelled=self.cancelled):
                if item.include and not any(fnmatch.fnmatch(path.name, pattern) for pattern in item.include):
                    continue
                size += path.stat().st_size
                count += 1
        parent = target.parent
        while not parent.exists():
            parent = parent.parent
        free = shutil.disk_usage(parent).free
        if free < size + 16 * 1024 ** 2:
            raise ValueError("storage.error.space")
        result = {"operation": "backup", "kind": key, "source": str(item.paths[0]),
                  "sources": [str(path) for path in item.paths], "target": str(target), "bytes": size, "files": count, "free": free}
        if project is not None:
            result["project"] = project
        return result

    def schedule(self, job):
        job = {**job, "id": uuid.uuid4().hex, "created": time.time()}
        def change(value):
            if any(previous["kind"] == job["kind"] for previous in value["jobs"]):
                raise ValueError("storage.error.pending")
            value["jobs"].append(job)
        update_settings(change)

    def cancel_jobs(self):
        update_settings(lambda value: value["jobs"].clear())


def _copy_file(source, target):
    before = stamp(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as original, target.open("xb") as output:
        expected = hashlib.sha256()
        for block in iter(lambda: original.read(1024 * 1024), b""):
            check_cancelled()
            expected.update(block)
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    actual = hashlib.sha256()
    with target.open("rb") as stored:
        for block in iter(lambda: stored.read(1024 * 1024), b""):
            check_cancelled()
            actual.update(block)
    if actual.digest() != expected.digest() or before != stamp(source):
        raise ValueError("storage.error.changed")
    return before


def _copy_stage(target):
    suffix = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:24]
    return target.parent / (".ops-copy-" + suffix)


def _copy_tree(source, target, *, database_backup=False, exclude=(".storage",), include=(), skip=()):
    check_plain_path(target)
    if target.exists():
        raise ValueError("storage.error.target")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = _copy_stage(target)
    stage.mkdir()
    manifest = {}
    try:
        def selected_paths():
            return [path for path in files_under(source, exclude=exclude, skip=skip, cancelled=_cancelled.get())
                    if (not include or any(fnmatch.fnmatch(path.name, pattern) for pattern in include))
                    and not (database_backup and path.name.endswith((".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal")))]
        paths = selected_paths()
        for path in paths:
            check_cancelled()
            relative = Path(path.name) if source.is_file() else path.relative_to(source)
            destination = stage / relative
            if database_backup and path.suffix == ".sqlite3":
                destination.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(destination)) as copied:
                    original.backup(copied, pages=256, progress=lambda *_: check_cancelled())
                    if copied.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("storage.error.database")
                manifest[str(relative)] = stamp(path)
            else:
                manifest[str(relative)] = _copy_file(path, destination)
        if {path_key(path) for path in selected_paths()} != {path_key(path) for path in paths}:
            raise ValueError("storage.error.changed")
        for relative, saved in manifest.items():
            path = source if source.is_file() else source / relative
            if not (database_backup and path.suffix == ".sqlite3") and stamp(path) != saved:
                raise ValueError("storage.error.changed")
        check_cancelled()
        os.replace(stage, target)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _execute(job, completed):
    service = StorageService(cancelled=_cancelled.get())
    kind, operation = job["kind"], job["operation"]
    if operation == "migrate":
        from ..utils.installation import check_storage_target
        check_storage_target(job["target"])
        if read_settings()["locations"].get(kind) == job["target"]:
            return
        plan = service.migration(kind, job["target"])
        if path_key(plan["source"]) != path_key(job["source"]):
            raise ValueError("storage.error.changed")
        if not job.get("rebuild") and plan["free"] < plan["bytes"] + 16 * 1024 ** 2:
            raise ValueError("storage.error.space")
        with ExitStack() as stack:
            for root in (Path(job["source"]) / "thumbnails", Path(job["source"]) / "edit_cache") if kind == "data" else (Path(job["source"]),):
                if (root / "cache.lock").exists():
                    stack.enter_context(file_lock(root / "cache.lock"))
            source, target = Path(job["source"]), Path(job["target"])
            if job.get("rebuild") and kind in ("thumbnails", "edit_cache"):
                manifest = {str(path.relative_to(source)): stamp(path) for path in files_under(source)}
                target.mkdir(parents=True, exist_ok=False)
            else:
                settings = read_settings()
                skip = (tuple(Path(path) for key, path in settings["locations"].items()
                              if key != "data" and contains(source, path))
                        + tuple(Path(entry["source"]) for entry in settings["retired"]
                                if contains(source, entry["source"]) and path_key(source) != path_key(entry["source"]))) if kind == "data" else ()
                manifest = _copy_tree(source, target, skip=skip)
            def publish(value):
                value["locations"][kind] = job["target"]
                value["retired"].append({"kind": kind, "source": job["source"], "target": job["target"], "files": manifest})
            update_settings(publish)
    elif operation == "backup":
        target = Path(job["target"])
        source = Path(job["source"])
        if contains(source, target) or contains(target, source):
            raise ValueError("storage.error.overlap")
        if kind == "projects":
            plan = service.backup_plan(kind, target, project=job["project"])
        else:
            plan = service.backup_plan(kind, target)
        if plan["sources"] != job["sources"]:
            raise ValueError("storage.error.changed")
        if kind == "projects":
            stage = _copy_stage(target)
            stage.mkdir(parents=True, exist_ok=False)
            try:
                project = Path(job["project"])
                before = _copy_file(project, stage / project.name)
                _copy_tree(project.with_suffix(".opsdata"), stage / project.with_suffix(".opsdata").name)
                if before != stamp(project):
                    raise ValueError("storage.error.changed")
                check_cancelled()
                os.replace(stage, target)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        else:
            item = service.item(kind) if kind != "data" else StorageItem("data", (data_root(),), (".storage",))
            if len(plan["sources"]) == 1:
                _copy_tree(source, target, database_backup=True, exclude=item.exclude, include=item.include, skip=item.skip)
            else:
                stage = _copy_stage(target)
                stage.mkdir(parents=True, exist_ok=False)
                try:
                    for root in plan["sources"]:
                        check_cancelled()
                        path = Path(root)
                        name = path.stem + "-" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:8] + path.suffix
                        if path.is_file():
                            _copy_file(path, stage / name)
                        else:
                            _copy_tree(path, stage / name, database_backup=True, exclude=item.exclude, include=item.include, skip=item.skip)
                    check_cancelled()
                    os.replace(stage, target)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
    elif operation == "drafts":
        from .draft_persistence import DraftPersistenceService, DraftSaveRequest
        path = data_root() / "drafts.json"
        current = json.loads(path.read_text(encoding="utf-8"))
        baseline = dict(current)
        for entry in job["entries"]:
            if entry["id"] in current and digest(current[entry["id"]]) != entry["hash"]:
                raise ValueError("storage.error.changed")
        for entry in job["entries"]:
            current.pop(entry["id"], None)
        result = DraftPersistenceService(path).save(DraftSaveRequest(0, current, baseline))
        if not result.success:
            raise ValueError("storage.error.write")
        completed["count"] = len(job["entries"])
    elif operation == "compact":
        database = {"assistant": data_root() / "assistant" / "assistant.sqlite3",
                    "library": data_root() / "library" / "library.sqlite3",
                    "exports": data_root() / "exports" / "exports.sqlite3"}[kind]
        if not database.is_file() or not plain(database):
            raise ValueError("storage.error.database")
        if shutil.disk_usage(database.parent).free < database.stat().st_size * 2:
            raise ValueError("storage.error.space")
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as db:
            db.set_progress_handler(lambda: int(_cancelled.get()()), 10000)
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("storage.error.database")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
    elif operation == "clean":
        current = {entry["id"]: entry for entry in service.clean_candidates(kind)}
        for entry in job["entries"]:
            check_cancelled()
            if entry["id"] not in current:
                raise ValueError("storage.error.changed")
            if "stamp" in entry and entry["stamp"] != current[entry["id"]]["stamp"]:
                raise ValueError("storage.error.changed")
        for entry in job["entries"]:
            check_cancelled()
            path = Path(entry["id"])
            check_plain_path(path)
            if kind in TEMP_KEYS:
                with file_lock(path / "owner.lock"):
                    if json.loads((path / "owner.json").read_text(encoding="utf-8"))["token"] != entry["token"]:
                        raise ValueError("storage.error.changed")
                shutil.rmtree(path)
            else:
                if not plain(path) or stamp(path) != entry["stamp"]:
                    raise ValueError("storage.error.changed")
                path.unlink()
            completed["count"] += 1
    else:
        raise ValueError("storage.error.operation")


def execute_pending(*, phase="data", cancelled=lambda: False, progress=lambda kind: None):
    _cancelled.set(cancelled)
    for job in read_settings()["jobs"]:
        if cancelled():
            break
        if (job["operation"] == "migrate") != (phase == "locations"):
            continue
        completed = {"count": 0}
        try:
            progress(job["kind"])
            _execute(job, completed)
            result = {"kind": job["kind"], "success": True, "completed": completed["count"],
                      "time": datetime.now().isoformat(timespec="seconds")}
        except Exception as error:
            result = {"kind": job["kind"], "success": False,
                      "error": "storage.cancelled" if cancelled() else str(error), "completed": completed["count"],
                      "time": datetime.now().isoformat(timespec="seconds")}
            if job["operation"] in ("migrate", "backup") and str(error) != "storage.error.installation":
                target = Path(job["target"])
                stage = _copy_stage(target)
                for remaining in (target, stage):
                    if remaining.exists() and read_settings()["locations"].get(job["kind"]) != str(remaining):
                        update_settings(lambda value: value["retired"].append({"kind": job["kind"], "source": str(remaining),
                                        "target": job.get("source", ""), "files": {}, "review": True}))
        result.update(operation=job["operation"], source=job.get("source", ""), target=job.get("target", ""))
        def finish(value):
            if result.get("error") != "storage.error.installation":
                value["jobs"] = [pending for pending in value["jobs"] if pending["id"] != job["id"]]
            value["results"] = (value["results"] + [result])[-20:]
        update_settings(finish)
    days = read_settings()["limits"].get("backup_days", 0)
    if days and phase == "data" and not cancelled():
        from .photo_library_store import PhotoLibraryStore
        try:
            backups = sorted((data_root() / "library").glob("before-restore-*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
            for path in backups[1:]:
                check_cancelled()
                if plain(path) and path.stat().st_mtime < time.time() - days * 86400:
                    PhotoLibraryStore.inspect_backup(path)
                    path.unlink()
        except Exception as error:
            result = {"kind": "backups", "success": False, "error": str(error), "time": datetime.now().isoformat(timespec="seconds")}
            update_settings(lambda value: value.update(results=(value["results"] + [result])[-20:]))
