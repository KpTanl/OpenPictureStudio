from concurrent.futures import ThreadPoolExecutor
from threading import Event

import numpy as np
import pytest
from PIL import Image

from src.application.core_api import CoreAPI
from src.application.image_resources import ImageResourcePolicy
from src.application.image_source_service import ImageSourceService
from src.application.result_types import CoreResult
from src.application.services import ExportRequest, ImageEngineAdapter, PreviewResult
from src.core.contracts import WorkflowGraph


def open_session(core, path, color=(60, 100, 180)):
    Image.new("RGB", (12, 10), color).save(path)
    source = core.open_source(str(path)).unwrap()
    return core.create_session(source).unwrap()


def graph(exposure):
    return WorkflowGraph.single_node("color_adjust", {"exposure": exposure})


@pytest.mark.parametrize("limits", [(0, 4), (512, 4), (10000, 0)])
def test_oversized_or_disabled_cache_still_renders_and_exports(tmp_path, limits):
    engine = ImageEngineAdapter()
    sources = ImageSourceService(engine=engine, policy=ImageResourcePolicy(
        source_cache_bytes=limits[0], source_cache_items=limits[1], proxy_cache_bytes=0,
    ))
    core = CoreAPI(
        engine=engine,
        image_source_service=sources,
        artifact_dir=str(tmp_path / "artifacts"),
    )
    session = open_session(core, tmp_path / "source.png")
    assert core.preview(session.session_id, graph(0.2)).success
    output = tmp_path / "export.png"
    assert core.export(session.session_id, ExportRequest(str(output))).success
    with Image.open(output) as image:
        assert image.size == (12, 10)
    usage = sources.resources.usage()
    assert usage["bytes"] == usage["reserved_bytes"] == usage["borrowers"] == 0


def test_eviction_preserves_history_and_reloads_identical_pixels(tmp_path):
    class Engine(ImageEngineAdapter):
        def __init__(self):
            self.loaded = []

        def load_engine_source(self, path, **kwargs):
            self.loaded.append(path)
            return super().load_engine_source(path, **kwargs)

    engine = Engine()
    sources = ImageSourceService(engine=engine, policy=ImageResourcePolicy(
        source_cache_bytes=1440, source_cache_items=1, proxy_cache_bytes=0,
    ))
    core = CoreAPI(
        engine=engine,
        image_source_service=sources,
        artifact_dir=str(tmp_path / "artifacts"),
    )
    first = open_session(core, tmp_path / "first.png")
    sid = first.session_id
    core.edit_workflow(sid, graph(0.2)).unwrap()
    before = core.preview(sid, graph(0.2), update_draft=False).unwrap().image
    core.edit_workflow(sid, graph(0.8)).unwrap()
    history = core.history_state(sid, include_revisions=True).unwrap()
    for index in range(12):
        open_session(core, tmp_path / f"other-{index}.png")
        usage = sources.resources.usage()
        assert usage["bytes"] <= 1440
        assert usage["entries"] <= 1
    assert core.history_state(sid, include_revisions=True).unwrap() == history
    core.undo(sid).unwrap()
    assert core.get_session(sid).unwrap().draft_workflow == graph(0.2)
    after = core.preview(sid, graph(0.2), update_draft=False).unwrap().image
    np.testing.assert_array_equal(np.asarray(before), np.asarray(after))
    assert engine.loaded.count(str(tmp_path / "first.png")) == 2
    core.redo(sid).unwrap()
    assert core.get_session(sid).unwrap().draft_workflow == graph(0.8)


