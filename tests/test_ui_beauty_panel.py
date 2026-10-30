"""美颜面板：参数行、人脸检测状态的标记与说明、用不了的参数置灰；另含旧降噪与旧画质增强面板的参数行。"""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

from src.core.tools import beauty_backend  # noqa: E402
from src.core.contracts.tools import ToolReference  # noqa: E402
from src.core.tools.beauty import BEAUTY_PARAMETERS, LANDMARK_SETTINGS  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.panels.ai_enhance_panel import LegacyAIEnhancePanel  # noqa: E402
from src.ui.panels.beauty_panel import LegacyBeautyPanel  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from src.ui.widgets.parameter_row import ParameterRow  # noqa: E402
from tests.support import wait_for_model_discovery, wait_for_panel_source  # noqa: E402

_SETTINGS = [field.name for field in BEAUTY_PARAMETERS.fields]


@pytest.fixture
def theme(qapp):
    previous = qapp.styleSheet(), qapp.property("ops.theme"), QPalette(qapp.palette())
    manager = ThemeManager()
    manager.apply("dark")
    yield manager
    qapp.setStyleSheet(previous[0])
    qapp.setProperty("ops.theme", previous[1])
    qapp.setPalette(previous[2])


@pytest.fixture
def language(qapp):
    previous = get_translation_service()
    service = TranslationService(default_language="zh-CN", fallback_language="en")
    set_translation_service(service)
    yield service
    set_translation_service(previous)


@pytest.fixture
def photo(tmp_path):
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (160, 120), (120, 160, 200)).save(path)
    return path


@pytest.fixture
def panel(theme, language, panel_factory, photo):
    """没有安装人脸模型时的美颜面板。"""
    widget = panel_factory("beauty", photo)
    widget.resize(308, 900)
    widget.show()
    wait_for_panel_source(widget, photo)
    wait_for_model_discovery(widget.models)
    widget._models_changed()
    changes = []
    widget.params_changed.connect(changes.append)
    return widget, changes


def _make_ready(widget, monkeypatch=None):
    """按模型已就绪投影；给出 monkeypatch 时同时跳过借用模型的准备，使参数当场发布。"""
    widget._readiness.states = lambda params: {"effect": "ready", "analysis": "ready"}
    widget._model_status_changed()
    if monkeypatch is not None:
        monkeypatch.setattr(widget._render_service, "needs_preparation", lambda tool_id, params: False)


def test_landmark_settings_are_beauty_parameters():
    assert set(LANDMARK_SETTINGS) < set(_SETTINGS)


def test_panel_lists_every_setting_as_a_parameter_row(panel):
    widget, _ = panel
    assert list(widget._group_cards) == ["skin", "eye", "shape", "detail"]
    assert [card.title() for card in widget._group_cards.values()] == ["皮肤", "眼部", "脸型", "精修"]
    rows = widget._parameter_rows
    assert set(rows) == set(_SETTINGS) and all(isinstance(row, ParameterRow) for row in rows.values())
    for field in BEAUTY_PARAMETERS.fields:
        slider = rows[field.name].slider
        assert (slider.minimum(), slider.maximum(), rows[field.name].value()) == (
            field.minimum, field.maximum, field.default)
    assert rows["skin_smooth"].label() == "磨皮" and rows["eye_bag_remove"].label() == "去眼袋"
    assert widget._btn_auto_beauty.text() == "自动美颜" and widget._btn_preset.text() == "推荐预设"
    # 重置在侧栏标题栏，面板里不再另放
    assert not hasattr(widget, "_btn_reset_beauty")


def test_missing_model_greys_out_the_settings_and_the_selector_explains(panel):
    widget, changes = panel
    selector = widget.model_selector
    assert selector.readiness() == "model_missing" and selector.gap.isVisible()
    assert all(not row.isEnabled() for row in widget._parameter_rows.values())
    assert not widget._btn_auto_beauty.isEnabled() and not widget._btn_preset.isEnabled()
    # 原因由模型选择说明，面板不再重复一遍
    assert widget._engine_badge.isHidden() and widget._engine_hint.isHidden()

    _make_ready(widget)
    assert all(row.isEnabled() for row in widget._parameter_rows.values())
    assert widget._btn_auto_beauty.isEnabled() and widget._btn_preset.isEnabled()
    # 新建的美颜是“保留细节”算法：标记写明算法，下面一行说明它做什么
    assert widget._engine_badge.isVisible() and widget._engine_badge.kind() == "success"
    assert widget._engine_badge.text() == "保留细节 · 五官检测可用"
    assert widget._engine_hint.isVisible() and widget._engine_hint.text() == widget._tr("beauty.detail.description_v3")
    assert not changes


