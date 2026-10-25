from __future__ import annotations

import os
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .export_store import ExportPreset, content_hash
from .output_writer import OperationCancellation


class ExportItemCancellation:
    def __init__(self, batch):
        self.batch = batch
        self.item = OperationCancellation()

    def is_cancelled(self):
        return self.batch.is_cancelled() or self.item.is_cancelled()

    def mark_published(self):
        self.item.mark_published()

    @contextmanager
    def publication(self):
        with self.batch.publication() as batch_active, self.item.publication() as item_active:
            yield batch_active and item_active


@dataclass
class ExportItem:
    path: str
    target: str
    snapshot: object = None
    status: str = "pending"
    error: dict | None = None
    record: dict | None = None
    record_state: str = "pending"
    verification: str = "unchecked"
    registered: bool = False


@dataclass
class ExportBatch:
    id: str
    preset: ExportPreset
    items: tuple[ExportItem, ...]
    origin: str
    paused: bool = False
    cancellation: OperationCancellation = field(default_factory=OperationCancellation)
    active_items: int = field(init=False)
    metadata_bytes: int = field(init=False)
    record_failures: int = field(init=False)
    next_index: int = 0

    def __post_init__(self):
        self.active_items = sum(item.status in ("pending", "exporting") for item in self.items)
        self.record_failures = sum(item.record_state == "failed" for item in self.items)
        snapshots = [item.snapshot for item in self.items if item.snapshot is not None]
        protections = {snapshot.protection for snapshot in snapshots}
        self.metadata_bytes = (sum(snapshot.size_bytes for snapshot in snapshots)
            + sum(protection.size_bytes for protection in protections) + len(self.items) * 512)


@dataclass(frozen=True)
class ExportCapture:
    preset: ExportPreset
    paths: tuple[str, ...]
    context: dict
    states: tuple
    drafts: dict
    generation: int
    protection: object
    reserved: tuple[str, ...]
    origin: str


