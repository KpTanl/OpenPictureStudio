"""模型选择、下拉里每一项的说明，以及带模型选择的面板怎样取得就绪状态；另含各面板共用的边距。"""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

from resources.styles.theme_variables import SPACING_MD, px  # noqa: E402
from src.application.tool_feature_readiness import ToolFeatureReadiness  # noqa: E402
from src.core.tools.managed_models import BACKGROUND_RESOURCE_FEATURES, MANAGED_BG_PARAMETERS  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.panels.registry import PANEL_TYPES  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from src.ui.widgets.apple_menu import AppleDropDown  # noqa: E402
from src.ui.widgets.collapsible_card import CollapsibleCard  # noqa: E402
from src.ui.widgets.model_selector import ModelSelector  # noqa: E402
from tests.support import wait_for_model_discovery, wait_for_panel_source  # noqa: E402


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


class _Models:
    """只有列表与订阅的模型服务替身。"""

    def __init__(self, *items):
        self.items = list(items)
        self.subscribers = []

    def list(self, capability):
        return [SimpleNamespace(state=state, descriptor=SimpleNamespace(
            adapter=adapter, name=name, key=SimpleNamespace(token=token)))
            for name, token, state, adapter in self.items]

    def subscribe(self, callback):
        self.subscribers.append(callback)
        return lambda: self.subscribers.remove(callback)


@pytest.fixture
def selector(theme, language):
    models = _Models(("NAFNet", "nafnet@1", "installed", "nafnet"), ("SCUNet", "scunet@1", "missing", "scunet"),
                     ("内置", "bundled@1", "installed", "bundled"))
    widget = ModelSelector(models, "denoise")
    widget.resize(300, 80)
    widget.show()
    yield widget, models
    widget.close_subscription()
    widget.deleteLater()


def test_dropdown_item_note_shows_in_the_list_and_on_the_button(theme):
    combo = AppleDropDown()
    combo.addItem("NAFNet", "a")
    combo.addItem("SCUNet", "b")
    combo.setItemNote(0, "已安装")
    combo.setItemNote(1, "未安装")
    actions = combo._menu.actions()
    assert [action.text() for action in actions] == ["NAFNet\t已安装", "SCUNet\t未安装"]
    assert combo.itemText(1) == "SCUNet" and combo.itemNote(1) == "未安装"
    assert combo.text() == "NAFNet" and combo._shown_note() == "已安装"
    combo.setCurrentIndex(1)
    assert combo.text() == "SCUNet" and combo._shown_note() == "未安装"
    # 改文字不丢说明；整个下拉的说明优先于所选项的说明
    combo.setItemText(1, "SCUNet 2")
    assert actions[1].text() == "SCUNet 2\t未安装"
    combo.set_note("不可用")
    assert combo._shown_note() == "不可用"
    combo.setItemNote(1, "")
    assert actions[1].text() == "SCUNet 2" and combo.itemNote(5) == ""
    combo.deleteLater()


def test_selector_lists_models_with_their_state_as_a_note(selector):
    widget, _models = selector
    combo = widget.combo
    # 随应用提供的模型不在选择里；状态不再拼进名称
    assert [combo.itemText(index) for index in range(combo.count())] == ["NAFNet", "SCUNet"]
    assert [combo.itemNote(index) for index in range(combo.count())] == ["已安装", "未安装"]
    assert not hasattr(widget, "manage")
    assert widget.readiness() == "ready" and widget.gap.isHidden()


def test_selector_backfill_is_silent_and_user_choice_is_announced(selector):
    widget, _models = selector
    chosen = []
    widget.selected.connect(chosen.append)
    widget.set_token("scunet@1")
    assert widget.combo.currentIndex() == 1 and widget.combo.text() == "SCUNet" and chosen == []
    widget.combo._menu.actions()[0].trigger()
    assert chosen == ["nafnet@1"] and widget.token() == "nafnet@1"
    # 目录里已经没有的模型仍然列出，并标为未安装
    widget.set_token("gone@1")
    assert widget.combo.itemText(2) == "gone@1" and widget.combo.itemNote(2) == "未安装"
    assert chosen == ["nafnet@1"]