@pytest.mark.parametrize("change", ["replace", "delete", "during_decode"])
def test_reload_rejects_changed_source_without_changing_session(
    tmp_path, monkeypatch, change
):
    engine = ImageEngineAdapter()
    sources = ImageSourceService(engine=engine, policy=ImageResourcePolicy(
        source_cache_items=1, proxy_cache_bytes=0,
    ))
    core = CoreAPI(engine=engine, image_source_service=sources,
                   artifact_dir=str(tmp_path / "artifacts"))
    path = tmp_path / "source.png"
    first = open_session(core, path)
    core.edit_workflow(first.session_id, graph(0.2)).unwrap()
    before = core.get_session(first.session_id).unwrap()
    open_session(core, tmp_path / "other.png")
    source_ids = set(core._sources)
    if change == "replace":
        Image.new("RGB", (12, 10), "red").save(path)
    elif change == "delete":
        path.unlink()
    else:
        original = core._engine.load_engine_source

        def mutate(source_path, **kwargs):
            result = original(source_path, **kwargs)
            Image.new("RGB", (12, 10), "red").save(source_path)
            return result

        monkeypatch.setattr(core._engine, "load_engine_source", mutate)
    result = core.preview(first.session_id, graph(0.7))
    assert result.error.code == (
        "SOURCE_NOT_FOUND" if change == "delete" else "SOURCE_CHANGED"
    )
    assert core.get_session(first.session_id).unwrap() == before
    assert set(core._sources) == source_ids


@pytest.mark.parametrize("close_during_render", [False, True])
def test_inflight_preview_survives_eviction_and_session_close(
    tmp_path, close_during_render
):
    entered, release = Event(), Event()

    class Preview:
        def render(self, source, workflow, **kwargs):
            snapshot = source.pixels.copy()
            entered.set()
            assert release.wait(10)
            np.testing.assert_array_equal(source.pixels, snapshot)
            return CoreResult.ok(
                PreviewResult(
                    Image.new("RGB", (12, 10)),
                    source.source_path,
                    workflow.workflow_hash(),
                    12,
                    10,
                )
            )

    core = CoreAPI(
        pixel_cache_max_items=1,
        preview_service=Preview(),
        artifact_dir=str(tmp_path / "artifacts"),
    )
    first = open_session(core, tmp_path / "source.png")
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(core.preview, first.session_id, graph(0.1))
        try:
            assert entered.wait(10)
            open_session(core, tmp_path / "other.png")
            assert core.image_sources.resources.usage()["borrowers"] >= 1
            if close_during_render:
                assert core.close_session(first.session_id).success
        finally:
            release.set()
        result = running.result(timeout=10)
        if close_during_render:
            assert result.error.code == "CANCELLED"
        else:
            assert result.success
            assert core.close_session(first.session_id).success
    assert core.preview(first.session_id, graph(0.1)).error.code == "SESSION_NOT_FOUND"


def test_shared_sessions_preserve_source_until_last_close(tmp_path):
    core = CoreAPI(artifact_dir=str(tmp_path / "artifacts"))
    first = open_session(core, tmp_path / "source.png")
    second = core.create_session(first.active_source).unwrap()
    assert core.close_session(first.session_id).success
    assert core.source_identity(second.session_id) is not None
    assert core.close_session(second.session_id).success
    assert core.source_identity(second.session_id) is None
    assert core.image_sources.resources.usage()["borrowers"] == 0
    core.image_sources.close()
    assert core.image_sources.resources.usage()["bytes"] == 0


def test_evicted_project_can_commit_export_and_reopen(tmp_path):
    core = CoreAPI(pixel_cache_max_items=1, artifact_dir=str(tmp_path / "artifacts"))
    first = open_session(core, tmp_path / "source.png")
    project = tmp_path / "project.opsproj"
    core.save_project(first.session_id, str(project)).unwrap()
    open_session(core, tmp_path / "other.png")
    committed = core.commit(first.session_id, graph(0.4)).unwrap()
    core.export(first.session_id, ExportRequest(str(tmp_path / "result.png"))).unwrap()
    reopened = CoreAPI(pixel_cache_max_items=0).open_project(str(project)).unwrap()
    assert reopened.current_revision.revision_id == committed.revision.revision_id
    assert reopened.draft_workflow == graph(0.4)
