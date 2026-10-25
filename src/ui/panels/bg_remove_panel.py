"""Background removal panel for in-window sidebar editing."""

import logging
from typing import Optional
from PIL import Image

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QColorDialog

from resources.styles.theme_variables import SPACING_SM, px
from .base_tool_panel import BaseToolPanel
from .manual_preview import ManualPreviewState
from ...core.tools.effects import BG_PARAMETERS
from ..widgets.chip import ChipGroup
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.color_swatch import ColorSwatch
from ..widgets.parameter_row import ParameterRow, ParameterSpec
from ..widgets.action_footer import ActionFooter
from ..widgets.segmented_control import AppleSegmentedControl

logger = logging.getLogger(__name__)

_FIELDS = {field.name: field for field in BG_PARAMETERS.fields}
_MODES = ("ai", "color")
_BACKGROUNDS = ("transparent", "white", "black")


class LegacyBGRemovePanel(BaseToolPanel):
    """背景移除：选好方式、边缘与背景，再点底部的“渲染预览”。

    与画质增强一样是手动预览：改参数只记入本次编辑，不自动渲染；预览任务归宿主，面板发出
    ``preview_requested``、``cancel_requested``，任务的状态由宿主经 ``set_preview_state()`` 报告。
    刚进入、还没改过参数时点“渲染预览”，会先把当前参数记入本次编辑再请求渲染。
    """

    PARAMETER_CONTRACT = BG_PARAMETERS
    PARAMETER_LABELS = {"tolerance": ("bg_remove.tolerance", "int"), "edge_radius": ("bg_remove.edge_radius", "px")}
    TOOL_ID = "bg_remove"
    TOOL_TITLE_KEY = "menu.edit.remove_bg"
    TOOL_TITLE_DEFAULT = "Remove Background"
    MANUAL_PREVIEW = True
    preview_requested = pyqtSignal()
    cancel_requested = pyqtSignal()

    def __init__(
        self,
        image_path: str,
        parent: Optional[QWidget] = None,
        edit_state_store=None,
        **services,
    ):
        self._original_image: Optional[Image.Image] = None
        self._generation = 0
        self._displayed_generation = -1

        self._color_target = (255, 255, 255)
        self._preview = ManualPreviewState()
        # 点了“渲染预览”、参数发布之后要接着请求渲染
        self._preview_wanted = False

        super().__init__(image_path, parent=parent, edit_state_store=edit_state_store, **services)

        self._load_source(image_path)
        self._restore_initial_params()
        self.update_preview()

    @staticmethod
    def _default_params():
        return BG_PARAMETERS.defaults(BG_PARAMETERS.version)

    def _load_source(self, path):
        self.load_panel_source(path, None)

    def _restore_initial_params(self) -> None:
        defaults = self._default_params()
        if self._edit_state_store and self._image_path:
            self._params = self._edit_state_store.get_initial_params(
                self._image_path, self.TOOL_ID, defaults
            )
        else:
            self._params = dict(defaults)
        c = self._params.get("target_color", [255, 255, 255])
        self._color_target = tuple(c) if len(c) == 3 else (255, 255, 255)
        self._sync_ui_from_params()

    def build_ui(self) -> None:
        layout = self._root_layout

        # 方式：AI 抠图要用模型，颜色抠图要指定去掉哪种颜色
        self._mode_card = CollapsibleCard("", self)
        self._mode_seg = AppleSegmentedControl(self)
        for mode in _MODES:
            self._mode_seg.add_segment("", mode)
        self._mode_seg.activated.connect(self._on_mode_changed)
        self._mode_card.add_widget(self._mode_seg)

        # 带模型的面板把模型选择放在这里，只在 AI 抠图时显示
        self._ai_box = QWidget(self)
        ai_layout = QVBoxLayout(self._ai_box)
        ai_layout.setContentsMargins(0, 0, 0, 0)
        self._mode_card.add_widget(self._ai_box)

        self._color_box = QWidget(self)
        color_layout = QVBoxLayout(self._color_box)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(px(SPACING_SM))
        color_row = QHBoxLayout()
        color_row.setSpacing(px(SPACING_SM))
        self._color_label = QLabel(self._color_box)
        self._color_swatch = ColorSwatch(self._color_box)
        self._color_swatch.clicked.connect(self._pick_color)
        self._color_name = QLabel(self._color_box)
        self._color_name.setObjectName("mutedText")
        color_row.addWidget(self._color_label)
        color_row.addStretch()
        color_row.addWidget(self._color_name)
        color_row.addWidget(self._color_swatch)
        color_layout.addLayout(color_row)
        self._tolerance_row = self._parameter_row("tolerance")
        color_layout.addWidget(self._tolerance_row)
        self._mode_card.add_widget(self._color_box)
        layout.addWidget(self._mode_card)

        # 边缘羽化对两种方式都起作用
        self._edge_card = CollapsibleCard("", self)
        self._edge_row = self._parameter_row("edge_radius")
        self._edge_card.add_widget(self._edge_row)
        layout.addWidget(self._edge_card)

        self._bg_card = CollapsibleCard("", self)
        self._bg_chips = ChipGroup(len(_BACKGROUNDS), self)
        self._bg_chips.set_items([(key, "") for key in _BACKGROUNDS])
        self._bg_chips.current_changed.connect(self._on_bg_type_changed)
        self._bg_card.add_widget(self._bg_chips)
        layout.addWidget(self._bg_card)

        layout.addStretch()

        # 说明将要做什么，点了才渲染。宿主支持时固定在底部，不随上面的内容滚动
        self._footer = ActionFooter(self)
        self._footer.primary_clicked.connect(self._request_preview)
        self._footer.cancel_clicked.connect(self._cancel_processing)
        layout.addWidget(self._footer)
        self.retranslate_ui()

    def take_footer(self):
        self._root_layout.removeWidget(self._footer)
        return self._footer

    def _parameter_row(self, key: str) -> ParameterRow:
        field = _FIELDS[key]
        label_key, value_format = self.PARAMETER_LABELS[key]
        row = ParameterRow(ParameterSpec(key, label_key, key, int(field.minimum), int(field.maximum),
                                         int(field.default), value_format), "", self)
        slider = row.slider
        self.track_edit_control(slider, started=slider.sliderPressed, finished=slider.sliderReleased,
                                is_active=slider.isSliderDown)
        row.typing_finished.connect(self.end_edit_group)
        row.value_edited.connect(lambda value, name=key: self._on_value_edited(name, value))
        return row

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        if not hasattr(self, "_bg_chips"):
            return
        self._mode_card.set_title(self._tr("bg_remove.mode", "Method"))
        for index, mode in enumerate(_MODES):
            self._mode_seg.set_segment_text(index, self._tr("bg_remove.mode." + mode, mode))
        self._color_label.setText(self._tr("bg_remove.target_color", "Color to remove"))
        self._color_swatch.setAccessibleName(self._tr("bg_remove.pick_color", "Pick the color to remove"))
        self._color_swatch.setToolTip(self._tr("bg_remove.pick_color", "Pick the color to remove"))
        self._tolerance_row.set_label(self._tr("bg_remove.tolerance", "Tolerance"))
        self._edge_card.set_title(self._tr("bg_remove.edge", "Edge"))
        self._edge_row.set_label(self._tr("bg_remove.edge_radius", "Feather"))
        self._bg_card.set_title(self._tr("bg_remove.bg_replace", "Background"))
        for key in _BACKGROUNDS:
            self._bg_chips.set_text(key, self._tr("bg_remove.bg." + key, key))
        self._project_footer()

    def _project_color(self) -> None:
        self._color_swatch.set_color(self._color_target)
        self._color_name.setText("RGB {} {} {}".format(*self._color_target))

    def _project_mode(self, mode: str) -> None:
        """只显示当前方式用得到的那一组。"""
        self._ai_box.setVisible(mode == "ai" and self._ai_box.layout().count() > 0)
        self._color_box.setVisible(mode == "color")

    def _pick_color(self) -> None:
        initial = QColor(*self._color_target)
        c = QColorDialog.getColor(initial, self, self._tr("bg_remove.pick_color", "Pick the color to remove"))
        if c.isValid():
            color = [c.red(), c.green(), c.blue()]
            if self._edit({**self._params, "target_color": color}):
                self._color_target = tuple(color)
                self._project_color()

    def _on_mode_changed(self, index: int) -> None:
        mode = _MODES[index]
        if self._edit({**self._params, "mode": mode}):
            self._project_mode(mode)

    def _on_value_edited(self, key: str, value: int) -> None:
        self._edit({**self._params, key: value})

    def _on_bg_type_changed(self, key: str) -> None:
        self._edit({**self._params, "bg_type": key})

    def _edit(self, params) -> bool:
        """提交一次用户编辑。改了参数，正在跑的预览作废，等用户再点“渲染预览”。"""
        self._preview_wanted = False
        if not self._commit_params(params):
            return False
        self.cancel_preview()
        self.cancel_requested.emit()
        return True

    # ---- 手动预览 ----
    @property
    def preview_ticket(self):
        """当前这份参数与图片对应的预览凭据；宿主报告状态时带回，过期的报告不处理。"""
        return self._preview.ticket

    def set_preview_state(self, state, ticket=None):
        if self._preview.report(state, ticket):
            self._project_footer()

    def _blocked_reason(self) -> str:
        """现在渲染不了的原因；可以渲染时为空。"""
        return ""

    def _project_footer(self) -> None:
        if not hasattr(self, "_footer"):
            return
        self._footer.show_preview(steps=self._tr("bg_remove.mode." + self._params.get("mode", "ai")),
                                preview_state=self._preview.state, blocked=self._blocked_reason(),
                                ready=self.source_bundle is not None)

    def _request_preview(self) -> None:
        """点“渲染预览”：先把当前参数记入本次编辑（没改过参数时也记），发布之后再请求渲染。"""
        self._preview_wanted = True
        self.notify_params_changed(self._params)

    def _publish_params(self, params, record_stash):
        super()._publish_params(params, record_stash)
        if self._preview_wanted and self._params == params:
            self._preview_wanted = False
            if self.external_preview:
                # 任务由宿主开始，开始后由它报告 processing
                self.preview_requested.emit()
            else:
                self.set_preview_state("processing")
                self.preview_panel_image()

    def _cancel_processing(self) -> None:
        if self.external_preview:
            # 任务停下后由宿主报告 stopped，在此之前显示“正在取消”
            self.set_preview_state("cancelling")
            self.cancel_requested.emit()
        else:
            self.cancel_preview()
            self.set_preview_state("cancelled")

    def _emit_panel_preview(self, image):
        super()._emit_panel_preview(image)
        self.set_preview_state("updated")

    def _processing_error(self, error):
        super()._processing_error(error)
        if not self.external_preview:
            self.set_preview_state("failed")

    def notify_params_changed(self, params, record_stash=True):
        self._preview.invalidate()
        super().notify_params_changed(params, record_stash)
        self._project_footer()

    def restore_params(self, params):
        self._preview_wanted = False
        self._preview.invalidate()
        super().restore_params(params)

    def update_preview(self):
        # 手动预览：参数或图片变化只更新底部的说明，不自动渲染
        self._project_footer()

    def _sync_ui_from_params(self) -> None:
        self._color_target = tuple(self._params.get("target_color", (255, 255, 255)))
        mode = self._params.get("mode", "ai")
        self._mode_seg.setCurrentIndex(_MODES.index(mode))
        self._project_mode(mode)
        self._tolerance_row.set_value(int(self._params.get("tolerance", 30)))
        self._edge_row.set_value(int(self._params.get("edge_radius", 2)))
        self._project_color()
        self._bg_chips.set_current(self._params.get("bg_type", "transparent"))
        self._project_footer()

    def apply(self):
        try:
            return self.render_panel_image()
        except Exception as error:
            self._processing_error(error)
            return None

    def reset(self) -> None:
        self._apply_params(self._default_params())

    def on_image_path_changed(self, path: str) -> None:
        self._preview_wanted = False
        self._preview.invalidate()
        self._load_source(path)
        self._restore_initial_params()
        self.update_preview()

    def _do_update_preview(self):
        self.preview_panel_image()


def __getattr__(name):
    if name == "BGRemovePanel":
        from .managed_model_panels import BGRemovePanel
        return BGRemovePanel
    raise AttributeError(name)
