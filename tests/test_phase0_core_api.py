"""Phase 0 acceptance tests: Core API wraps the existing engine headlessly."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from PIL import Image

from src.application import (
    CoreAPI,
    CommitPolicy,
    CoreError,
    CoreResult,
    ErrorCode,
    ExportRequest,
)
from src.core.contracts import ArtifactRef, RenderSource, RevisionRef, WorkflowGraph


@pytest.fixture
def sample_image(tmp_path):
    path = tmp_path / "sample.png"
    Image.new("RGB", (64, 48), color=(120, 160, 200)).save(path)
    return str(path)


class TestHeadlessImport:
    def test_core_layer_imports_without_qt(self):
        code = (
            "import sys; import src.application, src.core.contracts; "
            "assert 'PyQt6' not in sys.modules, 'Qt leaked into Core'; "
            "print('ok')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        assert proc.returncode == 0, proc.stderr
        assert "ok" in proc.stdout


class TestResultTypes:
    def test_ok_and_fail(self):
        ok = CoreResult.ok(42)
        assert ok.success and ok.data == 42 and ok.error is None

        fail = CoreResult.fail(ErrorCode.SOURCE_NOT_FOUND, "error.source.not_found")
        assert not fail.success and fail.data is None
        assert fail.error.code == "SOURCE_NOT_FOUND"

    def test_invariant_violations(self):
        with pytest.raises(ValueError):
            CoreResult(success=True, error=CoreError("X"))
        with pytest.raises(ValueError):
            CoreResult(success=False, data=1)

    def test_error_serialization(self):
        err = CoreError(ErrorCode.PARAMETER_INVALID, "e.param", {"k": 1}, retryable=True)
        restored = CoreError.from_dict(err.to_dict())
        assert restored == err

    def test_from_exception(self):
        res = CoreResult.from_exception(ValueError("boom"))
        assert res.error.code == ErrorCode.INTERNAL_ERROR.value
        assert res.error.details["exception"] == "ValueError"


class TestContracts:
    def test_source_roundtrip(self):
        src = RenderSource("sid", "/a/b.png", "deadbeef", 10, 20, "srgb8", "libraw")
        assert RenderSource.from_dict(src.to_dict()) == src

    def test_workflow_hash_is_stable_and_content_addressed(self):
        g1 = WorkflowGraph.single_node("color_adjust", {"exposure": 0.5, "contrast": 1.1})
        g2 = WorkflowGraph.single_node("color_adjust", {"contrast": 1.1, "exposure": 0.5})
        g3 = WorkflowGraph.single_node("color_adjust", {"exposure": 0.6})
        assert g1.workflow_hash() == g2.workflow_hash()
        assert g1.workflow_hash() != g3.workflow_hash()
        assert WorkflowGraph.from_dict(g1.to_dict()).workflow_hash() == g1.workflow_hash()

    def test_artifact_and_revision_roundtrip(self):
        art = ArtifactRef("h", "render", "image/png", "color_adjust", "0.11.1", "sh")
        rev = RevisionRef("r1", None, "wh", (art,), "label")
        assert ArtifactRef.from_dict(art.to_dict()) == art
        assert RevisionRef.from_dict(rev.to_dict()) == rev


class TestCoreAPI:
    def test_open_source_missing(self):
        api = CoreAPI()
        res = api.open_source("/definitely/not/here.png")
        assert not res.success and res.error.code == ErrorCode.SOURCE_NOT_FOUND.value

    def test_preview_commit_export_flow(self, sample_image, tmp_path):
        api = CoreAPI(artifact_dir=str(tmp_path / "artifacts"))

        opened = api.open_source(sample_image)
        assert opened.success, opened.error
        source = opened.data
        assert source.width == 64 and source.height == 48
        assert source.file_hash

        session = api.create_session(source).unwrap()
        wf = WorkflowGraph.single_node("color_adjust", {"exposure": 0.5})

        preview = api.preview(session.session_id, wf, compute_histogram=True)
        assert preview.success, preview.error
        assert preview.data.image.size == (64, 48)
        assert preview.data.histogram is not None

        committed = api.commit(session.session_id, wf, CommitPolicy.PROJECT)
        assert committed.success, committed.error
        assert committed.data.artifact.hash
        assert os.path.isfile(committed.data.artifact.path)

        session_after = api.get_session(session.session_id).unwrap()
        assert session_after.current_revision.revision_id == committed.data.revision.revision_id

        undo = api.undo(session.session_id)
        assert undo.success and undo.data.parent_id is None
        assert api.redo(session.session_id).success

        # Two commits -> undo/redo walks the pointer.
        wf2 = WorkflowGraph.single_node("color_adjust", {"exposure": 0.2})
        assert api.commit(session.session_id, wf2).success
        assert api.undo(session.session_id).data.revision_id == committed.data.revision.revision_id
        assert api.redo(session.session_id).success
        assert api.checkout(session.session_id, committed.data.revision.revision_id).success

        target = str(tmp_path / "out.png")
        exported = api.export(session.session_id, ExportRequest(target))
        assert exported.success, exported.error
        assert os.path.isfile(target)

        assert api.close_session(session.session_id).success
        assert not api.preview(session.session_id, wf).success

    def test_unknown_node_rejected(self, sample_image):
        api = CoreAPI()
        session = api.create_session(api.open_source(sample_image).data).unwrap()
        wf = WorkflowGraph.single_node("mystery_node", {})
        res = api.preview(session.session_id, wf)
        assert not res.success
        assert res.error.code == ErrorCode.NODE_NOT_REGISTERED.value
