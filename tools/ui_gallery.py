"""组件样例页：按三套主题和两种语言列出界面组件及其状态，仅供开发时核对，不进入发行包。

用法：
    python tools/ui_gallery.py                 打开样例页
    python tools/ui_gallery.py --shots <目录>   离屏导出各主题与语言的截图后退出
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if "--shots" in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtCore import QSize, Qt  # noqa: E402
from PySide6.QtGui import QAction, QFont, QFontDatabase  # noqa: E402
from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar,  # noqa: E402
                             QScrollArea, QVBoxLayout, QWidget)

from resources.styles.semantic_colors import SEMANTIC_KEYS, TRACK_GRADIENTS, mixer_track_colors  # noqa: E402
from resources.styles.theme_variables import (EDIT_SIDEBAR_WIDTH_WIDE, FONT_FAMILIES, SPACING_LG, SPACING_MD,  # noqa: E402
                                              SPACING_SM, SPACING_XL, SPACING_XS, px, TOP_BAR_HEIGHT)
from src.application.display_histogram import DisplayHistogramRequest  # noqa: E402
from src.application.image_info import ImageInfo, ImageInfoRequest, ImageInfoResult, PhotoMetadata  # noqa: E402
from src.application.assistant_models import AssistantContext, AssistantModel, context_defaults  # noqa: E402
from src.core.contracts.workflow import WorkflowGraph  # noqa: E402
from src.core.contracts.image_display import RawDisplayMode  # noqa: E402
from src.core.tools.color import LEGACY_OPS_LOOK, OPS_LOOK  # noqa: E402
from src.core.contracts.result import CoreError, ErrorCode  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.icons import themed_icon  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens, set_theme_manager  # noqa: E402
from src.ui.widgets.collapsible_card import CollapsibleCard  # noqa: E402
from src.ui.widgets.color_look_controls import ColorLookControls  # noqa: E402
from src.ui.widgets.color_mixer_widget import ColorMixerWidget  # noqa: E402
from src.ui.widgets.curves_widget import CURVE_CHANNELS, CurvesWidget  # noqa: E402
from src.ui.widgets.empty_state import EmptyState  # noqa: E402
from src.ui.widgets.histogram_widget import HistogramWidget  # noqa: E402
from src.ui.widgets.management_page import (ManagementInfo, ManagementListGroup, ManagementListItem,  # noqa: E402
                                             ManagementPage, management_label)
from src.ui.widgets.model_selector import ModelSelector  # noqa: E402
from src.ui.widgets.parameter_row import ParameterRow, ParameterSpec  # noqa: E402
from src.ui.widgets.photo_info_panel import PhotoInfoSidebar  # noqa: E402
from src.ui.widgets.action_footer import ActionFooter  # noqa: E402
from src.ui.widgets.assistant_composer import AssistantComposer  # noqa: E402
from src.ui.widgets.assistant_context_inspector import ContextInspector  # noqa: E402
from src.ui.widgets.assistant_conversation_list import ConversationList  # noqa: E402
from src.ui.widgets.assistant_transcript import AssistantTranscript  # noqa: E402
from src.ui.widgets.change_card import ChangeCard, ChangeRow  # noqa: E402
from src.ui.widgets.edit_stack import EditStackView, StackRow  # noqa: E402
from src.ui.widgets.history_list import HistoryList, HistoryStep  # noqa: E402
from src.ui.dialogs.settings_dialog import ShortcutEdit  # noqa: E402
from src.ui.widgets.message_dialog import DialogButton, MessageDialog  # noqa: E402
from src.ui.widgets.progress_dialog import ProgressDialog  # noqa: E402
from src.ui.widgets.setting_row import SettingRow, SettingSection  # noqa: E402
from src.ui.widgets.usage_bar import UsageBar  # noqa: E402
from src.ui.widgets.toast import Toast  # noqa: E402
from src.ui.widgets.toggle_section import ToggleSection  # noqa: E402
from src.ui.widgets.tool_rail import ToolRail  # noqa: E402
from src.ui.widgets.window_controls import WindowControls  # noqa: E402
from src.ui.widgets.canvas_toolbar import CanvasToolbar, CanvasToolbarActions, EDITOR  # noqa: E402
from src.ui.widgets import (AppleCheckBox, AppleDropDown, AppleRadioButton, AppleSpinBox, LabeledToggle,  # noqa: E402
                            ToggleSwitch)
from src.ui.widgets import (Chip, ChipGroup, ColorSwatch, GroupedOptionCard, NoticeBar, StatusBadge,  # noqa: E402
                            ToggleButton, set_tooltip)
from src.ui.widgets import (AppleBrandButton, AppleButton, AppleCompactButton, AppleDestructiveButton,  # noqa: E402
                            AppleGhostButton, AppleIconButton, ApplePrimaryButton, AppleSegmentedControl)

THEMES = (("dark", "深色", "Dark"), ("light", "浅色", "Light"), ("beige", "米黄色", "Beige"))
LANGUAGES = (("zh", "中文"), ("en", "English"))
# 自己取文案的组件（照片信息）跟随翻译服务的语言
LANGUAGE_CODES = {"zh": "zh-CN", "en": "en"}


def _row(*widgets: QWidget) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(px(SPACING_MD))
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch()
    return holder


def _caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("mutedText")
    label.setFixedWidth(150)
    return label


def _disabled(widget: QWidget) -> QWidget:
    widget.setEnabled(False)
    return widget


def _checked(button):
    button.setCheckable(True)
    button.setChecked(True)
    return button


def buttons_section(text) -> list[QWidget]:
    variants = (
        (text("主操作", "Main action"), AppleBrandButton, text("导出", "Export")),
        (text("确认", "Confirm"), ApplePrimaryButton, text("应用", "Apply")),
        (text("次要", "Secondary"), AppleButton, text("另存为…", "Save as…")),
        (text("文字", "Text"), AppleGhostButton, text("重置本工具", "Reset this tool")),
        (text("危险", "Danger"), AppleDestructiveButton, text("清除缓存", "Clear cache")),
    )
    rows = [_row(_caption(name), cls(sample), _disabled(cls(sample))) for name, cls, sample in variants]
    rows.append(_row(_caption(text("文字（选中）", "Text (checked)")),
                     _checked(AppleGhostButton(text("照片信息", "Photo info"))),
                     _checked(AppleCompactButton(text("适应", "Fit")))))
    rows.append(_row(_caption(text("紧凑", "Compact")), AppleCompactButton(text("自动", "Auto")),
                     AppleCompactButton(text("重置当前通道", "Reset channel")),
                     _disabled(AppleCompactButton(text("自动", "Auto")))))
    icons = [AppleIconButton(themed_icon(name)) for name in ("zoom-in", "zoom-out", "rotate-clockwise", "flip-horizontal")]
    for button in icons:
        button.setIconSize(QSize(20, 20))
    checked = _checked(AppleIconButton(themed_icon("layout-bottombar", role="accent")))
    checked.setIconSize(QSize(20, 20))
    off = _disabled(AppleIconButton(themed_icon("arrow-back-up")))
    off.setIconSize(QSize(20, 20))
    rows.append(_row(_caption(text("图标", "Icon")), *icons, checked, off))
    with_icon = AppleButton(text("打开", "Open"))
    with_icon.setIcon(themed_icon("folder-open", role="text_primary"))
    brand_icon = AppleBrandButton(text("导出", "Export"))
    brand_icon.setIcon(themed_icon("upload", role="on_brand"))
    rows.append(_row(_caption(text("带图标", "With icon")), with_icon, brand_icon))
    return rows


def segmented_section(text) -> list[QWidget]:
    def control(labels, current=0, enabled=True):
        segmented = AppleSegmentedControl()
        segmented.add_segments(labels)
        segmented.setCurrentIndex(current)
        segmented.setEnabled(enabled)
        segmented.setSizePolicy(segmented.sizePolicy().Policy.Fixed, segmented.sizePolicy().Policy.Fixed)
        return segmented

    return [
        _row(_caption(text("工作区", "Workspace")), control([text("看图", "View"), text("编辑", "Edit")])),
        _row(_caption(text("视图", "View")), control([text("适应", "Fit"), "1:1"], 1)),
        _row(_caption(text("侧栏标签", "Sidebar tabs")),
             control([text("调整", "Adjust"), text("历史", "History"), text("AI 助手", "AI assistant")], 1)),
        _row(_caption(text("四段", "Four segments")),
             control([text("自由", "Free"), text("网格", "Grid"), text("形状", "Shape"), text("套索", "Lasso")], 2)),
        _row(_caption(text("禁用", "Disabled")), control([text("看图", "View"), text("编辑", "Edit")], enabled=False)),
    ]


def parameter_rows_section(text) -> list[QWidget]:
    def row(label, minimum, maximum, default, value, value_format="int_signed", track=(), enabled=True):
        spec = ParameterSpec(label, "", label, minimum, maximum, default, value_format, tuple(track))
        widget = ParameterRow(spec, label)
        widget.set_value(value)
        widget.setEnabled(enabled)
        widget.setFixedWidth(280)
        return widget

    mixer = mixer_track_colors("orange")
    rejected = row(text("曝光度", "Exposure"), -300, 300, 0, 420, "ev")
    rejected.value_edited.connect(lambda _value: rejected.set_value(420))
    fractional_reset = row(text("阴影", "Shadows"), -100, 100, 0, .1)
    fractional_reset.value_edited.connect(lambda _value: fractional_reset.set_value(.1))
    return [
        _row(_caption(text("双向，未改动", "Bipolar, untouched")), row(text("对比度", "Contrast"), -100, 100, 0, 0),
             _caption(text("双向，已改动", "Bipolar, modified")), row(text("高光", "Highlights"), -100, 100, 0, -40)),
        _row(_caption(text("单向", "Unipolar")), row(text("羽化", "Feather"), 0, 100, 0, 35, "int"),
             _caption(text("带格式的读数", "Formatted value")), row(text("曝光度", "Exposure"), -300, 300, 0, 65, "ev")),
        _row(_caption(text("色温滑轨", "Temperature track")),
             row(text("色温", "Temperature"), -100, 100, 0, 20, "kelvin", TRACK_GRADIENTS["temperature"]),
             _caption(text("饱和度滑轨", "Saturation track")),
             row(text("饱和度", "Saturation"), -100, 100, 0, 0, track=TRACK_GRADIENTS["saturation"])),
        _row(_caption(text("混色通道滑轨", "Mixer channel tracks")), row(text("色相", "Hue"), -100, 100, 0, 15, track=mixer["hue"]),
             _caption(""), row(text("明度", "Luminance"), -100, 100, 0, -30, track=mixer["lum"])),
        _row(_caption(text("禁用", "Disabled")), row(text("去雾", "Dehaze"), -100, 100, 0, 25, enabled=False),
             _caption(text("百分比读数", "Percent value")), row(text("亮度降噪", "Luminance NR"), 0, 100, 0, 30, "percent")),
        _row(_caption(text("超出滑轨的保留读数", "Retained value beyond slider range")),
             row(text("曝光度", "Exposure"), -300, 300, 0, 420, "ev"),
             _caption(text("小数读数", "Fractional value")), row(text("阴影", "Shadows"), -100, 100, 0, 1.25)),
        _row(_caption(text("拒绝编辑后回填", "Restore after rejected edit")), rejected,
             _caption(text("拒绝小数重置", "Rejected fractional reset")), fractional_reset),
    ]


def masks_section(text) -> list[QWidget]:
    from src.core.local_masks import default_mask
    from src.core.mask_components import upgrade_components, component_from_shape
    from src.ui.panels.mask_panel import MaskPanel

    tr = get_translation_service().translate
    selected = default_mask("linear", (.5, .35), (1600, 1067), tr("mask.default_name", type=tr("mask.type.linear"), number=1))
    selected["params"] = {"exposure": -.7, "shadows": 12.5}
    disabled = default_mask("radial", (.6, .6), (1600, 1067), tr("mask.default_name", type=tr("mask.type.radial"), number=2))
    disabled["enabled"] = False

    def panel(params, identity=None, **options):
        widget = MaskPanel(tr, lambda *args, **kwargs: None, lambda: None)
        widget.setFixedWidth(320)
        widget.set_state(params, identity, ready=True, **options)
        return widget

    editor = panel({"process_version": 3, "masks": [selected, disabled]}, selected["id"], show_range=True)
    editor.set_clipping(("exposure",))
    components = upgrade_components([selected])
    components[0]["name"] = tr("mask.default_mask_name", number=1)
    gradient = components[0]["components"][0]
    gradient["name"] = tr("mask.type.linear")
    stroke = {"points": [[.3, .4], [.5, .45]], "radius": .03, "hardness": .5, "opacity": 1.0}
    erase = {"id": "00000000-0000-4000-8000-0000000000e1", "name": tr("mask.type.erase"), "enabled": True,
             "op": "subtract", "type": "brush", "strokes": [stroke, dict(stroke, points=[[.6, .5]])]}
    background = {"id": "00000000-0000-4000-8000-0000000000b1", "name": tr("mask.default_mask_name", number=2),
                  "enabled": False, "params": {"exposure": -.5}, "components": [
                      {"id": "00000000-0000-4000-8000-0000000000b2", "name": tr("mask.type.all"), "enabled": True,
                       "op": "add", "type": "all"},
                      dict(component_from_shape(disabled, "subtract"), id="00000000-0000-4000-8000-0000000000b3",
                           name=tr("mask.type.radial"), enabled=False)]}
    components[0]["components"].append(erase)
    composite = {"process_version": 4, "masks": [*components, background]}
    return [
        _row(_caption(text("空列表", "Empty list")), panel({"process_version": 4, "masks": None}),
             _caption(text("旧版", "Legacy")), panel({"process_version": 2, "masks": None}, message=tr("mask.legacy"), upgrade=True)),
        _row(_caption(text("渐变蒙版（处理版本 3）", "Gradient masks (process version 3)")), editor),
        _row(_caption(text("区域：添加、减去与画笔", "Areas: add, subtract and brush")),
             panel(composite, components[0]["id"], component=erase["id"], show_range=True),
             _caption(text("识别中", "Detecting")),
             panel(composite, background["id"], busy=("subject", background["id"]))),
    ]


class _SampleModels:
    """样例用的模型列表：一个已安装、一个未安装。"""

    def list(self, capability):
        from types import SimpleNamespace
        return [SimpleNamespace(state=state, descriptor=SimpleNamespace(
            adapter="sample", name=name, key=SimpleNamespace(token=token)))
            for name, token, state in (("Real-ESRGAN x4plus", "esrgan", "installed"),
                                       ("NAFNet SIDD width32", "nafnet", "missing"))]

    def subscribe(self, callback):
        return lambda: None


def color_looks_section(text) -> list[QWidget]:
    def controls(look, process_version=2):
        widget = ColorLookControls()
        widget.set_state(look, process_version)
        widget.setFixedWidth(280)
        return widget

    return [
        _row(_caption(text("观感", "Look")), controls("none"), controls(OPS_LOOK)),
        _row(_caption(text("旧版观感", "Legacy look")), controls(LEGACY_OPS_LOOK)),
        _row(_caption(text("旧版曝光", "Legacy exposure")), controls(LEGACY_OPS_LOOK, 1)),
    ]


def beauty_section(text) -> list[QWidget]:
    tr = get_translation_service().translate

    def state(kind, key, **values):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        badge = StatusBadge(kind, tr(key, **values), widget)
        layout.addWidget(badge)
        hint = QLabel(tr("beauty.detail.description_v3"), widget)
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        widget.setFixedWidth(320)
        return widget

    return [
        _row(_caption(text("准备与可用", "Preparing and ready")),
             state("pending", "beauty.detail.processing"), state("success", "beauty.detail.ready")),
        _row(_caption(text("检测结果", "Detection results")),
             state("warning", "beauty.detail.no_face"), state("warning", "beauty.detail.face_too_small")),
        _row(_caption(text("部分处理与失败", "Partial results and failure")),
             state("success", "beauty.detail.processed", count=2, skipped=1), state("warning", "beauty.detail.failed")),
        _row(_caption(text("升级", "Upgrade")), AppleButton(tr("beauty.detail.upgrade"))),
    ]


def choices_section(text) -> list[QWidget]:
    def dropdown(items, current=0, note="", enabled=True, flat=False):
        widget = AppleDropDown(flat=flat)
        widget.addItems(items)
        widget.setCurrentIndex(current)
        widget.set_note(note)
        widget.setEnabled(enabled)
        if not flat:
            widget.setFixedWidth(220)
        return widget

    def check(label, state=Qt.CheckState.Unchecked, enabled=True):
        widget = AppleCheckBox(label)
        widget.setTristate(True)
        widget.setCheckState(state)
        widget.setEnabled(enabled)
        return widget

    def radios(labels, current):
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_LG))
        for index, label in enumerate(labels):
            button = AppleRadioButton(label, holder)
            button.setChecked(index == current)
            layout.addWidget(button)
        return holder

    def switch(checked, enabled=True):
        widget = ToggleSwitch()
        widget.set_checked_silent(checked)
        widget.setEnabled(enabled)
        return widget

    def model(token, readiness):
        widget = ModelSelector(_SampleModels(), "sample")
        widget.set_token(token)
        widget.set_readiness(readiness)
        widget.setFixedWidth(260)
        return widget

    def stepper(value, minimum=1, maximum=10, enabled=True):
        widget = AppleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        widget.setEnabled(enabled)
        widget.setFixedWidth(120)
        return widget

    labeled = LabeledToggle(text("保持宽高比", "Keep aspect ratio"),
                            text("调整一边时另一边按比例变化", "The other side follows when one side changes"), True)
    labeled.setFixedWidth(320)
    models = [text("通用增强 ×4", "General ×4"), text("人像增强", "Portrait"), text("动漫增强", "Anime")]
    return [
        _row(_caption(text("下拉", "Dropdown")), dropdown(models),
             dropdown(models, 1, text("未安装", "Not installed")), dropdown(models, enabled=False)),
        _row(_caption(text("行内下拉", "Inline dropdown")),
             dropdown([text("全部图片", "All photos"), text("5 星", "5 stars")], flat=True)),
        _row(_caption(text("模型选择", "Model selector")), model("esrgan", "ready"),
             model("nafnet", "model_missing"), model("esrgan", "runtime_missing")),
        _row(_caption(text("复选", "Checkbox")), check(text("保留选区内", "Keep inside")),
             check(text("已选中", "Checked"), Qt.CheckState.Checked),
             check(text("部分选中", "Partial"), Qt.CheckState.PartiallyChecked),
             check(text("禁用", "Disabled"), Qt.CheckState.Checked, False)),
        _row(_caption(text("单选", "Radio")), radios([text("透明", "Transparent"), text("纯色", "Solid"),
                                                     text("图片", "Image")], 1)),
        _row(_caption(text("开关", "Switch")), switch(False), switch(True), switch(True, False)),
        _row(_caption(text("带标签的开关", "Labeled switch")), labeled),
        _row(_caption(text("步进器", "Stepper")), stepper(3), stepper(1), stepper(10), stepper(5, enabled=False)),
    ]


def small_widgets_section(text) -> list[QWidget]:
    def detail_toolbar(state):
        action = lambda name: QAction(name)
        toolbar = CanvasToolbar(CanvasToolbarActions(
            zoom_out=action("zoom_out"), zoom_in=action("zoom_in"),
            zoom_fit=action("zoom_fit"), zoom_actual=action("zoom_actual"),
            rotations=(action("rotate"),), flips=(action("flip"),)), get_translation_service().translate)
        toolbar.set_mode(EDITOR)
        toolbar.set_zoom_state(100, False)
        toolbar.set_detail_state(state, "error.image.memory_budget" if state == "failed" else "")
        return toolbar

    def swatch(color):
        widget = ColorSwatch()
        widget.set_color(color)
        return widget

    def toggle(label, icon, checked=False, enabled=True):
        widget = ToggleButton(label, icon)
        widget.setChecked(checked)
        widget.setEnabled(enabled)
        return widget

    def chips(items, current, columns=0, width=0):
        group = ChipGroup(columns)
        group.set_items([(label, label) for label in items])
        group.set_current(items[current])
        if width:
            group.setFixedWidth(width)
        return group

    def notice(kind, message, action=""):
        widget = NoticeBar(kind, message, action)
        widget.setFixedWidth(320)
        return widget

    def limited_chips():
        group = chips(["2×", "4×", "4K"], 0, columns=3, width=280)
        group.set_item_enabled("4×", False, text("输出尺寸超过处理上限", "Output exceeds the size limit"))
        return group

    def section(title, description, checked, available=True):
        widget = ToggleSection(title, description)
        widget.add_widget(QLabel(text("打开后才显示的内容", "Shown only when switched on")))
        widget.set_checked(checked)
        widget.set_switch_enabled(available)
        widget.setFixedWidth(260)
        return widget

    def toast(message):
        widget = Toast()
        widget.show_message(message, 0)
        return widget

    def meter(value, maximum=100):
        widget = QProgressBar()
        widget.setTextVisible(False)
        widget.setRange(0, maximum)
        widget.setValue(value)
        widget.setFixedWidth(220)
        return widget

    def labeled_meter(value, maximum, caption):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_XS))
        layout.addWidget(meter(value, maximum))
        label = QLabel(caption)
        label.setObjectName("mutedText")
        layout.addWidget(label)
        return holder

    with_text = QProgressBar()
    with_text.setRange(0, 100)
    with_text.setValue(42)
    with_text.setFormat("52.1 / 124.0 MB")
    with_text.setFixedWidth(220)

    tip_button = AppleIconButton(themed_icon("rotate-clockwise"))
    tip_button.setIconSize(QSize(20, 20))
    set_tooltip(tip_button, text("顺时针旋转 90°", "Rotate 90° clockwise"), "R")

    prompts = QWidget()
    prompts_layout = QHBoxLayout(prompts)
    prompts_layout.setContentsMargins(0, 0, 0, 0)
    prompts_layout.setSpacing(px(SPACING_SM))
    for label in (text("更通透", "Clearer"), text("压暗高光", "Tame highlights"), text("降低饱和度", "Less saturation")):
        prompts_layout.addWidget(Chip(label))

    return [
        _row(_caption(text("开关按钮", "Toggle button")),
             toggle(text("照片信息", "Photo info"), "layout-sidebar-right", True),
             toggle(text("胶片条", "Filmstrip"), "layout-bottombar"),
             toggle(text("照片信息", "Photo info"), "layout-sidebar-right", True, False)),
        _row(_caption(text("标签（单选）", "Chips (single choice)")), chips(["16:9", "3:2", "4:3", "1:1"], 0)),
        _row(_caption(text("标签（网格）", "Chips (grid)")),
             chips([text("原始", "Original"), "1:1", "4:5", "3:2", "16:9", "9:16"], 3, columns=3, width=280)),
        _row(_caption(text("标签（部分不可选）", "Chips (some unavailable)")), limited_chips()),
        _row(_caption(text("标签（点按）", "Chips (action)")), prompts),
        _row(_caption(text("色块", "Color swatch")), swatch((255, 255, 255)), swatch((38, 160, 90)),
             _disabled(swatch((30, 30, 30)))),
        _row(_caption(text("带开关的分组", "Toggle section")),
             section(text("降噪", "Denoise"), text("去掉高感光留下的噪点", "Remove high-ISO noise"), True),
             section(text("去模糊", "Deblur"), text("让虚掉的画面变清晰", "Make a soft photo sharper"), False),
             section(text("放大", "Upscale"), text("这张照片太大，现有模型无法放大",
                                                  "This photo is too large to enlarge"), False, False)),
        _row(_caption(text("状态标记", "Status badge")),
             StatusBadge("success", text("已安装", "Installed")), StatusBadge("pending", text("未安装", "Not installed")),
             StatusBadge("warning", text("文件损坏", "Corrupted")), StatusBadge("active", text("预览中", "Previewing"))),
        _row(_caption(text("准备中（延后显示）", "Preparing (delayed)")), detail_toolbar("preparing")),
        _row(_caption(text("清晰画面：失败", "Full resolution: failed")), detail_toolbar("failed")),
        _row(_caption(text("清晰画面：就绪", "Full resolution: ready")), detail_toolbar("")),
        _row(_caption(text("提示条", "Notice bar")),
             notice("warning", text("已恢复上次未应用的调整", "Restored unapplied changes"), text("放弃", "Discard")),
             notice("info", text("此模型需要先下载才能使用", "Download this model before using it"))),
        _row(_caption(text("轻提示", "Toast")), toast(text("已将 3 张图片评为 4 星", "Rated 3 images: ★★★★☆"))),
        _row(_caption(text("进度与占用", "Progress and usage")), meter(35),
             labeled_meter(1188, 2048, text("占用 1188 / 2048 MiB", "1188 / 2048 MiB used")), with_text),
        _row(_caption(text("悬停提示", "Tooltip")), tip_button,
             _caption(text("悬停在按钮上查看", "Hover the button"))),
    ]


def lists_section(text) -> list[QWidget]:
    navigation = GroupedOptionCard()
    for key, zh, en, icon in (("general", "常规", "General", "settings"), ("display", "图片显示", "Image display", "photo"),
                              ("tools", "工具", "Tools", "adjustments-horizontal"),
                              ("history", "历史", "History", "history")):
        navigation.add_item(key, text(zh, en), icon=icon)
    navigation.set_checked_index(1)
    navigation.setFixedWidth(220)

    options = GroupedOptionCard()
    for key, zh, en, note in (("fill", "填充", "Fill", text("推荐", "Recommended")), ("fit", "适应", "Fit", ""),
                              ("stretch", "拉伸", "Stretch", "")):
        options.add_item(key, text(zh, en), note)
    options.setFixedWidth(220)

    def group(title, expanded, with_action=False):
        card = CollapsibleCard(title, is_expanded=expanded)
        if with_action:
            reset = AppleIconButton(themed_icon("refresh", role="text_caption"), size=28)
            reset.setIconSize(QSize(16, 16))
            reset.setToolTip(text("重置本组", "Reset this group"))
            card.add_header_widget(reset)
        spec = ParameterSpec("sample", "", title, -100, 100, 0, "int_signed", ())
        card.add_widget(ParameterRow(spec, text("对比度", "Contrast")))
        return card

    groups = QWidget()
    groups.setFixedWidth(280)
    groups_layout = QVBoxLayout(groups)
    groups_layout.setContentsMargins(0, 0, 0, 0)
    groups_layout.setSpacing(px(SPACING_SM))
    groups_layout.addWidget(group(text("影调", "Tone"), True, with_action=True))
    groups_layout.addWidget(group(text("色彩", "Color"), True))
    groups_layout.addWidget(group(text("细节", "Detail"), False))

    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(px(SPACING_XL))
    for caption, widget in ((text("列表行（带图标）", "List rows (icons)"), navigation),
                            (text("列表行（带说明）", "List rows (notes)"), options),
                            (text("分组标题", "Group titles"), groups)):
        column = QVBoxLayout()
        column.setSpacing(px(SPACING_SM))
        label = QLabel(caption)
        label.setObjectName("mutedText")
        column.addWidget(label)
        column.addWidget(widget)
        column.addStretch()
        layout.addLayout(column)
    layout.addStretch()
    return [holder]


def management_section(text) -> list[QWidget]:
    page = ManagementPage()
    page.setMinimumHeight(460)
    page.navigation.set_groups((
        ManagementListGroup("editing", text("编辑工具", "Editing tools"), (
            ManagementListItem("crop", text("裁切", "Crop"), text("无需额外资源", "No extra resources"), "crop"),
            ManagementListItem("lasso", text("套索抠图", "Lasso"), text("无需额外资源", "No extra resources"), "lasso"))),
        ManagementListGroup("intelligent", text("智能处理", "Intelligent processing"), (
            ManagementListItem("enhance", text("画质增强", "Enhancement"), text("缺少模型", "Model required"), "sparkles"),))), "enhance")
    switch = ToggleSwitch()
    switch.set_checked_silent(True)
    page.header_actions.addWidget(switch)
    info = ManagementInfo(("download", "installed", "source"))
    info.set_row("download", text("下载大小", "Download size"), "64.3 MB")
    info.set_row("installed", text("本地占用", "Disk usage"), "0.0 MB")
    info.set_row("source", text("来源", "Source"), "Real-ESRGAN")
    page.body_layout.addWidget(info)
    files = CollapsibleCard(text("资源标识与文件", "Resource identity and files"), is_expanded=False)
    identity = management_label(files, "managementCaption", selectable=True)
    identity.setText("realesrgan-x4plus@v0.2.5-onnx-fp16")
    files.add_widget(identity)
    page.body_layout.addWidget(files)
    install = ApplePrimaryButton(text("安装所需内容", "Install required resources"))
    page.footer_layout.addWidget(install)

    def select(key):
        enhance = key == "enhance"
        page.title.setText(text("画质增强", "Enhancement") if enhance else
                           text("裁切", "Crop") if key == "crop" else text("套索抠图", "Lasso"))
        page.subtitle.setText(text("降噪 · 去模糊 · 超分", "Denoise · Deblur · Upscale") if enhance else
                              text("基础功能", "Basic features"))
        page.badge.set_status("warning" if enhance else "success",
                              text("缺少模型", "Model required") if enhance else text("可用", "Ready"))
        page.set_notice(text("需要下载模型才能使用超分辨率。", "Download the model to use super resolution.") if enhance else "")
        info.setVisible(enhance)
        files.setVisible(enhance)
        install.setEnabled(enhance)

    page.navigation.selection_changed.connect(select)
    select("enhance")
    return [page]


def _bell(center, width, height):
    return [int(height * math.exp(-((index - center) / width) ** 2)) for index in range(256)]


def _sample_histogram():
    return {"r": _bell(150, 45, 900), "g": _bell(125, 50, 1000), "b": _bell(95, 40, 800),
            "luma": _bell(125, 48, 1000)}


def scopes_section(text) -> list[QWidget]:
    def histogram(data=None):
        widget = HistogramWidget()
        widget.setFixedWidth(284)
        if data is not None:
            widget.set_histogram_data(data)
        return widget

    def curves(channel, points):
        card = CollapsibleCard(text("色调曲线", "Tone curves"))
        card.setFixedWidth(284)
        channels = ChipGroup(len(CURVE_CHANNELS), card)
        labels = ("RGB", text("红", "Red"), text("绿", "Green"), text("蓝", "Blue"))
        channels.set_items(list(zip(CURVE_CHANNELS, labels)))
        channels.set_current(channel)
        widget = CurvesWidget()
        widget.set_curves(points)
        widget.set_active_channel(channel)
        channels.current_changed.connect(widget.set_active_channel)
        card.add_widget(channels)
        card.add_widget(widget)
        return card

    mixer = CollapsibleCard(text("色彩混合", "Color mixer"))
    mixer.setFixedWidth(EDIT_SIDEBAR_WIDTH_WIDE)
    mixer_widget = ColorMixerWidget(get_translation_service().translate, mixer)
    mixer_widget.set_eyedropper_available(False)
    mixer.add_widget(mixer_widget)

    balanced = _sample_histogram()
    clipped = {name: list(values) for name, values in balanced.items()}
    clipped["b"][0] = 6000
    clipped["r"][255] = clipped["g"][255] = 6000
    return [
        _row(_caption(text("直方图", "Histogram")), histogram(balanced),
             _caption(text("两端裁切", "Clipped at both ends")), histogram(clipped)),
        _row(_caption(text("直方图（无数据）", "Histogram (no data)")), histogram()),
        _row(_caption(text("曲线（总曲线）", "Curves (RGB)")),
             curves("rgb", {"rgb": [(0, 0), (64, 44), (192, 214), (255, 255)]}),
             _caption(text("曲线（红色通道）", "Curves (red channel)")),
             curves("r", {"rgb": [(0, 0), (64, 44), (192, 214), (255, 255)], "r": [(0, 0), (128, 156), (255, 255)],
                          "b": [(0, 0), (128, 104), (255, 255)]})),
        _row(mixer),
    ]


def action_footer_section(text) -> list[QWidget]:
    steps = text("AI 智能抠图", "AI cutout")

    def footer(caption, *, action=None, **state):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        label = QLabel(caption)
        label.setObjectName("mutedText")
        layout.addWidget(label)
        widget = ActionFooter()
        widget.setFixedWidth(284)
        if action is None:
            widget.show_preview(steps=steps, **state)
        else:
            widget.show_action(**action)
        # 这里是横排的样例，行高不会按换行后的文字重算；按固定宽度先算好高度
        widget.setMinimumHeight(widget.heightForWidth(284))
        layout.addWidget(widget)
        layout.addStretch()
        return holder

    blocked = get_translation_service().translate(
        "preview.blocked", group=steps, reason=get_translation_service().translate("tool_manager.state.model_missing"))
    return [_row(footer(text("可以渲染", "Ready"), preview_state="not_previewed"),
                 footer(text("渲染中", "Rendering"), preview_state="processing"),
                 footer(text("已是最新", "Up to date"), preview_state="updated"),
                 footer(text("做不了", "Blocked"), preview_state="not_previewed", blocked=blocked)),
            _row(footer(text("失败", "Failed"), preview_state="failed"),
                 footer(text("已取消", "Cancelled"), preview_state="cancelled"),
                 footer(text("没有可做的处理", "Nothing to do"), preview_state="not_previewed",
                        idle=text("先在上面打开至少一种处理", "Turn on at least one option above"))),
            _row(footer(text("画布工具，调整时", "Canvas tool, adjusting"),
                        action=dict(summary=text("裁切为 4000 × 4000", "Crop to 4000 × 4000"),
                                    button=text("查看结果", "View Result"), enabled=True)),
                 footer(text("画布工具，还没有结果可看", "Canvas tool, nothing to view yet"),
                        action=dict(summary=text("还没有选区", "Nothing selected yet"),
                                    reason=text("先在照片上圈出一个区域", "Outline a region on the photo first"),
                                    button=text("查看结果", "View Result"), enabled=False)),
                 footer(text("画布工具，看结果时", "Canvas tool, viewing the result"),
                        action=dict(summary=text("正在查看结果", "Viewing the result"),
                                    button=text("继续调整", "Keep Adjusting"), enabled=True)),
                 footer(text("画布工具，结果还在渲染", "Canvas tool, result still rendering"),
                        action=dict(summary=text("正在生成结果…", "Rendering the result…"),
                                    button=text("取消", "Cancel"), enabled=True)))]


def empty_state_section(text) -> list[QWidget]:
    def sample(caption, widget, width, height):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        label = QLabel(caption)
        label.setObjectName("mutedText")
        layout.addWidget(label)
        widget.setFixedSize(width, height)
        layout.addWidget(widget)
        return holder

    def drop(active=False):
        widget = EmptyState("photo", actions=("open_file", "open_folder"), drop_target=True, on_canvas=True)
        widget.set_text(text("把图片或文件夹拖到这里", "Drop images or a folder here"),
                        text("支持 JPEG、PNG、WebP、TIFF 和常见的 RAW 格式",
                             "JPEG, PNG, WebP, TIFF and common RAW formats"))
        widget.set_action_text("open_file", text("打开图片", "Open Images"))
        widget.set_action_text("open_folder", text("打开文件夹", "Open Folder"))
        widget.set_drag_active(active)
        return widget

    def plain(icon, title, description="", action=""):
        widget = EmptyState(icon, actions=("retry",) if action else ())
        widget.set_text(title, description)
        if action:
            widget.set_action_text("retry", action)
        return widget

    return [_row(sample(text("接受拖放", "Drop target"), drop(), 600, 330),
                 sample(text("有东西拖到上方", "Something dragged over it"), drop(active=True), 600, 330)),
            _row(sample(text("只有一句话", "A single sentence"),
                        plain("cpu", text("暂无可用运行环境", "No runtimes available")), 300, 210),
                 sample(text("带说明", "With a description"),
                        plain("photo", text("还没有预览", "No preview yet"),
                              text("选好规格后点“生成预览”，结果会显示在这里",
                                   "Choose a size and generate a preview to see it here")), 300, 210),
                 sample(text("带一个操作", "With one action"),
                        plain("alert-triangle", text("OPS 初始化失败", "OPS failed to start"),
                              action=text("重试", "Retry")), 300, 210))]


def _embedded(dialog) -> QWidget:
    """把对话框当作普通控件摆进样例页。"""
    dialog.setWindowFlags(Qt.WindowType.Widget)
    return dialog


def dialogs_section(text) -> list[QWidget]:
    cancel = text("取消", "Cancel")
    choice = MessageDialog(
        title=text("应用修改", "Apply Changes"),
        message=text("如何保存已应用的修改？", "How do you want to save the applied changes?"),
        detail=text("覆盖后原文件的内容不能恢复。", "Overwriting replaces the original file and cannot be undone."),
        buttons=(DialogButton("cancel", cancel, "text"),
                 DialogButton("overwrite", text("覆盖原图", "Overwrite Original")),
                 DialogButton("save_as", text("另存为…", "Save As…"), "primary")),
        default="save_as", escape="cancel")
    danger = MessageDialog(
        title=text("清除评分", "Clear Ratings"),
        message=text("这将永久删除所有已保存的评分。继续吗？",
                     "This will permanently delete all saved ratings. Continue?"),
        buttons=(DialogButton("cancel", cancel), DialogButton("confirm", text("清除评分", "Clear Ratings"), "danger")),
        kind="danger", default="cancel", escape="cancel")
    error = MessageDialog(
        title=text("编辑失败", "Editing failed"),
        message=text("图片保存失败，请检查目标目录、可用空间和图像编码支持。",
                     "The image could not be saved. Check the destination, free space and image codec support."),
        buttons=(DialogButton("ok", text("确定", "OK"), "primary"),), kind="error", default="ok", escape="ok")
    busy = ProgressDialog(title=text("导出图片", "Export Image"), text=text("正在保存图片…", "Saving image…"),
                          cancel_text=cancel, modal=False)
    counted = ProgressDialog(title=text("正在导出", "Exporting"),
                             text=text("第 3 张，共 12 张 · DSC_6679.NEF", "3 of 12 · DSC_6679.NEF"),
                             cancel_text=cancel, modal=False)
    counted.set_progress(3, 12)
    cancelling = ProgressDialog(title=text("导出图片", "Export Image"), cancel_text=cancel, modal=False)
    cancelling.set_cancelling(text("正在取消…", "Cancelling…"))
    return [_row(_embedded(choice), _embedded(danger), _embedded(error)),
            _row(_embedded(busy), _embedded(counted), _embedded(cancelling))]


def setting_rows_section(text) -> list[QWidget]:
    from src.application.studio_preload import StudioPreloadSettings

    defaults = StudioPreloadSettings()
    holder = QWidget()
    holder.setObjectName("settingsPage")
    holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    holder.setFixedWidth(760)
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(px(SPACING_XL), px(SPACING_LG), px(SPACING_XL), px(SPACING_LG))
    layout.setSpacing(px(SPACING_LG))

    memory = SettingSection(holder)
    memory.set_title(text("内存", "Memory"))
    preview = memory.add_row(SettingRow(memory))
    preview.set_text(text("编辑预览缓存（内存，MiB）", "Edit preview cache (memory, MiB)"),
                     text("保存已处理的预览，加快切图和返回编辑状态。计入软件内存上限，关闭程序后释放。",
                          "Stores processed previews for faster photo switching and returning to edit states. "
                          "Counts toward the app memory limit and is released when the app closes."))
    preview_limit = AppleSpinBox()
    preview_limit.setRange(*StudioPreloadSettings.RANGES["memory_mib"])
    preview_limit.setValue(defaults.memory_mib)
    preview.add_control(preview_limit)
    layout.addWidget(memory)

    preload = SettingSection(holder)
    preload.set_title(text("预加载", "Preloading"))
    mode = preload.add_row(SettingRow(preload))
    mode.set_text(text("预加载范围", "Preload range"),
                  text("优先加载选中图片，后台准备预加载范围内的图片。",
                       "The selected image loads first. Images in the preload range are prepared in the background."))
    segments = AppleSegmentedControl()
    segments.add_segments((text("关闭", "Off"), text("相邻图片", "Nearby"), text("全部图片", "All")))
    segments.setCurrentIndex(1)
    mode.add_control(segments)
    count = preload.add_row(SettingRow(preload))
    count.set_text(text("向后预加载张数", "Next images"))
    stepper = AppleSpinBox()
    stepper.setRange(0, 10000)
    stepper.setValue(3)
    count.add_control(stepper)
    # 用不了的一行整行置灰
    locked = preload.add_row(SettingRow(preload))
    locked.set_text(text("向前预加载张数", "Previous images"),
                    text("预加载关闭时不可用。", "Unavailable while preloading is off."))
    locked_stepper = AppleSpinBox()
    locked_stepper.setRange(0, 10000)
    locked_stepper.setValue(1)
    locked.add_control(locked_stepper)
    locked.setEnabled(False)
    layout.addWidget(preload)

    cache = SettingSection(holder)
    cache.set_title(text("缩略图缓存（磁盘）", "Thumbnail cache (disk)"))
    capacity = cache.add_row(SettingRow(cache))
    capacity.set_text(text("容量上限（MiB）", "Limit (MiB)"),
                      text("保存图片列表和胶片条的小图，加快浏览。清除后按需重新生成。空间不足时自动淘汰较久未使用的缓存。",
                           "Stores small images for photo lists and filmstrips for faster browsing. Thumbnails are rebuilt "
                           "when needed after clearing. Older unused entries are removed when space runs low."))
    thumbnail_limit = AppleSpinBox()
    thumbnail_limit.setRange(*StudioPreloadSettings.RANGES["thumbnail_mib"])
    thumbnail_limit.setValue(defaults.thumbnail_mib)
    capacity.add_control(thumbnail_limit)
    usage = UsageBar()
    usage.set_progress(186, 512)
    usage.set_text(text("占用 186.0 / 512 MiB。", "186.0 of 512 MiB used."))
    row = cache.add_row(SettingRow(cache, leading=usage))
    row.add_control(AppleButton(text("清除缩略图缓存", "Clear thumbnail cache")))
    row.set_message(text("缩略图缓存已清除。", "Thumbnail cache cleared."))
    layout.addWidget(cache)

    proxy = SettingSection(holder)
    proxy.set_title(text("编辑代理缓存（磁盘）", "Edit proxy cache (disk)"))
    capacity = proxy.add_row(SettingRow(proxy))
    capacity.set_text(text("容量上限（MiB）", "Limit (MiB)"),
                      text("保存长边 1600 像素的编辑底图，减少原图解码。清除后保留原图、项目和编辑记录。空间不足时自动淘汰较久未使用的缓存。",
                           "Stores editing sources with a 1600-pixel long edge to reduce original image decoding. "
                           "Clearing keeps source files, projects and edit history. Older unused entries are removed "
                           "when space runs low."))
    proxy_limit = AppleSpinBox()
    proxy_limit.setRange(*StudioPreloadSettings.RANGES["disk_mib"])
    proxy_limit.setValue(defaults.disk_mib)
    capacity.add_control(proxy_limit)
    usage = UsageBar()
    usage.set_progress(1899.9, defaults.disk_mib)
    usage.set_text(text("占用 1899.9 / 2048 MiB。", "1899.9 of 2048 MiB used."))
    row = proxy.add_row(SettingRow(proxy, leading=usage))
    row.add_control(AppleButton(text("清除编辑代理缓存", "Clear edit proxy cache")))
    layout.addWidget(proxy)

    shortcuts = SettingSection(holder)
    shortcuts.set_title(text("快捷键", "Shortcuts"))
    samples = ((text("打开文件", "Open Files"), "Ctrl+O", "set", ""),
               (text("设为壁纸", "Set as Wallpaper"), "", "empty", ""),
               (text("网格裁切", "Grid Crop"), "Ctrl+Shift+G", "set",
                text("Ctrl+S 已用于“保存项目”，没有更改。", "Ctrl+S is already used by “Save Project”. Nothing was changed.")),
               (text("形状裁切", "Shape Crop"), "Ctrl+Shift+P", "recording", ""))
    for title, sequence, state, message in samples:
        row = shortcuts.add_row(SettingRow(shortcuts))
        row.set_text(title)
        edit = ShortcutEdit(row)
        edit.set_texts(text("请按下快捷键...", "Press keys..."), text("未设置", "Not set"))
        edit.set_sequence(sequence)
        if state == "recording":
            edit.click()
        row.add_control(edit)
        reset = AppleCompactButton(text("重置", "Reset"))
        reset.setEnabled(state == "set" and bool(message))
        row.add_control(reset)
        row.set_message(message, "warning")
    layout.addWidget(shortcuts)
    return [holder]


def storage_section(text) -> list[QWidget]:
    from src.application.storage_service import StorageItem
    from src.ui.dialogs.storage_page import StorageListDialog, StoragePage
    from src.ui.widgets.usage_bar import UsageBar

    translate = get_translation_service().translate
    page = StoragePage(None, translate, preferences={"limits": {}})
    page.setFixedWidth(680)
    for key, value, used, hint_key in (("thumbnails", 512, 186, "settings.thumbnails.hint"),
                                      ("edit_cache", 2048, 1900, "settings.edit_cache.hint")):
        section = SettingSection(page)
        section.set_title(translate("storage.kind." + key))
        row = section.add_row(SettingRow(section))
        row.set_text(translate("settings.ops.disk"), translate(hint_key))
        control = AppleSpinBox(row)
        control.setRange(128 if key == "edit_cache" else 16, 32768)
        control.setValue(value)
        row.add_control(control)
        usage = UsageBar(section)
        usage.set_progress(used, value)
        usage.set_text(translate("settings.edit_cache.usage" if key == "edit_cache" else "settings.thumbnails.usage", used=used, limit=value))
        usage_row = section.add_row(SettingRow(section, leading=usage))
        clear = AppleButton(translate("settings.edit_cache.clear" if key == "edit_cache" else "settings.thumbnails.clear"), usage_row)
        clear.setEnabled(False)
        usage_row.add_control(clear)
        page.add_cache_section(key, section, usage_row)
    base = Path(r"D:\照片资料\OpenPictureStudio")
    items = [StorageItem("thumbnails", (base / "thumbnails",), size=186 * 1024 ** 2, files=5200,
                               free=240 * 1024 ** 3),
                   StorageItem("edit_cache", (base / "edit_cache",), size=1900 * 1024 ** 2, files=98,
                               free=240 * 1024 ** 3),
                   StorageItem("drafts", (base / "drafts.json",), size=380000, files=1,
                               free=240 * 1024 ** 3),
                   StorageItem("projects", (base / "夜景.opsproj", base / "夜景.opsdata"), size=64 * 1024 ** 2,
                               files=32, free=240 * 1024 ** 3)]
    for item in items:
        item.unique_size = item.size
    page.set_snapshot(items, refreshed="14:35", job_count=0)
    cases = SettingSection(page)
    cases.set_title(translate("storage.kind.drafts"))
    row = cases.add_row(SettingRow(cases))
    row.set_text(text("列表状态", "List states"))
    def show_drafts(count):
        dialog = StorageListDialog(translate("storage.kind.drafts"), translate, page,
                                   selectable=True, empty_key="storage.empty.drafts",
                                   note=translate("storage.drafts_hint") if count else "",
                                   location=str(base / "2026-10-21" / "人像精选") if count else "")
        dialog.set_entries((
            {"id": str(index), "bytes": 3277}, f"DSC_{index + 1:04d}.NEF", "3.2 KiB", "")
            for index in range(count))
        dialog.add_action(translate("common.close"), dialog.reject)
        if count:
            remove = dialog.add_action(translate("storage.delete_selected"), dialog.accept, role="danger")
            remove.setEnabled(False)
            dialog.selection_changed.connect(lambda: remove.setEnabled(bool(dialog.selected_records())))
        dialog.exec()
        dialog.deleteLater()
    for count in (0, 1, 62):
        button = AppleButton(text(f"{count} 项", f"{count} items"), row)
        button.clicked.connect(lambda _checked=False, count=count: show_drafts(count))
        row.add_control(button)
    return [page, cases]


def photo_info_section(text) -> list[QWidget]:
    path = r"D:\照片\2026-09 湖边\DSC_6674.NEF"
    full = PhotoMetadata(
        shutter="1/250 s", aperture="f/5.6", iso="200", focal_length="35 mm", focal_length_35mm="52 mm",
        exposure_bias="-0.3 EV", captured_at="2026-09-21 18:42:07", exposure_program=3, metering_mode=5,
        white_balance=0, flash=False, camera_make="NIKON CORPORATION", camera_model="NIKON Z 50",
        lens_model="NIKKOR Z DX 16-50mm f/3.5-6.3 VR")
    partial = PhotoMetadata(iso="400", camera_model="NIKON Z 50", unreadable=frozenset(("shutter", "exposure_bias")))

    def info(photo, issue=""):
        return ImageInfo(5568, 3712, "RAW", photo.camera, path, "NEF", 25_800_000, "RAW", photo, True, issue)

    def sample(result_info=None, error=None, loading=False):
        # 样例页打开期间是它自己的翻译服务；带直方图的是看图工作区的用法
        bar = PhotoInfoSidebar(get_translation_service().translate, histogram=True)
        bar.setFixedHeight(760)
        bar.panel.set_active(True)
        bar.panel.set_path(path)
        bar.panel.set_expanded(True)
        if not loading:
            request = ImageInfoRequest(1, path, None, True)
            bar.panel.set_request(request)
            bar.panel.set_result(ImageInfoResult(request, result_info, error))
            if error is None:
                frame = DisplayHistogramRequest(1, path, (0, 0, 0), RawDisplayMode.OPS)
                bar.panel.set_histogram_request(frame)
                bar.panel.set_histogram(frame, _sample_histogram())
        return bar

    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(px(SPACING_XL))
    failed = CoreError(ErrorCode.DECODE_FAILED, "photo_info.read_failed", retryable=True)
    for caption, widget in ((text("读取完成", "Loaded"), sample(info(full))),
                            (text("部分信息缺失或无法读取", "Partly missing or unreadable"),
                             sample(info(partial, "photo_info.partial"))),
                            (text("读取失败", "Failed"), sample(error=failed)),
                            (text("读取中", "Loading"), sample(loading=True))):
        column = QVBoxLayout()
        column.setSpacing(px(SPACING_SM))
        label = QLabel(caption)
        label.setObjectName("mutedText")
        column.addWidget(label)
        column.addWidget(widget)
        column.addStretch()
        layout.addLayout(column)
    layout.addStretch()
    return [holder]


def tool_rail_section(text) -> list[QWidget]:
    entries = (("color", "adjustments-horizontal", text("调色", "Color")), ("crop", "crop", text("裁切", "Crop")),
               ("cutout", "lasso", text("抠图", "Cutout")), ("repair", "sparkles", text("修复", "Repair")),
               ("beauty", "mood-smile", text("美颜", "Beauty")))

    def rail(current, modified=(), enabled=True):
        widget = ToolRail()
        widget.set_entries(entries)
        widget.set_current(current)
        widget.set_modified(modified)
        widget.setFixedHeight(len(entries) * 60 + px(SPACING_LG))
        widget.setEnabled(enabled)
        return widget

    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(px(SPACING_XL))
    for caption, widget in ((text("选中“调色”", "Color selected"), rail("color")),
                            (text("选中“裁切”，调色与美颜已修改", "Crop selected; Color and Beauty modified"),
                             rail("crop", ("color", "beauty"))),
                            (text("禁用", "Disabled"), rail("color", ("crop",), enabled=False))):
        column = QVBoxLayout()
        column.setSpacing(px(SPACING_SM))
        label = QLabel(caption)
        label.setObjectName("mutedText")
        column.addWidget(label)
        column.addWidget(widget)
        column.addStretch()
        layout.addLayout(column)
    layout.addStretch()
    return [holder]


def edit_history_section(text) -> list[QWidget]:
    def stack(rows, expanded=True):
        view = EditStackView()
        view.set_texts(text("编辑栈", "Edit stack"),
                       text("还没有调整。改过参数的工具会按处理顺序列在这里。",
                            "No adjustments yet. Tools you adjust are listed here in processing order."),
                       text("重置这一步", "Reset this step"))
        view.set_rows(rows)
        view.set_expanded(expanded)
        view.setFixedWidth(300)
        return view

    rows = (StackRow("color", text("调色", "Color"), text("4 项调整", "4 adjustments"), "adjustments-horizontal",
                     current=True),
            StackRow("repair", text("画质增强", "Enhance"), text("亮度降噪 30 %", "Luminance 30 %"), "sparkles"),
            StackRow("crop", text("裁切", "Crop"), text("1 项调整", "1 adjustment"), "crop", resettable=False))

    history = HistoryList()
    history.set_steps((
        HistoryStep("0", text("打开 DSC_6674.NEF", "Opened DSC_6674.NEF"), "18:42"),
        HistoryStep("1", text("裁切 · 1 项调整", "Crop · 1 adjustment"), "18:43"),
        HistoryStep("2", text("画质增强 · 去模糊 20 %", "Enhance · Deblur 20 %"), "18:44"),
        HistoryStep("3", text("调色 · 曝光 +0.35", "Color · Exposure +0.35"), "18:45"),
        HistoryStep("4", text("调色 · 阴影 +32", "Color · Shadows +32"), "18:47", "current"),
        HistoryStep("5", text("调色 · 色温 5890K", "Color · Temperature 5890K"), text("已撤销", "Undone"), "undone")))
    history.setFixedWidth(300)

    def card(kind, status):
        widget = ChangeCard()
        widget.set_title(text("候选调整", "Proposed adjustments"))
        widget.set_status(kind, status)
        widget.set_rows((ChangeRow(text("调色 · 4 项", "Color · 4"), section=True),
                         ChangeRow(text("阴影", "Shadows"), "0", "+32"),
                         ChangeRow(text("高光", "Highlights"), "-38", "-52"),
                         ChangeRow(text("色调曲线 · RGB", "Tone Curves · RGB")),
                         ChangeRow(text("色彩混合 · 橙 · 饱和度", "Color Mixer · Orange · Saturation"), "+15", "-12"),
                         ChangeRow(text("美颜 · 1 项", "Beauty · 1"), section=True),
                         ChangeRow(text("磨皮", "Smoothing"), "0", "25")))
        widget.setFixedWidth(284)
        return widget

    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(px(SPACING_XL))
    for caption, widgets in (
            (text("编辑栈：当前在调色；最后一步不能重置", "Edit stack: editing Color; last step not resettable"),
             (stack(rows),)),
            (text("编辑栈：还没有调整、收起", "Edit stack: empty, collapsed"), (stack(()), stack(rows, expanded=False))),
            (text("历史：新的在上，当前一步突出，已撤销的减淡", "History: newest on top, undone steps dimmed"),
             (history,)),
            (text("改动卡片：预览中、已应用、已放弃", "Change card: previewing, applied, discarded"),
             (card("active", text("预览中", "Previewing")), card("success", text("已应用", "Applied")),
              card("pending", text("已放弃", "Discarded"))))):
        column = QVBoxLayout()
        column.setSpacing(px(SPACING_SM))
        label = QLabel(caption)
        label.setObjectName("mutedText")
        label.setWordWrap(True)
        label.setFixedWidth(300)
        column.addWidget(label)
        for widget in widgets:
            column.addWidget(widget)
        column.addStretch()
        layout.addLayout(column)
    layout.addStretch()
    return [holder]


def window_controls_section(text) -> list[QWidget]:
    def controls(caption, *, maximized=False, hovered=False, pressed=False):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        label = QLabel(caption)
        label.setObjectName("mutedText")
        layout.addWidget(label)
        widget = WindowControls(get_translation_service().translate)
        widget.setFixedHeight(TOP_BAR_HEIGHT)
        widget.set_maximized(maximized)
        widget.set_maximize_state(hovered, pressed)
        layout.addWidget(widget)
        return holder

    return [_row(controls(text("平时", "At rest")),
                 controls(text("最大化按钮悬停", "Maximize hovered"), hovered=True),
                 controls(text("最大化按钮按下", "Maximize pressed"), hovered=True, pressed=True),
                 controls(text("窗口已最大化", "Window maximized"), maximized=True))]


def icons_section(text) -> list[QWidget]:
    holder = QWidget()
    grid = QGridLayout(holder)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(px(SPACING_LG))
    grid.setVerticalSpacing(px(SPACING_SM))
    names = sorted(path.stem for path in (ROOT / "resources" / "icons" / "ui").glob("*.svg"))
    columns = 6
    for index, name in enumerate(names):
        cell = QWidget()
        cell_layout = QHBoxLayout(cell)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.setSpacing(px(SPACING_SM))
        picture = QLabel()
        picture.setPixmap(themed_icon(name).pixmap(QSize(20, 20)))
        picture.setFixedSize(20, 20)
        cell_layout.addWidget(picture)
        cell_layout.addWidget(QLabel(name))
        cell_layout.addStretch()
        grid.addWidget(cell, index // columns, index % columns)
    return [holder]


def colors_section(text) -> list[QWidget]:
    tokens = get_theme_tokens()
    holder = QWidget()
    grid = QGridLayout(holder)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(px(SPACING_LG))
    grid.setVerticalSpacing(px(SPACING_SM))
    columns = 4
    for index, key in enumerate(sorted(SEMANTIC_KEYS)):
        cell = QWidget()
        cell_layout = QHBoxLayout(cell)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.setSpacing(px(SPACING_SM))
        swatch = QFrame()
        swatch.setFixedSize(28, 20)
        swatch.setStyleSheet(f"background-color: {tokens[key]}; border: 1px solid {tokens['hairline_solid']};"
                             "border-radius: 4px;")
        cell_layout.addWidget(swatch)
        cell_layout.addWidget(QLabel(f"{key}  {tokens[key]}"))
        cell_layout.addStretch()
        grid.addWidget(cell, index // columns, index % columns)
    return [holder]


def assistant_settings_section(text) -> list[QWidget]:
    from dataclasses import asdict
    from PySide6.QtCore import QObject, Signal
    from src.application.assistant_models import AssistantConnection
    from src.ui.dialogs.assistant_settings_page import AssistantSettingsPage

    class PreviewRunner(QObject):
        changed = Signal(object)
        event_received = Signal(str, object)
        last_error = None

        def __init__(self):
            super().__init__()
            connection = AssistantConnection(name="API", endpoint="https://api.example.com/v1")
            self.state = {"ready": True, "connections": [asdict(connection)], "models": [], "default_model": ""}

        def command(self, name, /, **values):
            if name == "configuration":
                connection = dict(values["connection"])
                connection["revision"] += 1
                self.state["connections"] = [connection if item["id"] == connection["id"] else item
                                             for item in self.state["connections"]]
                self.state["models"] = [model for model in self.state["models"]
                                        if model["connection_id"] != connection["id"]] + values["models"]
                self.event_received.emit("configured", {"ticket": values["ticket"]})
            elif name == "diagnose":
                self.event_received.emit("diagnostic", {**values, "success": True,
                    "models": [{"id": model} for model in ("portrait-chat", "portrait-vision", "studio-reasoning")]})
            elif name == "add_model":
                model = AssistantModel(connection_id=values["connection_id"], model=values.get("model_id", ""),
                                       name=values.get("model_id", ""))
                self.state["models"].append(asdict(model))
                self.event_received.emit("model_added", {"connection_id": model.connection_id, "model_id": model.id})
            elif name == "add_connection":
                connection = AssistantConnection(name="API", endpoint="https://api.example.com/v1", provider=values["provider"])
                self.state["connections"].append(asdict(connection))
                self.event_received.emit("configuration_added", {"connection_id": connection.id})
            elif name == "default_model":
                self.state["default_model"] = values["identity"]
            elif name == "delete_model":
                self.state["models"] = [model for model in self.state["models"] if model["id"] != values["identity"]]
                if self.state["default_model"] == values["identity"]:
                    self.state["default_model"] = ""
            elif name == "delete_connection":
                self.state["connections"] = [item for item in self.state["connections"] if item["id"] != values["identity"]]
                self.state["models"] = [model for model in self.state["models"] if model["connection_id"] != values["identity"]]
            self.changed.emit(self.state)

    runner = PreviewRunner()
    page = AssistantSettingsPage(runner)
    runner.setParent(page)
    page.setMinimumHeight(680)
    return [page]


def assistant_section(text) -> list[QWidget]:
    from dataclasses import asdict
    from PIL import Image
    from src.application.photo_analysis import analyzed_photo, reference_thumbnail
    from src.application.assistant_images import image_capability
    from src.application.assistant_models import MAX_CONVERSATION_ATTACHMENT_BYTES
    svc = get_translation_service()
    tr = lambda key, default=None, **values: svc.translate(key, default=default, **values)
    model = AssistantModel(id="demo-model", connection_id="demo-connection", model="deepseek-flash", name="deepseek-flash",
                           reasoning="deepseek", efforts=("none", "low", "high", "max"),
                           image_input=True, tool_calling=True, image_effective_long_edge=768, image_detail="high")
    user = {"id": "demo-user", "parent": "", "role": "user", "text": tr("assistant.prompt.portrait"), "status": "completed"}
    with Image.new("RGB", (96, 64)) as image:
        image.putdata([(188 + x // 3, 166 + y, 136 + x // 4) for y in range(image.height) for x in range(image.width)])
        reference = {"role": "reference", "name": "reference.jpg", "attachment": "0" * 64,
                     "thumbnail": reference_thumbnail(image),
                     "analysis": analyzed_photo(image, identity={"reference_attachment": "0" * 64}, display="original")}
    user["reference"] = reference
    reply = {"id": "demo-reply", "parent": user["id"], "role": "assistant", "status": "completed", "mode": "edit",
             "text": text("曝光稍微提高，收住高光，保留自然肤色。", "Lift exposure slightly, soften highlights and keep natural skin tones."),
             "configuration": {"model": model.public(), "connection": {"name": "DeepSeek"}, "effort": "high"},
             "action": {"outcome": "applied", "adjustments": {"color": {"exposure_amt": 25, "highlights": -18},
                                                               "beauty": {"skin_smooth": 25}},
                        "changes": [{"tool": "color_adjust", "key": "exposure_amt", "before": 0, "after": 25},
                                    {"tool": "color_adjust", "key": "highlights", "before": 0, "after": -18},
                                    {"tool": "beauty", "key": "skin_smooth", "before": 0, "after": 25}]}}
    review = {"id": "demo-review", "parent": reply["id"], "role": "user", "status": "completed",
              "attachments": [], "text": text("肤色再自然一点。", "Keep the skin tone a little more natural.")}
    final = {"id": "demo-final", "parent": review["id"], "role": "assistant", "status": "completed", "mode": "edit",
             "configuration": reply["configuration"],
             "text": text("高光已经收住，肤色自然，不再调整。", "Highlights are under control and skin looks natural.")}
    variant_photo = AssistantContext("demo", 1, {}, WorkflowGraph(), object_key="photo", revision_id="demo-revision")
    pair = {"id": "demo-pair", "number": 1, "status": "pending", "run_id": "demo-run", "message_id": final["id"],
            "source": variant_photo.identity(), "baseline": {}, "derived_from": None, "before_attachment": "0" * 64,
            "reference_attachment": "0" * 64, "feedback": [], "receipt": None, "model": model.model,
            "variants": {key: {"description": description, "color": {"exposure_amt": amount}, "beauty": {},
                         "changes": [], "editor_changes": [{"tool": "color_adjust", "key": "exposure_amt", "before": 0, "after": amount}],
                         "attachment": key.lower() * 64, "workflow": {}}
                         for key, amount, description in (("A", 20, text("柔和暖调", "Soft and warm")),
                                                          ("B", -10, text("清透冷调", "Clear and cool")))}}
    final["variants"] = [pair]
    for message in (reply, review, final):
        message["run_id"] = "demo-run"
    reply["run"] = {"id": "demo-run", "runtime": "agent", "loop": True, "phase": "waiting", "reason": "completed",
                    "round": 2, "max_rounds": 5, "applied_rounds": 1, "elapsed": 12,
                    "tokens": 5600, "estimated_usage": True, "changes": reply["action"]["changes"],
                    "latest_message": reply["id"], "last_response": final["id"], "max_calls": 32,
                    "tool_calls": [{"id": name, "name": name, "status": "completed", "result": {"ok": True}}
                                   for name in ("inspect_photo", "analyze_photo", "compare_reference", "adjust_color", "adjust_beauty", "view_photo")],
                    "plan": [{"title": text("分析明暗与肤色", "Inspect brightness and skin tone"), "status": "completed"},
                             {"title": text("调色与自然美颜", "Adjust color and retouch naturally"), "status": "completed"}]}
    reply["run"]["tool_calls"].append({"id": "demo-fit", "name": "fit_reference", "status": "completed",
        "result": {"ok": True, "status": "suggested", "steps": [{"dimension": "tone", "code": "improved",
            "accepted": True, "before_error": {"tone": 12.5}, "after_error": {"tone": 3.2}}],
            "skipped": [{"field": "split_tone.shadows", "code": "near_black"}]}})
    reply["run"]["tool_calls"].append({"id": "demo-evaluation", "name": "evaluate_versions", "status": "completed",
        "result": {"ok": True, "backend": "main", "model": model.model, "criteria_version": 1, "status": "evaluated",
                   "dimensions": {"tone": {"choice": "A", "confidence": .82, "position_consistent": True},
                                  "skin": {"choice": "insufficient_evidence", "confidence": None, "position_consistent": False}},
                   "requests": [{"usage": {"input_tokens": 2600, "output_tokens": 80}},
                                {"usage": {"input_tokens": 2600, "output_tokens": 80}}]}})
    record = {"id": "demo", "title": text("自然肤色", "Natural skin tones"), "photo_name": "portrait.jpg", "summary": None,
              "messages": {item["id"]: item for item in (user, reply, review, final)}, "head": final["id"],
              "preferences": {"mode": "agent", "model_id": model.id, "effort": "high",
                              "context": context_defaults(), "context_start": ""}}
    state = {"current": "demo", "conversation": record, "messages": [user, reply, review, final], "photo": "photo",
             "reference": reference,
             "busy": [],
             "models": [asdict(model)], "connections": [{"id": "demo-connection", "name": "DeepSeek", "endpoint": "https://api.deepseek.com"}],
             "conversations": [{"id": "demo", "title": record["title"], "photo_name": "portrait.jpg", "object_key": "photo",
                                "archived": False, "pinned": True, "unread": False, "search_text": user["text"],
                                "updated": 1760500000.0}]}
    observation = [{"source": source, "quality": "native", "size": [768, 768], "bytes": 180000,
                    "estimated_tokens": 765, "estimate_basis": "pixels.heuristic"}
                   for source in ("original", "start", "current")]
    next(call for call in reply["run"]["tool_calls"] if call["name"] == "view_photo")["result"].update(images=observation)
    reply["context"] = {"manifest": [], "data": {"image_observation": image_capability(model)},
                        "sent_images": observation, "estimated_tokens": 5600,
                        "image_budget": {"count": 3, "max_count": 24, "bytes": 540000, "max_bytes": 64 * 1024 * 1024}}
    holder = QWidget()
    holder.setObjectName("aiAssistantPanel")
    layout = QVBoxLayout(holder)
    from src.ui.widgets.assistant_context_inspector import disclosure_text
    disclosure = QLabel(disclosure_text(record["preferences"]["context"], model.public(), tr), holder)
    disclosure.setObjectName("mutedText")
    disclosure.setWordWrap(True)
    layout.addWidget(disclosure)
    transcript = AssistantTranscript(holder)
    transcript.set_messages("demo", [user, reply, review, final], tr)
    transcript.set_action_states({reply["id"]: "applied"}, busy=False)
    transcript.setMinimumHeight(240)
    layout.addWidget(transcript)
    from src.ui.widgets.assistant_variants import AssistantVariants
    variants = AssistantVariants(holder)
    previews = {}
    for key in ("A", "B", "before", "reference"):
        with Image.new("RGBA", (96, 64)) as image:
            image.putdata([(174 + x // 3, 175 + y, 182 + x // 4, 255) if key == "B"
                           else (188 + x // 3, 166 + y, 136 + x // 4, 255)
                           for y in range(image.height) for x in range(image.width)])
            previews[key] = {"size": image.size, "rgba": image.tobytes()}
    variants.images_requested.connect(lambda group, ticket: variants.set_images(group, ticket, previews))
    variants.set_state(state, tr, photo=variant_photo, expanded=True)
    layout.addWidget(variants)
    composer = AssistantComposer(holder)
    composer.set_state(state, tr, context_count=2, estimate={"estimated_tokens": 1694, "window": model.context_window,
                       "reserved_tokens": model.max_output_tokens + model.reasoning_reserve, "omitted_messages": 0})
    layout.addWidget(composer)
    long_composer = AssistantComposer(holder)
    long_composer.set_state({**state, "models": [{**asdict(model), "name":
        "OpenPictureStudio Portrait & Color Reference · Extended Context 2026-10-19"}]}, tr)
    layout.addWidget(long_composer)
    conversations = ConversationList()
    conversations.set_state(state, tr)
    conversations.setMinimumHeight(240)
    context = ContextInspector()
    context.set_state(state, tr, photo=AssistantContext("demo", 1, {}, WorkflowGraph(), photo_name="portrait.jpg"))
    context.setMinimumHeight(300)
    from src.ui.widgets.assistant_styles import StyleConfirmation, StyleManager
    sample = {"id": "sample", "scene": text("日景人像", "Daylight portrait"),
              "thumbnails": {key: reference["thumbnail"] for key in ("before", "result")},
              "delta": {"color": {"exposure_amt": 10}, "beauty": {}, "mixer": {}}, "feedback": [],
              "before_revision": "r1", "result_revision": "r2", "source": variant_photo.identity()}
    style_state = {**state, "styles": [{"id": "style", "name": text("柔和暖调", "Soft warmth"), "description": "",
                    "status": "active", "version": 1, "samples": [sample]}],
                   "style_preferences": [{"id": "pref", "text": text("避免过饱和", "Avoid oversaturation")}], "style_busy": []}
    manager, confirmation = StyleManager(), StyleConfirmation()
    manager.set_state(style_state, tr)
    manager.select_sample("sample")
    confirmation.set_state({"ticket": "sample", "kind": "style", "status": "ready", "name": sample["scene"],
        "scene": sample["scene"], "history": [{"id": "r1", "number": 1}, {"id": "r2", "number": 2, "purpose": "style"}],
        "before_revision": "r1", "result_revision": "r2"}, style_state["styles"], tr)
    from src.ui.widgets.assistant_evaluations import EvaluationSettings
    evaluation = EvaluationSettings()
    evaluation.set_state({**state, "evaluations": {"backend": "main", "decisions_connection": "",
                                                  "ready_connections": [], "backends": ["main"]}}, tr)
    from src.ui.widgets.assistant_transcript import assistant_error_text
    capability = image_capability(AssistantModel(model="gpt-4o", image_detail="high", image_effective_long_edge=2048))
    image_limits = SettingSection()
    rule_row = SettingRow(image_limits)
    rule_row.set_text(tr("assistant.field.model.image_effective_long_edge"))
    rule_row.set_message(tr("assistant.image_rule_exceeded", limit=capability["rule_long_edge"],
                            effective=capability["effective_long_edge"]), "warning")
    image_limits.add_row(rule_row)
    storage_row = SettingRow(image_limits)
    storage_row.set_message(assistant_error_text({"code": "AI_ATTACHMENT_LIMIT", "bytes": 500 * 1024 * 1024,
                                                "max_bytes": MAX_CONVERSATION_ATTACHMENT_BYTES}, tr), "warning")
    image_limits.add_row(storage_row)
    return [holder, _row(conversations, context), _row(manager, confirmation), evaluation, image_limits]


def photo_batch_section(text) -> list[QWidget]:
    from types import SimpleNamespace
    from PySide6.QtCore import QObject, Signal
    from src.ui.dialogs.photo_batch_dialog import PhotoBatchDialog

    class PreviewSettings:
        def __init__(self):
            self.values = {}

        def value(self, key, default=None):
            return self.values.get(key, default)

        def setValue(self, key, value):
            self.values[key] = value

    class PreviewService:
        def batches(self):
            return ("summer-portraits",)

        def paths(self, batch_id):
            return ("DSC_1042.NEF", "DSC_1043.NEF", "DSC_1044.NEF")

        def summary(self, batch_id, *, limit=50):
            rows = [{"name": name, "status": "conflict" if index == 2 else "success",
                     "history": "changed" if index == 2 else "applied",
                     "error": {"message_key": "batch.conflict", "details": {}} if index == 2 else None}
                    for index, name in enumerate(self.paths(batch_id))]
            return {"batch": batch_id, "operation": "sync", "total": 3, "active": False, "started": True,
                    "save_state": "saved", "counts": {"success": 2, "unchanged": 0, "conflict": 1,
                        "failed": 0, "cancelled": 0, "pending": 0, "preparing": 0}, "items": rows[:limit]}

        def create(self, *args, **kwargs):
            return "summer-portraits"

        def preflight(self, batch_id):
            return self.summary(batch_id)

    class PreviewRunner(QObject):
        changed = Signal(str)

        def __init__(self):
            super().__init__()
            self.service = PreviewService()
            self.drafts = SimpleNamespace(loaded=True)

        def start(self, batch_id, **options):
            self.changed.emit(batch_id)

        def cancel(self, batch_id):
            self.changed.emit(batch_id)

    runner = PreviewRunner()
    dialog = PhotoBatchDialog(runner, lambda: {"scope": "summer", "sample_snapshot": "summer:sample",
        "sample_name": "DSC_1041.NEF", "selected_count": 4, "filtered_count": 36},
        lambda path: None, settings=PreviewSettings())
    dialog.capture_selection()
    dialog.show_batch("summer-portraits")
    return [_embedded(dialog)]


def photo_export_section(text) -> list[QWidget]:
    from types import SimpleNamespace
    from PySide6.QtCore import QObject, Signal
    from src.application.result_types import CoreResult
    from src.ui.dialogs.photo_export_dialog import PhotoExportDialog

    class PreviewService:
        def __init__(self):
            self.presets, self.records = {}, []
            self.configuration, self.store_error = None, None
            self.batches = {"portraits": SimpleNamespace(items=())}

        def configure(self, preset):
            self.configuration = preset

        def summary(self, identity, *, offset=0, limit=50):
            rows = [{"index": index, "name": f"DSC_{1041 + index}.NEF",
                     "target": f"D:/Photos/Exports/DSC_{1041 + index}_edited.jpg", "status": status,
                     "record_state": "saved" if status == "success" else "pending", "error": None,
                     "delivery": "modified" if index == 1 else "current"}
                    for index, status in enumerate(("success", "success", "pending"))]
            return {"export": identity, "total": 3, "counts": {"success": 2, "failed": 0, "cancelled": 0},
                    "items": rows[offset:offset + limit], "paused": True}

        def get(self, identity):
            return self.batches[identity]

    class PreviewRunner(QObject):
        changed = Signal(str)
        preview_ready = Signal(object)
        preview_cleared = Signal()

        def __init__(self):
            super().__init__()
            self.service = PreviewService()

        def control(self, identity, action):
            self.changed.emit(identity)

        def enqueue(self, *args, **kwargs):
            return self.service.summary("portraits")

        def save_preset(self, name, preset, callback):
            self.service.presets[name] = preset.to_dict()
            callback(CoreResult.ok())

        def cancel_preview(self):
            pass

        def preview(self, identity, index):
            self.preview_ready.emit(CoreResult.fail("INVALID_INPUT", "export_queue.capture_failed"))

    runner = PreviewRunner()
    dialog = PhotoExportDialog(runner, lambda: {"scope": "portraits", "selected_count": 3, "filtered_count": 36})
    dialog._directory.setText("D:/Photos/Exports")
    dialog.capture_selection()
    return [_embedded(dialog)]


def photo_browser_section(text) -> list[QWidget]:
    from types import SimpleNamespace
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
    from src.application.photo_browser import PhotoBrowserService
    from src.core.image_manager import ImageManager
    from src.ui.widgets.photo_browser import PhotoBrowserWorkspace

    class Metadata:
        def __init__(self):
            self.ratings, self.marks = {}, {}

        def get_rating(self, path):
            return self.ratings.get(path, 0)

        def get_mark(self, path):
            return self.marks.get(path, "unmarked")

        def set_marks(self, paths, mark):
            self.marks.update({path: mark for path in paths})

        def set_ratings(self, paths, rating):
            self.ratings.update({path: rating for path in paths})

    class PreviewRunner(QObject):
        ready = Signal(str)
        clearing = Signal(str)

        def __init__(self):
            super().__init__()
            self.images = {}

        def request(self, channel, paths, maximum=300):
            pass

        def forget(self, channel):
            pass

        def retain(self, channel, image):
            pass

        def retry(self):
            pass

        def scan_sources(self):
            pass

        def image(self, path, maximum=300, **kwargs):
            return self.images.get(path)

    class SampleBrowser(PhotoBrowserService):
        def photo_state(self, path):
            index = self.manager.image_list.index(path)
            return {"edited": index < 3, "delivery": "recorded" if index == 0 else "unrecorded", "workflow_hash": "sample"}

    metadata, manager, runner = Metadata(), ImageManager(), PreviewRunner()
    paths = [f"D:/Photos/Autumn/DSC_{1041 + index}.NEF" for index in range(8)]
    manager.publish_images(paths)
    metadata.ratings = {paths[0]: 5, paths[1]: 4, paths[3]: 3}
    metadata.marks = {paths[0]: "keep", paths[1]: "keep", paths[4]: "reject"}
    service = SampleBrowser(manager, metadata, is_raw=lambda path: True, drafts_ready=lambda: True)
    service.refresh()
    service.select(paths[:2])
    for index, path in enumerate(paths):
        pixmap = QPixmap(300, 200)
        painter = QPainter(pixmap)
        gradient = QLinearGradient(0, 0, 300, 200)
        gradient.setColorAt(0, QColor.fromHsl((index * 25 + 20) % 360, 55, 150))
        gradient.setColorAt(1, QColor.fromHsl((index * 25 + 80) % 360, 80, 70))
        painter.fillRect(pixmap.rect(), gradient)
        painter.end()
        runner.images[path] = SimpleNamespace(pixmap=pixmap, size=(3600, 2400), error="")
    translate = get_translation_service().translate
    widget = PhotoBrowserWorkspace(service, runner, translate)
    widget.changed.connect(widget.refresh)
    widget.image_requested.connect(manager.select_image)
    widget.setMinimumHeight(650)
    runner.setParent(widget)
    return [widget]


def photo_calibration_section(text) -> list[QWidget]:
    from types import SimpleNamespace
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
    from PySide6.QtWidgets import QCheckBox, QLineEdit, QTreeWidget, QTreeWidgetItem
    from src.ui.dialogs.photo_calibration_dialog import CalibrationPreview

    tr = lambda key, **values: get_translation_service().translate(key, **values)

    class PreviewRunner(QObject):
        ready = Signal(str)
        clearing = Signal(str)

        def __init__(self):
            super().__init__()
            self.images = {}

        def request_fixed(self, channel, snapshot, maximum=1600):
            pass

        def forget(self, channel):
            pass

        def retain(self, channel, image):
            pass

        def image(self, path, maximum=1600, *, fixed=None):
            return self.images[fixed.id]

    holder = QFrame()
    holder.setObjectName("surfaceCard")
    root = QVBoxLayout(holder)
    controls = QHBoxLayout()
    name = QLineEdit(text("日落人像", "Sunset portraits"))
    controls.addWidget(name, 1)
    sample = AppleDropDown(holder)
    sample.addItem("DSC_1041.NEF")
    controls.addWidget(sample)
    controls.addWidget(AppleButton(tr("calibration.update_group")))
    root.addLayout(controls)
    row = QHBoxLayout()
    for key in ("exposure", "white_balance"):
        check = QCheckBox(tr("calibration." + key))
        check.setChecked(True)
        row.addWidget(check)
    row.addStretch()
    row.addWidget(AppleButton(tr("calibration.prepare")))
    row.addWidget(ApplePrimaryButton(tr("calibration.apply")))
    root.addLayout(row)
    table = QTreeWidget(holder)
    table.setColumnCount(4)
    table.setHeaderLabels([tr(key) for key in ("batch.photo", "calibration.exposure", "calibration.white_balance", "batch.result")])
    QTreeWidgetItem(table, ["DSC_1042.NEF", "+0.00 → +0.33 EV", "0 / 0 → 8 / −2", tr("calibration.state.ready")])
    QTreeWidgetItem(table, ["DSC_1043.NEF", "–", "–", tr("calibration.special_mode")])
    table.setMaximumHeight(120)
    for column in range(3):
        table.resizeColumnToContents(column)
    root.addWidget(table)
    runner = PreviewRunner()
    snapshots = [SimpleNamespace(id="before", path="DSC_1042.NEF"), SimpleNamespace(id="after", path="DSC_1042.NEF")]
    for index, snapshot in enumerate(snapshots):
        pixmap = QPixmap(500, 280)
        painter = QPainter(pixmap)
        gradient = QLinearGradient(0, 0, 500, 280)
        gradient.setColorAt(0, QColor("#d5a987" if index else "#b8a5a0"))
        gradient.setColorAt(1, QColor("#655b56" if index else "#51545d"))
        painter.fillRect(pixmap.rect(), gradient)
        painter.end()
        runner.images[snapshot.id] = SimpleNamespace(pixmap=pixmap, size=(500, 280), error="")
    preview = CalibrationPreview(runner, tr, holder)
    preview.set_snapshots(*snapshots)
    runner.setParent(preview)
    root.addWidget(preview)
    notice = QLabel(tr("calibration.notice"), holder)
    notice.setWordWrap(True)
    root.addWidget(notice)
    return [holder]


def photo_library_section(text) -> list[QWidget]:
    from types import SimpleNamespace
    from PySide6.QtCore import QObject, Qt, Signal
    from src.application.photo_library import PhotoLibraryService
    from src.application.photo_library_store import path_key
    from src.ui.dialogs.photo_library_dialog import PhotoLibraryDialog, LibraryRuleDialog

    class LibraryRunner(QObject):
        changed = Signal()
        evaluated = Signal()
        failed = Signal(str)
        idle = Signal()

        def __init__(self):
            super().__init__()
            browser = SimpleNamespace(selected=(), scope="browser:gallery")
            self.service = PhotoLibraryService(None, SimpleNamespace(supports_quick_preview=lambda path: path.lower().endswith(".nef")), browser)
            photos = []
            for index, (name, rating, mark) in enumerate((("DSC_1042.NEF", 5, "keep"), ("DSC_1043.NEF", 4, "keep"), ("DSC_1044.NEF", 0, "reject"))):
                path = "D:/Photos/France 2026/" + name
                photos.append({"photo_id": str(index + 1) * 32, "current_path": path, "last_known_path": path,
                    "path_key": path_key(path), "file_hash": "a" * 64, "format": ".nef", "mtime_ns": 0, "size": 24000000,
                    "rating": rating, "mark": mark, "revision": 1, "keywords": ["travel"] if index < 2 else [],
                    "deliveries": [], "export_records": []})
            self.service.install({"photos": photos, "keywords": [{"keyword_id": "travel", "name": "France 2026", "name_key": "france 2026", "revision": 1}],
                "collections": [{"collection_id": "selects", "name": text("旅行精选", "Travel selects"), "kind": "ordinary", "rules": {}, "revision": 1,
                    "members": [photos[0]["photo_id"], photos[1]["photo_id"]]}], "pending": [], "aliases": []})
            self.service.availability = {p["photo_id"]: "available" if i < 2 else "missing" for i, p in enumerate(photos)}
            self.service._dynamic = {p["photo_id"]: {"edited": i < 2, "delivery": "unrecorded"} for i, p in enumerate(photos)}

        @property
        def busy(self):
            return False

        def command(self, operation, values, callback, cancelled=lambda: False):
            if operation == "preview_photo_collection":
                callback(self.service.evaluate(values["rules"]), "")
            elif operation == "get_photo_library":
                callback(self.service.query(), "")
            else:
                callback(None, "library.invalid_command")

        def scan(self):
            self.changed.emit()

        def target(self, path, kind):
            return "gallery-target"

    runner = LibraryRunner()
    translate = get_translation_service().translate
    library = PhotoLibraryDialog(runner, translate)
    library.setWindowFlags(Qt.WindowType.Widget)
    library.setMinimumSize(1060, 600)
    rules = LibraryRuleDialog(runner, translate, {"rating": [4, 5], "mark": ["keep"], "edited": [True], "delivery": ["unrecorded", "modified"]})
    rules.setWindowFlags(Qt.WindowType.Widget)
    rules.preview()
    library._gallery_runner = rules._gallery_runner = runner
    return [library, rules]


SECTIONS = (
    ("按钮", "Buttons", buttons_section),
    ("分段开关", "Segmented control", segmented_section),
    ("参数行", "Parameter rows", parameter_rows_section),
    ("观感选择", "Look selection", color_looks_section),
    ("局部蒙版", "Local masks", masks_section),
    ("成组调整", "Batch adjustments", photo_batch_section),
    ("批量导出", "Batch export", photo_export_section),
    ("选片与对照", "Photo selection and comparison", photo_browser_section),
    ("照片组与校准", "Photo groups and calibration", photo_calibration_section),
    ("图库与集合", "Photo library and collections", photo_library_section),
    ("保留细节美颜", "Detail-preserving beauty", beauty_section),
    ("选择与开关", "Choices and switches", choices_section),
    ("小组件", "Small widgets", small_widgets_section),
    ("直方图与曲线", "Histogram and curves", scopes_section),
    ("面板底部的操作区", "Panel footer", action_footer_section),
    ("列表行与分组标题", "List rows and group titles", lists_section),
    ("资源管理页", "Resource management", management_section),
    ("空状态", "Empty states", empty_state_section),
    ("对话框", "Dialogs", dialogs_section),
    ("表单行", "Setting rows", setting_rows_section),
    ("存储空间", "Storage", storage_section),
    ("照片信息", "Photo info", photo_info_section),
    ("工具栏", "Tool rail", tool_rail_section),
    ("编辑栈、历史与改动卡片", "Edit stack, history and change card", edit_history_section),
    ("助手对话、输入与上下文", "Assistant chats, composer and context", assistant_section),
    ("AI 服务连接与模型", "AI service setup", assistant_settings_section),
    ("窗口按钮", "Window controls", window_controls_section),
    ("图标", "Icons", icons_section),
    ("语义颜色", "Semantic colors", colors_section),
)


class Gallery(QWidget):
    def __init__(self, manager: ThemeManager):
        super().__init__()
        self._manager = manager
        self._language = "zh"
        # 样例页用自己的翻译服务，关闭时把原来的放回去
        self._previous_translations = get_translation_service()
        self._translations = TranslationService(default_language=LANGUAGE_CODES["zh"], fallback_language="en")
        set_translation_service(self._translations)
        self.setWindowTitle("OpenPictureStudio 组件样例页")
        self.resize(1280, 900)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(px(SPACING_LG), px(SPACING_SM), px(SPACING_LG), px(SPACING_SM))
        self._theme_switch = AppleSegmentedControl()
        self._theme_switch.add_segments([zh for _, zh, _ in THEMES])
        self._theme_switch.currentIndexChanged.connect(lambda index: self.set_theme(THEMES[index][0]))
        self._language_switch = AppleSegmentedControl()
        self._language_switch.add_segments([label for _, label in LANGUAGES])
        self._language_switch.currentIndexChanged.connect(lambda index: self.set_language(LANGUAGES[index][0]))
        for control in (self._theme_switch, self._language_switch):
            control.setSizePolicy(control.sizePolicy().Policy.Fixed, control.sizePolicy().Policy.Fixed)
            bar_layout.addWidget(control)
        bar_layout.addStretch()
        root.addWidget(bar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        root.addWidget(self._scroll, 1)
        self._rebuild()

    def set_theme(self, name: str) -> None:
        self._manager.apply(name)
        self._theme_switch.setCurrentIndex([theme for theme, _, _ in THEMES].index(name))
        self._rebuild()

    def set_language(self, language: str) -> None:
        self._language = language
        self._translations.set_language(LANGUAGE_CODES[language])
        self._language_switch.setCurrentIndex([code for code, _ in LANGUAGES].index(language))
        self._rebuild()

    def _rebuild(self) -> None:
        def text(zh: str, en: str) -> str:
            return zh if self._language == "zh" else en

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_LG), px(SPACING_XL), px(SPACING_XL))
        layout.setSpacing(px(SPACING_MD))
        for zh, en, build in SECTIONS:
            title = QLabel(text(zh, en))
            title.setObjectName("dialogTitle")
            layout.addSpacing(px(SPACING_XS))
            layout.addWidget(title)
            for widget in build(text):
                layout.addWidget(widget)
        layout.addStretch()
        self._release_samples()
        self._scroll.setWidget(content)

    def _release_samples(self) -> None:
        content = self._scroll.widget()
        if content is not None:
            for bar in content.findChildren(PhotoInfoSidebar):
                bar.panel.cleanup()
            for selector in content.findChildren(ModelSelector):
                selector.close_subscription()
            for controls in content.findChildren(ColorLookControls):
                controls.cleanup()

    def closeEvent(self, event) -> None:
        self._release_samples()
        if get_translation_service() is self._translations:
            set_translation_service(self._previous_translations)
        super().closeEvent(event)

    def content_height(self) -> int:
        return self._scroll.widget().sizeHint().height() + 80


def main() -> int:
    app = QApplication(sys.argv[:1])
    installed = set(QFontDatabase.families())
    font = QFont()
    families = [family for family in FONT_FAMILIES if family in installed]
    if families:
        font.setFamilies(families)
    app.setFont(font)
    manager = ThemeManager()
    manager.apply("dark")
    set_theme_manager(manager)
    gallery = Gallery(manager)
    if "--shots" not in sys.argv:
        gallery.show()
        return app.exec()

    target = Path(sys.argv[sys.argv.index("--shots") + 1])
    target.mkdir(parents=True, exist_ok=True)
    gallery.show()
    for theme, _, _ in THEMES:
        for language, _ in LANGUAGES:
            gallery.set_theme(theme)
            gallery.set_language(language)
            gallery.resize(1280, gallery.content_height())
            for _ in range(20):
                app.processEvents()
            gallery.grab().save(str(target / f"gallery_{theme}_{language}.png"))
            print("saved", theme, language, flush=True)
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
