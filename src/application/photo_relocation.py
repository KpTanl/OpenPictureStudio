from __future__ import annotations

import os
import uuid
from pathlib import Path

from .photo_library_store import path_key


class PhotoRelocationService:
    def __init__(self, library, sessions):
        self.library, self.sessions = library, sessions
        self.core = sessions.core
        self.proposals = {}

    def find(self, photo, root, cancelled):
        """返回 (浏览路径, 来源身份, 渲染契约)；新绑定保存用户选到的路径写法。"""
        candidates = []
        root = Path(root)
        paths = (str(root),) if root.is_file() else self._walk(root, cancelled)
        for path in paths:
            if cancelled():
                raise ValueError("error.cancelled")
            try:
                if Path(path).suffix.lower() != photo["format"] or os.stat(path).st_size != photo["size"]:
                    continue
                source = self.library.sources.identity(path, is_cancelled=cancelled)
                if source.file_hash != photo["file_hash"]:
                    continue
                contract = self.core.export_render_contract(source.path)
                candidates.append((os.path.normpath(os.path.abspath(path)), source, contract))
                if len(candidates) >= 100:
                    break
            except OSError:
                continue
        return candidates

    @staticmethod
    def _walk(root, cancelled):
        for directory, _, files in os.walk(root, followlinks=False):
            for name in files:
                if cancelled():
                    raise ValueError("error.cancelled")
                yield os.path.join(directory, name)

    def propose(self, photo, path, source, render_contract):
        occupied = self.library.photo_for_path(source.path)
        if occupied is not None and occupied["photo_id"] != photo["photo_id"]:
            raise ValueError("library.target_occupied")
        old = photo["current_path"] or photo["last_known_path"]
        if path_key(old) == path_key(path):
            raise ValueError("library.same_path")
        if self.sessions.get(path) is not None:
            raise ValueError("library.target_occupied")
        token = "relocation:" + uuid.uuid4().hex
        proposal = {"token": token, "photo": dict(photo), "path": path, "source": source,
                    "old_path": os.path.normpath(old), "render_contract": render_contract}
        self.proposals[token] = proposal
        while len(self.proposals) > 100:
            self.proposals.pop(next(iter(self.proposals)))
        return {"proposal": token, "photo": photo["photo_id"], "old_path": old,
                "new_path": path, "file_hash": source.file_hash, "verified": True}

    def acquire(self, token):
        proposal = self.proposals.pop(token, None)
        if proposal is None:
            raise ValueError("library.expired")
        photo, new, old = proposal["photo"], proposal["path"], proposal["old_path"]
        self.library.versions(self.library.scope, [photo["photo_id"]])
        if self.library.photos[photo["photo_id"]]["revision"] != photo["revision"]:
            raise ValueError("library.expired")
        if self.sessions.get(new) is not None:
            raise ValueError("library.target_occupied")
        drafts = self.sessions.drafts
        session = self.sessions.get(old)
        snapshot = drafts.snapshot()
        old_paths = {p for p in snapshot if path_key(p) == path_key(old)} | {old}
        if session is not None:
            old_paths.add(session.image_path)
        target_paths = {p for p in snapshot if path_key(p) == path_key(new)} | {new}
        before = next((snapshot[p] for p in old_paths if snapshot.get(p)), {})
        if any(snapshot.get(p) and snapshot[p] != before for p in old_paths | target_paths):
            raise ValueError("library.draft_conflict")
        draft_before = {p: snapshot.get(p, {}) for p in old_paths | target_paths}
        draft_after = {p: {} for p in draft_before}
        draft_after[new] = before
        state = self.core.get_session(session.core_id).unwrap() if session is not None else None
        if session is not None:
            if session.read_only or state.active_source.file_hash != proposal["source"].file_hash:
                raise ValueError("calibration.incompatible")
            locked = self.core.begin_source_relocation(session.core_id, expected_state=state.state_version,
                expected_source=state.active_source, token=token).unwrap()
        else:
            locked = None
        return {**proposal, "draft_before": draft_before, "draft_after": draft_after, "session": session, "state": state,
                "session_path": session.image_path if session else None,
                "was_relocated": getattr(session, "source_relocated", False),
                "old_identity": self.core.source_identity(session.core_id) if session else None,
                "version": locked, "applied": False, "draft_applied": False, "operation": None}

    @staticmethod
    def payload(job):
        source, photo = job["source"], job["photo"]
        return {"old_path": job["old_path"], "new_path": job["path"], "file_hash": source.file_hash,
                "revision": photo["revision"], "mtime_ns": source.mtime_ns, "size": source.size,
                "render_contract": job["render_contract"],
                "draft_before": job["draft_before"], "draft_after": job["draft_after"]}

    def verify(self, job, cancelled):
        source = self.library.sources.identity(job["source"].path, is_cancelled=cancelled)
        if source != job["source"]:
            raise ValueError("error.source.changed")
        if self.core.export_render_contract(source.path) != job["render_contract"]:
            raise ValueError("calibration.incompatible")
        return source

    def recovery_current(self, payload):
        current = {path: self.sessions.drafts.for_image(path) for path in payload["draft_before"]}
        if current not in (payload["draft_before"], payload["draft_after"]):
            raise ValueError("library.draft_conflict")
        return current

    def recoverable_target(self, payload, cancelled):
        source = self.library.sources.identity(payload["new_path"], is_cancelled=cancelled)
        if source.file_hash != payload["file_hash"]:
            return None
        if self.core.export_render_contract(source.path) != payload["render_contract"]:
            raise ValueError("calibration.incompatible")
        return source

    def restore_recovery_drafts(self, payload, current, forward):
        return self.sessions.drafts.replace_paths(current, payload["draft_after"] if forward else payload["draft_before"])

    def apply(self, job):
        session, source = job["session"], job["source"]
        if session is not None:
            job["version"] = self.core.relocate_source(session.core_id, source, token=job["token"],
                expected_state=job["version"], expected_source=job["state"].active_source).unwrap()
            job["applied"] = True
            self.sessions.rekey(job["old_path"], job["path"], session.core_id)
        version = self.sessions.drafts.replace_paths(job["draft_before"], job["draft_after"])
        job["draft_applied"] = True
        return version

    def rollback(self, job):
        session = job["session"]
        if job["applied"]:
            self.core.rollback_source_relocation(session.core_id, job["state"].active_source,
                job["old_identity"], token=job["token"], expected_state=job["version"]).unwrap()
            if self.sessions.get(job["path"]) is session:
                self.sessions.rekey(job["path"], job["session_path"], session.core_id)
            session.source_relocated = job["was_relocated"]
        drafts = self.sessions.drafts
        if job["draft_applied"]:
            drafts.replace_paths(job["draft_after"], job["draft_before"])
        return drafts.version

    def release(self, job):
        session = job["session"]
        if session is not None:
            self.core.end_source_relocation(session.core_id, job["token"])
            # 加锁时 Core 状态版本已推进；未改绑就失败时投影也要跟上，否则下一次调参按旧版本被拒
            session.refresh()
