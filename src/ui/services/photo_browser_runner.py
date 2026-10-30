from __future__ import annotations

import copy
import logging
from collections import OrderedDict, deque
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QImage, QPixmap

from ...application.export_store import ExportPreset
from ...application.output_writer import OperationCancellation
from ...application.result_types import CoreResult

logger = logging.getLogger(__name__)


@dataclass
class BrowserImage:
    path: str
    snapshot: object
    pixmap: object = None
    size: tuple = (0, 0)
    memory: object = None
    error: str = ""
    owned: bool = True

    def close(self, core):
        self.pixmap = None
        if self.memory is not None:
            self.memory.close()
            self.memory = None
        if self.owned:
            core.release_export_snapshot(self.snapshot)


class PhotoBrowserRunner(QObject):
    ready = Signal(str)
    changed = Signal()
    clearing = Signal(str)
    idle = Signal()
    _completed = Signal(object, object)

    def __init__(self, service, parent=None, *, foreground_busy=lambda: False, source_active=lambda: False):
        super().__init__(parent)
        self.service = service
        self._foreground_busy = foreground_busy
        self._source_active = source_active
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-browser")
        self._channels = {}
        self._retained = {}
        self._cache = OrderedDict()
        self._resolved = {}
        self._failures = {}
        self._active = None
        self._deferred_snapshots = {}
        self._closed = False
        self._scan = deque()
        self._source_checks = deque()
        self._source_requested = False
        self._checking = None
        self._check_generation = 0
        self._preview_pending = False
        self._retry_previews = set()
        self._retry_identities = {}
        self._check_version = None
        self._scan_version = None
        self._facts_version = None
        self._facts_pending = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._pump)
        self._source_timer = QTimer(self)
        self._source_timer.setSingleShot(True)
        self._source_timer.timeout.connect(self._check_sources)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(1000)
        self._retry_timer.timeout.connect(self._retry_failed)
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    @property
    def busy(self):
        return self._active is not None

    def _key(self, path, maximum, graph=None, fixed=None, *, square=False):
        if fixed is not None:
            return (self.service.key(path), fixed.stamp, fixed.workflow_hash, fixed.tool_generation, fixed.id, "fixed", square, maximum)
        graph = graph or self.service.workflow(path)[0]
        core = self.service.sessions.core
        return (self.service.key(path), core.image_sources.file_stamp(path), graph.workflow_hash(),
                core.tools.generation, self.service.raw_mode, self.service.image_mode, square, maximum)

    def request(self, channel, paths, maximum=300, *, square=False):
        if self._closed:
            return
        self._channels[channel] = tuple((path, maximum, None, square) for path in dict.fromkeys(paths))
        self._preview_pending = True
        if self._checking is not None:
            self._checking.cancel()
        self._cancel_unwanted()
        self._schedule_source_check()
        self._timer.start(0)

    def request_fixed(self, channel, snapshot, maximum=1600):
        if self._closed:
            return
        self._channels[channel] = ((snapshot.path, maximum, snapshot, False),)
        self._preview_pending = True
        if self._checking is not None:
            self._checking.cancel()
        self._cancel_unwanted()
        self._schedule_source_check()
        self._timer.start(0)

    def forget(self, channel):
        self._channels.pop(channel, None)
        self._retained.pop(channel, None)
        self._preview_pending = True
        self._cancel_unwanted()
        self._prune()
        self._schedule_source_check()

    def _cancel_unwanted(self):
        if self._active is None or self._active[0] is None:
            return
        key, _, _, cancellation, _, _ = self._active
        for requests in self._channels.values():
            for path, maximum, fixed, square in requests:
                try:
                    if self._key(path, maximum, fixed=fixed, square=square) == key:
                        return
                except (OSError, ValueError, KeyError):
                    continue
        cancellation.cancel()

    def retain(self, channel, image):
        if image is None:
            self._retained.pop(channel, None)
        else:
            self._retained[channel] = image

    def release_fixed(self, snapshot):
        if self._active is not None and self._active[2] is not None and self._active[2].id == snapshot.id:
            self._active[3].cancel()
            self._deferred_snapshots[snapshot.id] = snapshot
        else:
            self.service.sessions.core.release_export_snapshot(snapshot)

    def image(self, path, maximum=300, *, fixed=None, square=False):
        try:
            key = self._key(path, maximum, fixed=fixed, square=square)
        except (OSError, ValueError, KeyError) as error:
            message = str(error) if isinstance(error, ValueError) else "browser.source_failed"
            return BrowserImage(path, None, error=message, owned=False)
        resolved = self._resolved.get(key, key)
        value = self._cache.get(resolved)
        if value is not None:
            self._cache.move_to_end(resolved)
        return value

    def scan_sources(self):
        if self.service.exports is not None and self.service.exports.loaded:
            version = (self.service.manager.list_version, tuple(record["id"] for record in self.service.exports.records))
            if version != self._facts_version:
                self._facts_version = version
                self._facts_pending = tuple(self.service.manager.image_list)
        version = (self.service.manager.list_version, self.service.filters.delivery, self.service.source_generation)
        if self.service.filters.delivery == "all":
            self._scan.clear()
            self._scan_version = None
        elif version != self._scan_version:
            self._scan_version = version
            identities = self.service.identities()
            self._scan = deque(path for path in self.service.manager.image_list if self.service.key(path) not in identities)
        self._schedule_source_check()
        self._timer.start(0)

    def invalidate(self, path=None):
        if self._closed:
            return
        self.clearing.emit(path or "")
        if self._active is not None and (path is None or self.service.key(path) == self.service.key(self._active[1])):
            self._active[3].cancel()
        for key in list(self._cache):
            if path is None or key[0] == self.service.key(path):
                self._cache.pop(key).close(self.service.sessions.core)
                self._resolved.pop(key[:8], None)
        self._scan_version = None
        self._preview_pending = True
        self._timer.start(0)

    def retry(self):
        self._facts_version = None
        self.invalidate()
        self.scan_sources()

    def _schedule_retry(self):
        if not self._closed and not self._retry_timer.isActive():
            self._retry_timer.start()

    def _retry_failed(self):
        if self._closed:
            return
        wanted = {path for requests in self._channels.values() for path, _, _, _ in requests}
        paths, self._retry_previews = self._retry_previews, set()
        for path in paths & wanted:
            if path in self._failures or any(image.path == path and image.error for image in self._cache.values()):
                self._failures.pop(path, None)
                self.invalidate(path)
        identities, self._retry_identities = self._retry_identities, {}
        if self._source_requested or self.service.filters.delivery != "all":
            for path, version in identities.items():
                if version == self.service.manager.list_version and path not in self._scan:
                    self._scan.append(path)
            if self._scan:
                self._timer.start(0)

    def _sources_needed(self):
        return (self._source_requested or self._source_active() or self.service.filters.delivery != "all"
                or any(self._channels.values()))

    def _schedule_source_check(self):
        if self._closed or self.service.sessions is None or not self._sources_needed():
            self._source_timer.stop()
            self._source_checks.clear()
            if self._checking is not None:
                self._checking.cancel()
        elif not self._source_checks and self._checking is None and not self._source_timer.isActive():
            self._source_timer.start(max(1000, min(30000, len(self.service.manager.image_list) * 5)))

    def request_source_check(self):
        if self._closed or self.service.sessions is None:
            return
        self._source_requested = True
        self._check_sources(restart=True)

    def _check_sources(self, *, restart=False):
        if self._closed or self.service.sessions is None:
            return
        if not self._sources_needed() or not restart and (self._source_checks or self._checking is not None):
            self._schedule_source_check()
            return
        self._source_timer.stop()
        if self._checking is not None:
            self._checking.cancel()
        self._check_generation += 1
        self._check_version = self.service.manager.list_version
        self._source_checks.clear()
        self._source_checks.extend(self.service.identity_snapshot())
        if self._source_checks:
            self._timer.start(0)
        else:
            self._source_requested = False
            self._schedule_source_check()

    def _pump(self):
        if self._closed or self._active is not None or self.service.sessions is None:
            return
        if self._foreground_busy():
            self._timer.start(200)
            return
        if self._preview_pending:
            self._preview_pending = False
            for channel, requests in sorted(self._channels.items(), key=lambda value: not value[0].startswith("compare")):
                for path, maximum, fixed, square in requests:
                    try:
                        if fixed is None:
                            graph, source_hash, _ = self.service.workflow(path)
                        key = (self._key(path, maximum, fixed=fixed, square=square) if fixed is not None
                               else self._key(path, maximum, graph, square=square))
                    except (OSError, ValueError, KeyError) as error:
                        message = str(error) if isinstance(error, ValueError) else "browser.source_failed"
                        self._retry_previews.add(path)
                        self._schedule_retry()
                        if self._failures.get(path) != message:
                            self._failures[path] = message
                            self.ready.emit(path)
                        continue
                    self._failures.pop(path, None)
                    if self._resolved.get(key, key) in self._cache:
                        continue
                    cancellation = OperationCancellation()
                    core = self.service.sessions.core
                    captured = None if fixed is not None else (copy.deepcopy(graph), source_hash, core.capture_export_protection())
                    self._preview_pending = True
                    self._active = (key, path, fixed, cancellation, fixed is None, self.service.manager.list_version)
                    self._pool.submit(self._render, self._active, maximum, square, captured)
                    return
        if self._facts_pending is not None:
            paths, self._facts_pending = self._facts_pending, None
            cancellation = OperationCancellation()
            self._active = (None, "", None, cancellation, False, self.service.manager.list_version)
            self._pool.submit(self._load_facts, self._active, paths, self._facts_version)
            return
        if self._scan:
            path = self._scan.popleft()
            cancellation = OperationCancellation()
            self._active = (None, path, None, cancellation, False, self.service.manager.list_version)
            self._pool.submit(self._identity, self._active)
            return
        if self._check_version != self.service.manager.list_version:
            self._source_checks.clear()
            self._source_requested = False
        if self._source_checks:
            entries = tuple(self._source_checks.popleft() for _ in range(min(32, len(self._source_checks))))
            cancellation = OperationCancellation()
            self._checking = cancellation
            self._active = (None, "", None, cancellation, False, self._check_version)
            self._pool.submit(self._verify_sources, self._active, entries, self._check_generation)
            return
        self._schedule_source_check()
        self.idle.emit()

    def _verify_sources(self, active, entries, generation):
        invalid = []
        checked, failed = 0, False
        try:
            for path, identity in entries:
                if active[3].is_cancelled():
                    break
                if not self.service.sessions.core.image_sources.matches_file(identity, path=path):
                    invalid.append((path, identity))
                checked += 1
        except Exception:
            logger.exception("选片来源检查失败")
            failed = True
        self._complete(active, {"invalid_sources": invalid, "remaining_sources": entries[checked:],
                                "source_check_generation": generation, "source_check_failed": failed})

    def _load_facts(self, active, paths, version):
        try:
            facts = self.service.exports.store.delivery_facts(paths, is_cancelled=active[3].is_cancelled)
            packet = {"facts": facts, "version": version}
        except Exception as error:
            logger.exception("选片交付记录读取失败")
            packet = {"facts_error": str(error)}
        self._complete(active, packet)

    def _identity(self, active):
        try:
            result = self.service.sessions.core.image_sources.identity(active[1], is_cancelled=active[3].is_cancelled)
        except CancelledError:
            result = None
        except Exception:
            logger.exception("选片来源核验失败")
            result = None
        self._complete(active, {"identity": result, "source_failed": result is None})

    def _render(self, active, maximum, square, captured=None):
        snapshot, cancellation = active[2:4]
        core = self.service.sessions.core
        packet = {"snapshot": snapshot, "memory": None, "result": None, "identity": None}
        try:
            if cancellation.is_cancelled():
                raise CancelledError()
            if snapshot is None:
                graph, source_hash, protection = captured
                snapshot = core.capture_export_workflow(active[1], graph, source_hash=source_hash,
                    expected_tools=active[0][3], protection=protection, is_cancelled=cancellation.is_cancelled)
                packet["snapshot"] = snapshot
                if core.image_sources.file_stamp(active[1]) != active[0][1] or snapshot.stamp != active[0][1][:2]:
                    raise ValueError("error.source.changed")
            snapshot = core.prepare_export_snapshot(snapshot, is_cancelled=cancellation.is_cancelled)
            packet["snapshot"] = snapshot
            # 网格缩略图在工作流支持代理时用 1600 线性代理；对比视图保持完整来源
            result = core.preview_export_snapshot(snapshot, ExportPreset(), cancellation=cancellation, max_dimension=maximum,
                                                  proxy=maximum is not None and maximum <= 512, square=square)
            if result.success:
                frame, size, packet["memory"] = result.data
                packet["result"] = CoreResult.ok((frame, size))
                from ...application.image_source_service import SourceIdentity
                packet["identity"] = SourceIdentity(snapshot.path, snapshot.source_hash, *snapshot.stamp)
            else:
                packet["result"] = result
        except CancelledError:
            packet["result"] = CoreResult.fail("CANCELLED", "error.cancelled")
        except Exception as error:
            if cancellation.is_cancelled() or isinstance(error, InterruptedError) and str(error) == "error.cancelled":
                packet["result"] = CoreResult.fail("CANCELLED", "error.cancelled")
            else:
                logger.exception("选片预览失败")
                packet["result"] = CoreResult.from_exception(error, message_key=str(error) if isinstance(error, ValueError) else "error.preview.failed")
        self._complete(active, packet)

    def _complete(self, active, packet):
        if not self._closed:
            try:
                self._completed.emit(active, packet)
                return
            except RuntimeError:
                logger.debug("选片预览接收方已释放")
        if packet.get("memory") is not None:
            packet["memory"].close()
        if active[4] and packet.get("snapshot") is not None:
            self.service.sessions.core.release_export_snapshot(packet.get("snapshot", active[2]))
        self._release_deferred(active)

    def _release_deferred(self, active):
        snapshot = active[2]
        if snapshot is not None and self._deferred_snapshots.pop(snapshot.id, None) is not None:
            self.service.sessions.core.release_export_snapshot(snapshot)

    @Slot(object, object)
    def _deliver(self, active, packet):
        self._active = None
        key, path, snapshot, cancellation, owned, list_version = active
        core = self.service.sessions.core
        current = not self._closed and not cancellation.is_cancelled()
        same_list = list_version == self.service.manager.list_version
        if "invalid_sources" in packet:
            self._checking = None
            if not self._closed and same_list and packet["source_check_generation"] == self._check_generation:
                if self._sources_needed():
                    self._source_checks.extendleft(reversed(packet["remaining_sources"]))
                if packet["invalid_sources"] and self.service.invalidate_sources(packet["invalid_sources"], list_version=list_version):
                    self._preview_pending = True
                    self.changed.emit()
            if not self._source_checks and self._check_version == self.service.manager.list_version:
                self._source_requested = False
            self._schedule_source_check()
            if not self._closed:
                self._timer.start(1000 if packet["source_check_failed"] else 0)
            self.idle.emit()
            return
        if current and same_list and "facts" in packet and packet["version"] == self._facts_version:
            self.service.set_delivery_facts(packet["facts"])
            self.changed.emit()
        elif current and same_list and "facts_error" in packet:
            self.service.clear_delivery_facts()
            self.changed.emit()
        if current and same_list and packet.get("source_failed"):
            self.service.source_failed(path)
            self._retry_identities[path] = list_version
            self._schedule_retry()
            self.changed.emit()
        if key is not None:
            try:
                current = current and (not owned or same_list and self._key(path, key[-1], square=key[-2]) == key)
            except (OSError, ValueError, KeyError):
                current = False
            snapshot = packet["snapshot"]
            if current:
                result = packet["result"]
                image = BrowserImage(path, snapshot, memory=packet["memory"], owned=owned and snapshot is not None)
                if result.success:
                    frame, image.size = result.data
                    width, height = frame.size
                    qimage = QImage(frame.rgba, width, height, width * 4, QImage.Format.Format_RGBA8888)
                    image.pixmap = QPixmap.fromImage(qimage)
                    if image.pixmap.isNull():
                        image.pixmap = None
                        image.error = "error.image.memory_budget"
                        image.memory.close()
                        image.memory = None
                else:
                    image.error = result.error.message_key
                if image.error:
                    self._retry_previews.add(path)
                    self._schedule_retry()
                resolved = key + (snapshot.source_hash, snapshot.render_json) if snapshot is not None else key
                self._resolved[key] = resolved
                self._cache[resolved] = image
                self.ready.emit(path)
            else:
                if packet["memory"] is not None:
                    packet["memory"].close()
                if owned and snapshot is not None:
                    core.release_export_snapshot(snapshot)
        if current and same_list and packet.get("identity") is not None:
            self._retry_identities.pop(path, None)
            if self.service.set_identity(path, packet["identity"], list_version=list_version):
                self.changed.emit()
        self._release_deferred(active)
        if key is not None:
            self._prune()
        if not self._closed:
            self._timer.start(0)
        self.idle.emit()

    def _prune(self):
        if self.service.sessions is None:
            return
        wanted = set()
        for requests in self._channels.values():
            for path, maximum, fixed, square in requests:
                try:
                    wanted.add(self._key(path, maximum, fixed=fixed, square=square))
                except (OSError, ValueError, KeyError):
                    continue
        retained = {id(value) for value in self._retained.values()}
        total = sum(value.pixmap.width() * value.pixmap.height() * 12 for value in self._cache.values() if value.pixmap is not None)
        for key in list(self._cache):
            if key[:8] in wanted or id(self._cache[key]) in retained:
                continue
            if key[7] is not None and key[7] <= 512 and len(self._cache) <= 128 and total <= 48 * 1024 ** 2:
                continue
            value = self._cache.pop(key)
            self._resolved.pop(key[:8], None)
            if value.pixmap is not None:
                total -= value.pixmap.width() * value.pixmap.height() * 12
            value.close(self.service.sessions.core)

    def cancel(self):
        self._channels.clear()
        self._scan.clear()
        self._source_checks.clear()
        self._source_requested = False
        self._preview_pending = False
        self._retry_previews.clear()
        self._retry_identities.clear()
        self._retry_timer.stop()
        self._source_timer.stop()
        self._facts_pending = None
        self._timer.stop()
        if self._active is not None:
            self._active[3].cancel()

    def shutdown(self):
        if self._closed:
            return
        self.cancel()
        self.clearing.emit("")
        self._retained.clear()
        self._closed = True
        self._source_timer.stop()
        for value in self._cache.values():
            value.close(self.service.sessions.core)
        self._cache.clear()
        self._resolved.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
