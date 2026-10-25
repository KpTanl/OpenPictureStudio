import copy
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from PIL import Image

from src.application.agent_service import AgentService
from src.application.audit import AuditLog
from src.application.core_api import CoreAPI
from src.application.plugin_service import PluginService
from src.application.result_types import CoreResult
from src.application.services import ExportRequest, ImageEngineAdapter, PreviewResult, validate_workflow
from src.core.artifact_store import ArtifactStore
from src.core.contracts import WorkflowGraph, WorkflowNode
from src.core.project import ProjectStore
from src.core.project.revision_store import RevisionStore
from tests.support import wait_until


def session_at(root, **kwargs):
    path = root / "source.png"
    Image.new("RGB", (12, 10), (60, 100, 180)).save(path)
    core = CoreAPI(artifact_dir=str(root / "artifacts"), **kwargs)
    session = core.create_session(core.open_source(str(path)).unwrap()).unwrap()
    return core, session.session_id, path


def graph(exposure):
    return WorkflowGraph.single_node("color_adjust", {"exposure": exposure})


def test_phase0_import_is_qt_free(tmp_path):
    result = subprocess.run([sys.executable, "-c", "import sys; from src.core.workflow import WorkflowExecutor; from src.application import CoreAPI; assert 'PyQt6' not in sys.modules"], capture_output=True)
    assert result.returncode == 0, result.stderr


def test_session_and_candidate_snapshots_are_detached(tmp_path):
    core, sid, _ = session_at(tmp_path)
    view = core.get_session(sid).unwrap()
    view.draft_workflow = graph(2)
    assert core.get_session(sid).unwrap().draft_workflow == WorkflowGraph()
    agent = AgentService(core, AuditLog(str(tmp_path / "audit.jsonl")))
    proposed = agent.propose(sid, graph(0.3), model_version="fixture").unwrap()
    proposed["before"]["nodes"].append({"node_type": "invalid"})
    proposed["after"]["nodes"][0]["params"]["exposure"] = 3
    assert agent.preview(proposed["candidate_id"]).success
    assert agent.commit(proposed["candidate_id"], confirmed=True).success
    assert core.get_session(sid).unwrap().draft_workflow == graph(0.3)


def test_phase1_parameter_roundtrip_and_finite_values():
    from src.ui.adapters.tool_adapter import ColorAdjustAdapter
    adapter = ColorAdjustAdapter()
    ui = adapter.default_params()
    ui.update(texture_amt=30, clarity_amt=-20, dehaze_amt=12, vibrance_amt=40,
              curves={"rgb": [[0, 0], [255, 255]]})
    converted = adapter.ui_to_workflow(ui)
    restored = adapter.workflow_to_ui(converted)
    assert restored == ui
    converted["curves"]["rgb"][0][1] = 50
    assert ui["curves"]["rgb"][0][1] == 0
    assert adapter.validate({"temperature": float("nan")})


def test_out_of_order_preview_and_late_cancel(tmp_path):
    entered, release = Event(), Event()

    class Preview:
        def render(self, source, workflow, **kwargs):
            if kwargs["generation"] == 1:
                entered.set()
                assert release.wait(5)
            return CoreResult.ok(PreviewResult(Image.new("RGB", (2, 2)), source.source_path,
                                              workflow.workflow_hash(), 2, 2))

    core, sid, _ = session_at(tmp_path, preview_service=Preview())
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(core.preview, sid, graph(0.1), generation=1)
        try:
            assert entered.wait(5)
            assert core.preview(sid, graph(0.8), generation=2).success
        finally:
            release.set()
        assert first.result().error.code == "CANCELLED"
    assert core.get_session(sid).unwrap().draft_workflow == graph(0.8)
    assert core.preview(sid, graph(0.3), generation=3, is_cancelled=lambda: True).error.code == "CANCELLED"
    assert core.get_session(sid).unwrap().draft_workflow == graph(0.8)


