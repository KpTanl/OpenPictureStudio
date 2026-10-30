"""成组照片与图库复审修复：路径写法、图库核验与写入、失败影响面、只读查询、网格代理、批次判定与收尾。"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtCore import QCoreApplication, QObject, Signal

from src.application.core_api import CoreAPI
from src.application.edit_sessions import EditSessionService
from src.application.export_store import ExportPreset, ExportStore
from src.application.image_source_service import ImageSourceService, path_key
from src.application.output_writer import OperationCancellation
from src.application.photo_batch import PhotoBatchService, PhotoSelection
from src.application.photo_calibration import PhotoCalibrationService
from src.application.photo_exports import PhotoExportService
from src.application.photo_library import PhotoLibraryService
from src.application.photo_library_store import _SCHEMA, PhotoLibraryStore
from src.application.photo_relocation import PhotoRelocationService
from src.application.studio_source_service import StudioLoadRequest, StudioSourceService
from src.core.contracts import WorkflowGraph
from src.core.draft_store import DraftStore
from src.core.tools.color import COLOR_UI_PARAMETERS
from tests.support import wait_until


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QCoreApplication.instance() or QCoreApplication([])


def _photo(path, size=(64, 48), color=(120, 90, 60)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, quality=95)
    return str(path)


def _color(**values):
    return {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version), **values}


def _settled(core):
    # 新建 Core 后模型扫描会在后台推进一次工具代次；固定快照要在其后捕获
    import time
    generation, stable_since = core.tools.generation, time.monotonic()
    deadline = stable_since + 10
    while time.monotonic() - stable_since < 0.5 and time.monotonic() < deadline:
        time.sleep(0.05)
        if core.tools.generation != generation:
            generation, stable_since = core.tools.generation, time.monotonic()
    return core


def _workspace(tmp_path):
    core = _settled(CoreAPI(artifact_dir=str(tmp_path / "artifacts")))
    drafts = DraftStore()
    return core, drafts, EditSessionService(core, drafts, supported_tools=("color_adjust",))


class _Metadata(QObject):
    rating_changed = Signal(str, int)
    marks_changed = Signal()

    def __init__(self):
        super().__init__()
        self.ratings = []

    def get_rating(self, path):
        return 0

    def set_ratings(self, paths, value):
        self.ratings.append((tuple(paths), value))

    def set_marks(self, paths, value):
        pass


class _Drafts(QObject):
    ready = Signal()
    loaded = True


@contextmanager
def _runner(store):
    from src.ui.services.photo_library_runner import PhotoLibraryRunner
    browser = SimpleNamespace(invalidate=lambda: None, exports=None, source_identity=lambda path: None,
                              photo_state=lambda path: {"edited": False, "workflow_hash": "", "error": ""})
    service = PhotoLibraryService(store, ImageSourceService, browser)
    metadata = _Metadata()
    runner = PhotoLibraryRunner(service, metadata, _Drafts(), before_relocation=lambda *_: None, lock_ui=lambda _locked: None)
    try:
        yield runner, metadata
    finally:
        runner.shutdown()


# R1 路径写法 ------------------------------------------------------------------

def test_export_keeps_drafts_when_listing_path_differs_from_disk_path(tmp_path):
    _photo(tmp_path / "Photos" / "IMG_0001.JPG")
    listed = os.path.normpath(str(tmp_path / "photos" / "img_0001.jpg"))
    core, drafts, sessions = _workspace(tmp_path)
    drafts.update_drafts(listed, {"color_adjust": _color(exposure_amt=100)})
    batches = PhotoBatchService(sessions)
    context = batches.capture(PhotoSelection((listed,), (listed,), listed, 1, 1))
    exports = PhotoExportService(sessions, batches, ExportStore(tmp_path / "exports"))
    (tmp_path / "out").mkdir()
    exports.configure(ExportPreset(directory=str(tmp_path / "out")))
    item = exports.get(exports.enqueue(context["scope"], "selected")).items[0]

    expected = sessions.workflow_from_drafts(sessions.initial_workflow(listed, raw_display_mode="ops",
        non_raw_display_mode="original", drafts=drafts.for_image(listed)), drafts.for_image(listed))
    assert item.path == listed
    assert item.snapshot.workflow_hash == expected.workflow_hash()
    assert [node.node_id for node in item.snapshot.workflow.nodes] == ["color"]


@pytest.mark.skipif(sys.platform != "win32", reason="联接目录只在 Windows 上建立")
def test_existing_session_reopens_through_junction(tmp_path):
    _photo(tmp_path / "real" / "a.jpg")
    link = tmp_path / "link"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path / "real")], capture_output=True)
    if created.returncode:
        pytest.skip("无法建立联接目录")
    listed = os.path.normpath(str(link / "a.jpg"))
    core = CoreAPI(artifact_dir=str(tmp_path / "artifacts"))
    bound = core.bind_source(listed).unwrap()
    session_id = core.create_bound_session(bound).unwrap().session_id

    result = StudioSourceService(core).prepare(StudioLoadRequest(1, listed, session_id))

    assert result.success, result.error
    result.data.close()


def test_delivery_facts_match_records_written_with_disk_path(tmp_path):
    source = _photo(tmp_path / "Photos" / "IMG_0002.JPG")
    store = ExportStore(tmp_path / "exports")
    store.record({"id": "r1", "completed_at": "2026-10-20T00:00:00+00:00",
                  "source_path": str(ImageSourceService.identity(source).path), "source_hash": "h", "workflow_hash": "w"})
    listed = os.path.normpath(str(tmp_path / "photos" / "img_0002.jpg"))

    facts = store.delivery_facts([listed])

    assert facts[os.path.normcase(os.path.abspath(listed))] == {("h", "w")}


def test_registration_keeps_browsed_path_and_refreshes_old_disk_path(tmp_path):
    source = _photo(tmp_path / "Photos" / "IMG_0003.JPG")
    identity = ImageSourceService.identity(source)
    store = PhotoLibraryStore(tmp_path / "library")
    listed = os.path.normpath(str(tmp_path / "photos" / "img_0003.jpg"))

    identifier = store.register(((identity.path, identity),), {})[0]
    assert store.snapshot()["photos"][0]["current_path"] == identity.path
    assert store.register(((listed, identity),), {}) == [identifier]

    photo = store.snapshot()["photos"][0]
    assert (photo["current_path"], photo["last_known_path"], photo["revision"]) == (listed, listed, 2)
    assert photo["path_key"] == path_key(source)


# R2 核验与写入 ----------------------------------------------------------------

def test_scan_reads_files_only_when_stamps_change(tmp_path):
    same = _photo(tmp_path / "same.jpg")
    touched = _photo(tmp_path / "touched.jpg", color=(10, 20, 30))
    edited = _photo(tmp_path / "edited.jpg", color=(30, 20, 10))
    hashed = []

    class Sources:
        @staticmethod
        def identity(path, *, is_cancelled=lambda: False):
            hashed.append(os.path.basename(path))
            return ImageSourceService.identity(path, is_cancelled=is_cancelled)

    def row(identifier, path, stamp_offset=0):
        identity = ImageSourceService.identity(path)
        return {"photo_id": identifier, "current_path": path, "file_hash": identity.file_hash,
                "mtime_ns": identity.mtime_ns + stamp_offset, "size": identity.size}

    photos = [row("same", same), row("touched", touched, stamp_offset=-1), row("edited", edited),
              {**row("missing", same), "current_path": str(tmp_path / "missing.jpg")}]
    Image.new("RGB", (64, 48), (200, 0, 0)).save(edited, quality=95)
    service = PhotoLibraryService(None, Sources, None)

    states, stamps, rest = service.scan(photos, lambda: False)

    assert states == {"same": "available", "touched": "available", "edited": "changed", "missing": "missing"}
    assert sorted(hashed) == ["edited.jpg", "touched.jpg"]
    assert set(stamps) == {"touched"} and rest == []
    assert service.scan(photos, lambda: False, limit=2)[2] == photos[2:]


def test_exit_cancel_keeps_accepted_metadata_write(tmp_path):
    source = _photo(tmp_path / "a.jpg")
    store = PhotoLibraryStore(tmp_path / "library")
    store.register(((source, ImageSourceService.identity(source)),), {})
    with _runner(store) as (runner, _metadata):
        wait_until(lambda: runner.service.loaded and not runner.busy, "图库加载")
        runner.cancel()
        runner.write_metadata([source], rating=4)
        wait_until(lambda: not runner.busy, "写入完成")

    assert store.snapshot()["photos"][0]["rating"] == 4


def test_unregistered_marks_do_not_wait_for_library_queue(tmp_path):
    source = _photo(tmp_path / "a.jpg")
    with _runner(PhotoLibraryStore(tmp_path / "library")) as (runner, metadata):
        wait_until(lambda: runner.service.loaded and not runner.busy, "图库加载")
        finished = []
        runner.browser_metadata([source], {"rating": 3}, lambda value, error: finished.append((value, error)))
        assert not runner.busy
        wait_until(lambda: finished, "旧评分仓写入")

    assert metadata.ratings == [((source,), 3)]
    assert finished == [({"photos": [], "saved": True}, "")]


# 启动期评分、标记排队 ---------------------------------------------------------

@contextmanager
def _wired(store):
    """与主窗口相同的接线：评分存储经图库运行器读写已登记照片，首次加载期间排队。"""
    from src.core.rating_store import RatingStore
    from src.ui.services.photo_library_runner import PhotoLibraryRunner
    browser = SimpleNamespace(invalidate=lambda: None, exports=None, source_identity=lambda path: None,
                              photo_state=lambda path: {"edited": False, "workflow_hash": "", "error": ""})
    service = PhotoLibraryService(store, ImageSourceService, browser)
    ratings = RatingStore()
    runner = PhotoLibraryRunner(service, ratings, _Drafts(), before_relocation=lambda *_: None, lock_ui=lambda _locked: None)
    ratings.set_library(service.metadata_for_path, runner.write_metadata, unavailable=runner.unavailable, defer=runner.defer)
    try:
        yield runner, ratings
    finally:
        runner.shutdown()


def test_startup_ratings_and_marks_wait_for_library_in_order(tmp_path):
    registered = _photo(tmp_path / "a.jpg")
    legacy = _photo(tmp_path / "b.jpg", color=(10, 20, 30))
    store = PhotoLibraryStore(tmp_path / "library")
    store.register(((registered, ImageSourceService.identity(registered)),), {})
    with _wired(store) as (runner, ratings):
        shown, marks = [], []
        ratings.rating_changed.connect(lambda path, value: shown.append((path, value)))
        ratings.marks_changed.connect(lambda: marks.append(ratings.get_mark(registered)))
        assert runner.unavailable() == "library.loading"

        ratings.set_rating(registered, 5)
        ratings.set_rating(registered, 2)
        ratings.set_rating(legacy, 4)
        ratings.set_marks([registered, legacy], "keep")

        assert shown == [(registered, 5), (registered, 2), (legacy, 4)] and marks == ["keep"]
        assert (ratings.get_rating(registered), ratings.get_rating(legacy), ratings.get_mark(legacy)) == (2, 4, "keep")
        assert ratings.has_any() and ratings.legacy_values(legacy) == (0, "unmarked")
        wait_until(lambda: runner.service.loaded and not runner.busy, "排队写入完成")

        photo = store.snapshot()["photos"][0]
        assert (photo["rating"], photo["mark"]) == (2, "keep")
        assert ratings.legacy_values(registered) == (0, "unmarked")
        assert ratings.legacy_values(legacy) == (4, "keep")
        assert not ratings._queued
        assert (ratings.get_rating(registered), ratings.get_mark(registered)) == (2, "keep")


def test_startup_queue_reports_unreadable_library_and_restores_display(tmp_path):
    directory = tmp_path / "library"
    directory.mkdir()
    db = sqlite3.connect(directory / "library.sqlite3")
    db.execute("PRAGMA user_version=7")
    db.close()
    source = _photo(tmp_path / "a.jpg")
    with _wired(PhotoLibraryStore(directory)) as (runner, ratings):
        failures = []
        ratings.save_failed.connect(failures.append)
        ratings.set_rating(source, 3)
        assert ratings.get_rating(source) == 3
        wait_until(lambda: runner.service.error and not runner.busy, "图库加载失败")

        assert failures == ["library.unavailable"]
        assert ratings.get_rating(source) == 0 and ratings.legacy_values(source) == (0, "unmarked")


def test_browser_marks_queue_during_loading(tmp_path):
    source = _photo(tmp_path / "a.jpg")
    store = PhotoLibraryStore(tmp_path / "library")
    store.register(((source, ImageSourceService.identity(source)),), {})
    with _wired(store) as (runner, ratings):
        finished = []
        runner.browser_metadata([source], {"mark": "reject"}, lambda value, error: finished.append((value, error)))
        assert ratings.get_mark(source) == "reject"
        wait_until(lambda: finished and runner.service.loaded and not runner.busy, "排队写入完成")

    assert finished == [({"photos": [], "queued": True}, "")]
    assert store.snapshot()["photos"][0]["mark"] == "reject"


def test_consecutive_ratings_keep_the_last_value(tmp_path):
    source = _photo(tmp_path / "a.jpg")
    store = PhotoLibraryStore(tmp_path / "library")
    store.register(((source, ImageSourceService.identity(source)),), {})
    with _wired(store) as (runner, ratings):
        wait_until(lambda: runner.service.loaded and not runner.busy, "图库加载")
        failures = []
        runner.failed.connect(failures.append)
        for value in (3, 4, 1):
            ratings.set_rating(source, value)
        wait_until(lambda: not runner.busy, "写入完成")

    assert failures == [] and store.snapshot()["photos"][0]["rating"] == 1


# R3 图库打不开 ----------------------------------------------------------------

def test_half_created_library_loads_and_foreign_schema_is_rejected(tmp_path):
    db = sqlite3.connect(tmp_path / "library.sqlite3")
    db.execute(_SCHEMA[0])
    db.commit()
    db.close()
    assert PhotoLibraryStore(tmp_path).snapshot()["photos"] == []

    other = tmp_path / "other"
    other.mkdir()
    db = sqlite3.connect(other / "library.sqlite3")
    db.execute("CREATE TABLE photos (photo_id TEXT)")
    db.commit()
    db.close()
    with pytest.raises(ValueError, match="library.store_version"):
        PhotoLibraryStore(other).snapshot()


def test_unreadable_library_blocks_metadata_but_not_editing(tmp_path):
    directory = tmp_path / "library"
    directory.mkdir()
    db = sqlite3.connect(directory / "library.sqlite3")
    db.execute("PRAGMA user_version=7")
    db.close()
    with _runner(PhotoLibraryStore(directory)) as (runner, _metadata):
        assert runner.path_blocked(str(tmp_path / "a.jpg"))
        wait_until(lambda: runner.service.error and not runner.busy, "图库加载失败")

        assert runner.service.error == "library.store_version"
        assert not runner.path_blocked(str(tmp_path / "a.jpg"))
        assert runner.unavailable() == "library.unavailable"
        with pytest.raises(ValueError, match="library.unavailable"):
            runner.write_metadata([str(tmp_path / "a.jpg")], rating=2)


# R4 只读查询与动态重算 ---------------------------------------------------------

def _snapshot(photos):
    return {"photos": photos, "keywords": [], "collections": [], "pending": [], "aliases": []}


def _library_photo(identifier, path, *, rating=0, deliveries=()):
    return {"photo_id": identifier, "current_path": path, "path_key": path_key(path), "last_known_path": path,
            "file_hash": "0" * 64, "format": ".jpg", "mtime_ns": 1, "size": 1, "rating": rating, "mark": "keep",
            "revision": 1, "keywords": [], "deliveries": list(deliveries), "export_records": []}


def test_metadata_reads_skip_filesystem_for_unregistered_names(tmp_path, monkeypatch):
    registered = _photo(tmp_path / "Real" / "IMG_0001.jpg")
    service = PhotoLibraryService(None, None, None)
    service.install(_snapshot([_library_photo("p1", registered, rating=4)]))
    calls = []
    import src.application.photo_library as module
    monkeypatch.setattr(module, "path_key", lambda path: calls.append(path) or path_key(path))

    assert all(service.metadata_for_path(str(tmp_path / f"IMG_{index:04d}.jpg")) is None for index in range(2, 2002))
    assert service.metadata_for_path(registered.upper()) == (4, "keep")
    assert calls == []
    # 路径映射在后台准备（R05），查询只读投影：同名但不同目录的文件同样不访问文件系统
    assert service.metadata_for_path(str(tmp_path / "Other" / "IMG_0001.jpg")) is None
    assert calls == []


def test_rating_changes_do_not_restart_delivery_evaluation(tmp_path):
    path = _photo(tmp_path / "a.jpg")
    service = PhotoLibraryService(None, None, None)
    service.install(_snapshot([_library_photo("p1", path)]))
    revision = service.delivery_revision

    service.install(_snapshot([_library_photo("p1", path, rating=5)]))
    assert service.delivery_revision == revision
    service.install(_snapshot([_library_photo("p1", path, rating=5, deliveries=("w",))]))
    assert service.delivery_revision == revision + 1


# R5 网格缩略图 ----------------------------------------------------------------

def test_grid_preview_uses_proxy_and_prepared_hash(tmp_path, monkeypatch):
    source = _photo(tmp_path / "large.jpg", size=(2400, 1600))
    core = _settled(CoreAPI(artifact_dir=str(tmp_path / "artifacts")))
    snapshot = core.prepare_export_snapshot(core.capture_export_workflow(source, WorkflowGraph()))
    borrowed, hashed = [], []
    original_borrow, original_identity = core.image_sources.borrow_linear, core.image_sources.identity

    def borrow(path, **options):
        borrowed.append(options.get("max_side"))
        return original_borrow(path, **options)

    monkeypatch.setattr(core.image_sources, "borrow_linear", borrow)
    monkeypatch.setattr(core.image_sources, "identity", lambda path, **options: hashed.append(path) or original_identity(path, **options))

    for expected_hashes in (1, 1):  # 首次解码自带一次核验；之后代理已在内存，预览不再读文件
        borrowed.clear()
        result = core.preview_export_snapshot(snapshot, ExportPreset(), cancellation=OperationCancellation(),
                                              max_dimension=300, proxy=True)
        frame, size, memory = result.unwrap()
        memory.close()
        assert borrowed[0] == 1600 and len(hashed) == expected_hashes
        assert size == (2400, 1600) and max(frame.size) == 300

    borrowed.clear()
    full = core.preview_export_snapshot(snapshot, ExportPreset(), cancellation=OperationCancellation(), max_dimension=300)
    full.unwrap()[2].close()
    assert borrowed[0] is None


# R6、R7 批次判定 --------------------------------------------------------------

def test_exposure_reference_comes_from_targets_not_active_photo(tmp_path):
    core, drafts, sessions = _workspace(tmp_path)
    paths = [_photo(tmp_path / f"{name}.jpg") for name in ("a", "b", "c", "s")]
    for path in paths[2:]:
        drafts.update_drafts(path, {"color_adjust": _color(process_version=2)})
    batches = PhotoBatchService(sessions)
    context = batches.capture(PhotoSelection(tuple(paths), tuple(paths), paths[3], 1, 1))
    assert context["sample_snapshot"] == ""

    identifier = batches.create(context["scope"], "exposure", delta_ev=0.3)
    summary = batches.preflight(identifier)

    assert summary["reference_version"] == 4
    assert [item["status"] for item in summary["items"]] == ["pending", "pending", "failed", "failed"]
    assert summary["items"][2]["error"]["message_key"] == "batch.exposure_version"
    for index, path in enumerate(paths[:2]):
        batches.apply_item(identifier, index, core.bind_source(path).unwrap(), OperationCancellation())
    batch = batches.get(identifier)
    assert [item.status for item in batch.items[:2]] == ["success", "success"]
    assert sessions.get(paths[0]).color_params["exposure_amt"] == 30


def test_capture_is_current_until_sample_or_selection_changes(tmp_path):
    core, drafts, sessions = _workspace(tmp_path)
    paths = [_photo(tmp_path / f"{name}.jpg") for name in ("a", "b")]
    project = core.create_session(core.open_source(paths[0]).unwrap()).unwrap()
    sample = sessions.adopt(project, path=paths[0]).unwrap()
    batches = PhotoBatchService(sessions)
    selection = PhotoSelection(tuple(paths), tuple(paths), paths[0], 1, 1)
    scope = batches.capture(selection)["scope"]

    assert batches.context_current(scope, selection)
    assert not batches.context_current(scope, PhotoSelection((paths[1],), tuple(paths), paths[0], 1, 2))
    sample.color_params = _color(exposure_amt=50)
    sessions.sync(sample)
    assert not batches.context_current(scope, selection)


def test_cancelled_batch_commit_is_reported_as_cancelled(tmp_path):
    core, drafts, sessions = _workspace(tmp_path)
    path = _photo(tmp_path / "a.jpg")
    batches = PhotoBatchService(sessions)
    identifier = batches.create(batches.capture(PhotoSelection((path,), (path,), path, 1, 1))["scope"],
                                "exposure", delta_ev=0.5)

    class LateCancel:
        checks = 0

        def is_cancelled(self):
            self.checks += 1
            return self.checks > 1

        @contextmanager
        def publication(self):
            yield not self.is_cancelled()

        def mark_published(self):
            pass

    batches.apply_item(identifier, 0, core.bind_source(path).unwrap(), LateCancel())

    assert batches.get(identifier).items[0].status == "cancelled"


# R8、R10 收尾 -----------------------------------------------------------------

def test_failed_relocation_leaves_session_projection_current(tmp_path):
    core, drafts, sessions = _workspace(tmp_path)
    old = _photo(tmp_path / "a.jpg")
    new = str(tmp_path / "moved" / "a.jpg")
    os.makedirs(os.path.dirname(new))
    shutil.copy2(old, new)
    session = sessions.adopt(core.create_session(core.open_source(old).unwrap()).unwrap(), path=old).unwrap()
    identity = ImageSourceService.identity(old)
    library = PhotoLibraryService(None, ImageSourceService, None)
    library.install(_snapshot([{**_library_photo("p1", os.path.normpath(old)), "file_hash": identity.file_hash}]))
    relocations = PhotoRelocationService(library, sessions)
    proposal = relocations.propose(library.photos["p1"], os.path.normpath(new), ImageSourceService.identity(new), "contract")

    job = relocations.acquire(proposal["proposal"])
    relocations.release(job)

    assert session.state_version == core.get_session(session.core_id).unwrap().state_version
    session.color_params = _color(exposure_amt=20)
    assert sessions.sync(session).success


def test_calibration_plans_release_snapshots_beyond_limit():
    released = []
    service = PhotoCalibrationService(None, SimpleNamespace(core=None, sessions=None), released.append)
    for index in range(10):
        service.plans[f"plan{index}"] = {"batch": "", "state": "ready",
                                         "rows": [{"base": f"base{index}", "preview": f"preview{index}"}]}
    service.plans["preparing"] = {"batch": "", "state": "preparing", "rows": [{"base": "busy", "preview": None}]}

    service._trim_plans()

    assert released == ["base0", "preview0", "base1", "preview1"]
    assert "plan0" not in service.plans and "preparing" in service.plans and len(service.plans) == 9
