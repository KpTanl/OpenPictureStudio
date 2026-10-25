"""Draft store: per-image, per-tool uncommitted editing parameters.

This is the *Draft* tier of the three-tier edit state (Source / Draft /
Committed). It deliberately has no Qt dependency so Core code can construct and
inspect drafts headlessly.

``EditStateStore`` remains the compatibility facade and owns the cross-image
"last-used" tier; draft reads/writes are delegated here.
"""

from __future__ import annotations

import copy
from threading import Lock
from typing import Any, Callable, Dict, Optional


class DraftStore:
    """In-memory draft parameters keyed by ``(image_path, tool_id)``.

    Drafts are cheap, discardable and never create a revision. A change callback
    lets the Qt-facing facade re-emit its signals without coupling Core to Qt.
    """

    def __init__(self, on_changed: Optional[Callable[[str], None]] = None):
        self._drafts: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._on_changed = on_changed
        self._version = 0
        self._path_versions: Dict[str, int] = {}
        self._version_lock = Lock()

    @property
    def version(self) -> int:
        with self._version_lock:
            return self._version

    def path_version(self, image_path: str) -> int:
        with self._version_lock:
            return self._path_versions.get(image_path, 0)

    # ---- reads ----
    def get_draft(self, image_path: str, tool: str) -> Optional[Dict[str, Any]]:
        draft = self._drafts.get(image_path, {}).get(tool)
        return copy.deepcopy(draft) if draft is not None else None

    def has_draft(self, image_path: str, tool: str) -> bool:
        return tool in self._drafts.get(image_path, {})

    def has_pending(self, image_path: str) -> bool:
        return bool(self._drafts.get(image_path))

    def tools_for(self, image_path: str) -> list[str]:
        return list(self._drafts.get(image_path, {}).keys())

    def for_image(self, image_path: str) -> Dict[str, Dict[str, Any]]:
        return copy.deepcopy(self._drafts.get(image_path, {}))

    # ---- writes ----
    def set_draft(self, image_path: str, tool: str, params: Dict[str, Any]) -> None:
        self.update_drafts(image_path, {tool: params})

    def update_drafts(self, image_path: str, updates: Dict[str, Optional[Dict[str, Any]]]) -> None:
        if not image_path:
            return
        previous = self._drafts.get(image_path, {})
        current = dict(previous)
        for tool, params in updates.items():
            if params is None:
                current.pop(tool, None)
            else:
                current[tool] = copy.deepcopy(dict(params))
        if current == previous:
            return
        if current:
            self._drafts[image_path] = current
        else:
            self._drafts.pop(image_path, None)
        self._notify(image_path)

    def discard(self, image_path: str, tool: str) -> bool:
        if image_path in self._drafts and tool in self._drafts[image_path]:
            del self._drafts[image_path][tool]
            if not self._drafts[image_path]:
                del self._drafts[image_path]
            self._notify(image_path)
            return True
        return False

    def clear_path(self, path: str) -> bool:
        if path and path in self._drafts:
            del self._drafts[path]
            self._notify(path)
            return True
        return False

    def clear_all(self) -> list[str]:
        paths = list(self._drafts.keys())
        self._drafts.clear()
        for path in paths:
            self._notify(path)
        return paths

    # ---- snapshots ----
    def restore_paths(self, values):
        for path, params in values.items():
            if params:
                self._drafts[path] = copy.deepcopy(params)
            else:
                self._drafts.pop(path, None)
        for path in values:
            self._notify(path)

    def replace_paths(self, expected, values):
        if set(expected) != set(values) or any(self.for_image(path) != params for path, params in expected.items()):
            raise ValueError("library.draft_conflict")
        self.restore_paths(values)
        return self.version

    def snapshot(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        return copy.deepcopy(self._drafts)

    def restore(self, snapshot: Dict[str, Dict[str, Dict[str, Any]]], *, notify: bool = True) -> None:
        if snapshot == self._drafts:
            return
        changed = {path for path in self._drafts.keys() | snapshot.keys()
                   if self._drafts.get(path) != snapshot.get(path)}
        self._drafts = copy.deepcopy(snapshot)
        with self._version_lock:
            self._version += 1
            for path in changed:
                self._path_versions[path] = self._version
        if notify and self._on_changed is not None:
            for path in changed:
                self._on_changed(path)

    def _notify(self, image_path: str) -> None:
        with self._version_lock:
            self._version += 1
            self._path_versions[image_path] = self._version
        if self._on_changed is not None:
            self._on_changed(image_path)
