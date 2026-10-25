from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .assistant_models import (AssistantConnection, AssistantModel, MAX_CONVERSATION_ATTACHMENT_BYTES,
                               builtin_reasoning)
from .assistant_images import ImageObservationError
from .assistant_settings import AssistantSettingsStore, _protect


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _encrypted(value) -> str:
    return base64.b64encode(_protect(_json(value).encode("utf-8"))).decode("ascii")


def _decrypted(value: str):
    return json.loads(_protect(base64.b64decode(value, validate=True), decrypt=True).decode("utf-8"))


class AssistantStore:
    """由助手后台循环独占；数据库及附件不包含照片项目状态。"""

    def __init__(self, directory: str):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.attachments = self.directory / "attachments"
        self.attachments.mkdir(exist_ok=True)
        self.database = sqlite3.connect(self.directory / "assistant.sqlite3")
        self.database.execute("PRAGMA foreign_keys=ON")
        self.database.execute("PRAGMA journal_mode=WAL")
        version = self.database.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.database.close()
            raise ValueError("AI_STORE_VERSION")
        with self.database:
            self.database.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.database.execute("CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, value TEXT NOT NULL, secret TEXT NOT NULL)")
            self.database.execute("CREATE TABLE IF NOT EXISTS models (id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id), value TEXT NOT NULL, secret TEXT NOT NULL)")
            self.database.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, updated REAL NOT NULL, value TEXT NOT NULL)")
            self.database.execute("PRAGMA user_version=1")

    def setting(self, key: str, default=None):
        row = self.database.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self.database:
            self.database.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, _json(value)))

    def migrate(self) -> str:
        if self.setting("legacy_migrated", False):
            return ""
        if self.setting("legacy_connection", ""):
            return "AI_KEY_READ_FAILED"
        legacy = AssistantSettingsStore()
        values = legacy.load()
        if values.endpoint or values.model or values.api_key:
            try:
                provider = "deepseek" if urlsplit(values.endpoint).hostname == "api.deepseek.com" else "generic"
            except ValueError:
                provider = "generic"
            connection = AssistantConnection(name="DeepSeek" if provider == "deepseek" else "API",
                                             endpoint=values.endpoint, provider=provider, api_key=values.api_key)
            model = AssistantModel(connection_id=connection.id, model=values.model, name=values.model,
                                   **builtin_reasoning(provider, values.model))
            self.save_configuration(connection, (model,), validate=False)
            self.set_setting("default_model", model.id)
            if legacy.error:
                self.set_setting("legacy_connection", connection.id)
                return legacy.error
        self.set_setting("legacy_migrated", True)
        return legacy.error or ""

    def configurations(self):
        connections, models, errors = {}, {}, []
        overrides = self.setting("tool_calling_overrides", {})
        for identity, value, secret in self.database.execute("SELECT id,value,secret FROM connections"):
            data = json.loads(value)
            try:
                data.update(_decrypted(secret))
            except (ValueError, OSError, UnicodeError):
                errors.append("AI_KEY_READ_FAILED")
            connections[identity] = AssistantConnection(**data)
        for identity, value, secret in self.database.execute("SELECT id,value,secret FROM models"):
            data = json.loads(value)
            try:
                data.update(_decrypted(secret))
            except (ValueError, OSError, UnicodeError):
                errors.append("AI_KEY_READ_FAILED")
            connection = connections[data["connection_id"]]
            supported = builtin_reasoning(connection.provider, data["model"]).get("tool_calling")
            if identity in overrides:
                data["tool_calling"] = overrides[identity]
            elif supported:
                data["tool_calling"] = True
            models[identity] = AssistantModel.from_dict(data)
        return connections, models, errors

    def save_configuration(self, connection: AssistantConnection, models, *, validate=True, tool_calling_overrides=None) -> None:
        if validate:
            connection.validate()
            for model in models:
                if model.model or model.name:
                    model.validate()
                if model.connection_id != connection.id:
                    raise ValueError("AI_CONFIGURATION_INVALID")
        value = asdict(connection)
        secret = {key: value.pop(key) for key in ("api_key", "headers", "proxy")}
        encrypted_connection = _encrypted(secret)
        prepared = []
        for model in models:
            data = asdict(model)
            encrypted_model = _encrypted({"extra_fields": data.pop("extra_fields")})
            prepared.append((model.id, model.connection_id, _json(data), encrypted_model))
        with self.database:
            self.database.execute("INSERT INTO connections VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value,secret=excluded.secret",
                                  (connection.id, _json(value), encrypted_connection))
            for row in prepared:
                self.database.execute("INSERT OR REPLACE INTO models VALUES (?,?,?,?)", row)
            if tool_calling_overrides:
                overrides = {**self.setting("tool_calling_overrides", {}), **tool_calling_overrides}
                self.database.execute("INSERT OR REPLACE INTO settings VALUES ('tool_calling_overrides',?)", (_json(overrides),))

    def delete_connection(self, identity: str) -> None:
        with self.database:
            self.database.execute("DELETE FROM models WHERE connection_id=?", (identity,))
            self.database.execute("DELETE FROM connections WHERE id=?", (identity,))

    def delete_model(self, identity: str) -> None:
        with self.database:
            self.database.execute("DELETE FROM models WHERE id=?", (identity,))

    def conversations(self) -> dict:
        records = {}
        for identity, value in self.database.execute("SELECT id,value FROM conversations ORDER BY updated DESC"):
            record = json.loads(value)
            interrupted = False
            references = [record.get("draft_reference")] + [message.get("reference") for message in record["messages"].values()]
            for reference in references:
                if reference:
                    reference["missing"] = not self.attachment_present(reference["attachment"])
            for message in record["messages"].values():
                for group in message.get("variants", ()):
                    if group["status"] == "pending":
                        group["status"] = "restored"
                        interrupted = True
                    elif group["status"] == "adopting":
                        group["status"] = "invalid"
                        interrupted = True
                run = message.get("run")
                if run and not run.get("reason"):
                    run["reason"] = "interrupted"
                    interrupted = True
                if run:
                    for call in run.get("tool_calls", ()):
                        if call.get("status") == "running":
                            call.update(status="interrupted", result={"ok": False, "error": {"code": "AI_TOOL_INTERRUPTED"}})
                            interrupted = True
                if message.get("status") in ("queued", "preparing", "waiting", "streaming", "summarizing", "previewing", "applying"):
                    message["status"] = "interrupted"
                    interrupted = True
                action = message.get("action")
                if action and action.get("outcome") in ("pending", "ready", "previewing", "applying"):
                    action["outcome"] = "stale"
                    interrupted = True
            if interrupted:
                self.save_conversation(record)
            records[identity] = record
        return records

    def save_conversation(self, record: dict) -> None:
        with self.database:
            self.database.execute("INSERT OR REPLACE INTO conversations VALUES (?,?,?)",
                                  (record["id"], record["updated"], _json(record)))

    def delete_conversation(self, identity: str) -> None:
        with self.database:
            self.database.execute("DELETE FROM conversations WHERE id=?", (identity,))
        self.prune_attachments()

    def attachment_usage(self) -> dict:
        return {"bytes": sum(path.stat().st_size for path in self.attachments.glob("*.jpg")),
                "max_bytes": MAX_CONVERSATION_ATTACHMENT_BYTES}

    def save_attachment(self, data: bytes) -> str:
        return self.save_attachments([data])[0]

    def save_attachments(self, images) -> list[str]:
        images = list(images)
        identities = [hashlib.sha256(data).hexdigest() for data in images]
        unique = dict(zip(identities, images))

        def usage():
            occupied = self.attachment_usage()
            additional = sum(len(data) for identity, data in unique.items()
                             if not (self.attachments / (identity + ".jpg")).exists())
            return {**occupied, "additional_bytes": additional}

        current = usage()
        if current["bytes"] + current["additional_bytes"] > current["max_bytes"]:
            self.prune_attachments()
            current = usage()
        if current["bytes"] + current["additional_bytes"] > current["max_bytes"]:
            raise ImageObservationError("AI_ATTACHMENT_LIMIT", **current)
        for identity, data in unique.items():
            path = self.attachments / (identity + ".jpg")
            if not path.exists():
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(data)
                os.replace(temporary, path)
        return identities

    def attachment(self, identity: str) -> bytes:
        if len(identity) != 64 or any(char not in "0123456789abcdef" for char in identity):
            raise ValueError("AI_ATTACHMENT_MISSING")
        try:
            return (self.attachments / (identity + ".jpg")).read_bytes()
        except FileNotFoundError:
            raise ValueError("AI_ATTACHMENT_MISSING") from None

    def attachment_present(self, identity: str) -> bool:
        return (len(identity) == 64 and all(char in "0123456789abcdef" for char in identity)
                and (self.attachments / (identity + ".jpg")).is_file())

    def attachment_information(self, identity: str) -> dict:
        from PIL import Image
        if not self.attachment_present(identity):
            raise ValueError("AI_ATTACHMENT_MISSING")
        path = self.attachments / (identity + ".jpg")
        with Image.open(path) as image:
            return {"size": list(image.size), "bytes": path.stat().st_size}

    def prune_attachments(self, *, clear=False) -> None:
        used = set()
        if not clear:
            for (value,) in self.database.execute("SELECT value FROM conversations"):
                record = json.loads(value)
                reference = record.get("draft_reference")
                if reference:
                    used.add(reference["attachment"])
                for message in record["messages"].values():
                    used.update(message.get("attachments", ()))
                    used.update(message.get("baseline_attachments", ()))
                    if message.get("reference"):
                        used.add(message["reference"]["attachment"])
                    for group in message.get("variants", ()):
                        used.add(group["before_attachment"])
                        used.add(group.get("reference_attachment", ""))
                        used.update(item["attachment"] for item in group["variants"].values())
        for path in self.attachments.glob("*.jpg"):
            if path.stem not in used:
                path.unlink()

    def close(self) -> None:
        self.database.close()
