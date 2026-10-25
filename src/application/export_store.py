from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from string import Formatter

from ..core.image_resize import ResizeOptions
from ..core.output_formats import output_format, validate_quality


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExportPreset:
    directory: str = ""
    format: str = "jpg"
    quality: int = 95
    width: int | None = None
    height: int | None = None
    mode: str = "contain"
    pattern: str = "{stem}_edited"

    def validate(self):
        if not isinstance(self.directory, str) or not self.directory or not Path(self.directory).is_dir():
            raise ValueError("export_queue.directory_required")
        info = output_format(self.format)
        validate_quality(self.quality)
        ResizeOptions(self.width, self.height, self.mode)
        if not isinstance(self.pattern, str) or not self.pattern or len(self.pattern) > 160:
            raise ValueError("error.export.filename")
        try:
            for _, field, spec, conversion in Formatter().parse(self.pattern):
                if field is not None and (field not in ("stem", "folder", "index") or spec or conversion):
                    raise ValueError("error.export.filename")
            self.filename(Path("photos") / "photo.jpg", 1)
        except (KeyError, ValueError) as error:
            raise ValueError("error.export.filename") from error
        return info

    def filename(self, source, index):
        name = self.pattern.format(stem=source.stem, folder=source.parent.name, index=f"{index:04d}")
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if (not name or name in (".", "..") or name[-1] in " ." or len(name) > 220
                or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
                or name.split(".")[0].upper() in reserved):
            raise ValueError("error.export.filename")
        return name + "." + output_format(self.format).extension

    def specification(self):
        info = output_format(self.format)
        return {"format": info.extension, "quality": self.quality if info.supports_quality else None,
                "width": self.width, "height": self.height, "mode": self.mode,
                "bit_depth": info.bit_depth, "alpha": info.supports_alpha, "encoding": 1}

    def to_dict(self):
        return asdict(self)


class ExportStore:
    def __init__(self, directory=None):
        from .storage_paths import data_root
        self.directory = Path(directory) if directory is not None else data_root() / "exports"

    def _connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.directory / "exports.sqlite3", timeout=10)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("export_queue.store_version")
            if version == 0:
                with connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS presets (name TEXT PRIMARY KEY, settings TEXT NOT NULL)")
                    connection.execute("CREATE TABLE IF NOT EXISTS completed_exports (id TEXT PRIMARY KEY, completed_at TEXT NOT NULL, record TEXT NOT NULL)")
                    connection.execute("PRAGMA user_version=1")
            return connection
        except Exception:
            connection.close()
            raise

    def load(self):
        connection = self._connect()
        try:
            presets = {name: json.loads(settings) for name, settings in connection.execute("SELECT name, settings FROM presets ORDER BY name")}
            records = [json.loads(row[0]) for row in connection.execute("SELECT record FROM completed_exports ORDER BY completed_at DESC LIMIT 500")]
            return {"presets": presets, "records": records}
        finally:
            connection.close()

    def save_preset(self, name, preset):
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("export_queue.preset_name")
        preset.validate()
        connection = self._connect()
        try:
            with connection:
                connection.execute("INSERT INTO presets(name,settings) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET settings=excluded.settings",
                                   (name.strip(), canonical(preset.to_dict())))
        finally:
            connection.close()

    def delivery_facts(self, paths, *, is_cancelled=lambda: False):
        """按浏览键返回各照片的 (来源哈希, 工作流哈希)；记录里的来源路径按同一文件判定，在后台调用。"""
        from concurrent.futures import CancelledError
        from .image_source_service import path_key
        files = {}
        for path in paths:
            if is_cancelled():
                raise CancelledError()
            files.setdefault(path_key(path), []).append(os.path.normcase(os.path.abspath(path)))
        facts = {key: set() for keys in files.values() for key in keys}
        resolved = {}
        connection = self._connect()
        try:
            for row in connection.execute("SELECT record FROM completed_exports"):
                if is_cancelled():
                    raise CancelledError()
                record = json.loads(row[0])
                source = record["source_path"]
                if source not in resolved:
                    resolved[source] = path_key(source)
                for key in files.get(resolved[source], ()):
                    facts[key].add((record["source_hash"], record["workflow_hash"]))
            return facts
        finally:
            connection.close()

    def record(self, record):
        connection = self._connect()
        try:
            with connection:
                connection.execute("INSERT INTO completed_exports(id,completed_at,record) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
                                   (record["id"], record["completed_at"], canonical(record)))
        finally:
            connection.close()

    def library_records(self, *, is_cancelled=lambda: False):
        connection = self._connect()
        try:
            for row in connection.execute("SELECT record FROM completed_exports"):
                if is_cancelled():
                    raise ValueError("error.cancelled")
                yield json.loads(row[0])
        finally:
            connection.close()

    @staticmethod
    def verify(record, *, is_cancelled=lambda: False):
        from .services import hash_file
        path = Path(record["target_path"])
        try:
            before = path.stat()
            if before.st_size != record["product_size"]:
                return "changed"
            digest = hash_file(str(path), is_cancelled=is_cancelled)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return "changed"
            return "verified" if digest == record["product_hash"] else "changed"
        except FileNotFoundError:
            return "missing"

