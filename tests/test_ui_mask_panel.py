"""蒙版页：列表、区域与调整的投影，画笔连续涂抹归进同一块区域。"""

import copy

from PyQt6.QtCore import QEvent, QPointF, QRect, Qt
from PyQt6.QtGui import QMouseEvent

from src.core.mask_components import prepare_components
from src.ui.overlays.mask_overlay import MaskOverlay
from src.ui.panels.mask_panel import MaskPanel, area_label


def tr(key, default=None, **values):
    return key + ("(" + ",".join(f"{name}={value}" for name, value in sorted(values.items())) + ")" if values else "")


def _id(number):
    return "00000000-0000-4000-8000-%012d" % number


def _params():
    stroke = {"points": [[.2, .2], [.4, .4]], "radius": .05, "hardness": .5, "opacity": 1.0}
    return {"process_version": 4, "masks": [
        {"id": _id(1), "name": "Mask 1", "enabled": True, "params": {"exposure": .5}, "components": [
            {"id": _id(2), "name": "all", "enabled": True, "op": "add", "type": "all"},
            {"id": _id(3), "name": "erase", "enabled": False, "op": "subtract", "type": "brush", "strokes": [stroke, stroke]},
        ]},
        {"id": _id(4), "name": "Mask 2", "enabled": False, "params": {}, "components": []},
    ]}


def _panel(qapp):
    panel = MaskPanel(tr, lambda *args, **kwargs: None, lambda: None)
    panel.resize(320, 900)
    return panel


def test_empty_page_offers_ways_to_start(qapp):
    panel = _panel(qapp)
    requests = []
    panel.create_requested.connect(requests.append)
    panel.set_state({"process_version": 4, "masks": None}, None, ready=True)
    assert not panel._empty.isHidden() and panel._detail.isHidden()
    panel._quick["background"].click()
    panel._quick["brush"].click()
    assert requests == [{"kind": "background", "op": "add", "parent": None}, {"kind": "brush", "op": "add", "parent": None}]


def test_selected_mask_lists_its_areas_then_its_adjustments(qapp):
    panel = _panel(qapp)
    panel.set_state(_params(), _id(1), ready=True, component=_id(3))
    assert panel._title.text() == "Mask 1"
    rows = panel._component_rows
    assert rows[_id(2)].title == "mask.type.all"
    assert rows[_id(3)].title == "mask.brush_strokes(count=2,name=mask.type.erase)"
    assert rows[_id(3)].is_checked() and rows[_id(3)]._subtitle == "mask.disabled"
    assert panel._rows[_id(1)][0]._subtitle == "mask.area_count(count=2)"
    assert panel._rows[_id(4)][0]._subtitle == "mask.disabled"
    assert panel._areas_empty.isHidden() and panel._subtract.isEnabled()
    assert panel._parameter_rows["exposure_amt"].slider.value() == 50


def test_detecting_shows_progress_and_can_be_cancelled(qapp):
    panel = _panel(qapp)
    cancelled = []
    panel.generation_cancel_requested.connect(lambda: cancelled.append(True))
    panel.set_state(_params(), _id(1), ready=True, busy=("subject", _id(1)))
    assert not panel._busy_row.isHidden()
    assert panel._busy_label.text() == "mask.recognizing(type=mask.type.subject)"
    assert not panel._add.isEnabled() and not panel._new.isEnabled()
    panel._busy_cancel.click()
    panel.set_state(_params(), _id(1), ready=True, busy=("background", None))
    assert not panel._pending.isHidden() and panel._busy_row.isHidden()
    assert cancelled == [True]


def test_rename_happens_in_place_and_escape_keeps_the_name(qapp):
    panel = _panel(qapp)
    edits = []
    panel.edit_requested.connect(lambda operation, value: edits.append((operation, value)))
    panel.set_state(_params(), _id(1), ready=True)
    panel._begin_rename()
    assert not panel._rename.isHidden() and panel._title.isHidden()
    panel._rename.setText("Sky")
    panel._finish_rename()
    panel._begin_rename()
    panel._cancel_rename()
    assert edits == [("rename", "Sky")]
    assert panel._rename.isHidden() and not panel._title.isHidden()


def test_area_label_counts_strokes():
    brush = {"type": "brush", "op": "add", "points": [[.1, .1]], "radius": .1, "hardness": 1, "opacity": 1}
    assert area_label(brush, tr) == "mask.brush_strokes(count=1,name=mask.type.brush)"
    assert area_label({"type": "subject", "op": "subtract"}, tr) == "mask.type.subject"


class _Viewer:
    def __init__(self):
        self.tracking = []

    def setMouseTracking(self, value):
        self.tracking.append(value)

    def installEventFilter(self, _filter):
        pass

    def removeEventFilter(self, _filter):
        pass

    def update(self):
        pass

    def setCursor(self, _cursor):
        pass


def _mouse(kind, x, y):
    button = Qt.MouseButton.LeftButton
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
    if kind == QEvent.Type.MouseMove:
        button = Qt.MouseButton.NoButton
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier)


def _stroke(overlay, rect, points):
    overlay.handle_mouse_press(_mouse(QEvent.Type.MouseButtonPress, *points[0]), rect)
    for point in points[1:]:
        overlay.handle_mouse_move(_mouse(QEvent.Type.MouseMove, *point), rect)
    overlay.handle_mouse_release(_mouse(QEvent.Type.MouseButtonRelease, *points[-1]), rect)


def test_painting_keeps_every_stroke_in_one_brush_area(qapp):
    overlay, viewer = MaskOverlay(), _Viewer()
    overlay.set_viewer(viewer)
    rect = QRect(0, 0, 400, 300)
    params = {"process_version": 4, "masks": []}
    finished, ended = [], []
    overlay.creation_finished.connect(lambda: ended.append(True))

    def commit(mask):
        finished.append(copy.deepcopy(mask))
        masks = [item for item in params["masks"] if item["id"] != mask["id"]] + [mask]
        params["masks"] = masks
        prepare_components(masks, active_only=False)
        overlay.set_state(masks, mask["id"], (4000, 3000), enabled=True, component=overlay.selected_component, composite=True)

    overlay.gesture_finished.connect(commit)
    overlay.set_state([], None, (4000, 3000), enabled=True, composite=True)
    overlay.begin_creation("brush", "Brush", operation="add", brush={"radius": .02, "hardness": .5, "opacity": 1.0},
                           mask_name="Mask 1")
    _stroke(overlay, rect, [(40, 40), (80, 60), (120, 90)])
    _stroke(overlay, rect, [(200, 150), (260, 160)])
    assert overlay.creation == ("brush", "add") and not ended
    mask = params["masks"][0]
    assert mask["name"] == "Mask 1" and len(mask["components"]) == 1
    component = mask["components"][0]
    assert component["op"] == "add" and len(component["strokes"]) == 2
    assert component["strokes"][0]["points"][0] == [0.1, 40 / 300]
    overlay.cancel_interaction()
    assert ended == [True] and overlay.creation is None and viewer.tracking[-1] is False
    overlay.begin_creation("brush", "Brush", parent=mask, operation="add", target=component["id"])
    _stroke(overlay, rect, [(300, 200)])
    assert len(params["masks"][0]["components"]) == 1
    assert len(params["masks"][0]["components"][0]["strokes"]) == 3
