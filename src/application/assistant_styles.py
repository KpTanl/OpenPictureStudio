from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time

from .assistant_models import MAX_ATTACHMENT_BYTES, new_id
from .storage_paths import file_lock


class AssistantStyles:
    def __init__(self, database, directory):
        self.database = database
        self.directory = Path(directory) / "styles"
        self.directory.mkdir(parents=True, exist_ok=True)
        with database:
            database.execute("CREATE TABLE IF NOT EXISTS styles (id TEXT PRIMARY KEY, updated REAL NOT NULL, value TEXT NOT NULL)")
        self._records = {}
        self._data_version = None
        self.prune()

    def _refresh(self):
        version = self.database.execute("PRAGMA data_version").fetchone()[0]
        if version != self._data_version:
            self._records = {identity: json.loads(value) for identity, value in self.database.execute(
                "SELECT id,value FROM styles ORDER BY updated DESC")}
            self._data_version = version

    @contextmanager
    def _mutation(self):
        with file_lock(self.directory / "assets.lock", blocking=True):
            self._refresh()
            yield

    def records(self):
        self._refresh()
        return copy.deepcopy(list(self._records.values()))

    def view_records(self):
        self._refresh()
        return [{key: copy.deepcopy(record[key]) for key in ("id", "name", "description", "status", "version")}
                | {"samples": [{key: copy.deepcopy(sample[key]) for key in (
                    "id", "scene", "thumbnails", "delta", "feedback", "before_revision", "result_revision", "source")}
                    for sample in record["samples"]]} for record in self._records.values()]

    def get(self, identity, *, active=False):
        self._refresh()
        record = self._records.get(identity)
        if not record:
            raise ValueError("AI_STYLE_MISSING")
        if active and record["status"] != "active":
            raise ValueError("AI_STYLE_DISABLED")
        return copy.deepcopy(record)

    def _write(self, record):
        with self.database:
            self.database.execute("INSERT INTO styles VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,value=excluded.value",
                                  (record["id"], time.time(), json.dumps(record, ensure_ascii=False, allow_nan=False)))
        self._data_version = None

    def _check_name(self, name, identity=""):
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("AI_STYLE_NAME")
        if any(record["id"] != identity and record["name"].casefold() == name.casefold() for record in self._records.values()):
            raise ValueError("AI_STYLE_NAME")
        return name

    def _checked(self, identity, version):
        record = self.get(identity)
        if record["version"] != version:
            raise ValueError("AI_STYLE_CHANGED")
        return record

    def save_sample(self, sample, assets, *, name, description="", style_id="", version=None):
        with self._mutation():
            record = self._checked(style_id, version) if style_id else {
                "id": new_id(), "name": self._check_name(name), "description": description.strip()[:2000],
                "status": "active", "version": 0, "samples": []}
            sample = copy.deepcopy(sample)
            sample["id"] = new_id()
            identities = {key: hashlib.sha256(data).hexdigest() for key, data in assets.items()}
            unique = {identities[key]: data for key, data in assets.items()}
            occupied = sum(path.stat().st_size for path in self.directory.glob("*.jpg"))
            additional = sum(len(data) for identity, data in unique.items() if not (self.directory / (identity + ".jpg")).exists())
            if occupied + additional > MAX_ATTACHMENT_BYTES:
                raise ValueError("AI_STYLE_ASSET_LIMIT")
            created = []
            try:
                for identity, data in unique.items():
                    target = self.directory / (identity + ".jpg")
                    if target.exists():
                        continue
                    temporary = self.directory / (identity + "." + new_id() + ".tmp")
                    try:
                        temporary.write_bytes(data)
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
                    created.append(target)
                sample["assets"] = identities
                record["samples"].append(sample)
                record["version"] += 1
                self._write(record)
            except Exception:
                for path in created:
                    path.unlink(missing_ok=True)
                raise
            return record, sample["id"]

    def edit(self, identity, version, *, name, description, status, scenes):
        with self._mutation():
            record = self._checked(identity, version)
            if status not in ("active", "disabled") or len(description) > 2000:
                raise ValueError("AI_STYLE_ARGUMENTS")
            record.update(name=self._check_name(name, identity), description=description.strip(), status=status)
            for sample in record["samples"]:
                if sample["id"] in scenes:
                    sample["scene"] = str(scenes[sample["id"]]).strip()[:240]
            record["version"] += 1
            self._write(record)

    def remove(self, identity, version, sample_id=""):
        with self._mutation():
            record = self._checked(identity, version)
            if sample_id:
                if not any(sample["id"] == sample_id for sample in record["samples"]):
                    raise ValueError("AI_STYLE_MISSING")
                record["samples"] = [sample for sample in record["samples"] if sample["id"] != sample_id]
                record["version"] += 1
                self._write(record)
            else:
                with self.database:
                    self.database.execute("DELETE FROM styles WHERE id=?", (identity,))
                self._data_version = None
            self._prune()

    def prune(self):
        with self._mutation():
            self._prune()

    def _prune(self):
        self._refresh()
        used = {identity for record in self._records.values() for sample in record["samples"] for identity in sample["assets"].values()}
        for path in self.directory.glob("*.jpg"):
            if path.stem not in used:
                path.unlink()
        for path in self.directory.glob("*.tmp"):
            path.unlink()

    def preferences(self):
        row = self.database.execute("SELECT value FROM settings WHERE key='style_preferences'").fetchone()
        return json.loads(row[0]) if row else []

    def remember_preference(self, text):
        text = text.strip()
        if not 1 <= len(text) <= 2000:
            raise ValueError("AI_STYLE_ARGUMENTS")
        values = self.preferences()
        if not any(item["text"] == text for item in values):
            values.append({"id": new_id(), "text": text})
        with self.database:
            self.database.execute("INSERT OR REPLACE INTO settings VALUES ('style_preferences',?)",
                                  (json.dumps(values, ensure_ascii=False),))

    def forget_preference(self, identity):
        values = [item for item in self.preferences() if item["id"] != identity]
        with self.database:
            self.database.execute("INSERT OR REPLACE INTO settings VALUES ('style_preferences',?)",
                                  (json.dumps(values, ensure_ascii=False),))


def style_catalog(records):
    return [{key: copy.deepcopy(record[key]) for key in ("id", "name", "description", "status", "version")}
            | {"samples": [{"id": sample["id"], "scene": sample["scene"]} for sample in record["samples"]]}
            for record in records]


def mark_style_references(value, identities):
    if isinstance(value, dict):
        if isinstance(value.get("style"), dict) and "id" in value["style"]:
            value["style"]["deleted"] = value["style"]["id"] not in identities
        for item in value.get("saved_styles", ()):
            item["deleted"] = item["id"] not in identities
        for item in value.values():
            mark_style_references(item, identities)
    elif isinstance(value, list):
        for item in value:
            mark_style_references(item, identities)
