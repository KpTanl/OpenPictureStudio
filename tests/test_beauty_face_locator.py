"""美颜多人脸（效果 5、检测规格 3）：YuNet 找脸、关键点模型逐框确认；在模型下拉里选用，以及模型需求与界面提示。"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.application.model_service import ModelService  # noqa: E402
from src.core.beauty_detection import BeautyDetector, DETECTION_SPEC_V3, preparation_memory  # noqa: E402
from src.core.contracts.beauty import DETAIL_BEAUTY_PROFILES, detail_beauty_algorithm  # noqa: E402
from src.core.contracts.tool_resources import ToolParameterRequest  # noqa: E402
from src.core.face_regions import EYES, OVAL  # noqa: E402
from src.core.tools.managed_models import (FACE_LOCATOR_MODEL, MANAGED_BEAUTY_PARAMETERS,  # noqa: E402
                                           beauty_required_resources)
from tests.support import wait_for_model_discovery  # noqa: E402

WIDTH, HEIGHT = 1600, 1000
CATALOG = os.path.join(os.path.dirname(__file__), "..", "resources", "models", "catalog.json")


def _face_points(cx, cy, r):
    """一张合成的 478 点人脸：外轮廓是椭圆，两眼各一圈，其余点在脸中心；坐标为整图像素。"""
    points = np.tile(np.array([cx, cy], np.float64), (478, 1))
    angles = np.linspace(0, 2 * np.pi, len(OVAL), endpoint=False)
    points[list(OVAL)] = np.stack([cx + r * np.cos(angles), cy + 1.25 * r * np.sin(angles)], axis=1)
    for eye, side in zip(EYES, (-1, 1)):
        angles = np.linspace(0, 2 * np.pi, len(eye), endpoint=False)
        points[list(eye)] = np.stack([cx + side * 0.35 * r + 0.08 * r * np.cos(angles),
                                      cy - 0.2 * r + 0.08 * r * np.sin(angles)], axis=1)
    return points


class _Scene:
    """模拟两个模型：关键点模型只认出占输入长边足够大的脸，YuNet 给出预先写好的框。"""

    def __init__(self, faces, boxes, *, ratio=0.12, refuse=None):
        self.faces = faces
        self.boxes = boxes
        self.ratio = ratio
        self.refuse = dict(refuse or {})
        self.crops = []

    def landmarks(self, bounds, *, crop):
        left, top, right, bottom = bounds
        width, height = right - left, bottom - top
        found = []
        for index, (cx, cy, r) in enumerate(self.faces):
            if not (left <= cx < right and top <= cy < bottom) or 2 * r / max(width, height) < self.ratio:
                continue
            if crop and self.refuse.get(index, 0) > 0:
                self.refuse[index] -= 1
                continue
            found.append([SimpleNamespace(x=(x - left) / width, y=(y - top) / height)
                          for x, y in _face_points(cx, cy, r)])
        return found


class _Locator:
    def __init__(self, scene):
        self.scene = scene

    def setInputSize(self, size):
        assert tuple(size) == (WIDTH, HEIGHT)

    def detect(self, image):
        rows = [[x, y, w, h, *([0.0] * 10), score] for x, y, w, h, score in self.scene.boxes]
        return 1, (np.array(rows, np.float32) if rows else None)


def _detector(scene):
    detector = object.__new__(BeautyDetector)
    detector._spec = DETECTION_SPEC_V3
    detector._locator = _Locator(scene)
    detector._landmarks = lambda image, cancelled: scene.landmarks((0, 0, WIDTH, HEIGHT), crop=False)

    def window(source, bounds, cancelled):
        scene.crops.append(bounds)
        return scene.landmarks(bounds, crop=True), (bounds[2] - bounds[0], bounds[3] - bounds[1])

    detector._detect_window = window
    return detector


def _prepare(scene):
    source = SimpleNamespace(pixels=np.zeros((HEIGHT, WIDTH, 3), np.float32), alpha=None, is_proxy=False)
    return _detector(scene).prepare(source, "identity", lambda: False)


def _box(cx, cy, r, score=0.9):
    side = 2.2 * r
    return cx - side / 2, cy - side / 2, side, side, score


def _centers(snapshot):
    return sorted((round(float(np.mean([face.points[i][0] for i in OVAL])) * WIDTH),
                   round(float(np.mean([face.points[i][1] for i in OVAL])) * HEIGHT)) for face in snapshot.faces)


def test_whole_image_face_is_kept_and_the_small_face_is_found_from_its_box():
    big, small = (400, 400, 150), (1200, 600, 30)
    scene = _Scene([big, small], [_box(*big), _box(*small)])
    snapshot = _prepare(scene)
    assert snapshot.status == "ready" and snapshot.processed == 2 and snapshot.skipped == 0
    assert _centers(snapshot) == [(400, 400), (1200, 600)]
    # 大脸由整图那次调用找到，它的框不再裁切；小脸按 1.5 倍框长裁一次就找到
    assert len(scene.crops) == 1
    left, top, right, bottom = scene.crops[0]
    assert right - left == pytest.approx(1.5 * 2.2 * 30, abs=2)


def test_a_larger_crop_is_tried_when_the_tighter_one_finds_nothing():
    face = (800, 500, 30)
    scene = _Scene([face], [_box(*face)], refuse={0: 1})
    snapshot = _prepare(scene)
    assert snapshot.processed == 1 and len(scene.crops) == 2
    assert scene.crops[1][2] - scene.crops[1][0] > scene.crops[0][2] - scene.crops[0][0]


def test_boxes_without_a_face_or_below_the_size_floor_do_not_enter():
    # 一个框处没有脸（如兔子、花束）；另一个框在检测图上只有 10 像素（如挂件）
    scene = _Scene([], [(300, 300, 120, 120, 0.8), (900, 500, 10, 10, 0.9)])
    snapshot = _prepare(scene)
    assert snapshot.status == "no_face" and snapshot.faces == ()
    assert len(scene.crops) == len(DETECTION_SPEC_V3[12])


def test_a_face_seen_through_another_box_is_not_taken_twice():
    face = (800, 500, 40)
    # 第一个框是脸旁边的误报，它的裁切里能看见这张脸，但脸的中心不在框内，不算这个框的结果
    scene = _Scene([face], [(800 + 50, 500 - 40, 80, 80, 0.95), _box(*face, score=0.9)])
    snapshot = _prepare(scene)
    assert snapshot.processed == 1 and _centers(snapshot) == [(800, 500)]


def test_at_most_fifty_faces_are_delivered_keeping_the_largest():
    faces = []
    for index in range(55):
        column, row = index % 11, index // 11
        faces.append((80 + column * 140, 100 + row * 180, 14 + index * 0.5))
    scene = _Scene(faces, [_box(*face) for face in faces])
    snapshot = _prepare(scene)
    assert snapshot.processed == DETECTION_SPEC_V3[14] == 50
    kept = {center for center in _centers(snapshot)}
    assert all((round(cx), round(cy)) in kept for cx, cy, _ in faces[5:])


def test_multi_face_needs_the_locator_only_when_chosen():
    assert DETAIL_BEAUTY_PROFILES[5] == ("9", 3, 3)
    assert detail_beauty_algorithm("5-neutral") == 5
    assert detail_beauty_algorithm("5-cv4.13.0-np2.4.4-mp0.10.35") == 5
    plain = {**MANAGED_BEAUTY_PARAMETERS.defaults(3), "skin_smooth": 40, "face_detection_model": "face"}
    located = {**plain, "face_locator_model": FACE_LOCATOR_MODEL}
    assert [item.role for item in beauty_required_resources(plain, 5)] == ["face_model"]
    assert [item.role for item in beauty_required_resources(located)] == ["face_model", "face_locator"]
    required = beauty_required_resources(located, 5)
    assert [item.role for item in required] == ["face_model", "face_locator"]
    assert required[1].model_token == FACE_LOCATOR_MODEL and required[1].capability == "face_localization"
    # 模型选择不算效果强度：只选了模型时参数仍为全 0
    neutral = {**MANAGED_BEAUTY_PARAMETERS.defaults(3), "face_detection_model": "face",
               "face_locator_model": FACE_LOCATOR_MODEL}
    assert MANAGED_BEAUTY_PARAMETERS.is_neutral(neutral, 3) and beauty_required_resources(neutral, 5) == ()
    legacy = {key: value for key, value in plain.items() if key != "face_locator_model"}
    assert MANAGED_BEAUTY_PARAMETERS.normalize(legacy, 3).data["face_locator_model"] == ""
    assert preparation_memory(SimpleNamespace(pixels=np.zeros((10, 10, 3)), alpha=None),
                              detection_spec=DETECTION_SPEC_V3) > 64 * 1024 * 1024


def test_catalog_lists_yunet_and_rejects_a_broken_file(tmp_path):
    from pathlib import Path
    models = ModelService._read_catalog(Path(CATALOG))
    yunet = next(item for item in models if item.key.token == FACE_LOCATOR_MODEL)
    assert (yunet.capability, yunet.adapter, yunet.runtime, yunet.license) == ("face_localization", "yunet", "cv2", "MIT")
    assert yunet.files[0].size == 232589
    (tmp_path / yunet.files[0].name).write_bytes(b"not a model")
    with pytest.raises(ValueError, match="models.error.structure"):
        ModelService._validate_structure(yunet, tmp_path)


def test_resource_status_follows_the_record_and_the_choice(tool_services):
    from src.core.contracts.result import ErrorCode
    runtime = tool_services["render_service"].runtime
    wait_for_model_discovery(runtime.models)
    plain = {**MANAGED_BEAUTY_PARAMETERS.defaults(3), "skin_smooth": 40,
             "face_detection_model": runtime.models.default("face_detection")}
    located = {**plain, "face_locator_model": FACE_LOCATOR_MODEL}

    def requested(params, version):
        request = ToolParameterRequest("beauty", params, 3, version)
        snapshot = next(item for item in runtime.resource_status(None, requests=(request,))
                        if item.tool_id == "beauty" and item.contextual)
        return {item.role: item for item in snapshot.required_resources}, snapshot.implementation_version, snapshot.issue

    for version in ("", "5-neutral"):
        roles, implementation, issue = requested(plain, version)
        assert set(roles) == {"face_model"} and not issue
        assert implementation.startswith("5-cv")
    for version in ("", "5-neutral"):
        roles, implementation, issue = requested(located, version)
        assert set(roles) == {"face_model", "face_locator"} and implementation.startswith("5-cv") and not issue
        assert roles["face_locator"].model_token == FACE_LOCATOR_MODEL and roles["face_locator"].runtime == "cv2"
    _, _, issue = requested(located, "3-neutral")
    assert issue == ErrorCode.TOOL_INCOMPATIBLE.value


@pytest.fixture
def language(qapp):
    from src.i18n import TranslationService, get_translation_service, set_translation_service
    previous = get_translation_service()
    set_translation_service(TranslationService(default_language="zh-CN", fallback_language="en"))
    yield
    set_translation_service(previous)


def _beauty_panel(panel_factory, tmp_path):
    from PIL import Image
    from tests.support import wait_for_panel_source
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (160, 120), (120, 160, 200)).save(path)
    widget = panel_factory("beauty", path)
    wait_for_panel_source(widget, path)
    wait_for_model_discovery(widget.models)
    return widget


def test_multi_face_is_an_option_in_the_beauty_model_list(language, panel_factory, tmp_path):
    widget = _beauty_panel(panel_factory, tmp_path)
    combo = widget.model_selector.combo
    multi = combo.findData((widget._params["face_detection_model"], FACE_LOCATOR_MODEL))
    assert multi == combo.count() - 1 and "YuNet" in combo.itemText(multi)
    # 测试环境没有下载 YuNet，新开默认不选
    assert widget._params["face_locator_model"] == "" and "多人脸" in widget._multi_face_hint.text()
    committed = []
    widget._commit_params = committed.append
    combo.activated.emit(multi)
    assert committed[-1]["face_locator_model"] == FACE_LOCATOR_MODEL
    assert committed[-1]["face_detection_model"] == widget._params["face_detection_model"]


def test_beauty_panel_names_the_missing_locator_and_follows_the_record_version(language, panel_factory, tmp_path):
    from src.core.contracts.tools import ToolReference
    widget = _beauty_panel(panel_factory, tmp_path)
    versions = []
    original = widget._readiness.set_implementation
    widget._readiness.set_implementation = lambda version: (versions.append(version), original(version))
    widget.set_beauty_reference(ToolReference("beauty", 3, "beauty.detail", "5-neutral"))
    widget.set_beauty_reference(None)
    assert versions == ["5-neutral", ""]
    # 关键点模型已就绪、只缺人脸定位模型时，说明指向缺的那一个
    widget._readiness.states = lambda params: {"effect": "model_missing", "analysis": "model_missing"}
    widget._readiness.missing_model = lambda params, feature: SimpleNamespace(token=FACE_LOCATOR_MODEL)
    widget._model_status_changed()
    assert "OpenCV YuNet" in widget.model_selector.reason.text()
    widget._readiness.missing_model = lambda params, feature: SimpleNamespace(token=widget._params["face_detection_model"])
    widget._model_status_changed()
    assert "OpenCV YuNet" not in widget.model_selector.reason.text()


def _models():
    def snapshot(token, name, capability):
        model_id, revision = token.split("@")
        key = SimpleNamespace(token=token, model_id=model_id, revision=revision)
        return SimpleNamespace(descriptor=SimpleNamespace(key=key, name=name, capability=capability, adapter="x"),
                               state="installed")

    class Models:
        items = (snapshot("face-landmarker@a", "MediaPipe Face Landmarker", "face_detection"),
                 snapshot(FACE_LOCATOR_MODEL, "OpenCV YuNet", "face_localization"))

        def list(self, capability=None):
            return tuple(item for item in self.items if capability in (None, item.descriptor.capability))

        def subscribe(self, listener):
            return lambda: None

    return Models()


def test_selector_points_at_the_missing_companion_model(language):
    from src.ui.widgets.model_selector import ModelSelector
    selector = ModelSelector(_models(), "face_detection")
    try:
        selector.set_token("face-landmarker@a")
        requested = []
        selector.manage_requested.connect(lambda capability, token: requested.append((capability, token)))
        selector.set_readiness("model_missing", FACE_LOCATOR_MODEL)
        assert "OpenCV YuNet" in selector.reason.text() and not selector.install.isHidden()
        selector.install.click()
        selector.set_readiness("model_missing")
        selector.install.click()
        assert requested == [("face_localization", FACE_LOCATOR_MODEL), ("face_detection", "face-landmarker@a")]
    finally:
        selector.close_subscription()
        selector.deleteLater()


def test_selector_pairing_option_reports_both_models(language):
    from src.ui.widgets.model_selector import ModelSelector
    selector = ModelSelector(_models(), "face_detection", pairing=(FACE_LOCATOR_MODEL, "beauty.model.multi_face"))
    try:
        selector.set_token("face-landmarker@a")
        changes, single = [], []
        selector.selection_changed.connect(lambda token, paired: changes.append((token, paired)))
        selector.selected.connect(single.append)
        combo = selector.combo
        assert combo.count() == 2 and combo.currentIndex() == 0
        combo.activated.emit(1)
        assert selector.paired() == FACE_LOCATOR_MODEL
        combo.activated.emit(0)
        assert changes == [("face-landmarker@a", FACE_LOCATOR_MODEL), ("face-landmarker@a", "")] and single == []
        selector.set_token("face-landmarker@a", FACE_LOCATOR_MODEL)
        assert combo.currentIndex() == 1
        selector.set_pairing_enabled(False)
        assert not combo._items[1]["action"].isEnabled()
    finally:
        selector.close_subscription()
        selector.deleteLater()