def test_selector_explains_the_gap_it_is_told_about(selector, language, monkeypatch):
    widget, models = selector
    opened = []
    widget.manage_requested.connect(lambda capability, token: opened.append((capability, token)))
    widget.set_readiness("model_missing")
    assert widget.gap.isVisible() and widget.reason.text() == "需要先安装这个模型"
    assert widget.install.isVisible() and widget.install.text() == "安装模型…"
    widget.install.click()
    # 控件自己不打开窗口，只报告要管理的是哪一类、哪一个模型
    assert opened == [(widget.capability, widget.token())]
    # 不是缺模型的缺口只说明原因，没有安装入口
    widget.set_readiness("runtime_missing")
    assert widget.gap.isVisible() and widget.reason.text() == "需要安装运行库" and widget.install.isHidden()
    language.set_language("en")
    widget.retranslate_ui()
    assert widget.reason.text() == language.translate("tool_manager.state.runtime_missing")
    assert widget.combo.itemNote(1) == language.translate("models.state.missing")
    # 模型状态变化后说明原位更新，选择不变
    models.items[1] = ("SCUNet", "scunet@1", "installed", "scunet")
    widget.set_token("scunet@1")
    widget.retranslate_ui()
    assert widget.combo.itemNote(1) == language.translate("models.state.installed") and widget.token() == "scunet@1"
    widget.set_readiness("ready")
    assert widget.gap.isHidden()


def test_feature_readiness_depends_only_on_the_chosen_models(tool_services):
    runtime = tool_services["render_service"].runtime
    wait_for_model_discovery(runtime.models)
    defaults = MANAGED_BG_PARAMETERS.defaults(3)
    defaults["segmentation_model"] = runtime.models.default("segmentation")

    def fresh(params):
        return dict(ToolFeatureReadiness(runtime, runtime.models, "bg_remove", MANAGED_BG_PARAMETERS,
                                         BACKGROUND_RESOURCE_FEATURES).states(params))

    ai, color = fresh({**defaults, "mode": "ai"}), fresh({**defaults, "mode": "color"})
    assert ai == color and set(ai) == {"color", "ai"}
    assert ai["color"] == "ready" and ai["ai"] == "model_missing"
    # 同一组选择只查询一次，refresh 之后重新查询
    readiness = ToolFeatureReadiness(runtime, runtime.models, "bg_remove", MANAGED_BG_PARAMETERS,
                                     BACKGROUND_RESOURCE_FEATURES)
    calls = []
    overview = readiness._resources.overview
    readiness._resources.overview = lambda context: calls.append(True) or overview(context)
    readiness.states({**defaults, "mode": "ai"})
    readiness.states({**defaults, "mode": "color"})
    assert len(calls) == 1
    readiness.refresh()
    readiness.states(defaults)
    assert len(calls) == 2


def test_background_panel_only_asks_for_the_model_in_ai_mode(theme, language, panel_factory, photo):
    panel = panel_factory("bg_remove", photo)
    panel.show()
    wait_for_panel_source(panel, photo)
    wait_for_model_discovery(panel.models)
    panel._models_changed()
    selector = panel.model_selector
    assert panel.parameters["mode"] == "ai"
    assert not panel.models_ready and selector.readiness() == "model_missing"
    assert selector.gap.isVisible() and selector.install.isVisible()
    changes = []
    panel.availability_changed.connect(lambda: changes.append(panel.models_ready))
    panel.restore_params({**panel.parameters, "mode": "color"})
    assert panel.models_ready and selector.readiness() == "ready" and selector.gap.isHidden()
    assert changes == [True]
    # 模型装好后原位恢复
    panel.restore_params({**panel.parameters, "mode": "ai"})
    panel._readiness.states = lambda params: {"color": "ready", "ai": "ready"}
    panel._model_status_changed()
    assert panel.models_ready and selector.gap.isHidden() and changes == [True, False, True]


def test_beauty_panel_names_the_missing_model_before_any_slider_moves(theme, language, panel_factory, photo):
    panel = panel_factory("beauty", photo)
    panel.show()
    wait_for_panel_source(panel, photo)
    wait_for_model_discovery(panel.models)
    panel._models_changed()
    selector = panel.model_selector
    # 参数都是零时用不到模型，可以提交；缺口仍然先说明
    assert panel.models_ready and selector.readiness() == "model_missing" and selector.gap.isVisible()
    panel.restore_params({**panel.parameters, "skin_smooth": 30})
    assert not panel.models_ready and selector.gap.isVisible()


@pytest.mark.parametrize("tool_id", sorted(PANEL_TYPES))
def test_panel_content_lines_up_with_its_group_titles(theme, language, panel_factory, photo, tool_id):
    panel = panel_factory(tool_id, photo)
    margins = panel.layout().contentsMargins()
    edge = px(SPACING_MD)
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (edge, edge, edge, edge)
    cards = panel.findChildren(CollapsibleCard)
    assert cards
    for card in cards:
        content = card.findChild(QWidget, "collapsibleContent").layout()
        for index in range(content.count()):
            nested = content.itemAt(index).layout()
            if nested is not None:
                inner = nested.contentsMargins()
                assert (inner.left(), inner.top(), inner.right(), inner.bottom()) == (0, 0, 0, 0), card.title()
