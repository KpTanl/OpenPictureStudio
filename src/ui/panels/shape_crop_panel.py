"""Shape crop panel for in-window sidebar editing."""

import logging
from typing import Optional
from PIL import Image

from PyQt6.QtWidgets import QWidget, QLabel

from .base_tool_panel import BaseToolPanel
from .canvas_result import ResultViewControl
from ...core.tools.geometry import SHAPE_PARAMETERS
from ..widgets.chip import ChipGroup
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.parameter_row import ParameterRow, ParameterSpec
from ..overlays.shape_crop_overlay import ShapeCropOverlay

logger = logging.getLogger(__name__)

_FIELDS = {field.name: field for field in SHAPE_PARAMETERS.fields}
_SHAPES = ("circle", "ellipse", "round_rect", "rect")


class ShapeCropPanel(BaseToolPanel):
    """几何形状裁剪侧栏参数面板。

    调整时画布上是这一步之前的画面与形状框。刚进入、还没动过任何控件时，这个工具还没有进入本次编辑；
    底部的“查看结果”先把当前参数记入，再请宿主显示裁切之后的画面，之后改形状、拖形状框都直接生效。
    """

    PARAMETER_CONTRACT = SHAPE_PARAMETERS
    PARAMETER_LABELS = {"corner_radius": ("shape_crop.corner_radius", "int")}
    TOOL_ID = "shape_crop"
    TOOL_TITLE_KEY = "menu.edit.shape_crop"
    TOOL_TITLE_DEFAULT = "Shape Crop"

    def __init__(
        self,
        image_path: str,
        parent: Optional[QWidget] = None,
        edit_state_store=None,
        **services,
    ):
        self._original_image: Optional[Image.Image] = None
        self.overlay = ShapeCropOverlay()
        self.overlay.geometry_changed.connect(self._on_geometry_changed)

        super().__init__(image_path, parent=parent, edit_state_store=edit_state_store, **services)
        # 一次拖动记一步撤销
        self.track_edit_control(self.overlay, started=self.overlay.drag_started,
                                finished=self.overlay.drag_finished, is_active=self.overlay.is_dragging)

        self._load_source(image_path)

    @staticmethod
    def _default_params():
        return SHAPE_PARAMETERS.defaults(SHAPE_PARAMETERS.version)

    def _on_geometry_changed(self) -> None:
        self._commit_params({
            "shape": self.overlay.shape_type,
            "corner_radius": self.overlay.corner_radius,
            "rect": list(self.overlay.norm_rect),
        })

    def _sync_ui_from_params(self) -> None:
        self.overlay.norm_rect = list(self._params.get("rect", [0.15, 0.15, 0.7, 0.7]))
        self.overlay.shape_type = self._params.get("shape", "circle")
        self.overlay.corner_radius = self._params.get("corner_radius", 20)
        self._shape_chips.set_current(self.overlay.shape_type)
        self._radius_row.set_value(int(self.overlay.corner_radius))
        # 圆角只对圆角矩形有意义
        self._radius_row.setVisible(self.overlay.shape_type == "round_rect")
        self.overlay.update_view()
        self._project_result()

    def _load_source(self, path):
        self.load_panel_source(path, self._sync_ui_from_params)

    def build_ui(self) -> None:
        layout = self._root_layout

        self._shape_card = CollapsibleCard("", self)
        self._shape_chips = ChipGroup(2, self)
        self._shape_chips.set_items([(shape, "") for shape in _SHAPES])
        self._shape_chips.current_changed.connect(self._on_shape_changed)
        self._shape_card.add_widget(self._shape_chips)

        radius = _FIELDS["corner_radius"]
        self._radius_row = ParameterRow(ParameterSpec(
            "corner_radius", self.PARAMETER_LABELS["corner_radius"][0], "Corner radius",
            int(radius.minimum), int(radius.maximum), int(radius.default)), "", self)
        slider = self._radius_row.slider
        self.track_edit_control(slider, started=slider.sliderPressed, finished=slider.sliderReleased,
                                is_active=slider.isSliderDown)
        self._radius_row.typing_finished.connect(self.end_edit_group)
        self._radius_row.value_edited.connect(self._on_radius_changed)
        self._shape_card.add_widget(self._radius_row)
        layout.addWidget(self._shape_card)

        self._tip_label = QLabel(self)
        self._tip_label.setWordWrap(True)
        self._tip_label.setObjectName("mutedText")
        layout.addWidget(self._tip_label)

        layout.addStretch()

        # 宿主支持时固定在底部：查看裁切之后的画面，或回到拖框
        self.result_view = ResultViewControl(self, prepare=self._use_shape)
        self.result_view.footer.setVisible(self.external_preview)
        layout.addWidget(self.result_view.footer)
        self.retranslate_ui()

    def take_footer(self):
        self._root_layout.removeWidget(self.result_view.footer)
        return self.result_view.footer

    def _project_result(self) -> None:
        if hasattr(self, "result_view"):
            shape = self._tr("shape_crop.shape." + self._params.get("shape", "circle"))
            self.result_view.describe(self._tr("shape_crop.result.summary", shape=shape))

    def _use_shape(self) -> None:
        """看结果之前把当前参数记入本次编辑，没改过参数时也记。"""
        self.notify_params_changed(self._params)

    def _publish_params(self, params, record_stash):
        super()._publish_params(params, record_stash)
        self._project_result()

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        if not hasattr(self, "_tip_label"):
            return
        self._shape_card.set_title(self._tr("shape_crop.settings", "Shape"))
        for shape in _SHAPES:
            self._shape_chips.set_text(shape, self._tr("shape_crop.shape." + shape, shape))
        self._radius_row.set_label(self._tr("shape_crop.corner_radius", "Corner radius"))
        self._tip_label.setText(self._tr(
            "shape_crop.tip", "Drag the handles to resize; drag inside to move. Everything outside the shape becomes transparent."))
        self._project_result()

    def _on_shape_changed(self, shape: str) -> None:
        self._apply_params({**self._params, "shape": shape})

    def _on_radius_changed(self, value: int) -> None:
        self._apply_params({**self._params, "corner_radius": float(value)})

    def apply(self):
        try:
            return self.render_panel_image()
        except Exception as error:
            self._processing_error(error)
            return None

    def reset(self):
        self._apply_params(self._default_params())

    def on_image_path_changed(self, path: str) -> None:
        self._load_source(path)

    def _do_update_preview(self):
        self.preview_panel_image()
