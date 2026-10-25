"""AI Enhance Panel for in-window sidebar editing."""

import logging
from typing import Optional
from PIL import Image

from PyQt6.QtWidgets import QWidget, QLabel, QFormLayout

from .base_tool_panel import BaseToolPanel
from ...core.tools.effects import AI_PARAMETERS
from ..widgets.apple_menu import AppleDropDown
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.parameter_row import ParameterRow, ParameterSpec

logger = logging.getLogger(__name__)

_STRENGTH = next(field for field in AI_PARAMETERS.fields if field.name == "strength")




class LegacyAIEnhancePanel(BaseToolPanel):
    """AI 修图与画质增强侧栏面板。"""

    PARAMETER_CONTRACT = AI_PARAMETERS
    PARAMETER_LABELS = {"strength": ("ai_enhance.strength_label", "int")}
    TOOL_ID = "ai_enhance"
    TOOL_TITLE_KEY = "menu.edit.ai_enhance"
    TOOL_TITLE_DEFAULT = "AI Enhance"

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

        super().__init__(image_path, parent=parent, edit_state_store=edit_state_store, **services)

        self._load_source(image_path)
        self._restore_initial_params()
        self.update_preview()

    @staticmethod
    def _default_params():
        return AI_PARAMETERS.defaults(AI_PARAMETERS.version)

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
        self._sync_ui_from_params()

    def build_ui(self) -> None:
        layout = self._root_layout

        # 卡片 1: 模式与放大倍率
        card_mode = CollapsibleCard(self._tr("ai_enhance.mode_group", "Enhance Mode"), self)
        form_mode = QFormLayout()

        self._mode_combo = AppleDropDown(self)
        self._mode_combo.addItem(self._tr("ai_enhance.mode_auto", "Auto Smart"), "auto")
        self._mode_combo.addItem(self._tr("ai_enhance.mode_sharpen", "Clarity & Sharpen"), "sharpen")
        self._mode_combo.addItem(self._tr("ai_enhance.mode_deblur", "AI Deblur"), "deblur")
        self._mode_combo.addItem(self._tr("ai_enhance.mode_classic", "Classic Upscale"), "classic")
        self._mode_combo.addItem(self._tr("ai_enhance.mode_night", "Night"), "night")
        self._mode_combo.activated.connect(self._on_mode_changed)
        form_mode.addRow(QLabel(self._tr("ai_enhance.mode_label", "Mode:"), self), self._mode_combo)

        self._upscale_combo = AppleDropDown(self)
        self._upscale_combo.addItem("1x", "1x")
        self._upscale_combo.addItem("2x", "2x")
        self._upscale_combo.addItem("4x", "4x")
        self._upscale_combo.activated.connect(self._on_upscale_changed)
        form_mode.addRow(QLabel(self._tr("ai_enhance.upscale_label", "Upscale:"), self), self._upscale_combo)

        card_mode.setContentLayout(form_mode)
        layout.addWidget(card_mode)

        # 卡片 2: 强度调节
        card_param = CollapsibleCard(self._tr("ai_enhance.params_group", "Parameters"), self)
        self._strength_row = ParameterRow(ParameterSpec(
            "strength", self.PARAMETER_LABELS["strength"][0], "Strength", int(_STRENGTH.minimum),
            int(_STRENGTH.maximum), int(_STRENGTH.default)), self._tr("ai_enhance.strength_label", "Strength"), self)
        slider = self._strength_row.slider
        self.track_edit_control(slider, started=slider.sliderPressed, finished=slider.sliderReleased,
                                is_active=slider.isSliderDown)
        self._strength_row.typing_finished.connect(self.end_edit_group)
        self._strength_row.value_edited.connect(self._on_strength_changed)
        card_param.add_widget(self._strength_row)
        layout.addWidget(card_param)

        layout.addStretch()

    def _on_mode_changed(self, index: int) -> None:
        if self._commit_params({**self._params, "mode": self._mode_combo.itemData(index)}):
            self.update_preview()

    def _on_upscale_changed(self, index: int) -> None:
        if self._commit_params({**self._params, "upscale": self._upscale_combo.itemData(index)}):
            self.update_preview()

    def _on_strength_changed(self, value: int) -> None:
        if self._commit_params({**self._params, "strength": value}):
            self.update_preview()

    def _sync_ui_from_params(self) -> None:
        mode = self._params.get("mode", "auto")
        for i in range(self._mode_combo.count()):
            if self._mode_combo.itemData(i) == mode:
                self._mode_combo.setCurrentIndex(i)
                break

        upscale = self._params.get("upscale", "1x")
        for i in range(self._upscale_combo.count()):
            if self._upscale_combo.itemData(i) == upscale:
                self._upscale_combo.setCurrentIndex(i)
                break

        self._strength_row.set_value(int(self._params.get("strength", _STRENGTH.default)))




    def apply(self):
        try:
            return self.render_panel_image()
        except Exception as error:
            self._processing_error(error)
            return None

    def reset(self) -> None:
        self._apply_params(self._default_params())

    def on_image_path_changed(self, path: str) -> None:
        self._load_source(path)
        self._restore_initial_params()
        self.update_preview()




    def _do_update_preview(self):
        self.preview_panel_image()

    def cleanup(self):
        super().cleanup()


def __getattr__(name):
    if name == "AIEnhancePanel":
        from .neural_enhance_panel import AIEnhancePanel
        return AIEnhancePanel
    raise AttributeError(name)
