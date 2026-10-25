from PIL import Image
from src.core.contracts import WorkflowGraph, WorkflowNode
from src.core.workflow import NodeRegistry, NodeSpec, WorkflowExecutor, validate
from src.core.artifact_store import ArtifactStore

def test_validator_rejects_cycle_and_unknown():
    reg = NodeRegistry(); reg.register(NodeSpec("x", execute=lambda v,p:v))
    assert not validate(WorkflowGraph.single_node("missing", {}), reg).success
    g = WorkflowGraph(nodes=(WorkflowNode("a","x",inputs=("b",)),WorkflowNode("b","x",inputs=("a",))))
    assert validate(g, reg).error.code == "CYCLE_DETECTED"

def test_executor_topological_order():
    reg = NodeRegistry(); reg.register(NodeSpec("add", execute=lambda v,p:v+p["n"]))
    g = WorkflowGraph(nodes=(WorkflowNode("a","add",{"n":2}),WorkflowNode("b","add",{"n":3},("a",))))
    out = WorkflowExecutor(reg).execute(1,g); assert out.success and out.data == 6

def test_artifact_store_is_content_addressed(tmp_path):
    s=ArtifactStore(str(tmp_path)); a=s.put(b"abc"); b=s.put(b"abc"); assert a.hash==b.hash and s.read(a)==b"abc"


def test_edges_drive_pixels_and_cancellation_precedes_execution(tmp_path):
    from src.application.core_api import CoreAPI
    from src.application.services import _workflow_executor
    source = tmp_path / "image.png"
    Image.new("RGB", (10, 8), (80, 120, 170)).save(source)
    core = CoreAPI()
    session = core.create_session(core.open_source(str(source)).data).data
    graph = WorkflowGraph(nodes=(WorkflowNode("end", "export"),
                                 WorkflowNode("crop", "crop", {"box": [1, 1, 6, 5]}),
                                 WorkflowNode("source", "source")),
                          edges=(("source", "crop"), ("crop", "end")))
    preview = core.preview(session.session_id, graph)
    assert preview.success, preview.error
    assert preview.data.image.size == (5, 4)
    committed = core.commit(session.session_id, graph)
    assert committed.success, committed.error
    from src.core.photo_engine import _pil_from
    from src.core.render_codec import read_linear
    artifact = read_linear(committed.data.artifact.path)
    assert artifact.orig_size == (5, 4)
    assert _pil_from(artifact).tobytes() == preview.data.image.tobytes()
    original = core.get_session(session.session_id).data.draft_workflow
    assert core.preview(session.session_id, WorkflowGraph(), is_cancelled=lambda: True).error.code == "CANCELLED"
    assert core.get_session(session.session_id).data.draft_workflow == original
    assert not _workflow_executor().execute(None, WorkflowGraph.single_node("crop", {"box": [2, 2, 1, 1]})).success
