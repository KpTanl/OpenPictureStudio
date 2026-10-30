"""Color Adjust Panel for in-window sidebar editing."""

import copy
from uuid import uuid4
import logging
from typing import Optional
from PIL import Image

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel

from resources.styles.theme_variables import SPACING_SM, px
from .base_tool_panel import BaseToolPanel
from ...core.tools.color import COLOR_UI_PARAMETERS, color_look_options, preserve_color_identity, color_ui_contract, reset_color_params
from ...core.local_masks import (LOCAL_UI_SCALES, MASK_LIMIT, copy_mask, local_engine_key)
from ...core.mask_components import (COMPONENT_LIMIT, NODE_COMPONENT_LIMIT, POINT_LIMIT, brush_strokes, new_mask,
                                     prepare_components)
from ..adapters.tool_adapter import ColorAdjustAdapter
from ..widgets.apple_button import AppleButton
from ..widgets.apple_menu import AppleDropDown
from ..widgets.histogram_widget import HistogramWidget
from ..widgets.curves_widget import CURVE_CHANNELS, CurvesWidget
from ..widgets.color_look_controls import ColorLookControls
from ..widgets.collapsible_card import CollapsibleCard
from ..overlays.eyedropper_overlay import EyedropperOverlay
from ..overlays.mask_overlay import MaskOverlay
from ..services.mask_range_runner import MaskRangeRunner
from ..widgets.segmented_control import AppleSegmentedControl
from .mask_panel import MaskPanel
from ..widgets.color_mixer_widget import MIXER_PARAMETER_LABELS, ColorMixerWidget
from ..widgets.color_sliders import WB_SLIDER_DEFS, TONE_SLIDER_DEFS, PRESENCE_SLIDER_DEFS
from ..widgets.parameter_row import ParameterRow, ParameterSpec
from ..widgets.chip import ChipGroup
from ...core.color_adjuster import ColorAdjuster, FilterPreset
from ...core.photo_engine import adapt_auto_params_for_ui

logger = logging.getLogger(__name__)

# 曲线通道 -> 文案键
_CURVE_CHANNEL_KEYS = {"rgb": "color_adjust.curves.rgb", "r": "color_adjust.curves.red",
                       "g": "color_adjust.curves.green", "b": "color_adjust.curves.blue"}
# 分组 -> (标题的文案键, 默认文案, 是否默认展开)；曲线与色彩混合用得少，默认收起
_GROUPS = {
    "tone": ("color_adjust.group.tone", "Tone & White Balance", True),
    "presence": ("color_adjust.group.presence", "Presence & Saturation", True),
    "curves": ("color_adjust.group.curves", "Tone Curves", False),
    "color_mixer": ("color_adjust.group.color_mixer", "Color Mixer", False),
}
_IDENTITY_CURVE = [(0, 0), (255, 255)]