def test_edits_commit_and_backfill_is_silent(panel, monkeypatch):
    widget, changes = panel
    _make_ready(widget, monkeypatch)
    rows = widget._parameter_rows
    rows["skin_smooth"].slider.edit_value(40)
    assert changes[-1]["skin_smooth"] == 40 and rows["skin_smooth"].value_edit.text() == "40"
    rows["eye_distance"].value_edit.setText("-25")
    rows["eye_distance"].value_edit.editingFinished.emit()
    assert changes[-1]["eye_distance"] == -25 and rows["eye_distance"].value_edit.text() == "-25"
    rows["eye_distance"].reset()
    assert changes[-1]["eye_distance"] == 0 and rows["eye_distance"].value_edit.text() == "0"

    # 一次拖动是一个编辑组，松手即结束
    finished = []
    widget.edit_group_finished.connect(lambda: finished.append(True))
    slider = rows["sharpen"].slider
    slider.setSliderDown(True)
    group = widget.edit_group
    slider.edit_value(10)
    slider.edit_value(20)
    assert group is not None and widget.edit_group == group
    slider.setSliderDown(False)
    assert widget.edit_group is None and finished == [True]

    count = len(changes)
    widget.restore_params({**widget.parameters, "skin_smooth": 5, "sharpen": 0, "nose_slim": 30})
    assert len(changes) == count
    assert rows["skin_smooth"].value() == 5 and rows["nose_slim"].value() == 30
    # “保留细节”的推荐预设要先分析人脸；固定数值的推荐预设属于旧的受管美颜引用
    widget.set_beauty_reference(ToolReference("beauty", 3, "beauty.managed", "4-neutral"))
    widget._btn_preset.click()
    assert changes[-1]["skin_smooth"] == 50 and rows["skin_smooth"].value() == 50
    assert changes[-1]["face_detection_model"] == widget.parameters["face_detection_model"]
    widget.reset()
    assert all(row.value() == 0 for row in rows.values())


def test_panel_follows_a_language_switch_without_inline_styles(panel, language):
    widget, changes = panel
    _make_ready(widget)
    language.set_language("en")
    widget.retranslate_ui()
    assert widget._group_cards["detail"].title() == language.translate("beauty.detail_group")
    assert widget._parameter_rows["teeth_whiten"].label() == language.translate("beauty.detail.teeth_whiten")
    assert not any(row.label().endswith((":", "：")) for row in widget._parameter_rows.values())
    assert widget._btn_preset.text() == language.translate("beauty.action.preset")
    assert widget._engine_badge.text() == language.translate("beauty.detail.ready")
    assert not changes
    assert not any(child.styleSheet() for child in [widget, *widget.findChildren(QWidget)])


@pytest.mark.parametrize("backend,kind,blocked,hint_key", [
    (None, "warning", set(_SETTINGS), "error.tool.resource_missing"),
    ("haar", "warning", set(LANDMARK_SETTINGS), "beauty.landmarks_required"),
    ("mediapipe_tasks", "success", set(), ""),
])
def test_legacy_panel_projects_the_detection_backend(theme, language, tool_services, photo, monkeypatch,
                                                     backend, kind, blocked, hint_key):
    """打开旧参数时用的面板：按本机可用的人脸检测决定哪些参数能用。"""
    backends = () if backend is None else (SimpleNamespace(name=backend),)
    monkeypatch.setattr(beauty_backend, "available_backends", lambda: backends)
    widget = LegacyBeautyPanel(photo, **tool_services)
    try:
        widget.show()
        assert widget._engine_badge.isVisible() and widget._engine_badge.kind() == kind
        assert widget._engine_badge.text() == language.translate(
            "beauty.backend." + (backend or "unavailable"))
        assert {key for key, row in widget._parameter_rows.items() if not row.isEnabled()} == blocked
        assert widget._engine_hint.isVisible() == bool(hint_key)
        if hint_key:
            assert widget._engine_hint.text() == language.translate(hint_key)
        usable = blocked != set(_SETTINGS)
        assert widget._btn_auto_beauty.isEnabled() == usable and widget._btn_preset.isEnabled() == usable
    finally:
        widget.cleanup()
        widget.deleteLater()


def test_legacy_denoise_and_enhance_panels_use_parameter_rows(theme, language, panel_factory, tool_services, photo):
    denoise = panel_factory("denoise", photo)
    changes = []
    denoise.params_changed.connect(changes.append)
    rows = denoise._parameter_rows
    assert list(rows) == ["strength", "color_strength", "sharpen"]
    assert [row.label() for row in rows.values()] == ["强度", "彩色噪点", "降噪后锐化"]
    rows["color_strength"].slider.edit_value(35)
    assert changes[-1]["color_strength"] == 35 and rows["color_strength"].value_edit.text() == "35"
    count = len(changes)
    denoise.restore_params({**denoise.parameters, "strength": 12, "color_strength": 0})
    assert len(changes) == count and rows["strength"].value() == 12 and rows["color_strength"].value() == 0
    language.set_language("en")
    denoise.retranslate_ui()
    assert rows["sharpen"].label() == language.translate("denoise.sharpen_label")
    language.set_language("zh-CN")

    enhance = LegacyAIEnhancePanel(photo, **tool_services)
    try:
        changes = []
        enhance.params_changed.connect(changes.append)
        row = enhance._strength_row
        assert isinstance(row, ParameterRow) and row.label() == "强度" and row.value() == 50
        # 旧画质增强的参数要先准备资源再发布，这里只看面板自己的参数
        row.slider.edit_value(70)
        assert enhance.parameters["strength"] == 70 and row.value_edit.text() == "70"
        count = len(changes)
        enhance.restore_params({**enhance.parameters, "strength": 20})
        assert len(changes) == count and row.value() == 20
        for widget in (denoise, enhance):
            assert not any(child.styleSheet() for child in [widget, *widget.findChildren(QWidget)])
    finally:
        enhance.cleanup()
        enhance.deleteLater()
