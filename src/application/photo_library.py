from __future__ import annotations

import copy
import os
from threading import RLock

from .photo_library_store import path_key, validate_rules


LIBRARY_TOOLS = ("get_photo_library", "register_library_photos", "edit_library_metadata",
                 "manage_library_keyword", "manage_photo_collection", "preview_photo_collection",
                 "show_photo_collection", "find_missing_photos", "prepare_photo_relocation",
                 "confirm_photo_relocation", "cancel_photo_relocation", "backup_photo_library",
                 "inspect_library_backup", "restore_photo_library")


class PhotoLibraryService:
    def __init__(self, store, sources, browser):
        self.store, self.sources, self.browser = store, sources, browser
        self.photos, self.keywords, self.collections = {}, {}, {}
        self.by_path, self.availability = {}, {}
        self.pending = []
        self.aliases = []
        self.loaded = False
        self.error = ""
        self.revision = 0
        self.delivery_ready = False
        self.delivery_revision = 0
        self._display, self._keys = {}, {}
        self._snapshot = None
        self._lock = RLock()
        self.active_collection = ""
        self.library_view = False
        self._evaluated = None
        self._dynamic = {}
        self._dynamic_cursor = 0

    @property
    def scope(self):
        return "library:" + str(self.revision)

    @staticmethod
    def _delivery_inputs(photos):
        return {identity: (photo["current_path"], photo["file_hash"], tuple(photo["deliveries"]))
                for identity, photo in photos.items()}

    def install(self, snapshot):
        with self._lock:
            if self.loaded and self._snapshot == snapshot:
                return False
            first = not self.loaded
            previous = self._delivery_inputs(self.photos)
            self._snapshot = snapshot
            self.photos = {p["photo_id"]: p for p in snapshot["photos"]}
            self.by_path = {p["path_key"]: p["photo_id"] for p in self.photos.values() if p["path_key"]}
            self._display = {}
            for photo in self.photos.values():
                if photo["path_key"]:
                    self._display[os.path.normcase(os.path.abspath(photo["current_path"]))] = photo["photo_id"]
            self.keywords = {p["keyword_id"]: p for p in snapshot["keywords"]}
            self.collections = {p["collection_id"]: p for p in snapshot["collections"]}
            self.pending = snapshot["pending"]
            self.aliases = snapshot["aliases"]
            self.loaded, self.error = True, ""
            self.availability.clear()
            self.revision += 1
            # 评分、标记、关键词不影响编辑与交付状态，不重算动态状态
            delivery_changed = first or previous != self._delivery_inputs(self.photos)
            if delivery_changed:
                self.delivery_revision += 1
        if delivery_changed:
            self.reset_dynamic()
        else:
            self.invalidate()
        return True

    def set_delivery_ready(self, ready):
        if ready != self.delivery_ready:
            self.delivery_ready = ready
            self.delivery_revision += 1
        self.reset_dynamic()

    def invalidate(self):
        self._evaluated = None

    def reset_dynamic(self):
        self._dynamic.clear()
        self._dynamic_cursor = 0
        self.invalidate()

    def update_dynamic(self, limit=24):
        if self._dynamic_cursor >= len(self.photos):
            return False
        ids = tuple(self.photos)
        changed = False
        for identity in ids[self._dynamic_cursor:self._dynamic_cursor + limit]:
            photo = self.photos[identity]
            state = {"edited": None, "delivery": "unknown"}
            if self.availability.get(identity) == "available" and photo["current_path"]:
                current = self.browser.photo_state(photo["current_path"])
                source = self.browser.source_identity(photo["current_path"])
                source_changed = source is not None and source.file_hash != photo["file_hash"]
                if source_changed:
                    self.availability[identity] = "changed"
                if not current["error"] and not source_changed:
                    state["edited"] = current["edited"]
                if (not current["error"] and not source_changed and current["workflow_hash"] and self.browser.exports is not None and self.browser.exports.loaded
                        and not self.browser.exports.store_error and self.delivery_ready):
                    hashes = photo["deliveries"]
                    state["delivery"] = "recorded" if current["workflow_hash"] in hashes else "modified" if hashes else "unrecorded"
            if self._dynamic.get(identity) != state:
                self._dynamic[identity] = state
                changed = True
        self._dynamic_cursor += limit
        if changed:
            self.invalidate()
        return changed

    def install_paths(self, paths, *, replace=False, retained=()):
        with self._lock:
            if replace:
                self._keys = {path: key for path, key in self._keys.items() if path in retained}
            self._keys.update((os.path.normcase(os.path.abspath(path)), key) for path, key in paths)

    def _identity_for_path(self, path):
        if not path:
            return None
        display = os.path.normcase(os.path.abspath(path))
        identity = self._display.get(display)
        if identity is not None:
            return identity
        return self.by_path.get(self._keys.get(display, display))

    def photo_for_path(self, path):
        with self._lock:
            return copy.deepcopy(self.photos.get(self._identity_for_path(path)))

    def metadata_for_path(self, path):
        """已登记照片的 (评分, 标记)，未登记为 None；只读，供高频绘制与筛选。"""
        with self._lock:
            photo = self.photos.get(self._identity_for_path(path))
            return (photo["rating"], photo["mark"]) if photo is not None else None

    def project_source(self, old_path, digest):
        with self._lock:
            ids = {a["photo_id"] for a in self.aliases if a["file_hash"] == digest and path_key(a["old_path"]) == path_key(old_path)}
            matches = {self.photos[p]["current_path"] for p in ids if p in self.photos and self.photos[p]["current_path"]}
            return next(iter(matches)) if len(matches) == 1 else None

    def versions(self, scope, ids):
        if not self.loaded:
            raise ValueError("library.unavailable" if self.error else "library.loading")
        if scope != self.scope or len(ids) != len(set(ids)) or any(p not in self.photos for p in ids):
            raise ValueError("library.expired")
        blocked = {row["photo_id"] for row in self.pending}
        if blocked.intersection(ids):
            raise ValueError("library.recovery_required")
        return {identity: self.photos[identity]["revision"] for identity in ids}

    def capture_registration(self, browser_scope, references, metadata):
        paths = self.browser.resolve(browser_scope, references) if references is not None else self.browser.selected
        if browser_scope != self.browser.scope or not paths:
            raise ValueError("browser.scope_expired")
        legacy, fixed = {}, {}
        for path in paths:
            key, values = path_key(path), metadata.legacy_values(path)
            if key in legacy and legacy[key] != values and self.photo_for_path(path) is None:
                raise ValueError("library.legacy_conflict")
            legacy[key] = values
            fixed.setdefault(key, path)
        return tuple(fixed.values()), legacy

    def register(self, paths, legacy, *, new_content=False, cancelled=lambda: False):
        identities = [self.sources.identity(path, is_cancelled=cancelled) for path in paths]
        if any(not self.sources.matches_file(source) for source in identities):
            raise ValueError("error.source.changed")
        return self.store.register(tuple(zip(paths, identities)), legacy, new_content=new_content, cancelled=cancelled)

    def scan(self, photos, cancelled, *, limit=128, hash_budget=256 * 1024 ** 2):
        """核验一块照片的可用性：文件标记与登记时一致即可用，变化才完整哈希。

        返回 (状态, 需刷新的文件标记, 未处理的照片)；每块最多 limit 张或累计哈希 hash_budget 字节。"""
        states, stamps, hashed = {}, {}, 0
        for index, photo in enumerate(photos):
            if cancelled():
                raise ValueError("error.cancelled")
            if index >= limit or hashed >= hash_budget:
                return states, stamps, photos[index:]
            path = photo["current_path"]
            try:
                stat = os.stat(path) if path else None
            except OSError:
                stat = None
            if stat is None or not os.path.isfile(path):
                states[photo["photo_id"]] = "missing"
                continue
            if (stat.st_mtime_ns, stat.st_size) == (photo["mtime_ns"], photo["size"]):
                states[photo["photo_id"]] = "available"
                continue
            try:
                source = self.sources.identity(path, is_cancelled=cancelled)
            except OSError:
                states[photo["photo_id"]] = "missing"
                continue
            hashed += source.size
            if source.file_hash == photo["file_hash"]:
                states[photo["photo_id"]] = "available"
                stamps[photo["photo_id"]] = (photo["file_hash"], source.mtime_ns, source.size)
            else:
                states[photo["photo_id"]] = "changed"
        return states, stamps, []

    def apply_scan(self, scanned, states, stamps):
        """把一块核验结果并入投影；核验期间换了路径或内容的照片不采用。"""
        with self._lock:
            for photo in scanned:
                current = self.photos.get(photo["photo_id"])
                if (current is None or photo["photo_id"] not in states
                        or (current["current_path"], current["file_hash"]) != (photo["current_path"], photo["file_hash"])):
                    continue
                stamp = stamps.get(photo["photo_id"])
                if stamp is not None:
                    current["mtime_ns"], current["size"] = stamp[1], stamp[2]
                elif (current["mtime_ns"], current["size"]) != (photo["mtime_ns"], photo["size"]):
                    continue
                self.availability[photo["photo_id"]] = states[photo["photo_id"]]
        self.reset_dynamic()

    def state(self, photo):
        if any(row["photo_id"] == photo["photo_id"] for row in self.pending):
            return {"edited": None, "delivery": "unknown", "availability": "unknown"}
        availability = self.availability.get(photo["photo_id"], "unknown")
        return {**self._dynamic.get(photo["photo_id"], {"edited": None, "delivery": "unknown"}), "availability": availability}

    def evaluate(self, rules):
        rules = validate_rules(rules)
        rule_keywords = set(rules.get("keywords", [])) | set(rules.get("exclude_keywords", []))
        unknown_keyword = bool(rule_keywords - self.keywords.keys())
        matched, pending = [], []
        for identity, photo in self.photos.items():
            static = {"rating": photo["rating"], "mark": photo["mark"],
                      "kind": "raw" if self.sources.supports_quick_preview(photo["last_known_path"]) else "image"}
            if any(static[key] not in rules[key] for key in static if key in rules):
                continue
            keys, include, exclude = set(photo["keywords"]), set(rules.get("keywords", [])), set(rules.get("exclude_keywords", []))
            if exclude & keys or not unknown_keyword and include and (not include <= keys if rules.get("keyword_mode", "all") == "all" else not include & keys):
                continue
            state = self.state(photo)
            unknown = unknown_keyword
            rejected = False
            for key in ("edited", "delivery", "availability"):
                if key in rules:
                    if state[key] is None or state[key] == "unknown":
                        unknown = True
                    elif state[key] not in rules[key]:
                        rejected = True
            if not rejected:
                (pending if unknown else matched).append(identity)
        return {"photos": matched, "pending": pending, "count": len(matched), "pending_count": len(pending)}

    def collection_result(self, identity):
        collection = self.collections.get(identity)
        if collection is None:
            raise ValueError("library.expired")
        if collection["kind"] == "smart":
            return self.evaluate(collection["rules"])
        return {"photos": list(collection["members"]), "pending": [], "count": len(collection["members"]), "pending_count": 0}

    def allowed_paths(self):
        if not self.library_view:
            return None
        if self._evaluated is None:
            if self.active_collection and self.active_collection not in self.collections:
                self.active_collection = ""
            ids = self.collection_result(self.active_collection)["photos"] if self.active_collection else self.photos.keys()
            blocked = {row["photo_id"] for row in self.pending}
            self._evaluated = frozenset(os.path.normcase(os.path.abspath(self.photos[p]["current_path"])) for p in ids
                if self.photos[p]["current_path"] and self.availability.get(p) == "available" and p not in blocked)
        return self._evaluated

    def browse_paths(self):
        collection = self.collections.get(self.active_collection)
        ids = collection["members"] if collection is not None and collection["kind"] == "ordinary" else self.photos.keys()
        return tuple(self.photos[p]["current_path"] for p in ids if p in self.photos and self.photos[p]["current_path"])

    def query(self, *, offset=0, limit=100):
        ids = list(self.photos)
        return {"scope": self.scope, "loaded": self.loaded, "error": self.error, "total": len(ids),
                "photos": [{**copy.deepcopy(self.photos[p]), **self.state(self.photos[p])} for p in ids[offset:offset + limit]],
                "has_more": offset + limit < len(ids), "keywords": list(self.keywords.values()),
                "collections": list(self.collections.values()), "recovery": [{"operation": p["operation_id"], "photo": p["photo_id"]} for p in self.pending]}