class ColorAdjustPanel(BaseToolPanel):
    """调色实验室侧栏面板。

    参数控制、直方图、曲线与 HSL 调色板位于右侧常驻面板，
    主查看器负责 1:1 / 适应窗口的全幅实时预览。
    """

    PARAMETER_LABELS = {
        **{spec.key: (spec.label_key, spec.value_format)
           for spec in WB_SLIDER_DEFS + TONE_SLIDER_DEFS + PRESENCE_SLIDER_DEFS},
        "curves": (_GROUPS["curves"][0], ""),
        **{"curves." + channel: (label_key, "") for channel, label_key in _CURVE_CHANNEL_KEYS.items()},
        "color_mixer": (_GROUPS["color_mixer"][0], ""),
        **MIXER_PARAMETER_LABELS,
        "look": ("color_adjust.look.label", ""),
    }

    PARAMETER_CONTRACT = COLOR_UI_PARAMETERS
    TOOL_ID = "color_adjust"
    TOOL_TITLE_KEY = "menu.edit.color_lab"
    TOOL_TITLE_DEFAULT = "Color Lab"
    TOOL_ADAPTER = ColorAdjustAdapter()
    sampling_requested = Signal(bool)
    sampling_picked = Signal(float, float)
    sampled_preview_ready = Signal(object)
    mask_mode_changed = Signal(bool)
    mask_ui_changed = Signal()
    mask_create_requested = Signal(object)
    mask_upgrade_requested = Signal()
    mask_gesture_started = Signal(object)
    mask_gesture_changed = Signal(object)
    mask_gesture_finished = Signal(object)
    mask_gesture_cancelled = Signal()
    # 画布上正在新建区域（类型、添加或减去）与结束；画笔设置条随之出现与收起
    mask_tool_started = Signal(str, str)
    mask_tool_finished = Signal()
    mask_generation_cancel_requested = Signal()

    def __init__(
        self,
        image_path: str,
        parent: Optional[QWidget] = None,
        edit_state_store=None,
        **services,
    ):
        self._original_image: Optional[Image.Image] = None

        self._params = self._default_params()
        self._reset_params = self._default_params()
        self._undo_stack: list[dict] = []
        self._redo_stack: list[dict] = []


        self._parameter_rows: dict[str, ParameterRow] = {}
        self._group_cards: dict[str, CollapsibleCard] = {}
        self._group_modified: dict[str, bool] = {}

        self._sampling_state = "closed"
        self._mask_page = False
        self._mask_selected = None
        self._mask_component = None
        self._mask_range = False
        self._mask_ready = False
        self._mask_reference_size = (1, 1)
        self._mask_context = None
        self._mask_engine_params = None
        self._mask_preview_params = None
        self._mask_message = ""
        self._mask_can_upgrade = False
        self._mask_range_runner = None
        self._mask_busy = None
        self._brush_settings = {"radius": .03, "hardness": .5, "opacity": 1.0}

        super().__init__(image_path, parent=parent, edit_state_store=edit_state_store, **services)

        # 加载初始图片与恢复暂存参数
        self._load_source(image_path)
        self._restore_initial_params()
        self.update_preview()

    @staticmethod
    def _default_params():
        return COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version)

    @staticmethod
    def _from_engine_params(engine: dict) -> dict:
        return ColorAdjustAdapter().workflow_to_ui(engine)

    def _load_source(self, path):
        self.load_panel_source(path, self._sync_look_controls)

    def _restore_initial_params(self) -> None:
        """从 EditStateStore 恢复暂存或上次使用的参数。"""
        defaults = self._default_params()
        initial = defaults
        store = self._edit_state_store
        if store and self._image_path:
            initial = store.get_tool_stash(self._image_path, self.TOOL_ID)
            if initial is None:
                last_used = store.get_last_used(self.TOOL_ID)
                # 上次参数只沿用调整；观感与处理版本属于各张照片，取契约默认。
                initial = preserve_color_identity(last_used, defaults) if last_used else defaults
                initial = {**initial, "masks": None}
        try:
            self.restore_params(initial)
        except ValueError as error:
            self._processing_error(error)
            self.restore_params(defaults)

    def build_ui(self) -> None:
        root = self._root_layout
        self._mode = AppleSegmentedControl(self)
        self._mode.add_segments(("", ""))
        self._mode.activated.connect(self._on_mask_mode)
        root.addWidget(self._mode)
        self._global_page = QWidget(self)
        layout = QVBoxLayout(self._global_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        root.addWidget(self._global_page)

        # 快捷操作：一键得到一组参数
        quick_bar = QHBoxLayout()
        quick_bar.setSpacing(px(SPACING_SM))
        self._btn_auto = AppleButton("", self)
        self._btn_auto.clicked.connect(self._on_auto)
        self._btn_bw = AppleButton("", self)
        self._btn_bw.clicked.connect(self._on_bw)
        self._btn_hdr = AppleButton("", self)
        self._btn_hdr.clicked.connect(self._on_hdr)
        for button in (self._btn_auto, self._btn_bw, self._btn_hdr):
            quick_bar.addWidget(button, 1)
        layout.addLayout(quick_bar)

        # 观感与预设：两行的名称同宽，后面的控件左边对齐
        self._look_controls = ColorLookControls(self)
        self._look_controls.look_edited.connect(self._on_look_edited)
        self._look_controls.upgrade_requested.connect(self._on_exposure_upgrade)
        layout.addWidget(self._look_controls)

        preset_layout = QHBoxLayout()
        preset_layout.setSpacing(px(SPACING_SM))
        self._preset_label = QLabel(self)
        preset_layout.addWidget(self._preset_label)
        self._preset_combo = AppleDropDown(self)
        self._preset_combo.activated.connect(self._on_preset_changed)
        preset_layout.addWidget(self._preset_combo, 1)
        layout.addLayout(preset_layout)

        self._histogram_widget = HistogramWidget(self)
        layout.addWidget(self._histogram_widget)

        for group, (_title_key, _title, expanded) in _GROUPS.items():
            self._group_cards[group] = CollapsibleCard("", self, is_expanded=expanded)

        # 参数行自带上边距，行与行之间不再另加间距
        for group, specs in (("tone", WB_SLIDER_DEFS + TONE_SLIDER_DEFS), ("presence", PRESENCE_SLIDER_DEFS)):
            rows = QVBoxLayout()
            rows.setSpacing(0)
            for spec in specs:
                rows.addWidget(self._parameter_row(spec))
            self._group_cards[group].add_layout(rows)

        # 曲线：上面选要调的那一条，四条曲线都画在同一张图里
        self._curve_channels = ChipGroup(len(CURVE_CHANNELS), self)
        self._curve_channels.set_items([(channel, "") for channel in CURVE_CHANNELS])
        self._curves_widget = CurvesWidget(self)
        self._curve_channels.set_current(self._curves_widget.active_channel())
        self._curve_channels.current_changed.connect(self._curves_widget.set_active_channel)
        self._curves_widget.curves_changed.connect(self._on_curve_changed)
        self.track_edit_control(self._curves_widget,
            started=self._curves_widget.interaction_started,
            finished=self._curves_widget.interaction_finished,
            is_active=self._curves_widget.is_interacting)
        self._group_cards["curves"].add_widget(self._curve_channels)
        self._group_cards["curves"].add_widget(self._curves_widget)

        self._mixer_widget = ColorMixerWidget(self._tr, parent=self)
        self._mixer_widget.values_changed.connect(self._on_mixer_changed)
        for slider in self._mixer_widget.edit_controls():
            self.track_edit_control(slider, started=slider.sliderPressed,
                finished=slider.sliderReleased, is_active=slider.isSliderDown)
        # 吸管：在画布上点击颜色，切换到对应的混色通道
        self.overlay = EyedropperOverlay(self)
        self._eyedropper_overlay = self.overlay
        self.mask_overlay = MaskOverlay(self)
        self.mask_overlay.selected.connect(self._select_mask)
        self.mask_overlay.gesture_started.connect(self.mask_gesture_started.emit)
        self.mask_overlay.gesture_changed.connect(self.mask_gesture_changed.emit)
        self.mask_overlay.gesture_finished.connect(self.mask_gesture_finished.emit)
        self.mask_overlay.gesture_cancelled.connect(self.mask_gesture_cancelled.emit)
        self.mask_overlay.constraint_reached.connect(lambda key: self.request_status.emit(self._tr(key)))
        self.mask_overlay.creation_started.connect(self.mask_tool_started.emit)
        self.mask_overlay.creation_finished.connect(self.mask_tool_finished.emit)
        self._mixer_widget.eyedropper_toggled.connect(self._on_eyedropper_toggled)
        self.overlay.picked.connect(self._on_eyedropper_picked)
        self._group_cards["color_mixer"].add_widget(self._mixer_widget)

        for card in self._group_cards.values():
            layout.addWidget(card)
        layout.addStretch()
        self._mask_panel = MaskPanel(self._tr, self.track_edit_control, self.end_edit_group, self)
        self._mask_panel.selected.connect(self._select_mask)
        self._mask_panel.component_selected.connect(self._select_component)
        self._mask_panel.create_requested.connect(self.mask_create_requested.emit)
        self._mask_panel.generation_cancel_requested.connect(self.mask_generation_cancel_requested.emit)
        self._mask_panel.edit_requested.connect(self._edit_mask)
        self._mask_panel.range_requested.connect(self._show_mask_range)
        self._mask_panel.upgrade_requested.connect(self.mask_upgrade_requested.emit)
        if self._render_service is not None:
            self._mask_panel.configure_models(self._render_service.runtime, self.model_manager_requested.emit)
        root.addWidget(self._mask_panel)
        self._mask_panel.hide()
        self.retranslate_ui()

    @property
    def mask_mode(self):
        return self._mask_page

    @property
    def mask_reference_size(self):
        return self._mask_reference_size

    def mask_ui_state(self):
        return self._mask_page, self._mask_selected, self._mask_range, self._mask_component

    def restore_mask_ui(self, state):
        self.mask_overlay.cancel_interaction()
        self._mask_page, self._mask_selected, self._mask_range = state[:3]
        self._mask_component = state[3] if len(state) > 3 else None
        self._mode.setCurrentIndex(int(self._mask_page))
        self._global_page.setVisible(not self._mask_page)
        self._mask_panel.setVisible(self._mask_page)
        self.overlay = self.mask_overlay if self._mask_page else self._eyedropper_overlay
        self._sync_masks()

    def _on_mask_mode(self, index):
        self.end_edit_group()
        self.cancel_sampling()
        self.mask_overlay.cancel_interaction()
        self._mask_page = bool(index)
        self._global_page.setVisible(not self._mask_page)
        self._mask_panel.setVisible(self._mask_page)
        self.overlay = self.mask_overlay if self._mask_page else self._eyedropper_overlay
        self._mask_ready = False
        self._mask_engine_params = None
        self._mask_message = "mask.preparing"
        self._mask_preview_params = None
        self.mask_overlay.set_range()
        if self._mask_range_runner is not None:
            self._mask_range_runner.cancel()
        self._sync_masks()
        self.mask_mode_changed.emit(self._mask_page)
        self.mask_ui_changed.emit()

    def _select_mask(self, identity):
        self.mask_overlay.cancel_interaction()
        if isinstance(identity, tuple):
            self._mask_selected, self._mask_component = identity
        else:
            self._mask_selected, self._mask_component = identity, None
        self._sync_masks()
        self._request_mask_range()
        self.mask_ui_changed.emit()

    def _select_component(self, identity):
        self.mask_overlay.cancel_interaction()
        self._mask_component = identity
        self._sync_masks()
        self.mask_ui_changed.emit()

    def _show_mask_range(self, show):
        self._mask_range = show
        self.mask_overlay.set_range()
        self._request_mask_range()
        self.mask_ui_changed.emit()

    def clear_mask_range(self):
        self.mask_overlay.set_range()
        if self._mask_range_runner is not None:
            self._mask_range_runner.cancel()

    def set_mask_context(self, token, reference_size, *, ready, message="", upgrade=False, engine_params=None):
        self._mask_context, self._mask_reference_size = token, reference_size
        self._mask_engine_params = copy.deepcopy(engine_params) if ready else None
        self._mask_ready, self._mask_message, self._mask_can_upgrade = ready, message, upgrade
        self._sync_masks()
        if ready:
            self._request_mask_range()
        else:
            self.mask_overlay.set_range()
            if self._mask_range_runner is not None:
                self._mask_range_runner.cancel()

    def set_mask_busy(self, busy):
        """正在识别的区域：(类型, 所属蒙版)；None 表示没有。"""
        self._mask_busy = busy
        self._sync_masks()

    def brush_settings(self):
        return dict(self._brush_settings)

    def set_brush_settings(self, settings):
        self._brush_settings = dict(settings)
        self.mask_overlay.set_brush_settings(self._brush_settings)

    def set_mask_preview(self, params, token=None):
        self._mask_preview_params = copy.deepcopy(params)
        if token is not None:
            self._mask_context = token
        self._sync_masks()
        self._request_mask_range()

    def _sync_masks(self):
        if not hasattr(self, "_mask_panel"):
            return
        displayed = self._mask_preview_params or self._params
        masks = displayed.get("masks") or []
        if not any(mask.get("id") == self._mask_selected for mask in masks):
            self._mask_selected = None
        current = next((mask for mask in masks if mask.get("id") == self._mask_selected), {})
        if not any(item["id"] == self._mask_component for item in current.get("components", ())):
            self._mask_component = None
        self._mask_panel.set_state(self._params, self._mask_selected, ready=self._mask_ready,
            message=self._tr(self._mask_message) if self._mask_message else "",
            upgrade=self._mask_can_upgrade, show_range=self._mask_range, component=self._mask_component,
            busy=self._mask_busy)
        self.mask_overlay.set_state(masks if displayed.get("process_version") in (3, 4) else [], self._mask_selected,
                                    self._mask_reference_size, enabled=self._mask_page and self._mask_ready,
                                    component=self._mask_component, composite=displayed.get("process_version") == 4)

    def _request_mask_range(self):
        if (not self._mask_page or not self._mask_ready or self._mask_selected is None
                or self._params.get("process_version") not in (3, 4) or self._mask_engine_params is None):
            self.mask_overlay.set_range()
            if self._mask_range_runner is not None:
                self._mask_range_runner.cancel()
            return
        if self._mask_range_runner is None:
            self._mask_range_runner = MaskRangeRunner(self._ensure_preview_runner(), self._image_source_service,
                self._mask_range_ready, self._processing_error, assets=self._ensure_render_service().runtime.mask_assets)
        token = self._mask_context, self._mask_selected, self._mask_range
        displayed = self._mask_preview_params or self._params
        # 全局取值来自上一帧实际渲染的节点；蒙版与它的处理版本取当前参数，中性照片第一次建蒙版时两者版本不同
        settings = {**self._mask_engine_params, "masks": displayed.get("masks"),
                    "process_version": displayed.get("process_version")}
        self._mask_range_runner.request(token, settings, self._mask_selected,
                                        self._mask_reference_size, self._mask_range)

    def _mask_range_ready(self, frame):
        if self._mask_page and self._mask_ready and frame.token == (self._mask_context, self._mask_selected, self._mask_range):
            self.mask_overlay.set_range(*frame.take())
            self._mask_panel.set_clipping(frame.clipped)

    def apply_mask_candidate(self, params, selected=None, *, component=None):
        self._mask_preview_params = None
        self._mask_selected = selected
        current = next((mask for mask in params.get("masks") or () if mask["id"] == selected), {})
        self._mask_component = component if any(item["id"] == component for item in current.get("components", ())) else None
        return self._apply_params(params)

    def mask_creation_spec(self, request):
        """新建一块区域要用的名字与所属蒙版；超出数量限制时说明原因并返回 None。

        “背景”是整张照片减去识别出的主体：识别的是主体，结果放进新蒙版里一块整张照片的后面。
        """
        request = {"kind": request, "op": "add", "parent": None} if isinstance(request, str) else request
        masks = self._params.get("masks") or []
        parent = next((mask for mask in masks if mask["id"] == request["parent"]), None)
        if parent is None and len(masks) >= MASK_LIMIT:
            self.request_status.emit(self._tr("error.mask.limit"))
            return None
        fill = request["kind"] == "background"
        continuing = request["kind"] == "brush" and any(
            part["id"] == request.get("target") and part["type"] == "brush"
            for part in (parent or {}).get("components", ()))
        added = 0 if continuing else (2 if fill else 1)
        if parent is not None and len(parent.get("components", ())) + added > COMPONENT_LIMIT:
            self.request_status.emit(self._tr("error.mask.component_limit"))
            return None
        if sum(len(mask.get("components", ())) for mask in masks) + added > NODE_COMPONENT_LIMIT:
            self.request_status.emit(self._tr("error.mask.component_limit"))
            return None
        kind, operation = ("subject", "subtract") if fill else (request["kind"], request["op"])
        points = sum(len(stroke["points"]) for mask in masks for part in mask.get("components", ())
                     if part["type"] == "brush" for stroke in brush_strokes(part))
        if kind == "brush" and points >= POINT_LIMIT:
            self.request_status.emit(self._tr("error.mask.point_limit"))
            return None
        if self._params.get("process_version") == 4:
            name = self._tr("mask.type.erase" if kind == "brush" and operation == "subtract" else "mask.type." + kind)
        else:
            name = self._tr("mask.default_name", type=self._tr("mask.type." + kind), number=len(masks) + 1)
        return {"kind": kind, "name": name, "parent": parent, "operation": operation, "fill": fill,
                "mask_name": self._tr("mask.default_mask_name", number=len(masks) + 1)[:80]}

    def begin_mask_creation(self, request):
        """开始新建一块区域：整张照片直接加上；画笔与渐变在画布上画。request 带 target 时接着涂那一块画笔。"""
        if not self._mask_ready or self._params.get("process_version") not in (3, 4):
            return
        spec = self.mask_creation_spec(request)
        if spec is None:
            return
        if spec["kind"] == "all":
            self.mask_overlay.cancel_interaction()
            self._add_components([{"id": str(uuid4()), "name": spec["name"], "enabled": True,
                                   "op": spec["operation"], "type": "all"}], spec)
            return
        self._mask_range = True
        self.mask_overlay.begin_creation(spec["kind"], spec["name"], parent=spec["parent"], operation=spec["operation"],
                                         brush=self._brush_settings, mask_name=spec["mask_name"],
                                         target=request.get("target") if isinstance(request, dict) else None)
        self._sync_masks()
        self._request_mask_range()

    def mask_model_token(self, kind):
        return self._mask_panel.model_token(kind)

    def apply_generated_component(self, component, spec, publication):
        components = [component]
        if spec["fill"]:
            components.insert(0, {"id": str(uuid4()), "name": self._tr("mask.type.all"), "enabled": True,
                                  "op": "add", "type": "all"})
        return self._add_components(components, spec, publication=publication)

    def _add_components(self, components, spec, *, publication=None):
        masks = copy.deepcopy(self._params.get("masks") or [])
        parent_id = spec["parent"]["id"] if spec["parent"] is not None else None
        parent = next((mask for mask in masks if mask["id"] == parent_id), None)
        if parent is None:
            parent = new_mask(spec["mask_name"])
            masks.append(parent)
        parent["components"].extend(components)
        self._mask_selected, self._mask_component = parent["id"], components[-1]["id"]
        self._mask_range = True
        applied = self._apply_params({**self._params, "masks": masks}, publication=publication)
        self.mask_ui_changed.emit()
        return applied

    def _edit_mask(self, operation, value):
        if not self._mask_ready or self._params.get("process_version") not in (3, 4):
            return
        if operation != "parameter":
            # 调整滑块时画笔仍在涂抹状态，涂几笔、调一调、再接着涂
            self.mask_overlay.cancel_interaction()
        masks = copy.deepcopy(self._params.get("masks") or [])
        identity = value[0] if operation == "enabled" else self._mask_selected
        current = next((mask for mask in masks if mask["id"] == identity), None)
        if current is None:
            return
        if operation != "parameter":
            self.end_edit_group()
        if operation == "parameter":
            key = local_engine_key(value[0])
            current.setdefault("params", {})[key] = value[1] / LOCAL_UI_SCALES[key]
        elif operation == "enabled":
            current["enabled"] = value[1]
        elif operation == "rename":
            current["name"] = value
        elif operation == "reset":
            current["params"] = {}
        elif operation == "delete":
            masks.remove(current)
            self._mask_selected, self._mask_component = None, None
        elif operation == "invert":
            current["invert"] = not current.get("invert", False)
        elif operation == "duplicate":
            if len(masks) >= MASK_LIMIT:
                return
            duplicate = copy_mask(current, self._tr("mask.copy_name", name=current["name"])[:80])
            if "components" in duplicate:
                for component in duplicate["components"]:
                    component["id"] = str(uuid4())
                try:
                    prepare_components([*masks, duplicate], active_only=False)
                except ValueError as error:
                    self.request_status.emit(self._tr(str(error)))
                    return
            masks.append(duplicate)
            self._mask_selected = duplicate["id"]
        elif operation.startswith("component_"):
            component_id = value[0] if isinstance(value, tuple) else value
            component = next((item for item in current.get("components", ()) if item["id"] == component_id), None)
            if component is None:
                return
            if operation == "component_continue":
                self.begin_mask_creation({"kind": "brush", "op": component["op"], "parent": current["id"],
                                          "target": component["id"]})
                return
            if operation == "component_delete":
                current["components"].remove(component)
                self._mask_component = None
            elif operation == "component_enabled":
                component["enabled"] = value[1]
            elif operation == "component_op":
                component["op"] = "subtract" if component["op"] == "add" else "add"
            elif operation == "component_invert":
                component["invert"] = not component.get("invert", False)
            elif operation in ("component_up", "component_down"):
                index = current["components"].index(component)
                target = index + (-1 if operation == "component_up" else 1)
                if 0 <= target < len(current["components"]):
                    current["components"][index], current["components"][target] = current["components"][target], component
        self._mask_preview_params = None
        self._apply_params({**self._params, "masks": masks})
        self.mask_ui_changed.emit()

    def handle_mask_key(self, key, *, canvas_focus=False):
        if not self._mask_page:
            return False
        if self.mask_overlay.handle_key_press(key):
            return True
        from PySide6.QtCore import Qt
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._mask_selected is not None:
            # 删除的是键盘焦点所在那一层的选中项；焦点在画布上时先删选中的区域，没有选中区域再删蒙版
            focus = self._mask_panel.focused_list()
            if focus is None and canvas_focus:
                focus = "area" if self._mask_component is not None else "mask"
            if focus == "area" and self._mask_component is not None:
                self._edit_mask("component_delete", self._mask_component)
                return True
            if focus == "mask":
                self._edit_mask("delete", None)
                return True
        return False

    def _parameter_row(self, spec: ParameterSpec) -> ParameterRow:
        row = ParameterRow(spec, self._tr(spec.label_key, spec.label_fallback), self)
        slider = row.slider
        self.track_edit_control(slider, started=slider.sliderPressed,
            finished=slider.sliderReleased, is_active=slider.isSliderDown)
        row.typing_finished.connect(self.end_edit_group)
        row.value_edited.connect(lambda value, key=spec.key: self._on_slider_changed(key, value))
        self._parameter_rows[spec.key] = row
        return row

    def _on_slider_changed(self, key: str, value: int) -> None:
        if not self._commit_params({**self._params, key: value}):
            return

        if hasattr(self, "_preset_combo") and self._preset_combo.currentIndex() != 0:
            self._preset_combo.setCurrentIndex(0)

        self.update_preview()

    def _on_curve_changed(self) -> None:
        if self._commit_params({**self._params, "curves": self._curves_widget.get_curves()}):
            self._group_modified = self._modified_groups()
            self.update_preview()

    def _on_mixer_changed(self) -> None:
        if self._commit_params({**self._params, "color_mixer": self._mixer_widget.get_values()}):
            self._group_modified = self._modified_groups()
            self.update_preview()

    def _modified_groups(self) -> dict[str, bool]:
        """默认收起的两个分组里现在有没有改动。"""
        curves = self._params.get("curves") or {}
        mixer = self._params.get("color_mixer") or {}
        return {
            "curves": any(points and [tuple(point) for point in points] != _IDENTITY_CURVE
                          for points in curves.values()),
            "color_mixer": any(values.get(key, 0) for values in mixer.values() for key in ("hue", "sat", "lum")),
        }

    def _reveal_modified_groups(self) -> None:
        """回填进来的参数让收起的分组有了改动时展开它，免得改动藏在看不见的地方；用户之后再收起不会被反复展开。"""
        modified = self._modified_groups()
        for group, now in modified.items():
            card = self._group_cards[group]
            if now and not self._group_modified.get(group) and not card.is_expanded():
                card.set_expanded(True)
        self._group_modified = modified

    def _on_eyedropper_toggled(self, active):
        self.sampling_requested.emit(active)

    def _on_eyedropper_picked(self, x: float, y: float) -> None:
        if self._sampling_state == "ready":
            self.sampling_picked.emit(x, y)

    def set_sampling_state(self, state):
        self._sampling_state = state
        self._mixer_widget.set_eyedropper_state(state)
        self._eyedropper_overlay.set_active(state == "ready")

    def set_sampling_available(self, available):
        self._mixer_widget.set_eyedropper_available(available)

    def select_sampling_channel(self, channel_id):
        self._mixer_widget.select_channel(channel_id)

    def cancel_sampling(self):
        if self._sampling_state != "closed":
            self.sampling_requested.emit(False)
            self.set_sampling_state("closed")

    def prepare_sampling(self, epoch):
        self._preview_timer.stop()
        self.cancel_preview()
        self.cancel_background()
        if self.source_bundle is None or self.parameters_pending:
            self.cancel_sampling()
            return
        service = self._ensure_render_service()
        identity, sources = self.source_bundle.identity, self._image_source_service
        params = self.tool_adapter.ui_to_workflow(self.parameters)
        def failed(error):
            self.cancel_sampling()
            self._processing_error(error)
        self.submit_background(None, lambda image, cancelled: service.render_sampled(
            identity, self.TOOL_ID, params, source_service=sources, epoch=epoch,
            is_cancelled=cancelled), self.sampled_preview_ready.emit, failed, cancellable=True)

    def update_preview(self):
        self.cancel_sampling()
        if not self.external_preview:
            self.set_sampling_available(self.source_bundle is not None and not self.parameters_pending)
        super().update_preview()

    def load_panel_source(self, path, on_loaded=None):
        self.cancel_sampling()
        if not self.external_preview:
            self.set_sampling_available(False)
        super().load_panel_source(path, on_loaded)

    def restore_params(self, params):
        self.cancel_sampling()
        self.mask_overlay.cancel_interaction()
        self.clear_mask_range()
        self._mask_ready = False
        self._mask_engine_params = None
        self._mask_message = "mask.preparing"
        reference = self._bound_tool_reference
        contract = color_ui_contract(params)
        if reference is not None:
            bound = color_ui_contract(params, reference.params_version)
            if bound.normalize(params, bound.version).success:
                contract = bound
        self.PARAMETER_CONTRACT = contract
        self._mask_preview_params = None
        super().restore_params(params)

    def _on_look_edited(self, look):
        self.end_edit_group()
        self._apply_params({**self._params, "look": look})

    def _on_exposure_upgrade(self):
        self.end_edit_group()
        self._apply_params({**self._params, "process_version": 2})

    def _sync_look_controls(self):
        self._look_controls.set_state(self._params["look"], self._params["process_version"],
                                      version=self.PARAMETER_CONTRACT.version)

    def _sync_ui_from_params(self) -> None:
        """根据 self._params 同步所有滑块和控件位置。"""
        self._sync_look_controls()
        for key, val in self._params.items():
            if key in self._parameter_rows:
                self._parameter_rows[key].set_value(int(val))

        if "curves" in self._params and self._params["curves"]:
            self._curves_widget.set_curves(self._params["curves"])
        else:
            self._curves_widget.set_curves({})

        if "color_mixer" in self._params and self._params["color_mixer"]:
            self._mixer_widget.set_values(self._params["color_mixer"])
        else:
            self._mixer_widget.reset_all()
        self._reveal_modified_groups()
        self._sync_masks()

    def _on_auto(self):
        self.end_edit_group()
        if self.source_bundle is None:
            return
        image = self.source_bundle.proxy
        source_format = self.source_bundle.render_source.source_format
        current = copy.deepcopy(self._params)
        version = self.PARAMETER_CONTRACT.version
        def analyze(snapshot):
            auto_params = ColorAdjuster(engine="pro").analyze_auto_params(snapshot)
            ui = preserve_color_identity(self._from_engine_params(auto_params), current)
            ui = adapt_auto_params_for_ui(ui, snapshot, source_format)
            ui["curves"] = None
            ui["masks"] = copy.deepcopy(current.get("masks"))
            ui["look"] = color_look_options(current["look"], current["process_version"], version=version)[1]
            return ui
        def ready(params):
            self.end_edit_group()
            self._apply_params(params)
        self.submit_background(image, analyze, ready, self._processing_error)

    def _on_bw(self) -> None:
        self.end_edit_group()
        self._apply_params({**self._params, "saturation_amt": -100, "vibrance_amt": -100})

    def _on_hdr(self) -> None:
        self.end_edit_group()
        self._apply_params({**self._params, "highlights": -40, "shadows": 50, "clarity_amt": 25, "dehaze_amt": 15})

    def _populate_presets(self) -> None:
        cur_data = self._preset_combo.currentData()
        self._preset_combo.clear()

        # 选项一：无 (None)
        none_label = self._tr("color_adjust.preset_none", "None")
        self._preset_combo.addItem(none_label, userData=None)

        # 滤镜预设
        for name in FilterPreset.preset_names():
            i18n_key = FilterPreset.PRESET_I18N_KEYS.get(name, "")
            fallback = FilterPreset.PRESET_LABELS.get(name, name)
            label = self._tr(i18n_key, fallback)
            self._preset_combo.addItem(label, userData=name)

        idx = self._preset_combo.findData(cur_data)
        if idx >= 0:
            self._preset_combo.setCurrentIndex(idx)
        else:
            self._preset_combo.setCurrentIndex(0)

    def _on_preset_changed(self, index: int) -> None:
        preset_name = self._preset_combo.itemData(index)
        if not preset_name:
            params = preserve_color_identity(self._default_params(), self._params)
        else:
            target = FilterPreset.get_params(preset_name)
            if not target:
                return
            params = preserve_color_identity({**self._params, **self._from_engine_params(target)}, self._params)
        params["masks"] = copy.deepcopy(self._params.get("masks"))
        self.end_edit_group()
        self._apply_params(params)



    # ---- 生命周期与抽象接口实现 ----
    def apply(self):
        try:
            return self.render_panel_image()
        except Exception as error:
            self._processing_error(error)
            return None

    def reset(self) -> None:
        """重置调整，观感与处理版本取会话基线。"""
        if hasattr(self, "_preset_combo"):
            self._preset_combo.setCurrentIndex(0)
        self.end_edit_group()
        self._apply_params(reset_color_params(self._reset_params, self._params), record_stash=False)

    def set_reset_identity(self, params):
        self._reset_params = preserve_color_identity(self._default_params(), params)

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        if not hasattr(self, "_mixer_widget"):
            return
        self._btn_auto.setText(self._tr("color_adjust.mode.auto", "Auto"))
        self._btn_auto.setToolTip(self._tr("color_adjust.mode.auto_tip", "Smart automatic color & exposure"))
        self._btn_bw.setText(self._tr("color_adjust.mode.bw", "B&W").replace("&", "&&"))
        self._btn_hdr.setText(self._tr("color_adjust.mode.hdr", "HDR"))
        self._preset_label.setText(self._tr("color_adjust.presets", "Presets"))
        self._populate_presets()
        # 观感一行自己也订阅语言，这里先让它换好文字，再把两行的名称对齐
        self._look_controls.retranslate_ui()
        width = max(self._look_controls.label_width_hint(), self._preset_label.sizeHint().width())
        self._look_controls.set_label_width(width)
        self._preset_label.setFixedWidth(width)
        for group, (title_key, title, _expanded) in _GROUPS.items():
            self._group_cards[group].set_title(self._tr(title_key, title))
        for channel in CURVE_CHANNELS:
            self._curve_channels.set_text(channel, self._tr(_CURVE_CHANNEL_KEYS[channel], channel.upper()))
        for row in self._parameter_rows.values():
            row.set_label(self._tr(row.spec.label_key, row.spec.label_fallback))
        self._mode.set_segment_text(0, self._tr("mask.global"))
        self._mode.set_segment_text(1, self._tr("mask.title"))
        self._mask_panel.retranslate_ui()
        self._sync_masks()
        self._request_mask_range()

    def on_image_path_changed(self, path: str) -> None:
        """切图响应：载入新图代理并恢复暂存。"""
        self._load_source(path)
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._restore_initial_params()
        self.update_preview()

    # ---- 撤销 / 重做 ----
    def _push_undo(self) -> None:
        if self.external_preview:
            return
        if len(self._undo_stack) > 30:
            self._undo_stack.pop(0)
        self._undo_stack.append(copy.deepcopy(self._params))
        self._redo_stack.clear()

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def undo(self) -> None:
        if not self._undo_stack:
            return
        self._redo_stack.append(copy.deepcopy(self._params))
        self._params = self._undo_stack.pop()
        self._sync_ui_from_params()
        self.notify_params_changed(self._params)
        self.update_preview()

    def redo(self) -> None:
        if not self._redo_stack:
            return
        self._undo_stack.append(copy.deepcopy(self._params))
        self._params = self._redo_stack.pop()
        self._sync_ui_from_params()
        self.notify_params_changed(self._params)
        self.update_preview()


    def _do_update_preview(self):
        self.preview_panel_image()

    def cleanup(self):
        self.mask_overlay.cancel_interaction()
        self.mask_overlay.set_range()
        self._mask_ready = False
        self._mask_engine_params = None
        self._mask_message = ""
        self._mask_can_upgrade = False
        self._mask_preview_params = None
        if self._mask_range_runner is not None:
            self._mask_range_runner.close()
            self._mask_range_runner = None
        self.cancel_sampling()
        self.set_sampling_available(False)
        self._look_controls.cleanup()
        super().cleanup()
