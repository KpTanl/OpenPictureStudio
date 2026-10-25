from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import shutil
import unicodedata
import uuid
from contextlib import contextmanager, closing
from pathlib import Path

from .image_source_service import path_key


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def backup_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def name_value(value):
    if not isinstance(value, str):
        raise ValueError("library.invalid_name")
    value = unicodedata.normalize("NFC", value.strip())
    if not value or len(value) > 120 or any(ord(c) < 32 for c in value):
        raise ValueError("library.invalid_name")
    return value


RULE_VALUES = {
    "rating": tuple(range(6)), "mark": ("keep", "reject", "unmarked"),
    "kind": ("raw", "image"), "edited": (True, False),
    "delivery": ("recorded", "unrecorded", "modified"),
    "availability": ("available", "missing", "changed"),
}


def validate_rules(rules):
    if not isinstance(rules, dict) or set(rules) - (RULE_VALUES.keys() | {"keywords", "exclude_keywords", "keyword_mode"}):
        raise ValueError("library.invalid_rules")
    for key, allowed in RULE_VALUES.items():
        if key in rules and (not isinstance(rules[key], list) or not rules[key]
                or any(type(v) is not type(allowed[0]) or v not in allowed for v in rules[key])):
            raise ValueError("library.invalid_rules")
    for key in ("keywords", "exclude_keywords"):
        if key in rules and (not isinstance(rules[key], list) or len(rules[key]) > 100
                             or any(not isinstance(v, str) or not v for v in rules[key])):
            raise ValueError("library.invalid_rules")
    if rules.get("keyword_mode", "all") not in ("any", "all"):
        raise ValueError("library.invalid_rules")
    return json.loads(canonical(rules))


_SCHEMA = (
    "CREATE TABLE photos (photo_id TEXT PRIMARY KEY,current_path TEXT,path_key TEXT UNIQUE,last_known_path TEXT NOT NULL,file_hash TEXT NOT NULL,format TEXT NOT NULL,mtime_ns INTEGER NOT NULL,size INTEGER NOT NULL,rating INTEGER NOT NULL CHECK(rating BETWEEN 0 AND 5),mark TEXT NOT NULL CHECK(mark IN ('keep','reject','unmarked')),revision INTEGER NOT NULL)",
    "CREATE TABLE keywords (keyword_id TEXT PRIMARY KEY,name TEXT NOT NULL,name_key TEXT UNIQUE NOT NULL,revision INTEGER NOT NULL)",
    "CREATE TABLE photo_keywords (photo_id TEXT REFERENCES photos ON DELETE CASCADE,keyword_id TEXT REFERENCES keywords ON DELETE CASCADE,PRIMARY KEY(photo_id,keyword_id))",
    "CREATE TABLE collections (collection_id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('ordinary','smart')),rules_version INTEGER NOT NULL,rules TEXT NOT NULL,revision INTEGER NOT NULL)",
    "CREATE TABLE collection_members (collection_id TEXT REFERENCES collections ON DELETE CASCADE,photo_id TEXT REFERENCES photos ON DELETE CASCADE,position INTEGER NOT NULL,PRIMARY KEY(collection_id,photo_id))",
    "CREATE TABLE legacy_imports (path_key TEXT PRIMARY KEY,photo_id TEXT REFERENCES photos,rating INTEGER NOT NULL,mark TEXT NOT NULL)",
    "CREATE TABLE export_links (photo_id TEXT REFERENCES photos ON DELETE CASCADE,record_id TEXT NOT NULL,workflow_hash TEXT NOT NULL,PRIMARY KEY(photo_id,record_id))",
    "CREATE TABLE relocations (operation_id TEXT PRIMARY KEY,photo_id TEXT REFERENCES photos,stage TEXT NOT NULL,payload TEXT NOT NULL)",
)


class PhotoLibraryStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "library.sqlite3"

    @contextmanager
    def connect(self, cancelled=lambda: False):
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise ValueError("library.store_version")
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                self._create_schema(db)
            yield db
            if cancelled():
                raise ValueError("error.cancelled")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _create_schema(db):
        # 与版本号同在一个写事务里；中断留下的半建库中，定义一致的表沿用
        existing = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall())
        definitions = {statement.split()[2]: statement for statement in _SCHEMA}
        if set(existing) - definitions.keys() or any(existing[name] != sql for name, sql in definitions.items() if name in existing):
            raise ValueError("library.store_version")
        for name, sql in definitions.items():
            if name not in existing:
                db.execute(sql)
        db.execute("PRAGMA user_version=1")

    def snapshot(self):
        with self.connect() as db:
            photos = [dict(row) for row in db.execute("SELECT * FROM photos ORDER BY rowid")]
            keywords = [dict(row) for row in db.execute("SELECT * FROM keywords ORDER BY name_key")]
            collections = [dict(row) for row in db.execute("SELECT * FROM collections ORDER BY rowid")]
            for item in collections:
                item["rules"] = json.loads(item["rules"])
                item["members"] = [r[0] for r in db.execute("SELECT photo_id FROM collection_members WHERE collection_id=? ORDER BY position", (item["collection_id"],))]
            by_id = {row["photo_id"]: row for row in photos}
            for row in photos:
                row["keywords"], row["deliveries"], row["export_records"] = [], [], []
            for row in db.execute("SELECT * FROM photo_keywords"):
                by_id[row["photo_id"]]["keywords"].append(row["keyword_id"])
            for row in db.execute("SELECT * FROM export_links"):
                by_id[row["photo_id"]]["deliveries"].append(row["workflow_hash"])
                by_id[row["photo_id"]]["export_records"].append({"record": row["record_id"], "workflow_hash": row["workflow_hash"]})
            pending = [dict(row) for row in db.execute("SELECT * FROM relocations WHERE stage NOT IN ('completed','rolled_back')")]
            for row in pending:
                row["payload"] = json.loads(row["payload"])
            aliases = [json.loads(row[0]) for row in db.execute("SELECT payload FROM relocations WHERE stage='completed'")]
            return {"photos": photos, "keywords": keywords, "collections": collections, "pending": pending, "aliases": aliases}

    @staticmethod
    def check_rows(db, versions):
        """修订为 None 表示只核对照片仍在：用于以最后一次为准的取值写入，例如用户给定的评分、标记。"""
        for identity, revision in versions.items():
            row = db.execute("SELECT revision FROM photos WHERE photo_id=?", (identity,)).fetchone()
            if row is None or revision is not None and row[0] != revision:
                raise ValueError("library.expired")

    def register(self, sources, legacy, *, new_content=False, cancelled=lambda: False):
        """sources 为 (浏览路径, 来源身份)；显示路径存浏览路径，同一文件以 path_key 判定。"""
        ids = []
        with self.connect(cancelled) as db:
            for path, source in sources:
                if cancelled():
                    raise ValueError("error.cancelled")
                display = os.path.normpath(os.path.abspath(path))
                key = path_key(display)
                if key != path_key(source.path):
                    raise ValueError("error.source.changed")
                row = db.execute("SELECT * FROM photos WHERE path_key=?", (key,)).fetchone()
                if row is not None and row["file_hash"] == source.file_hash:
                    if row["current_path"] != display:
                        db.execute("UPDATE photos SET current_path=?,last_known_path=?,revision=revision+1 WHERE photo_id=?",
                                   (display, display, row["photo_id"]))
                    ids.append(row["photo_id"])
                    continue
                if row is not None:
                    if not new_content:
                        raise ValueError("library.source_changed")
                    db.execute("UPDATE photos SET current_path=NULL,path_key=NULL,revision=revision+1 WHERE photo_id=?", (row["photo_id"],))
                identity = uuid.uuid4().hex
                imported = db.execute("SELECT 1 FROM legacy_imports WHERE path_key=?", (key,)).fetchone()
                rating, mark = legacy.get(key, (0, "unmarked")) if imported is None else (0, "unmarked")
                db.execute("INSERT INTO photos VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                           (identity, display, key, display, source.file_hash, Path(display).suffix.lower(), source.mtime_ns, source.size, rating, mark))
                if imported is None:
                    db.execute("INSERT INTO legacy_imports VALUES(?,?,?,?)", (key, identity, rating, mark))
                ids.append(identity)
        return ids

    def metadata(self, versions, *, rating=None, mark=None, add=(), remove=(), cancelled=lambda: False):
        if rating is not None and (type(rating) is not int or not 0 <= rating <= 5):
            raise ValueError("browser.invalid_rating")
        if mark is not None and mark not in ("keep", "reject", "unmarked"):
            raise ValueError("browser.invalid_mark")
        with self.connect(cancelled) as db:
            self.check_rows(db, versions)
            for keyword in (*add, *remove):
                if db.execute("SELECT 1 FROM keywords WHERE keyword_id=?", (keyword,)).fetchone() is None:
                    raise ValueError("library.expired")
            for identity in versions:
                if rating is not None:
                    db.execute("UPDATE photos SET rating=? WHERE photo_id=?", (rating, identity))
                if mark is not None:
                    db.execute("UPDATE photos SET mark=? WHERE photo_id=?", (mark, identity))
                for keyword in add:
                    db.execute("INSERT OR IGNORE INTO photo_keywords VALUES(?,?)", (identity, keyword))
                for keyword in remove:
                    db.execute("DELETE FROM photo_keywords WHERE photo_id=? AND keyword_id=?", (identity, keyword))
                db.execute("UPDATE photos SET revision=revision+1 WHERE photo_id=?", (identity,))

    def keyword(self, action, *, name="", keyword="", revision=None, count=None, cancelled=lambda: False):
        with self.connect(cancelled) as db:
            if action == "create":
                name = name_value(name)
                key = name.casefold()
                row = db.execute("SELECT keyword_id FROM keywords WHERE name_key=?", (key,)).fetchone()
                identity = row[0] if row else uuid.uuid4().hex
                if row is None:
                    db.execute("INSERT INTO keywords VALUES(?,?,?,1)", (identity, name, key))
                return identity
            row = db.execute("SELECT revision FROM keywords WHERE keyword_id=?", (keyword,)).fetchone()
            if row is None or row[0] != revision:
                raise ValueError("library.expired")
            if action == "rename":
                name = name_value(name)
                if db.execute("SELECT 1 FROM keywords WHERE name_key=? AND keyword_id<>?", (name.casefold(), keyword)).fetchone():
                    raise ValueError("library.name_exists")
                db.execute("UPDATE keywords SET name=?,name_key=?,revision=revision+1 WHERE keyword_id=?", (name, name.casefold(), keyword))
            elif action == "delete":
                if db.execute("SELECT count(*) FROM photo_keywords WHERE keyword_id=?", (keyword,)).fetchone()[0] != count:
                    raise ValueError("library.expired")
                db.execute("UPDATE photos SET revision=revision+1 WHERE photo_id IN (SELECT photo_id FROM photo_keywords WHERE keyword_id=?)", (keyword,))
                db.execute("DELETE FROM keywords WHERE keyword_id=?", (keyword,))
            else:
                raise ValueError("library.invalid_command")
            return keyword

    def collection(self, action, *, collection="", name="", kind="ordinary", rules=None, members=(), versions=None, revision=None, cancelled=lambda: False):
        with self.connect(cancelled) as db:
            if action in ("create", "rules"):
                rules = validate_rules(rules or {})
                for keyword in (*rules.get("keywords", []), *rules.get("exclude_keywords", [])):
                    if db.execute("SELECT 1 FROM keywords WHERE keyword_id=?", (keyword,)).fetchone() is None:
                        raise ValueError("library.expired")
            if action == "create":
                if kind not in ("ordinary", "smart"):
                    raise ValueError("library.invalid_rules")
                identity = uuid.uuid4().hex
                db.execute("INSERT INTO collections VALUES(?,?,?,1,?,1)", (identity, name_value(name), kind, canonical(validate_rules(rules or {}))))
                return identity
            row = db.execute("SELECT * FROM collections WHERE collection_id=?", (collection,)).fetchone()
            if row is None or revision != row["revision"]:
                raise ValueError("library.expired")
            self.check_rows(db, versions or {})
            if action == "delete":
                db.execute("DELETE FROM collections WHERE collection_id=?", (collection,))
                return collection
            if action == "rename":
                db.execute("UPDATE collections SET name=? WHERE collection_id=?", (name_value(name), collection))
            elif action == "rules" and row["kind"] == "smart":
                db.execute("UPDATE collections SET rules=? WHERE collection_id=?", (canonical(validate_rules(rules)), collection))
            elif action in ("add", "remove", "reorder") and row["kind"] == "ordinary":
                previous = [r[0] for r in db.execute("SELECT photo_id FROM collection_members WHERE collection_id=? ORDER BY position", (collection,))]
                if action == "reorder":
                    if len(members) != len(set(members)) or set(previous) != set(members):
                        raise ValueError("library.expired")
                    ordered = list(members)
                elif action == "add":
                    ordered = [*previous, *(p for p in dict.fromkeys(members) if p not in previous)]
                else:
                    ordered = [p for p in previous if p not in members]
                if any(p not in (versions or {}) for p in members):
                    raise ValueError("library.expired")
                db.execute("DELETE FROM collection_members WHERE collection_id=?", (collection,))
                db.executemany("INSERT INTO collection_members VALUES(?,?,?)", ((collection, p, n) for n, p in enumerate(ordered)))
            else:
                raise ValueError("library.invalid_command")
            db.execute("UPDATE collections SET revision=revision+1 WHERE collection_id=?", (collection,))
            return collection

    def refresh_stamps(self, stamps, cancelled=lambda: False):
        """核验确认内容未变后刷新文件标记；不递增资料修订。"""
        with self.connect(cancelled) as db:
            for identity, (file_hash, mtime_ns, size) in stamps.items():
                db.execute("UPDATE photos SET mtime_ns=?,size=? WHERE photo_id=? AND file_hash=?",
                           (mtime_ns, size, identity, file_hash))

    def link_exports(self, records, cancelled=lambda: False):
        with self.connect(cancelled) as db:
            for record in records:
                row = db.execute("SELECT photo_id,file_hash FROM photos WHERE path_key=?", (path_key(record["source_path"]),)).fetchone()
                if row is not None and row["file_hash"] == record["source_hash"]:
                    db.execute("INSERT OR IGNORE INTO export_links VALUES(?,?,?)", (row["photo_id"], record["id"], record["workflow_hash"]))

    def prepare_relocation(self, photo, payload):
        identity = uuid.uuid4().hex
        with self.connect() as db:
            self.check_rows(db, {photo["photo_id"]: photo["revision"]})
            if db.execute("SELECT 1 FROM relocations WHERE photo_id=? AND stage='prepared'", (photo["photo_id"],)).fetchone():
                raise ValueError("library.recovery_required")
            if db.execute("SELECT 1 FROM photos WHERE path_key=? AND photo_id<>?", (path_key(payload["new_path"]), photo["photo_id"])).fetchone():
                raise ValueError("library.target_occupied")
            db.execute("INSERT INTO relocations VALUES(?,?,'prepared',?)", (identity, photo["photo_id"], canonical(payload)))
        return identity

    def finish_relocation(self, operation, *, rollback=False, identity=None, cancelled=lambda: False):
        with self.connect(cancelled) as db:
            row = db.execute("SELECT * FROM relocations WHERE operation_id=?", (operation,)).fetchone()
            if row is None or row["stage"] != "prepared":
                raise ValueError("library.expired")
            data = json.loads(row["payload"])
            if not rollback:
                if identity is not None:
                    if identity.file_hash != data["file_hash"] or path_key(identity.path) != path_key(data["new_path"]):
                        raise ValueError("error.source.changed")
                    data["mtime_ns"], data["size"] = identity.mtime_ns, identity.size
                self.check_rows(db, {row["photo_id"]: data["revision"]})
                db.execute("UPDATE photos SET current_path=?,path_key=?,last_known_path=?,mtime_ns=?,size=?,revision=revision+1 WHERE photo_id=?",
                           (data["new_path"], path_key(data["new_path"]), data["new_path"], data["mtime_ns"], data["size"], row["photo_id"]))
            summary = {} if rollback else {"old_path": data["old_path"], "photo_id": row["photo_id"], "file_hash": data["file_hash"]}
            db.execute("UPDATE relocations SET stage=?,payload=? WHERE operation_id=?", ("rolled_back" if rollback else "completed", canonical(summary), operation))

    @staticmethod
    def inspect_backup(path):
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1 or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("library.invalid_backup")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("library.invalid_backup")
            expected = {statement.split()[2] for statement in _SCHEMA}
            actual = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if actual != expected:
                raise ValueError("library.invalid_backup")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone():
                raise ValueError("library.invalid_backup")
            definitions = {statement.split()[2]: statement for statement in _SCHEMA}
            for table, sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"):
                if sql != definitions[table]:
                    raise ValueError("library.invalid_backup")
            reference = sqlite3.connect(":memory:")
            try:
                for statement in _SCHEMA:
                    reference.execute(statement)
                for table in expected:
                    if db.execute(f"PRAGMA table_info({table})").fetchall() != reference.execute(f"PRAGMA table_info({table})").fetchall():
                        raise ValueError("library.invalid_backup")
            finally:
                reference.close()
            if db.execute("SELECT 1 FROM relocations WHERE stage NOT IN ('completed','rolled_back')").fetchone():
                raise ValueError("library.recovery_required")
            for row in db.execute("SELECT rules,rules_version FROM collections"):
                if row[1] != 1:
                    raise ValueError("library.invalid_backup")
                validate_rules(json.loads(row[0]))
            for row in db.execute("SELECT photo_id,file_hash,rating,mark,revision,size,mtime_ns,current_path,path_key FROM photos"):
                if (len(row[0]) != 32 or any(c not in "0123456789abcdef" for c in row[0])
                        or len(row[1]) != 64 or any(c not in "0123456789abcdef" for c in row[1])
                        or type(row[2]) is not int or not 0 <= row[2] <= 5 or row[3] not in ("keep", "reject", "unmarked")
                        or any(type(row[i]) is not int or row[i] < 0 for i in (4, 5, 6))
                        or (row[7] is None) != (row[8] is None)
                        or row[7] is not None and (
                            any(not isinstance(value, str) or not os.path.isabs(value) or "\0" in value
                                for value in row[7:9])
                            or row[8] != os.path.normcase(os.path.normpath(row[8])))):
                    raise ValueError("library.invalid_backup")
            for name, key in db.execute("SELECT name,name_key FROM keywords"):
                if name_value(name) != name or name.casefold() != key:
                    raise ValueError("library.invalid_backup")
            for stage, payload in db.execute("SELECT stage,payload FROM relocations"):
                data = json.loads(payload)
                if stage not in ("completed", "rolled_back") or not isinstance(data, dict):
                    raise ValueError("library.invalid_backup")
                if stage == "completed" and (set(data) != {"old_path", "photo_id", "file_hash"}
                        or any(not isinstance(data[key], str) or not data[key] for key in data)):
                    raise ValueError("library.invalid_backup")
            return {"photos": db.execute("SELECT count(*) FROM photos").fetchone()[0],
                    "collections": db.execute("SELECT count(*) FROM collections").fetchone()[0],
                    "keywords": db.execute("SELECT count(*) FROM keywords").fetchone()[0]}
        except (sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("library.invalid_backup") from error
        finally:
            db.close()

    def backup(self, target, *, cancelled=lambda: False):
        target = Path(target)
        if target.exists() or path_key(str(target)) == path_key(str(self.path)):
            raise ValueError("library.target_occupied")
        with self.connect() as db:
            if db.execute("SELECT 1 FROM relocations WHERE stage='prepared'").fetchone():
                raise ValueError("library.recovery_required")
        temp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(temp)) as destination:
                source.backup(destination)
            self.inspect_backup(temp)
            with open(temp, "r+b") as stream:
                os.fsync(stream.fileno())
            if cancelled():
                raise ValueError("error.cancelled")
            if os.name == "nt":
                os.rename(temp, target)
            else:
                os.link(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return str(target)

    def restore(self, source, *, expected_hash, cancelled=lambda: False):
        if backup_hash(source) != expected_hash or cancelled():
            raise ValueError("library.expired")
        counts = self.inspect_backup(source)
        preserved = self.directory / ("before-restore-" + uuid.uuid4().hex + ".sqlite3")
        self.backup(preserved)
        temp = self.path.with_name(uuid.uuid4().hex + ".tmp")
        try:
            shutil.copyfile(source, temp)
            self.inspect_backup(temp)
            if backup_hash(temp) != expected_hash:
                raise ValueError("library.expired")
            if cancelled():
                raise ValueError("library.expired")
            with open(temp, "r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)
        return {**counts, "preserved": str(preserved)}