def test_agent_preview_never_restores_over_user_edit(tmp_path):
    entered, release = Event(), Event()

    class Preview:
        def render(self, source, workflow, **kwargs):
            if workflow == graph(0.3):
                entered.set()
                assert release.wait(5)
            return CoreResult.ok(PreviewResult(Image.new("RGB", (2, 2)), source.source_path,
                                              workflow.workflow_hash(), 2, 2))

    core, sid, _ = session_at(tmp_path, preview_service=Preview())
    agent = AgentService(core, AuditLog(str(tmp_path / "audit.jsonl")))
    candidate = agent.propose(sid, graph(0.3), model_version="fixture").unwrap()["candidate_id"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        preview = pool.submit(agent.preview, candidate)
        try:
            assert entered.wait(5)
            assert core.preview(sid, graph(0.8)).success
        finally:
            release.set()
        assert not preview.result().success
    assert core.get_session(sid).unwrap().draft_workflow == graph(0.8)


def test_invalid_parameters_rejected_before_engine(tmp_path):
    class Engine(ImageEngineAdapter):
        def render_source(self, source, params):
            raise AssertionError("不应执行非法工作流")

    core, sid, _ = session_at(tmp_path, engine=Engine())
    for params in ({"exposure": "bad"}, {"unknown": 1}, {"masks": [3]}, {"curves": {"rgb": [[0, 0], [0, 255]]}}):
        result = core.preview(sid, WorkflowGraph.single_node("color_adjust", params))
        assert result.error.code == "PARAMETER_INVALID"
    assert not validate_workflow(WorkflowGraph(nodes=(WorkflowNode("n", "source", version=True),))).success
    assert core.commit(sid, graph(0.3), is_cancelled=lambda: True).error.code == "CANCELLED"
    assert not list((tmp_path / "artifacts").glob("*.png"))


def test_committed_project_survives_journal_cleanup_failure(tmp_path):
    core, sid, _ = session_at(tmp_path)
    project = tmp_path / "saved.opsproj"
    assert core.save_project(sid, str(project)).success
    real_unlink = Path.unlink

    def fail_pending(path, *args, **kwargs):
        if path.name == "pending.json":
            raise PermissionError("模拟清理失败")
        return real_unlink(path, *args, **kwargs)

    with patch.object(Path, "unlink", fail_pending):
        committed = core.commit(sid, graph(0.6))
    assert committed.success, committed.error
    assert core.get_session(sid).unwrap().current_revision == committed.data.revision
    reopened = CoreAPI().open_project(str(project)).unwrap()
    assert reopened.current_revision.revision_id == committed.data.revision.revision_id
    assert reopened.document_id == core.get_session(sid).unwrap().document_id


def test_project_rejects_cyclic_history_invalid_draft_and_redo(tmp_path):
    core, sid, _ = session_at(tmp_path)
    assert core.commit(sid, graph(0.1)).success
    assert core.commit(sid, graph(0.2)).success
    project = tmp_path / "history.opsproj"
    assert core.save_project(sid, str(project)).success
    original = json.loads(project.read_text(encoding="utf-8"))
    changes = []
    cyclic = copy.deepcopy(original)
    cyclic["revisions"][1]["parent_id"] = cyclic["revisions"][2]["revision_id"]
    changes.append(cyclic)
    invalid = copy.deepcopy(original)
    invalid["draft"]["schema_version"] = 999
    changes.append(invalid)
    redo = copy.deepcopy(original)
    redo["redo_targets"] = {redo["current_revision"]: redo["revisions"][0]["revision_id"]}
    changes.append(redo)
    changes.append([])
    for changed in changes:
        project.write_text(json.dumps(changed), encoding="utf-8")
        result = CoreAPI().open_project(str(project))
        assert not result.success
        assert project.read_text(encoding="utf-8") == json.dumps(changed)


def test_project_recovers_semantically_corrupt_json(tmp_path):
    core, sid, _ = session_at(tmp_path)
    project = tmp_path / "saved.opsproj"
    assert core.save_project(sid, str(project)).success
    original = project.read_bytes()
    pending = project.with_suffix(".opsdata") / "journal" / "pending.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_bytes(original)
    document = json.loads(original)
    document["current_revision"] = "absent"
    project.write_text(json.dumps(document), encoding="utf-8")
    assert CoreAPI().open_project(str(project)).success
    assert project.read_bytes() == original


def test_project_preserves_unavailable_tool_and_blocks_execution(tmp_path):
    from tests.support import wait_for_model_discovery
    core, sid, _ = session_at(tmp_path)
    project = tmp_path / "unavailable.opsproj"
    assert core.save_project(sid, str(project)).success
    document = json.loads(project.read_text(encoding="utf-8"))
    workflow = WorkflowGraph.single_node("missing_tool", {"strength": 25})
    document["draft"] = workflow.to_dict()
    project.write_text(json.dumps(document), encoding="utf-8")
    original = project.read_bytes()
    restored = CoreAPI()
    opened = restored.open_project(str(project))
    assert opened.success, opened.error
    assert opened.data.draft_workflow == workflow
    wait_for_model_discovery(restored.models)
    inspection = restored.project_capabilities(opened.data.session_id)
    assert inspection.success and not inspection.data["can_execute"]
    assert inspection.data["issues"][0]["code"] == "NODE_NOT_REGISTERED"
    from src.ui.adapters.studio_adapter import StudioSession
    facade = StudioSession(restored, opened.data)
    assert facade.read_only and facade.issues[0]["code"] == "NODE_NOT_REGISTERED"
    before = restored.get_session(opened.data.session_id).unwrap()
    assert not facade.sync().success
    assert restored.get_session(opened.data.session_id).unwrap() == before
    target = tmp_path / "missing-export.png"
    result = restored.export(opened.data.session_id, ExportRequest(str(target)))
    assert not result.success and result.error.code == "NODE_NOT_REGISTERED"
    assert not target.exists() and project.read_bytes() == original


def test_workflow_store_rejects_wrong_hash_and_corruption(tmp_path):
    store = RevisionStore(str(tmp_path / "workflows"))
    workflow = graph(0.4)
    path = Path(store.write_workflow(workflow.workflow_hash(), workflow.to_dict()))
    path.write_text("corrupt", encoding="utf-8")
    for digest in ("../../escape", workflow.workflow_hash()):
        try:
            store.write_workflow(digest, workflow.to_dict())
        except ValueError:
            pass
        else:
            raise AssertionError("无效快照必须被拒绝")
    assert path.read_text(encoding="utf-8") == "corrupt"


def test_export_failure_and_protected_targets(tmp_path):
    class Engine(ImageEngineAdapter):
        def save_source(self, source, target_path, **kwargs):
            Path(target_path).write_bytes(b"partial")
            raise OSError("模拟编码失败")

    core, sid, source = session_at(tmp_path, engine=Engine())
    target = tmp_path / "existing.png"
    target.write_bytes(b"original")
    result = core.export(sid, ExportRequest(str(target), overwrite=True))
    assert not result.success and target.read_bytes() == b"original"
    assert not core.save_project(sid, str(source)).success
    assert not core.export(sid, ExportRequest(str(source), overwrite=True)).success
    project = tmp_path / "saved.opsproj"
    assert core.save_project(sid, str(project)).success
    assert not core.export(sid, ExportRequest(str(project), ext=".png", overwrite=True)).success


def test_failed_undo_restores_redo_target(tmp_path):
    core, sid, _ = session_at(tmp_path)
    first = core.commit(sid, graph(0.1)).unwrap().revision
    second = core.commit(sid, graph(0.2)).unwrap().revision
    assert core.checkout(sid, first.revision_id).success
    third = core.commit(sid, graph(0.3)).unwrap().revision
    assert core.checkout(sid, second.revision_id).success
    project = tmp_path / "saved.opsproj"
    assert core.save_project(sid, str(project)).success
    before = project.read_bytes()
    before_state = core.get_session(sid).unwrap()
    with patch.object(ProjectStore, "publish", side_effect=OSError("模拟项目发布失败")):
        failed = core.undo(sid)
        assert not failed.success and failed.error.code == "ARTIFACT_WRITE_FAILED"
    assert project.read_bytes() == before
    assert core.get_session(sid).unwrap() == before_state
    assert core.get_session(sid).unwrap().current_revision.revision_id == second.revision_id
    assert core.checkout(sid, first.revision_id).success
    assert core.redo(sid).unwrap().revision_id == third.revision_id


def test_audit_failure_does_not_allow_repeated_commit(tmp_path):
    core, sid, _ = session_at(tmp_path)

    class Audit:
        def append(self, event, **fields):
            if event == "agent.commit.result":
                raise OSError("模拟审计磁盘故障")

    agent = AgentService(core, Audit())
    candidate = agent.propose(sid, graph(0.3), model_version="fixture").unwrap()["candidate_id"]
    assert agent.preview(candidate).success
    result = agent.commit(candidate, confirmed=True)
    assert result.error.code == "AUDIT_WRITE_FAILED"
    assert result.error.details["operation_applied"] is True
    revision = core.get_session(sid).unwrap().current_revision
    assert not agent.commit(candidate, confirmed=True).success
    assert core.get_session(sid).unwrap().current_revision == revision


def plugin_at(root, script):
    root.mkdir()
    (root / "main.py").write_text(script, encoding="utf-8")
    manifest = {"plugin_id": "review", "version": "1.0", "manifest_version": "1.0",
                "runtime": "subprocess_python", "entrypoint": "main.py", "permissions": {"network": False},
                "declared_nodes": [{"node_id": "copy", "contract_version": "3.4.0", "is_deterministic": True}]}
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_plugin_executes_hashed_snapshot_and_audits_inputs(tmp_path):
    store = ArtifactStore(str(tmp_path / "artifacts"))
    source = tmp_path / "image.png"
    Image.new("RGB", (4, 4), "green").save(source)
    artifact = store.put(source.read_bytes())
    plugin = plugin_at(tmp_path / "plugin", 'import json,sys,shutil\nr=json.load(sys.stdin)\nshutil.copyfile(r["inputs"][0]["path"],r["output_path"])\nprint(json.dumps({"protocol":"1.0","status":"ok"}))\n')
    trusted = PluginService.inspect(plugin)[2]
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    runtime = PluginService(store, audit, trusted_hashes=[trusted])
    real_popen = subprocess.Popen
    changed = []

    def mutate_before_spawn(args, **kwargs):
        (plugin / "main.py").write_text('raise RuntimeError("changed")', encoding="utf-8")
        changed.append(args[-1])
        assert Path(args[-1]).parent != plugin
        return real_popen(args, **kwargs)

    with patch("src.application.plugin_service.subprocess.Popen", side_effect=mutate_before_spawn):
        result = runtime.run(plugin, "copy", [artifact], {}, seed=17)
    assert changed and result.success, result.error
    assert result.data.hash == artifact.hash
    request = json.loads(audit.path.read_text(encoding="utf-8").splitlines()[0])
    assert request["seed"] == 17 and request["inputs"][0]["hash"] == artifact.hash
    assert len(request["executable_hash"]) == 64
    assert runtime.run(plugin, "copy", [artifact], {}).error.code == "PERMISSION_DENIED"


def test_plugin_log_overflow_terminates_before_cleanup(tmp_path):
    plugin = plugin_at(tmp_path / "plugin", 'import sys,time\nsys.stderr.write("x" * 1100000)\nsys.stderr.flush()\ntime.sleep(10)\n')
    runtime = PluginService(ArtifactStore(str(tmp_path / "artifacts")),
                            AuditLog(str(tmp_path / "audit.jsonl")),
                            trusted_hashes=[PluginService.inspect(plugin)[2]])
    real_popen = subprocess.Popen
    processes = []
    folders = []

    def remember(args, **kwargs):
        process = real_popen(args, **kwargs)
        processes.append(process)
        folders.append(Path(kwargs["cwd"]))
        return process

    with patch("src.application.plugin_service.subprocess.Popen", side_effect=remember):
        result = runtime.run(plugin, "copy", [], {}, timeout=2)
    assert result.error.code == "PLUGIN_CRASH"
    assert "日志过大" in result.error.details["message"]
    assert all(process.poll() is not None for process in processes)
    wait_until(lambda: all(not folder.exists() for folder in folders), "插件工作目录在后台回收")


def test_panel_rejects_old_image_preview(tmp_path, panel_factory, monkeypatch):
    from src.ui.services.preview_task_runner import PreviewResult as PanelPreviewResult
    from tests.support import wait_for_panel_source

    source = tmp_path / "panel.png"
    Image.new("RGB", (12, 10), "gray").save(source)
    panel = panel_factory("color_adjust", str(source))
    wait_for_panel_source(panel, source)
    panel._preview_timer.stop()
    panel.cancel_preview()
    pending = {}

    def submit(revision, image, renderer, done, **kwargs):
        job_id = 100 + len(pending)
        pending[job_id] = (revision, done)
        return job_id

    monkeypatch.setattr(panel._preview_runner, "submit", submit)
    images = []
    panel.preview_ready.connect(images.append)
    preview = Image.new("RGB", (2, 2), "magenta")

    def request():
        return panel.submit_preview(preview, lambda image: image, panel._emit_panel_preview)

    def deliver(job_id):
        revision, done = pending[job_id]
        done(PanelPreviewResult(job_id, revision, preview))

    old = request()
    current = request()
    deliver(old)
    assert images == []
    deliver(current)
    assert len(images) == 1 and images[-1].size().width() == 2
    old = request()
    panel.reload()
    panel._preview_timer.stop()
    deliver(old)
    assert len(images) == 1
    wait_for_panel_source(panel, source)
    old = request()
    other = tmp_path / "other.png"
    Image.new("RGB", (10, 12), "blue").save(other)
    panel.set_image_path(str(other))
    panel._preview_timer.stop()
    deliver(old)
    assert len(images) == 1
    wait_for_panel_source(panel, other)
    deliver(request())
    assert len(images) == 2


def test_project_save_as_detaches_artifacts_from_old_project(tmp_path):
    core, sid, _ = session_at(tmp_path)
    core.commit(sid, graph(0.2)).unwrap()
    first = tmp_path / "first.opsproj"
    second = tmp_path / "second.opsproj"
    assert core.save_project(sid, str(first)).success
    original_artifact = Path(core.get_session(sid).unwrap().current_revision.artifact_refs[0].path)
    assert core.save_project(sid, str(second)).success
    original_artifact.unlink()
    current = core.get_session(sid).unwrap().current_revision.artifact_refs[0]
    assert Path(current.path).is_relative_to(second.with_suffix(".opsdata"))
    assert core.export(sid, ExportRequest(str(tmp_path / "export.png"))).success
    assert core.commit(sid, graph(0.4)).success
    assert CoreAPI().open_project(str(second)).success


def test_stale_project_session_cannot_overwrite_new_revision(tmp_path):
    core, sid, _ = session_at(tmp_path)
    project = tmp_path / "shared.opsproj"
    core.save_project(sid, str(project)).unwrap()
    second = CoreAPI()
    second_sid = second.open_project(str(project)).unwrap().session_id
    core.commit(sid, graph(0.3)).unwrap()
    saved = project.read_bytes()
    rejected = second.commit(second_sid, graph(0.9))
    assert rejected.error.code == "PROJECT_CHANGED"
    assert project.read_bytes() == saved
    assert second.get_session(second_sid).unwrap().current_revision.parent_id is None


def test_close_session_preserves_shared_source_until_last_close(tmp_path):
    core, sid, source = session_at(tmp_path)
    descriptor = core.open_source(str(source)).unwrap()
    second = core.create_session(descriptor).unwrap().session_id
    assert core.close_session(sid).success
    assert core.preview(second, graph(0.1)).success
    assert core.close_session(second).success
    assert core.image_sources.resources.usage()["borrowers"] == 0
    assert descriptor.source_id not in core._sources


def test_export_honors_explicit_format_and_no_clobber(tmp_path):
    core, sid, _ = session_at(tmp_path)
    for index in range(2):
        if index:
            core.commit(sid, graph(0.2)).unwrap()
        target = tmp_path / f"format-{index}.bin"
        result = core.export(sid, ExportRequest(str(target), ext="png"))
        assert result.success, result.error
        with Image.open(target) as image:
            assert image.format == "PNG"
        original = target.read_bytes()
        assert not core.export(sid, ExportRequest(str(target), ext="png")).success
        assert target.read_bytes() == original
