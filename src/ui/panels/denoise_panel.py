"""Image Denoise Panel for in-window sidebar editing."""

import logging
from typing import Optional
from PIL import Image

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QWidget, QLabel, QFormLayout

from .base_tool_panel import BaseToolPanel
from ...application.result_types import CoreOperationError
from ..widgets.apple_menu import AppleDropDown
from ..widgets.apple_button import ApplePrimaryButton
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.parameter_row import ParameterRow, ParameterSpec
from ...core.tools.denoise import DENOISE_PARAMETERS, DenoiseBackendUnavailable

logger = logging.getLogger(__name__)

_FIELDS = {field.name: field for field in DENOISE_PARAMETERS.fields}
# (参数名, 文案键, 默认文案)
_STRENGTHS = (
    ("strength", "denoise.strength_label", "Strength"),
    ("color_strength", "denoise.color_label", "Color Denoise"),
    ("sharpen", "denoise.sharpen_label", "Post Sharpen"),
)


class DenoisePanel(BaseToolPanel):
    """图片降噪单窗口侧栏面板。"""

    PARAMETER_CONTRACT = DENOISE_PARAMETERS
    PARAMETER_LABELS = {key: (label_key, "int") for key, label_key, _fallback in _STRENGTHS}
    TOOL_ID = "denoise"
    TOOL_TITLE_KEY = "menu.edit.denoise"
    TOOL_TITLE_DEFAULT = "Image Denoise"

    def __init__(
        self,
        image_path: str,
        parent: Optional[QWidget] = None,
        edit_state_store=None,
        **services,
    ):
        self._undo_stack: list[dict] = []
        self._redo_stack: list[dict] = []
        self._parameter_rows: dict[str, ParameterRow] = {}

        super().__init__(image_path, parent=parent, edit_state_store=edit_state_store, **services)

        self._restore_initial_params()
        self._load_source(image_path)

    @staticmethod
    def _default_params() -> dict:
        return DENOISE_PARAMETERS.defaults(2)

    def _load_source(self, path: str) -> None:
        if not path:
            return
        self.load_source_async(
            path,
            lambda bundle: self.update_preview(),
            self._on_source_error,
        )

    def _on_source_error(self, error: BaseException) -> None:
        logger.warning("Denoise source load failed: %s", error)
        self.request_status.emit(self._tr("denoise.source_load_failed", "Unable to load image"))

    def _on_processing_error(self, error: BaseException) -> None:
        logger.warning("Denoise processing failed: %s", error)
        if isinstance(error, CoreOperationError):
            key, fallback = error.error.message_key, self._tr("denoise.processing_failed")
        elif isinstance(error, FileNotFoundError):
            key, fallback = "denoise.model_missing", "AI denoise model is unavailable"
        elif isinstance(error, DenoiseBackendUnavailable):
            key, fallback = "denoise.backend_unavailable", "OpenCV and NumPy are required for denoise"
        else:
            key, fallback = "denoise.processing_failed", "Denoise failed"
        self.request_status.emit(self._tr(key, fallback))

    def _restore_initial_params(self) -> None:
        defaults = self._default_params()
        draft = self._draft_store.get_draft(self._image_path, self.TOOL_ID) if self._draft_store and self._image_path else None
        checked = DENOISE_PARAMETERS.normalize(draft if draft is not None else defaults, 2)
        self._params = checked.data if checked.success else defaults
        if not checked.success:
            request_path = self._image_path
            QTimer.singleShot(0, lambda: self.request_status.emit(
                self._tr("denoise.invalid_params", "Invalid denoise settings"))
                if self._image_path == request_path else None)
        self._sync_ui_from_params()

    def build_ui(self) -> None:
        layout = self._root_layout

        card_method = CollapsibleCard(self._tr("denoise.mode_group", "Denoise Algorithm"), self)
        form_method = QFormLayout()

        self._method_combo = AppleDropDown(self)
        self._method_combo.addItem(self._tr("denoise.method.bilateral", "Fast Bilateral"), "bilateral")
        self._method_combo.addItem(self._tr("denoise.method.gaussian", "Gaussian"), "gaussian")
        self._method_combo.addItem(self._tr("denoise.method.median", "Median"), "median")
        self._method_combo.addItem(self._tr("denoise.method.ai", "AI Neural Denoise"), "ai")
        self._method_combo.activated.connect(self._on_method_changed)
        form_method.addRow(QLabel(self._tr("denoise.method_label", "Method:"), self), self._method_combo)

        card_method.setContentLayout(form_method)
        layout.addWidget(card_method)

        card_params = CollapsibleCard(self._tr("denoise.params_group", "Parameters"), self)
        for key, label_key, fallback in _STRENGTHS:
            card_params.add_widget(self._parameter_row(key, label_key, fallback))
        layout.addWidget(card_params)

        layout.addStretch()

        self._run_btn = ApplePrimaryButton(self._tr("denoise.run", "Run Denoise"), self)
        self._run_btn.setObjectName("denoiseRunBtn")
        self._run_btn.setMinimumHeight(34)
        self._run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._run_btn.clicked.connect(self._on_run_clicked)
        layout.addWidget(self._run_btn)

    def _on_run_clicked(self) -> None:
        self.notify_params_changed(self._params)
        self.update_preview()
        self.request_status.emit(self._tr("denoise.run", "Run Denoise"))

    def _on_method_changed(self, index: int) -> None:
        if self._commit_params({**self._params, "method": self._method_combo.itemData(index)}):
            self.update_preview()

    def _parameter_row(self, key: str, label_key: str, fallback: str) -> ParameterRow:
        field = _FIELDS[key]
        row = ParameterRow(ParameterSpec(key, label_key, fallback, int(field.minimum), int(field.maximum),
                                         int(field.default)), self._tr(label_key, fallback), self)
        slider = row.slider
        self.track_edit_control(slider, started=slider.sliderPressed, finished=slider.sliderReleased,
                                is_active=slider.isSliderDown)
        row.typing_finished.connect(self.end_edit_group)
        row.value_edited.connect(lambda value, name=key: self._on_value_edited(name, value))
        self._parameter_rows[key] = row
        return row

    def _on_value_edited(self, key: str, value: int) -> None:
        if self._commit_params({**self._params, key: value}):
            self.update_preview()

    def _sync_ui_from_params(self) -> None:
        method = self._params.get("method", "bilateral")
        for i in range(self._method_combo.count()):
            if self._method_combo.itemData(i) == method:
                self._method_combo.setCurrentIndex(i)
                break

        for key, row in self._parameter_rows.items():
            row.set_value(int(self._params[key]))

    def _do_update_preview(self) -> None:
        self.preview_panel_image()

    def apply(self) -> Optional[Image.Image]:
        bundle = self.source_bundle
        if bundle is None or not self._image_source_service.matches_file(bundle.identity):
            self.request_status.emit(self._tr("denoise.source_unavailable", "The image is not ready yet. Try again shortly."))
            return None
        try:
            return self.render_panel_image()
        except Exception as error:
            logger.exception("DenoisePanel.apply failed")
            self._on_processing_error(error)
            return None

    def reset(self) -> None:
        self._apply_params(self._default_params())

    def on_image_path_changed(self, path: str) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._restore_initial_params()
        self._load_source(path)

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        if hasattr(self, "_method_combo"):
            curr_idx = self._method_combo.currentIndex()
            self._method_combo.clear()
            self._method_combo.addItem(self._tr("denoise.method.bilateral", "Fast Bilateral"), "bilateral")
            self._method_combo.addItem(self._tr("denoise.method.gaussian", "Gaussian"), "gaussian")
            self._method_combo.addItem(self._tr("denoise.method.median", "Median"), "median")
            self._method_combo.addItem(self._tr("denoise.method.ai", "AI Neural Denoise"), "ai")
            self._method_combo.setCurrentIndex(curr_idx)
        if hasattr(self, "_run_btn"):
            self._run_btn.setText(self._tr("denoise.run", "Run Denoise"))
        for row in self._parameter_rows.values():
            row.set_label(self._tr(row.spec.label_key, row.spec.label_fallback))

    def cleanup(self) -> None:
        super().cleanup()
        self._undo_stack.clear()
        self._redo_stack.clear()

    def restore_params(self, params: dict) -> None:
        checked = DENOISE_PARAMETERS.normalize(params, 2)
        if not checked.success:
            raise ValueError("无效降噪参数")
        super().restore_params(checked.data)

    def _push_undo(self) -> None:
        if len(self._undo_stack) > 30:
            self._undo_stack.pop(0)
        self._undo_stack.append(dict(self._params))
        self._redo_stack.clear()

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def undo(self) -> None:
        if not self._undo_stack:
            return
        self._redo_stack.append(dict(self._params))
        self._params = self._undo_stack.pop()
        self._sync_ui_from_params()
        self.notify_params_changed(self._params)
        self.update_preview()

    def redo(self) -> None:
        if not self._redo_stack:
            return
        self._undo_stack.append(dict(self._params))
        self._params = self._redo_stack.pop()
        self._sync_ui_from_params()
        self.notify_params_changed(self._params)
        self.update_preview()
