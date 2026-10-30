"""Lasso cutout panel for in-window sidebar editing."""

import copy
import logging
from typing import Optional
from PIL import Image

from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel

from .base_tool_panel import BaseToolPanel
from .canvas_result import ResultViewControl
from ...core.tools.geometry import LASSO_PARAMETERS
from ..widgets.apple_button import AppleButton
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.parameter_row import ParameterRow, ParameterSpec
from ..widgets.segmented_control import AppleSegmentedControl
from ..overlays.lasso_overlay import LassoOverlay

logger = logging.getLogger(__name__)

_FIELDS = {field.name: field for field in LASSO_PARAMETERS.fields}


class LassoPanel(BaseToolPanel):
    """套索抠图侧栏参数面板：上面是选区的现状与清空，下面是保留哪一侧与边缘羽化。

    调整时画布上是这一步之前的画面与轮廓；底部的“查看结果”先闭合正在画的轮廓，
    再请宿主显示抠完之后的画面，这时拖羽化、换保留哪一侧都能直接看到。
    """

    PARAMETER_CONTRACT = LASSO_PARAMETERS
    PARAMETER_LABELS = {"feather": ("lasso.feather", "px")}
    TOOL_ID = "lasso"
    TOOL_TITLE_KEY = "menu.edit.lasso"
    TOOL_TITLE_DEFAULT = "Lasso Cutout"

    def __init__(
        self,
        image_path: str,
        parent: Optional[QWidget] = None,
        edit_state_store=None,
        **services,
    ):
        self._original_image: Optional[Image.Image] = None
        self.overlay = LassoOverlay()
        self.overlay.geometry_changed.connect(self._on_geometry_changed)

        super().__init__(image_path, parent=parent, edit_state_store=edit_state_store, **services)

        self._load_source(image_path)

    @staticmethod
    def _default_params():
        return LASSO_PARAMETERS.defaults(LASSO_PARAMETERS.version)

    def _on_geometry_changed(self) -> None:
        self._commit_params({
            **self._params,
            "regions": copy.deepcopy(self.overlay.regions),
            "draft_points": copy.deepcopy(self.overlay.draft_points),
        })
        self._project_selection()

    def _sync_ui_from_params(self) -> None:
        self.overlay.draft_points = copy.deepcopy(self._params.get("draft_points", []))
        self.overlay.regions = copy.deepcopy(self._params.get("regions", []))
        self._feather_row.set_value(int(self._params.get("feather", 0)))
        self._keep_segments.setCurrentIndex(0 if self._params.get("keep_inside", True) else 1)
        self._project_selection()
        self.overlay.update_view()

    def _load_source(self, path):
        self.load_panel_source(path, self._sync_ui_from_params)

    def build_ui(self) -> None:
        layout = self._root_layout

        # 选区：已经圈了什么、怎么圈、清空
        self._selection_card = CollapsibleCard("", self)
        self._state_label = QLabel(self)
        self._state_label.setWordWrap(True)
        self._selection_card.add_widget(self._state_label)
        self._tip_label = QLabel(self)
        self._tip_label.setWordWrap(True)
        self._tip_label.setObjectName("mutedText")
        self._selection_card.add_widget(self._tip_label)
        clear_row = QHBoxLayout()
        self._clear_button = AppleButton(parent=self)
        self._clear_button.clicked.connect(self.overlay.clear)
        clear_row.addWidget(self._clear_button)
        clear_row.addStretch()
        self._selection_card.add_layout(clear_row)
        layout.addWidget(self._selection_card)

        # 结果：保留哪一侧、边缘羽化
        self._result_card = CollapsibleCard("", self)
        self._keep_segments = AppleSegmentedControl(self)
        self._keep_segments.add_segments(["", ""])
        self._keep_segments.activated.connect(self._on_keep_activated)
        self._result_card.add_widget(self._keep_segments)

        feather = _FIELDS["feather"]
        self._feather_row = ParameterRow(ParameterSpec(
            "feather", self.PARAMETER_LABELS["feather"][0], "Feather",
            int(feather.minimum), int(feather.maximum), int(feather.default),
            self.PARAMETER_LABELS["feather"][1]), "", self)
        slider = self._feather_row.slider
        self.track_edit_control(slider, started=slider.sliderPressed, finished=slider.sliderReleased,
                                is_active=slider.isSliderDown)
        self._feather_row.typing_finished.connect(self.end_edit_group)
        self._feather_row.value_edited.connect(self._on_feather_changed)
        self._result_card.add_widget(self._feather_row)
        layout.addWidget(self._result_card)

        layout.addStretch()

        # 宿主支持时固定在底部：查看抠完之后的画面，或回到圈选
        self.result_view = ResultViewControl(self, prepare=self.overlay.close_selection)
        self.result_view.footer.setVisible(self.external_preview)
        layout.addWidget(self.result_view.footer)
        self.retranslate_ui()

    def take_footer(self):
        self._root_layout.removeWidget(self.result_view.footer)
        return self.result_view.footer

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        if not hasattr(self, "_feather_row"):
            return
        self._selection_card.set_title(self._tr("lasso.group.selection", "Selection"))
        self._tip_label.setText(self._tr(
            "lasso.tip", "Click on the photo to place points; right-click or click the first point to close the outline. "
                         "You can draw several regions."))
        self._clear_button.setText(self._tr("lasso.clear_selection", "Clear selection"))
        self._result_card.set_title(self._tr("lasso.group.result", "Result"))
        self._keep_segments.set_segment_text(0, self._tr("lasso.keep.inside", "Keep inside"))
        self._keep_segments.set_segment_text(1, self._tr("lasso.keep.outside", "Remove inside"))
        self._feather_row.set_label(self._tr("lasso.feather", "Feather"))
        self._project_selection()

    def _project_selection(self) -> None:
        """写出已经圈了几个区域、正在画的轮廓有几个点；没有可清空的内容时清空不可用。"""
        regions, points = len(self.overlay.regions), len(self.overlay.draft_points)
        if regions and points:
            text = self._tr("lasso.state.both", "{count} region(s) selected; the outline in progress has {points} point(s)",
                            count=regions, points=points)
        elif regions:
            text = self._tr("lasso.state.regions", "{count} region(s) selected", count=regions)
        elif points:
            text = self._tr("lasso.state.drafting", "The outline in progress has {points} point(s)", points=points)
        else:
            text = self._tr("lasso.state.none", "Nothing selected yet")
        self._state_label.setText(text)
        self._clear_button.setEnabled(bool(regions or points))
        if not hasattr(self, "result_view"):
            return
        if regions:
            side = self._tr("lasso.keep.inside" if self._params.get("keep_inside", True) else "lasso.keep.outside")
            self.result_view.describe(self._tr("lasso.result.summary", count=regions, side=side))
        elif points >= 3:
            self.result_view.describe(self._tr("lasso.result.unclosed"))
        else:
            self.result_view.describe(text, self._tr("lasso.result.reason"))

    def _on_feather_changed(self, value: int) -> None:
        self._commit_params({**self._params, "feather": value})

    def _on_keep_activated(self, index: int) -> None:
        self._commit_params({**self._params, "keep_inside": index == 0})
        self._project_selection()

    def apply(self):
        try:
            return self.render_panel_image()
        except Exception as error:
            self._processing_error(error)
            return None

    def reset(self):
        self._apply_params(self._default_params())

    def on_image_path_changed(self, path: str) -> None:
        self._params = {**self._params, "regions": [], "draft_points": []}
        self._sync_ui_from_params()
        self._load_source(path)

    def _do_update_preview(self):
        self.preview_panel_image()

    def cleanup(self):
        super().cleanup()
