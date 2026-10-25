from __future__ import annotations

import copy
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, CancelledError
from threading import Event

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal

from ...application.photo_relocation import PhotoRelocationService
from ...application.photo_library_store import canonical, backup_hash
from ...application.result_types import CoreOperationError


logger = logging.getLogger(__name__)


class PhotoLibraryRunner(QObject):
    changed = pyqtSignal()
    evaluated = pyqtSignal()
    failed = pyqtSignal(str)
    idle = pyqtSignal()
    relocated = pyqtSignal(str, str)
    session_released = pyqtSignal(str)
    browse_requested = pyqtSignal(object)
    _completed = pyqtSignal(object, object, object)

    def __init__(self, service, metadata, drafts, parent=None, *, before_relocation, lock_ui):
        super().__init__(parent)
        self.service, self.metadata, self.drafts = service, metadata, drafts
        self.before_relocation, self.lock_ui = before_relocation, lock_ui
        self.relocations = None
        self.exports = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-library")
        self._count = 0
        self._closed = False
        self._cancel = Event()
        self._relocating = False
        self._recovering = False
        self._waiting = False
        self._targets = {}
        self._inspected = {}
        self._receipts = {}
        self._export_refresh_pending = False
        self._export_refresh_again = False
        self._deferred = []
        self._path_resolutions = 0
        self._path_error = ""
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._dynamic_timer = QTimer(self)
        self._dynamic_timer.setInterval(50)
        self._dynamic_timer.timeout.connect(self._update_dynamic)
        self._dynamic_timer.start()
        self.submit(lambda: None, self._loaded, refresh=True)
        drafts.ready.connect(self.recover)

    @property
    def busy(self):
        return bool(self._count or self._relocating or self._recovering or self._waiting)

    def _update_dynamic(self):
        if not self._closed and not self._relocating and self.service.update_dynamic():
            self.evaluated.emit()

    def attach(self, sessions, exports):
        self.relocations = PhotoRelocationService(self.service, sessions)
        self.exports = exports
        sessions.core.project_source_resolver = self.service.project_source
        sessions.core.source_access_guard = lambda path: not self.path_blocked(path)
        self.refresh_exports()
        self.recover()

    def target(self, path, kind):
        identity = "library-target:" + uuid.uuid4().hex
        self._targets[identity] = (os.path.abspath(path), kind)
        while len(self._targets) > 64:
            self._targets.pop(next(iter(self._targets)))
        return identity

    def submit(self, work, callback=None, *, refresh=False):
        if self._closed:
            if callback:
                callback(None, "error.cancelled")
            return
        self._count += 1
        def run():
            try:
                value = work()
            except Exception as error:
                if isinstance(error, (ValueError, CancelledError)):
                    logger.info("图库操作未完成：%s", error)
                else:
                    logger.exception("图库任务失败")
                key = "error.cancelled" if isinstance(error, CancelledError) else str(error) if isinstance(error, ValueError) else "library.failed"
                result = (None, None, key, "")
            else:
                snapshot, projection_error = None, ""
                if refresh:
                    try:
                        snapshot = self.service.store.snapshot()
                    except Exception as failure:
                        logger.exception("图库投影读取失败")
                        projection_error = (str(failure) if isinstance(failure, ValueError) and str(failure).startswith("library.")
                                            else "library.refresh_failed")
                result = (value, snapshot, "", projection_error)
            self._completed.emit(callback, result, refresh)
        self._pool.submit(run)

    def _deliver(self, callback, result, refresh):
        self._count -= 1
        if self._closed:
            return
        value, snapshot, error, projection_error = result
        if snapshot is not None:
            previous = self.service.photos
            availability = self.service.availability.copy()
            installed = self.service.install(snapshot)
            self.service.availability = {identity: state for identity, state in availability.items()
                if identity in previous and identity in self.service.photos
                and all(previous[identity][key] == self.service.photos[identity][key] for key in ("current_path", "file_hash", "mtime_ns", "size"))}
            if installed:
                before_paths = {p["path_key"]: p for p in previous.values() if p["path_key"]}
                after_paths = {p["path_key"]: p for p in self.service.photos.values() if p["path_key"]}
                marks_changed = False
                for key in before_paths.keys() | after_paths.keys():
                    old, new = before_paths.get(key), after_paths.get(key)
                    path = (new or old)["current_path"]
                    if old is None or new is None or old["rating"] != new["rating"]:
                        self.metadata.rating_changed.emit(path, self.metadata.get_rating(path))
                    marks_changed |= old is None or new is None or old["mark"] != new["mark"]
                if marks_changed:
                    self.metadata.marks_changed.emit()
                self.changed.emit()
        elif projection_error:
            self.service.loaded = False
            self.service.error = projection_error
            self.failed.emit(projection_error)
            self.changed.emit()
        if error:
            self.failed.emit(error)
        if callback:
            callback(value, error)
        if not self.busy:
            self.idle.emit()

    def _loaded(self, _value, error):
        if error or not self.service.loaded:
            self.service.error = error or self.service.error
            self._run_deferred()
            self.changed.emit()
            return
        # 排队的评分、标记先于核验与恢复提交，按提交顺序写入
        self._run_deferred()
        self.scan()
        self.recover()

    def defer(self, operation):
        """图库加载或路径解析期间排队评分、标记写入，返回是否已排队。"""
        if self._closed or self.unavailable() != "library.loading":
            return False
        self._deferred.append(operation)
        return True

    def _run_deferred(self):
        if self._path_resolutions:
            return
        operations, self._deferred = self._deferred, []
        for operation in operations:
            operation()

    def install_paths(self, paths, *, replace=False):
        self.service.install_paths(paths, replace=replace, retained=self.metadata.pending_paths())

    def resolve_paths(self, paths):
        from ...application.image_source_service import path_key
        paths = tuple(paths)
        self._path_resolutions += 1
        def resolved(value, error):
            self._path_resolutions -= 1
            self._path_error = error
            if not error:
                self.install_paths(value)
            if self.service.loaded or self.service.error:
                self._run_deferred()
            for path in paths:
                self.metadata.rating_changed.emit(path, self.metadata.get_rating(path))
            self.metadata.marks_changed.emit()
            self.changed.emit()
        self.submit(lambda: tuple((path, path_key(path)) for path in paths), resolved)

    def refresh_exports(self):
        if self.exports is None or self._closed or self._relocating:
            return
        if self._export_refresh_pending:
            self._export_refresh_again = True
            return
        self._export_refresh_pending = True
        self.service.set_delivery_ready(False)
        store = self.exports.store
        def finished(_value, error):
            self._export_refresh_pending = False
            self.service.set_delivery_ready(not bool(error))
            self.service.browser.invalidate()
            self.changed.emit()
            if self._export_refresh_again:
                self._export_refresh_again = False
                self.refresh_exports()
        self.submit(lambda: self.service.store.link_exports(store.library_records(is_cancelled=self._cancel.is_set), self._cancel.is_set), finished, refresh=True)

    def scan(self, callback=None, ids=None):
        if not self.service.loaded:
            self.submit(lambda: None, self._loaded, refresh=True)
            return
        if self.exports is not None and not self.service.delivery_ready and not self._export_refresh_pending and not self._cancel.is_set():
            self.refresh_exports()
        photos = [copy.deepcopy(photo) for identity, photo in self.service.photos.items() if ids is None or identity in ids]
        states = {}
        # 分块核验，块与块之间让出队列，资料写入不必等完整个图库
        def step(remaining):
            def done(value, error):
                if error:
                    if callback:
                        callback(None, error)
                    return
                chunk, stamps, rest = value
                states.update(chunk)
                self.service.apply_scan(remaining[:len(remaining) - len(rest)], chunk, stamps)
                if stamps:
                    self.submit(lambda: self.service.store.refresh_stamps(stamps, self._cancel.is_set))
                self.evaluated.emit()
                if rest:
                    step(rest)
                elif callback:
                    callback(states, "")
            self.submit(lambda: self.service.scan(remaining, self._cancel.is_set), done)
        step(photos)

    def write_metadata(self, paths, done=None, **values):
        reason = self.unavailable()
        if reason or self._relocating or self._recovering:
            raise ValueError(reason or "library.busy")
        ids = list(self.service.photos) if paths is None else [self.service.photo_for_path(p)["photo_id"] for p in paths]
        versions = self._latest(ids)
        # 已受理的资料写入不随退出取消回滚
        self.submit(lambda: self.service.store.metadata(versions, **values), done, refresh=True)

    def _latest(self, ids):
        """用户给定的评分、标记以最后一次为准：核对照片仍在、没有待恢复的重定位，不比修订，
        前一次写入尚未投影回来时连续设置也不判为过期。"""
        return dict.fromkeys(self.service.versions(self.service.scope, tuple(dict.fromkeys(ids))))

    def _pending_path(self, path):
        if not self.service.pending:
            return False
        from ...application.photo_library_store import path_key
        key = path_key(path)
        return any(
            key in (path_key(row["payload"]["old_path"]), path_key(row["payload"]["new_path"])) for row in self.service.pending)

    def path_blocked(self, path):
        # 资料读取失败时无从得知待恢复路径，只锁首次加载期间；恢复凭据会在下次可读时比对草稿前后状态
        if self._relocating or self._pending_path(path):
            return True
        return not self.service.loaded and not self.service.error

    def unavailable(self):
        if self._path_error:
            return self._path_error
        if self._path_resolutions:
            return "library.loading"
        if self.service.loaded:
            return ""
        return "library.unavailable" if self.service.error else "library.loading"

    def browser_metadata(self, paths, values, done, cancelled=lambda: False):
        if not paths or self._relocating or self._recovering or self._waiting:
            raise ValueError("library.busy" if paths else "batch.no_targets")
        reason = self.unavailable()
        if reason == "library.loading":
            # 交评分存储排到首次加载之后写入，排队期间照片按排队的值显示；写入失败由评分存储或本运行器提示
            for field, write in (("rating", self.metadata.set_ratings), ("mark", self.metadata.set_marks)):
                if field in values:
                    write(paths, values[field])
            QTimer.singleShot(0, lambda: done({"photos": [], "queued": True}, ""))
            return
        if reason:
            raise ValueError(reason)
        registered, legacy = [], []
        for path in paths:
            photo = self.service.photo_for_path(path)
            if photo is None:
                legacy.append(path)
            else:
                registered.append(photo["photo_id"])
        versions = self._latest(registered)
        registered = list(versions)
        def saved(_value, error):
            if not error:
                try:
                    if "rating" in values:
                        self.metadata.set_ratings(legacy, values["rating"])
                    if "mark" in values:
                        self.metadata.set_marks(legacy, values["mark"])
                except ValueError as failure:
                    error = str(failure)
            done({"photos": registered, "saved": not bool(error)}, error)
        if not registered:
            QTimer.singleShot(0, lambda: saved(None, ""))
            return
        self.submit(lambda: self.service.store.metadata(versions, cancelled=cancelled, **values), saved, refresh=True)

    def command(self, operation, values, callback, cancelled=lambda: False, *, origin="manual"):
        host_cancelled = cancelled
        cancelled = lambda: self._cancel.is_set() or host_cancelled()
        try:
            if self._closed or cancelled():
                raise ValueError("error.cancelled")
            values = copy.deepcopy(values)
            request = values.pop("request", None)
            fingerprint = canonical({"operation": operation, "values": values})
            if request is not None and request in self._receipts:
                prior = self._receipts[request]
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("library.expired")
                if "result" in prior:
                    callback(*prior["result"])
                else:
                    prior["callbacks"].append(callback)
                return
            if (self._relocating or self._recovering or self._waiting) and operation != "get_photo_library":
                raise ValueError("library.busy")
            if operation in ("edit_library_metadata", "manage_library_keyword", "manage_photo_collection"):
                cancelled = host_cancelled
            if request is not None:
                self._receipts[request] = {"fingerprint": fingerprint, "callbacks": [callback]}
                def finish(value, error):
                    entry = self._receipts[request]
                    entry["result"] = (value, error)
                    for waiting in entry.pop("callbacks"):
                        waiting(value, error)
                callback = finish
            self._command(operation, values, callback, cancelled, origin)
        except (ValueError, KeyError, TypeError, CoreOperationError) as error:
            callback(None, str(error) if isinstance(error, (ValueError, CoreOperationError)) else "library.invalid_command")

    def _command(self, operation, values, done, cancelled, origin):
        service, store = self.service, self.service.store
        if operation == "get_photo_library":
            done({**service.query(offset=values.get("offset", 0), limit=values.get("limit", 100)),
                  "targets": [{"target": key, "kind": value[1], "name": os.path.basename(value[0])} for key, value in self._targets.items()]}, "")
        elif operation == "register_library_photos":
            paths, legacy = service.capture_registration(values["scope"], values.get("photos"), self.metadata)
            def registered(ids, error):
                if not error:
                    self.refresh_exports()
                    self.scan(ids=set(ids))
                done({"photos": ids}, error)
            self.submit(lambda: service.register(paths, legacy, new_content=values.get("new_content", False), cancelled=cancelled), registered, refresh=True)
        elif operation == "edit_library_metadata":
            versions = service.versions(values.pop("scope"), values.pop("photos"))
            self.submit(lambda: store.metadata(versions, cancelled=cancelled, **values), lambda _v, e: done({"photos": list(versions)}, e), refresh=True)
        elif operation == "manage_library_keyword":
            self._check_scope(values)
            if values["action"] != "create":
                keyword = service.keywords.get(values.get("keyword"))
                if keyword is None:
                    raise ValueError("library.expired")
                values["revision"] = keyword["revision"]
                if values["action"] == "delete":
                    values["count"] = sum(keyword["keyword_id"] in p["keywords"] for p in service.photos.values())
            self.submit(lambda: store.keyword(cancelled=cancelled, **values), lambda v, e: done({"keyword": v}, e), refresh=True)
        elif operation == "manage_photo_collection":
            scope, ids = values.pop("scope"), values.get("members", [])
            versions = service.versions(scope, ids)
            if values.get("action") != "create":
                collection = service.collections.get(values.get("collection"))
                if collection is None:
                    raise ValueError("library.expired")
                values["revision"] = collection["revision"]
            self.submit(lambda: store.collection(versions=versions, cancelled=cancelled, **values), lambda v, e: done({"collection": v}, e), refresh=True)
        elif operation == "preview_photo_collection":
            self._check_scope(values)
            done(service.evaluate(values["rules"]) if "rules" in values else service.collection_result(values["collection"]), "")
        elif operation == "show_photo_collection":
            self._check_scope(values)
            identity = values.get("collection", "")
            if identity and identity not in service.collections:
                raise ValueError("library.expired")
            service.active_collection = identity
            service.library_view = True
            service.invalidate()
            self.browse_requested.emit(service.collection_result(identity)["photos"] if identity else list(service.photos))
            self.changed.emit()
            done({"collection": identity}, "")
        elif operation == "find_missing_photos":
            self.scan(lambda states, error: done({"photos": [p for p, state in (states or {}).items() if state != "available"]}, error))
        elif operation == "prepare_photo_relocation":
            service.versions(values["scope"], [values["photo"]])
            if self.relocations is None:
                raise ValueError("library.loading")
            path, kind = self._get_target(values["target"], ("photo", "directory"))
            photo = copy.deepcopy(service.photos[values["photo"]])
            def found(candidates, error):
                if error:
                    done(None, error)
                    return
                try:
                    service.versions(values["scope"], [values["photo"]])
                    valid, rejected = [], []
                    for candidate, source, contract in candidates:
                        try:
                            valid.append(self.relocations.propose(photo, candidate, source, contract))
                        except ValueError as failure:
                            rejected.append({"name": os.path.basename(candidate), "reason": str(failure)})
                    done({"candidates": valid, "rejected": rejected}, "")
                except ValueError as failure:
                    done(None, str(failure))
            self.submit(lambda: self.relocations.find(photo, path, cancelled), found)
        elif operation == "cancel_photo_relocation":
            if self.relocations:
                self.relocations.proposals.pop(values["proposal"], None)
            done({"cancelled": True}, "")
        elif operation == "confirm_photo_relocation":
            self._start_relocation(values["proposal"], done, cancelled, origin)
        elif operation in ("backup_photo_library", "inspect_library_backup", "restore_photo_library"):
            self._check_scope(values)
            path, _ = self._get_target(values["target"], ("backup_out",) if operation == "backup_photo_library" else ("backup_in",))
            if service.pending:
                raise ValueError("library.recovery_required")
            if operation == "inspect_library_backup":
                def inspect():
                    before = backup_hash(path)
                    counts = store.inspect_backup(path)
                    if backup_hash(path) != before:
                        raise ValueError("library.expired")
                    return counts, before
                def inspected(result, error):
                    if not error:
                        counts, digest = result
                        self._inspected[values["target"]] = digest
                        done(counts, "")
                    else:
                        done(None, error)
                self.submit(inspect, inspected)
            elif operation == "restore_photo_library":
                if values.get("confirmation") != "restore":
                    raise ValueError("library.confirm_restore")
                digest = self._inspected.get(values["target"])
                if digest is None:
                    raise ValueError("library.confirm_restore")
                self._restore_library(path, digest, done, cancelled, origin)
            else:
                self.submit(lambda: {"path": store.backup(path, cancelled=cancelled)}, done)
        else:
            raise ValueError("library.invalid_command")

    def _check_scope(self, values):
        self.service.versions(values.pop("scope"), [])

    def _restore_library(self, path, digest, done, cancelled, origin):
        try:
            if self._closed or cancelled():
                raise ValueError("error.cancelled")
            if self._count:
                raise ValueError("library.busy")
            try:
                self.before_relocation(None, origin)
            except ValueError as error:
                if str(error) != "library.waiting_ai":
                    raise
                self._waiting = True
                QTimer.singleShot(100, lambda: self._restore_library(path, digest, done, cancelled, origin))
                return
            self._waiting = False
            self._relocating = True
            self.lock_ui(True)
            def restored(value, error):
                self._relocating = False
                self.lock_ui(False)
                if not error:
                    self.service.active_collection = ""
                    self.service.library_view = False
                    self.service.invalidate()
                    self.refresh_exports()
                    self.scan()
                done(value, error)
                self.idle.emit()
            self.submit(lambda: self.service.store.restore(path, expected_hash=digest, cancelled=cancelled), restored, refresh=True)
        except (ValueError, CoreOperationError) as error:
            self._waiting = False
            done(None, str(error))
            self.idle.emit()

    def _get_target(self, reference, kinds):
        target = self._targets.get(reference)
        if target is None or target[1] not in kinds:
            raise ValueError("library.target_required")
        return target

    def _start_relocation(self, proposal, done, cancelled, origin):
        if self._closed or cancelled():
            self._waiting = False
            done(None, "error.cancelled")
            self.idle.emit()
            return
        if self.relocations is None or self._count or not self.drafts.loaded:
            raise ValueError("library.busy")
        pending = self.relocations.proposals.get(proposal)
        if pending is None:
            raise ValueError("library.expired")
        try:
            self.before_relocation(pending["old_path"], origin)
        except ValueError as error:
            if str(error) == "library.waiting_ai":
                self._waiting = True
                QTimer.singleShot(100, lambda: self._resume_relocation(proposal, done, cancelled, origin))
                return
            raise
        self._waiting = False
        job = self.relocations.acquire(proposal)
        self._relocating = True
        self.lock_ui(True)
        def finish(error=""):
            self.relocations.release(job)
            self._relocating = False
            self.lock_ui(False)
            if error and job["session"] is not None:
                self.session_released.emit(job["session"].image_path)
            done({"photo": job["photo"]["photo_id"], "relocated": not bool(error)}, error)
            self.idle.emit()
        def rollback(error):
            try:
                version = self.relocations.rollback(job)
            except Exception:
                logger.exception("重定位回滚需要恢复")
                finish("library.recovery_required")
                self.submit(lambda: None, refresh=True)
                return
            def saved(result):
                if not result.success:
                    finish("library.recovery_required")
                    self.submit(lambda: None, refresh=True)
                elif job["operation"]:
                    self.submit(lambda: self.service.store.finish_relocation(job["operation"], rollback=True),
                                lambda _v, e: finish(e or error), refresh=True)
                else:
                    finish(error)
            self.drafts.wait_for_version(version, saved)
        def applied(operation, error):
            if error:
                rollback(error)
                return
            job["operation"] = operation
            self.service.pending = [*self.service.pending, {"operation_id": operation, "photo_id": job["photo"]["photo_id"],
                                         "stage": "prepared", "payload": self.relocations.payload(job)}]
            try:
                if cancelled():
                    raise ValueError("error.cancelled")
                version = self.relocations.apply(job)
            except Exception as failure:
                rollback(str(failure) if isinstance(failure, ValueError) else "library.failed")
                return
            def saved(result):
                if not result.success:
                    rollback("studio.draft_save_failed")
                    return
                if cancelled():
                    rollback("error.cancelled")
                    return
                def committed(_value, failure):
                    if failure:
                        rollback(failure)
                    else:
                        finish()
                        self.relocated.emit(job["old_path"], job["path"])
                        self.scan(ids={job["photo"]["photo_id"]})
                def commit():
                    self.relocations.verify(job, cancelled)
                    self.service.store.finish_relocation(operation, cancelled=cancelled)
                self.submit(commit, committed, refresh=True)
            self.drafts.wait_for_version(version, saved)
        def flushed(result):
            if not result.success:
                rollback("studio.draft_save_failed")
                return
            def prepare():
                self.relocations.verify(job, cancelled)
                return self.service.store.prepare_relocation(job["photo"], self.relocations.payload(job))
            self.submit(prepare, applied)
        self.drafts.flush(flushed)

    def _resume_relocation(self, proposal, done, cancelled, origin):
        try:
            self._start_relocation(proposal, done, cancelled, origin)
        except (ValueError, CoreOperationError) as error:
            self._waiting = False
            done(None, str(error))
            self.idle.emit()

    def retain_recovery_drafts(self, operation):
        if self.busy or self.relocations is None:
            raise ValueError("library.busy")
        row = next((p for p in self.service.pending if p["operation_id"] == operation), None)
        if row is None:
            raise ValueError("library.expired")
        payload = row["payload"]
        if any(self.relocations.sessions.get(payload[key]) is not None for key in ("old_path", "new_path")):
            raise ValueError("library.restart_recovery")
        def flushed(result):
            if result.success:
                self.submit(lambda: self.service.store.finish_relocation(operation, rollback=True), refresh=True)
        self.drafts.flush(flushed)

    def recover(self):
        if (self._closed or not self.service.loaded or not self.drafts.loaded or self.relocations is None
                or self._relocating or self._recovering or not self.service.pending):
            return
        pending = copy.deepcopy(self.service.pending)
        self._recovering = True
        drafts = self.relocations.sessions.drafts
        def next_item():
            if not pending:
                self._recovering = False
                self.submit(lambda: None, lambda _v, _e: self.scan(), refresh=True)
                return
            row = pending.pop(0)
            payload = row["payload"]
            try:
                current = self.relocations.recovery_current(payload)
            except ValueError as error:
                self.failed.emit(str(error))
                next_item()
                return
            def verified(value, error):
                forward = not error and value
                try:
                    self.relocations.restore_recovery_drafts(payload, current, forward)
                except ValueError as failure:
                    self.failed.emit(str(failure))
                    next_item()
                    return
                def saved(result):
                    if not result.success:
                        self.failed.emit("library.recovery_required")
                        next_item()
                        return
                    self.submit(lambda: self.service.store.finish_relocation(row["operation_id"], rollback=not forward, identity=value if forward else None),
                                lambda _v, _e: next_item())
                self.drafts.wait_for_version(drafts.version, saved)
            self.submit(lambda: self.relocations.recoverable_target(payload, self._cancel.is_set), verified)
        next_item()

    def cancel(self):
        self._cancel.set()

    def resume(self):
        if not self.busy:
            self._cancel.clear()

    def shutdown(self):
        self._closed = True
        self._dynamic_timer.stop()
        self._cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
