"""Phase 1 acceptance tests: DraftStore split + ToolAdapter mapping."""

from __future__ import annotations

import os

import pytest
from PIL import Image

from src.core.draft_store import DraftStore
from src.core.edit_state import EditStateStore
from src.ui.adapters.tool_adapter import (
    ADAPTERS,
    ColorAdjustAdapter,
    ToolAdapter,
    get_adapter,
)


class TestDraftStore:
    def test_set_get_and_has(self):
        store = DraftStore()
        assert store.get_draft("/a.png", "color_adjust") is None
        store.set_draft("/a.png", "color_adjust", {"temperature": 20})
        assert store.has_draft("/a.png", "color_adjust")
        assert store.has_pending("/a.png")
        assert store.get_draft("/a.png", "color_adjust") == {"temperature": 20}

    def test_get_returns_copy(self):
        store = DraftStore()
        store.set_draft("/a.png", "t", {"v": 1})
        draft = store.get_draft("/a.png", "t")
        draft["v"] = 99
        assert store.get_draft("/a.png", "t") == {"v": 1}

    def test_discard_clear_and_callback(self):
        seen = []
        store = DraftStore(on_changed=seen.append)
        store.set_draft("/a.png", "t1", {"v": 1})
        store.set_draft("/a.png", "t2", {"v": 2})
        assert store.discard("/a.png", "t1") is True
        assert not store.has_draft("/a.png", "t1")
        assert store.has_draft("/a.png", "t2")
        assert store.clear_path("/a.png") is True
        assert not store.has_pending("/a.png")
        assert seen  # callbacks fired

    def test_snapshot_restore(self):
        store = DraftStore()
        store.set_draft("/a.png", "t", {"v": 5})
        snap = store.snapshot()
        store.clear_all()
        assert not store.has_pending("/a.png")
        store.restore(snap)
        assert store.get_draft("/a.png", "t") == {"v": 5}


class TestEditStateStoreCompat:
    def test_stash_delegates_to_drafts(self):
        store = EditStateStore()
        store.stash("/a.png", "color_adjust", {"temperature": 10})
        assert store.drafts.has_draft("/a.png", "color_adjust")
        assert store.has_tool_stash("/a.png", "color_adjust")
        assert store.get_tool_stash("/a.png", "color_adjust") == {"temperature": 10}

    def test_commit_clears_draft_and_sets_last_used(self):
        store = EditStateStore()
        store.stash("/a.png", "color_adjust", {"temperature": 10})
        store.commit("/a.png", "/a_done.png", "color_adjust", {"temperature": 10})
        assert not store.has_pending("/a.png")
        assert store.get_last_used("color_adjust") == {"temperature": 10}

    def test_initial_params_priority(self):
        store = EditStateStore()
        defaults = {"temperature": 0}
        assert store.get_initial_params("/a.png", "color_adjust", defaults) == defaults
        store.stash("/a.png", "color_adjust", {"temperature": 33})
        assert store.get_initial_params("/a.png", "color_adjust", defaults) == {"temperature": 33}


class TestColorAdjustAdapter:
    def test_protocol_conformance_and_registry(self):
        adapter = ColorAdjustAdapter()
        assert isinstance(adapter, ToolAdapter)
        assert get_adapter("color_adjust") is ADAPTERS["color_adjust"]

    def test_ui_to_workflow_units(self):
        ui = ColorAdjustAdapter().default_params()
        ui["exposure_amt"] = 35
        ui["contrast_amt"] = 28
        ui["saturation_amt"] = 20
        eng = ColorAdjustAdapter().ui_to_workflow(ui)
        assert eng["exposure"] == pytest.approx(0.35)
        assert eng["contrast"] == pytest.approx(1.1)
        assert eng["saturation"] == pytest.approx(1.1)

    def test_workflow_to_ui_engine_params(self):
        ui = ColorAdjustAdapter().workflow_to_ui(
            {"exposure": 0.35, "contrast": 1.1, "saturation": 1.1, "temperature": 12}
        )
        assert ui["exposure_amt"] == 35
        assert ui["contrast_amt"] == 28
        assert ui["saturation_amt"] == 20
        assert ui["temperature"] == 12

    def test_workflow_to_ui_ui_shaped_passthrough(self):
        src = {"exposure_amt": 50, "contrast_amt": -10, "temperature": 5}
        ui = ColorAdjustAdapter().workflow_to_ui(src)
        assert ui["exposure_amt"] == 50
        assert ui["contrast_amt"] == -10
        assert ui["temperature"] == 5

    def test_validate(self):
        adapter = ColorAdjustAdapter()
        assert adapter.validate({"exposure_amt": 50}) == []
        for params in ({"temperature": 500}, {"tint": "x"}):
            errors = adapter.validate(params)
            assert len(errors) == 1
            assert errors[0].code == "PARAMETER_INVALID"

    def test_roundtrip_is_stable(self):
        adapter = ColorAdjustAdapter()
        ui = adapter.default_params()
        ui.update({"exposure_amt": 42, "contrast_amt": -14, "highlights": 30, "shadows": -20})
        back = adapter.workflow_to_ui(adapter.ui_to_workflow(ui))
        for key in ("exposure_amt", "contrast_amt", "highlights", "shadows", "temperature", "tint"):
            assert back[key] == ui[key]


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication([])
    return app


class TestPanelDraftWiring:
    def test_panel_uses_draft_tier(self, qapp, tmp_path):
        from src.ui.panels.color_adjust_panel import ColorAdjustPanel

        image_path = str(tmp_path / "photo.png")
        Image.new("RGB", (32, 24), color=(10, 20, 30)).save(image_path)
        store = EditStateStore()
        panel = None
        try:
            panel = ColorAdjustPanel(image_path, parent=None, edit_state_store=store)
            assert panel._draft_store is store.drafts
            assert isinstance(panel.tool_adapter, ColorAdjustAdapter)
            panel.notify_params_changed({"temperature": 7}, record_stash=True)
            expected = ColorAdjustAdapter().default_params()
            expected["temperature"] = 7
            assert store.drafts.get_draft(image_path, "color_adjust") == expected
        finally:
            if panel is not None:
                panel.cleanup()
