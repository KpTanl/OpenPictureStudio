from __future__ import annotations

import os
import logging
import uuid
from dataclasses import asdict, dataclass, replace

from .photo_batch import PhotoSelection


BROWSER_TOOLS = ("get_browser_context", "set_photo_marks", "rate_photos", "filter_photos", "select_photos",
                 "compare_photos", "set_sample_photo", "set_browser_view")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhotoFilter:
    rating: str = "all"
    mark: str = "all"
    kind: str = "all"
    edited: str = "all"
    delivery: str = "all"

    def validate(self):
        if (self.rating not in ("all", "rated", "unrated", "0", "1", "2", "3", "4", "5", "1+", "2+", "3+", "4+", "5+")
                or self.mark not in ("all", "keep", "reject", "unmarked")
                or self.kind not in ("all", "raw", "image")
                or self.edited not in ("all", "edited", "unedited")
                or self.delivery not in ("all", "recorded", "unrecorded", "modified", "unknown")):
            raise ValueError("browser.invalid_filter")


class PhotoBrowserService:
    def __init__(self, manager, metadata, *, is_raw, drafts_ready):
        self.manager, self.metadata = manager, metadata
        self.is_raw, self.drafts_ready = is_raw, drafts_ready
        self.filters = PhotoFilter()
        self.revision = 0
        self.sessions = self.exports = None
        self.raw_mode, self.image_mode = "ops", "original"
        self.selected = ()
        self._results = ()
        self._references = {}
        self._paths = {}
        self._identities = {}
        self._source_errors = {}
        self._states = {}
        self._delivery_facts = {}
        self._list_version = -1
        self.source_generation = 0
        self.library = None
        self._library_revision = None
        self.metadata_command = None

    @staticmethod
    def key(path):
        return os.path.normcase(os.path.abspath(path))

    @property
    def results(self):
        return self._results

    @property
    def scope(self):
        return f"{self.manager.list_version}:{self.revision}"

    def identities(self):
        return frozenset(self._identities)

    def identity_snapshot(self):
        return tuple(self._identities.items())

    def invalidate_sources(self, entries, *, list_version):
        if list_version != self.manager.list_version:
            return False
        changed = False
        for path, identity in entries:
            key = self.key(path)
            if self._identities.get(key) == identity:
                self._identities.pop(key)
                self._states.pop(key, None)
                changed = True
        if changed:
            self.source_generation += 1
            if self.library is not None:
                self.library.reset_dynamic()
        return changed

    def source_identity(self, path):
        return self._identities.get(self.key(path))

    def attach(self, sessions, exports):
        self.sessions, self.exports = sessions, exports
        self.invalidate()

    def workflow(self, path):
        if self.sessions is None or not self.drafts_ready():
            raise ValueError("batch.drafts_loading")
        session = self.sessions.get(path)
        if session is not None:
            state = self.sessions.core.get_session(session.core_id).unwrap()
            return state.draft_workflow, state.active_source.file_hash, state.state_version
        initial = self.sessions.initial_workflow(path, raw_display_mode=self.raw_mode, non_raw_display_mode=self.image_mode)
        drafts = self.sessions.drafts.for_image(os.path.normpath(path))
        return self.sessions.workflow_from_drafts(initial, drafts), "", self.sessions.drafts.version

    def shows_source(self, path):
        """非 RAW 照片的工作流为空时，编辑结果就是源画面，缩略图可直接用源缩略图。"""
        try:
            return not self.is_raw(path) and not self.workflow(path)[0].nodes
        except (OSError, ValueError, KeyError):
            return False

    def photo_state(self, path):
        identity = self.key(path)
        if identity in self._states:
            return self._states[identity]
        edited, delivery, workflow_hash, error_key = None, "unknown", "", self._source_errors.get(identity, "")
        try:
            graph, expected_source, _ = self.workflow(path)
            workflow_hash = graph.workflow_hash()
            baseline = self.sessions.initial_workflow(path, raw_display_mode=self.raw_mode, non_raw_display_mode=self.image_mode)
            edited = workflow_hash != baseline.workflow_hash()
            source = self._identities.get(identity)
            if source is not None and expected_source and expected_source != source.file_hash:
                raise ValueError("error.source.changed")
            if source is not None and self.exports is not None and self.exports.loaded and not self.exports.store_error:
                records = [record for record in self.exports.records if self.key(record["source_path"]) == identity
                           and record["source_hash"] == source.file_hash and self.exports.record_state(record) == "saved"]
                facts = self._delivery_facts.get(identity)
                library_photo = self.library.photo_for_path(path) if self.library is not None else None
                if library_photo is not None:
                    facts = ({(source.file_hash, value) for value in library_photo["deliveries"]}
                             if self.library.delivery_ready and source.file_hash == library_photo["file_hash"] else None)
                same_version = any(record["workflow_hash"] == workflow_hash for record in records)
                if facts is not None:
                    same_version = same_version or (source.file_hash, workflow_hash) in facts
                    delivery = "recorded" if same_version else "modified" if any(item[0] == source.file_hash for item in facts) or records else "unrecorded"
                elif same_version:
                    delivery = "recorded"
        except ValueError as error:
            error_key = str(error)
        except (KeyError, RuntimeError):
            logger.exception("选片状态读取失败")
            error_key = "error.preview.failed"
        value = {"edited": edited, "delivery": delivery, "workflow_hash": workflow_hash, "error": error_key}
        self._states[identity] = value
        return value

    def set_identity(self, path, identity, *, list_version):
        key = self.key(path)
        if (list_version != self.manager.list_version or key not in self._references or self.sessions is None
                or not self.sessions.core.image_sources.matches_file(identity, path=path)):
            return False
        if self._identities.get(key) != identity:
            self._identities[key] = identity
            self._source_errors.pop(key, None)
            self._states.pop(key, None)
            if self.library is not None:
                self.library.reset_dynamic()
            return True
        return False

    def source_failed(self, path):
        key = self.key(path)
        if key not in self._references:
            return
        self._identities.pop(key, None)
        self._source_errors[key] = "browser.source_failed"
        self._states.pop(key, None)

    def invalidate(self, path=None):
        if path:
            self._states.pop(self.key(path), None)
        else:
            self._states.clear()
        return self.refresh()

    def set_delivery_facts(self, facts):
        self._delivery_facts = facts
        self.invalidate()

    def clear_delivery_facts(self):
        self._delivery_facts.clear()
        self._states.clear()

    def refresh(self):
        from ..core.rating_store import matches_rating
        if self._list_version != self.manager.list_version:
            self._list_version = self.manager.list_version
            paths = {}
            for path in self.manager.image_list:
                key = self.key(path)
                reference = self._references.setdefault(key, "photo:" + uuid.uuid4().hex)
                paths[reference] = path
            self._paths = paths
            keys = {self.key(path) for path in paths.values()}
            self._references = {key: reference for key, reference in self._references.items() if key in keys}
            self._identities = {key: value for key, value in self._identities.items() if key in keys}
            self._source_errors = {key: value for key, value in self._source_errors.items() if key in keys}
            self._states.clear()
        if self.library is not None and self.library.delivery_revision != self._library_revision:
            self._library_revision = self.library.delivery_revision
            self._states.clear()
        rule = self.filters
        library_paths = self.library.allowed_paths() if self.library is not None else None
        result, seen = [], set()
        for path in self.manager.image_list:
            key = self.key(path)
            if key in seen:
                continue
            seen.add(key)
            if library_paths is not None and key not in library_paths:
                continue
            if not matches_rating(self.metadata.get_rating(path), rule.rating):
                continue
            if rule.mark != "all" and self.metadata.get_mark(path) != rule.mark:
                continue
            if rule.kind != "all" and self.is_raw(path) != (rule.kind == "raw"):
                continue
            if rule.edited != "all" or rule.delivery != "all":
                state = self.photo_state(path)
                if rule.edited != "all" and state["edited"] is not (rule.edited == "edited"):
                    continue
                if rule.delivery != "all" and state["delivery"] != rule.delivery:
                    continue
            result.append(path)
        results = tuple(result)
        selected_keys = {self.key(item) for item in self.selected}
        selected = tuple(path for path in results if self.key(path) in selected_keys)
        changed = results != self._results or selected != self.selected
        self._results, self.selected = results, selected
        if changed:
            self.revision += 1
        return changed

    def set_filter(self, values):
        rule = replace(self.filters, **values)
        rule.validate()
        if rule != self.filters:
            self.filters = rule
            self.revision += 1
            self.refresh()

    def select(self, paths):
        keys = {self.key(path) for path in paths}
        selected = tuple(path for path in self.results if self.key(path) in keys)
        if selected != self.selected:
            self.selected = selected
            self.revision += 1

    def selection(self):
        return PhotoSelection(self.selected, self.results, self.manager.current_path,
                              self.manager.list_version, self.revision)

    def reference(self, path):
        return self._references.get(self.key(path)) if path else None

    def resolve(self, scope, references):
        if scope != self.scope:
            raise ValueError("browser.scope_expired")
        allowed = {self.key(path) for path in self.results}
        paths = []
        for reference in references:
            path = self._paths.get(reference)
            if path is None or self.key(path) not in allowed:
                raise ValueError("browser.scope_expired")
            if path not in paths:
                paths.append(path)
        return tuple(paths)

    def mark(self, paths, value):
        self.metadata.set_marks(paths, value)
        self.refresh()

    def rate(self, paths, value):
        self.metadata.set_ratings(paths, value)
        self.refresh()

    def query(self, *, offset=0, limit=50):
        self.refresh()
        rows = [{"photo": self.reference(path), "name": os.path.basename(path),
                 "rating": self.metadata.get_rating(path), "mark": self.metadata.get_mark(path),
                 "selected": path in self.selected, "raw": self.is_raw(path), **self.photo_state(path)}
                for path in self.results[offset:offset + limit]]
        return {"scope": self.scope, "list_version": self.manager.list_version,
                "filters": asdict(self.filters), "total": len(self.results), "selected": len(self.selected),
                "current": self.reference(self.manager.current_path), "photos": rows,
                "offset": offset, "has_more": offset + limit < len(self.results), "runtime_only": True,
                "completion_facts_only": True, "existing_batch_scopes_unchanged": True}
