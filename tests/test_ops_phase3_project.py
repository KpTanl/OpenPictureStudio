from pathlib import Path
from src.core.project import ProjectDocument, ProjectStore
from src.core.project.recovery import has_pending_journal
from src.core.project.revision_store import RevisionStore
from src.application.core_api import CoreAPI
def valid_document(source_path="a.jpg"):
    from src.core.contracts import WorkflowGraph, RevisionRef
    graph = WorkflowGraph()
    revision = RevisionRef("r1", workflow_hash=graph.workflow_hash())
    return ProjectDocument({"path": source_path, "file_hash": "a" * 64}, "r1",
                           [revision.to_dict()], workflows={"r1": graph.to_dict()}, draft=graph.to_dict())


def test_project_roundtrip_atomic(tmp_path):
    path = tmp_path / "x.opsproj"
    document = valid_document()
    store = ProjectStore()
    store.save(str(path), document)
    assert store.load(str(path)).to_dict() == document.to_dict()


def test_revision_snapshot_and_pending_journal(tmp_path):
    from src.core.contracts import WorkflowGraph
    graph = WorkflowGraph()
    root = tmp_path / "data"
    path = RevisionStore(str(root / "workflows")).write_workflow(graph.workflow_hash(), graph.to_dict())
    assert Path(path).name == graph.workflow_hash() + ".json"
    assert not has_pending_journal(str(root))
    (root / "journal").mkdir()
    (root / "journal" / "pending").write_text("x", encoding="utf-8")
    assert has_pending_journal(str(root))


def test_core_api_project_missing_source_is_structured(tmp_path):
    p=tmp_path/'missing.opsproj'; ProjectStore().save(str(p), valid_document(str(tmp_path/'gone.jpg')))
    result=CoreAPI().load_project(str(p)); assert not result.success and result.error.code == 'SOURCE_NOT_FOUND'


def test_project_reopens_history_and_detects_source_changes(tmp_path):
    from PIL import Image
    from src.core.contracts import WorkflowGraph
    from src.application.services import ExportRequest

    source = tmp_path / "source.png"
    Image.new("RGB", (16, 12), (70, 120, 180)).save(source)
    core = CoreAPI()
    sid = core.create_session(core.open_source(str(source)).data).data.session_id
    first = core.commit(sid, WorkflowGraph.single_node("color_adjust", {"exposure": 0.2}))
    assert first.success
    project = tmp_path / "photo.opsproj"
    assert core.save_project(sid, str(project)).success
    second = core.commit(sid, WorkflowGraph.single_node("color_adjust", {"exposure": 0.6}))
    assert second.success
    assert core.undo(sid).success
    core.close_session(sid)
    fresh = CoreAPI()
    opened = fresh.open_project(str(project))
    assert opened.success, opened.error
    session = opened.data
    assert session.current_revision.revision_id == first.data.revision.revision_id
    assert fresh.redo(session.session_id).data.revision_id == second.data.revision.revision_id
    assert fresh.checkout(session.session_id, first.data.revision.revision_id).success
    assert fresh.export(session.session_id, ExportRequest(str(tmp_path / "export.png"))).success
    moved = tmp_path / "moved.png"
    source.rename(moved)
    assert fresh.open_project(str(project)).error.code == "SOURCE_NOT_FOUND"
    assert fresh.open_project(str(project), source_path=str(moved)).success
    Image.new("RGB", (16, 12), "red").save(moved)
    assert fresh.open_project(str(project), source_path=str(moved)).error.code == "SOURCE_CHANGED"


def test_interrupted_save_recovers_previous_document(tmp_path):
    from src.core.project.project_store import atomic_write
    project = tmp_path / "recover.opsproj"
    store = ProjectStore()
    original = valid_document("sample.png")
    store.save(str(project), original)
    pending = project.with_suffix(".opsdata") / "journal" / "pending.json"
    atomic_write(pending, project.read_bytes())
    project.write_text("{broken", encoding="utf-8")
    assert store.load(str(project)).to_dict() == original.to_dict()
    assert not pending.exists()


def test_failed_project_replace_preserves_disk_and_session(tmp_path):
    from unittest.mock import patch
    from PIL import Image
    from src.core.contracts import WorkflowGraph
    import os

    source = tmp_path / "source.png"
    Image.new("RGB", (8, 8), "gray").save(source)
    core = CoreAPI()
    session = core.create_session(core.open_source(str(source)).data).data
    target = tmp_path / "save.opsproj"
    assert core.save_project(session.session_id, str(target)).success
    original = target.read_bytes()
    revision = session.current_revision
    real_replace = os.replace
    failures = []

    def fail_target(src, dst):
        if Path(dst) == target:
            failures.append(str(dst))
            raise OSError("模拟保存中断")
        return real_replace(src, dst)

    with patch("src.core.project.project_store.os.replace", side_effect=fail_target):
        result = core.commit(session.session_id, WorkflowGraph.single_node("color_adjust", {"exposure": 0.3}))
    assert not result.success
    assert failures == [str(target)]
    assert core.get_session(session.session_id).data.current_revision == revision
    assert target.read_bytes() == original
    assert CoreAPI().open_project(str(target)).success