class PhotoExportService:
    MAX_FINISHED_BATCHES = 16
    MAX_METADATA_BYTES = 64 * 1024 ** 2

    def __init__(self, sessions, scopes, store):
        self.sessions, self.scopes, self.store = sessions, scopes, store
        self.core = sessions.core
        self.batches = OrderedDict()
        self.presets = {}
        self.records = []
        self.configuration = None
        self.loaded = False
        self.store_error = None

    def configure(self, preset):
        preset.validate()
        self.configuration = preset

    def context(self):
        return {"configured": self.configuration.to_dict() if self.configuration else None,
                "loaded": self.loaded, "store_error": self.store_error,
                "recent_exports": [self.summary(identity, limit=0) for identity in list(self.batches)[-16:]],
                "runtime_only": True}

    def enqueue(self, scope, range_kind, *, origin="manual"):
        return self.publish(self.prepare(self.capture(scope, range_kind, origin=origin)))

    def capture(self, scope, range_kind, *, origin="manual"):
        if self.configuration is None:
            raise ValueError("export_queue.directory_required")
        paths = tuple(self.scopes.selection_paths(scope, range_kind))
        if not paths:
            raise ValueError("batch.no_targets")
        self.prune()
        generation = self.core.tools.generation
        states = tuple((session.image_path, session.state_version, self.core.get_session(session.core_id))
                       for session in self.sessions.sessions())
        drafts = {os.path.normpath(os.path.abspath(path)): self.sessions.drafts.for_image(os.path.normpath(path))
                  for path in paths}
        return ExportCapture(self.configuration, paths, self.scopes.context(scope), states, drafts,
            generation, self.core.capture_export_protection(paths),
            tuple(item.target for batch in self.batches.values() for item in batch.items), origin)

    def prepare(self, capture, cancelled=lambda: False):
        from concurrent.futures import CancelledError
        from .color_initialization import initial_color_identity, color_baseline_workflow
        from .result_types import CoreOperationError, CoreResult
        preset = capture.preset
        preset.validate()
        def check():
            if cancelled():
                raise CancelledError()
            if self.core.tools.generation != capture.generation:
                raise ValueError("export_queue.render_changed")
        check()
        unique = {}
        for path in capture.paths:
            check()
            unique.setdefault(self.sessions.key(path), os.path.normpath(os.path.abspath(path)))
        source_paths = tuple(unique.values())
        reserved = set()
        for path in capture.reserved:
            check()
            reserved.add(self.sessions.key(path))
        reserved.update(unique)
        states = {}
        for path, version, result in capture.states:
            check()
            states[self.sessions.key(path)] = version, result
        directory = Path(preset.directory).resolve()
        protection = capture.protection.prepare(cancelled)
        items = []
        try:
            for index, path in enumerate(source_paths, 1):
                check()
                destination = directory / preset.filename(Path(path), index)
                suffix = 1
                while self.sessions.key(destination) in reserved or destination.exists() or destination.is_symlink():
                    check()
                    suffix += 1
                    original = Path(preset.filename(Path(path), index))
                    destination = directory / f"{original.stem}-{suffix:02d}{original.suffix}"
                reserved.add(self.sessions.key(destination))
                item = ExportItem(path, str(destination))
                items.append(item)
                try:
                    current = states.get(self.sessions.key(path))
                    source_hash = ""
                    if current is not None:
                        version, result = current
                        if not result.success:
                            raise CoreOperationError(result.error)
                        state = result.data
                        if state.state_version != version:
                            raise ValueError("error.agent.stale_candidate")
                        workflow, source_hash = state.draft_workflow, state.active_source.file_hash
                    else:
                        drafts = capture.drafts[path]
                        identity = initial_color_identity(drafts, is_raw=self.core.image_sources.supports_quick_preview(path),
                            raw_display_mode=capture.context["raw"], non_raw_display_mode=capture.context["non_raw"])
                        initial = color_baseline_workflow(self.core.tools, identity)
                        workflow = self.sessions.workflow_from_drafts(initial, drafts)
                    snapshot = self.core.capture_export_workflow(path, workflow, source_hash=source_hash,
                        expected_tools=capture.generation, protection=protection)
                    item.snapshot = snapshot
                    item.registered = True
                except Exception as error:
                    item.status = "failed"
                    item.error = CoreResult.from_exception(error, message_key="export_queue.capture_failed").error.to_dict()
            check()
            return ExportBatch(uuid.uuid4().hex, preset, tuple(items), capture.origin)
        except Exception:
            for item in items:
                self.release_item(item)
            raise

    def publish(self, batch):
        try:
            if any(item.snapshot is not None and item.snapshot.tool_generation != self.core.tools.generation
                   for item in batch.items):
                raise ValueError("export_queue.render_changed")
            self.prune(required=self.metadata_bytes((batch,)))
            self.batches[batch.id] = batch
            self.prune(keep=batch.id)
            return batch.id
        except Exception:
            for item in batch.items:
                self.release_item(item)
            raise

    @staticmethod
    def metadata_bytes(batches):
        return sum(batch.metadata_bytes for batch in batches)

    @staticmethod
    def update_snapshot(batch, item, snapshot):
        batch.metadata_bytes += snapshot.size_bytes - item.snapshot.size_bytes
        item.snapshot = snapshot

    def complete(self, identity, result):
        batch = self.get(identity[0])
        item = batch.items[identity[1]]
        previous_failure = item.record_state == "failed"
        snapshot, item.record, item.error, item.record_state = result
        batch.record_failures += int(item.record_state == "failed") - int(previous_failure)
        self.update_snapshot(batch, item, snapshot)
        batch.active_items -= 1
        item.status = ("success" if item.record is not None else
                       "cancelled" if item.error and item.error["code"] == "CANCELLED" else "failed")
        self.release_item(item)
        if item.record is not None:
            self.records.insert(0, item.record)
            self.records = self.records[:500]
        self.prune(keep=batch.id)
        return batch

    def release_item(self, item):
        if item.registered:
            self.core.release_export_snapshot(item.snapshot)
            item.registered = False

    def prune(self, *, required=0, keep=""):
        terminal = [batch for batch in self.batches.values() if not batch.active_items]
        candidates = [batch for batch in terminal if batch.id != keep
                      and not batch.record_failures]
        size = self.metadata_bytes(tuple(self.batches.values()))
        for batch in candidates:
            if len(terminal) <= self.MAX_FINISHED_BATCHES and size + required <= self.MAX_METADATA_BYTES:
                break
            self.batches.pop(batch.id)
            terminal.remove(batch)
            for item in batch.items:
                self.release_item(item)
            size = self.metadata_bytes(tuple(self.batches.values()))
        if required and size + required > self.MAX_METADATA_BYTES:
            raise ValueError("export_queue.snapshot_limit")

    def get(self, identity):
        batch = self.batches.get(identity)
        if batch is None:
            raise ValueError("export_queue.expired")
        return batch

    def control(self, identity, action):
        batch = self.get(identity)
        if action == "cancel":
            batch.cancellation.cancel()
            for item in batch.items:
                if item.status == "pending":
                    item.status = "cancelled"
                    batch.active_items -= 1
                    self.release_item(item)
        elif action == "pause":
            batch.paused = True
        elif action == "resume":
            batch.paused = False
        elif action == "retry":
            if any(item.status == "exporting" for item in batch.items):
                raise ValueError("batch.busy")
            retained = []
            try:
                for item in batch.items:
                    if item.status == "failed" and item.snapshot is not None:
                        retained.append((item, self.core.retain_export_snapshot(item.snapshot)))
            except Exception:
                for _, snapshot in retained:
                    self.core.release_export_snapshot(snapshot)
                raise
            batch.cancellation = OperationCancellation()
            batch.paused = False
            batch.next_index = 0
            for item, snapshot in retained:
                self.update_snapshot(batch, item, snapshot)
                item.registered = True
                item.status, item.error = "pending", None
                batch.active_items += 1
        else:
            raise ValueError("batch.invalid_fields")
        self.prune(keep=identity)

    def next_item(self):
        for batch in self.batches.values():
            if not batch.paused and not batch.cancellation.is_cancelled():
                while batch.next_index < len(batch.items):
                    index = batch.next_index
                    batch.next_index += 1
                    item = batch.items[index]
                    if item.status == "pending":
                        item.status = "exporting"
                        return batch, index
        return None

    @staticmethod
    def record_for(snapshot, target, preset, result):
        return {"id": uuid.uuid4().hex, "source_path": snapshot.path, "source_hash": snapshot.source_hash,
                "workflow_hash": snapshot.workflow_hash, "render_hash": content_hash(snapshot.render_json),
                "render_contract": snapshot.render_json, "specification": preset.specification(),
                "specification_hash": content_hash(preset.specification()), "target_path": target,
                "product_hash": result.product_hash, "product_size": result.bytes_written,
                "output_size": result.size, "completed_at": datetime.now(timezone.utc).isoformat()}

    def delivery_state(self, path, workflow_hash, source_hash=""):
        current = self.sessions.get(path)
        if current is None:
            return "unchecked"
        state = self.core.get_session(current.core_id)
        if not state.success:
            return "unchecked"
        return "current" if (state.data.draft_workflow.workflow_hash() == workflow_hash
            and (not source_hash or state.data.active_source.file_hash == source_hash)) else "modified"

    def summary(self, identity, *, offset=0, limit=50):
        batch = self.get(identity)
        counts = {status: sum(item.status == status for item in batch.items)
                  for status in ("pending", "exporting", "success", "failed", "cancelled")}
        rows = []
        for index, item in enumerate(batch.items[offset:offset + limit], offset):
            rows.append({"index": index, "name": Path(item.path).name, "target": item.target,
                         "status": item.status, "error": item.error, "record_state": item.record_state,
                         "verification": item.verification,
                         "record": item.record["id"] if item.record else None,
                         "delivery": self.delivery_state(item.path, item.snapshot.workflow_hash, item.snapshot.source_hash) if item.snapshot else "unchecked"})
        return {"export": identity, "total": len(batch.items), "counts": counts, "paused": batch.paused,
                "record_failures": batch.record_failures,
                "cancelled": batch.cancellation.is_cancelled(), "items": rows, "offset": offset,
                "has_more": offset + limit < len(batch.items), "queued": True, "runtime_only": True}

    def record_state(self, record):
        item = self.record_item(record["id"])
        return item.record_state if item is not None else "saved"

    def record_item(self, identity):
        return next((item for batch in self.batches.values() for item in batch.items
                     if item.record is not None and item.record["id"] == identity), None)

    def update_record(self, identity, verification, *, saved=False):
        for batch in self.batches.values():
            for item in batch.items:
                if item.record is not None and item.record["id"] == identity:
                    item.verification = verification
                    if saved:
                        batch.record_failures -= int(item.record_state == "failed")
                        item.record_state, item.error = "saved", None
                    self.prune(keep=batch.id)
                    return

    def close(self):
        for batch in self.batches.values():
            batch.cancellation.cancel()
            for item in batch.items:
                self.release_item(item)
