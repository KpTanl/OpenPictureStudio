import json
from pathlib import Path

from PIL import Image

from src.application.core_api import CoreAPI
from src.application.audit import AuditLog
from src.application.agent_service import AgentService
from src.application.plugin_service import PluginService
from src.core.artifact_store import ArtifactStore
from src.core.contracts import WorkflowGraph


def test_agent_confirmation_stale_and_audit(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (12, 8), "gray").save(source)
    core = CoreAPI()
    session = core.create_session(core.open_source(str(source)).data).data
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    agent = AgentService(core, audit)
    graph = WorkflowGraph.single_node("color_adjust", {"exposure": 0.3})
    assert agent.propose(session.session_id, graph).error.code == "MODEL_NOT_INSTALLED"
    baseline = session.current_revision
    candidate = agent.propose(session.session_id, graph, model_version="manual-fixture-1").data["candidate_id"]
    assert agent.commit(candidate).error.code == "PERMISSION_DENIED"
    assert core.get_session(session.session_id).data.current_revision == baseline
    original_draft = session.draft_workflow
    assert agent.preview(candidate).success
    assert core.get_session(session.session_id).data.draft_workflow == original_draft
    assert core.get_session(session.session_id).data.current_revision == baseline
    assert agent.commit(candidate, confirmed=True).success
    stale = agent.propose(session.session_id, graph, model_version="manual-fixture-1").data["candidate_id"]
    core.preview(session.session_id, WorkflowGraph())
    assert agent.preview(stale).error.code == "STALE_CANDIDATE"
    events = [json.loads(line)["event"] for line in audit.path.read_text(encoding="utf-8").splitlines()]
    assert "agent.commit.result" in events


def make_plugin(root, script):
    root.mkdir()
    (root / "main.py").write_text(script, encoding="utf-8")
    manifest = {"plugin_id": "test.copy", "version": "1.0", "manifest_version": "1.0",
                "runtime": "subprocess_python", "entrypoint": "main.py",
                "permissions": {"network": False},
                "declared_nodes": [{"node_id": "copy", "contract_version": "3.4.0", "is_deterministic": True}]}
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_plugin_trust_success_crash_timeout_and_recovery(tmp_path):
    store = ArtifactStore(str(tmp_path / "artifacts"))
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    source = tmp_path / "source.png"
    Image.new("RGB", (12, 8), "blue").save(source)
    ref = store.put(source.read_bytes())
    good = make_plugin(tmp_path / "good", 'import json,sys,shutil\nr=json.load(sys.stdin)\nshutil.copyfile(r["inputs"][0]["path"], r["output_path"])\nprint(json.dumps({"protocol":"1.0","status":"ok"}))\n')
    crash = make_plugin(tmp_path / "crash", 'raise RuntimeError("fixture")\n')
    slow = make_plugin(tmp_path / "slow", 'import time\ntime.sleep(5)\n')
    assert PluginService(store, audit).run(good, "copy", [ref], {}).error.code == "PERMISSION_DENIED"
    hashes = [PluginService.inspect(path)[2] for path in (good, crash, slow)]
    runtime = PluginService(store, audit, trusted_hashes=hashes)
    result = runtime.run(good, "copy", [ref], {})
    assert result.success, result.error
    assert result.data.hash == ref.hash and result.data.external_resource_hash == hashes[0]
    assert runtime.run(crash, "copy", [ref], {}).error.code == "PLUGIN_CRASH"
    assert runtime.run(slow, "copy", [ref], {}, timeout=0.1).error.code == "TIMEOUT"
    assert runtime.run(good, "copy", [ref], {}, is_cancelled=lambda: True).error.code == "CANCELLED"
    assert runtime.run(good, "copy", [ref], {}).success
    events = [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()]
    assert sum(item["event"] == "plugin.request" for item in events) == sum(item["event"] == "plugin.result" for item in events)


def test_plugin_tampered_code_revokes_trust(tmp_path):
    root = make_plugin(tmp_path / "plugin", 'print("ok")')
    trusted = PluginService.inspect(root)[2]
    (root / "main.py").write_text('print("changed")', encoding="utf-8")
    runtime = PluginService(ArtifactStore(str(tmp_path / "artifacts")), AuditLog(str(tmp_path / "audit.jsonl")), trusted_hashes=[trusted])
    assert runtime.run(root, "copy", [], {}).error.code == "PERMISSION_DENIED"
