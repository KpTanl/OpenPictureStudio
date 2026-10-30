"""Beauty enhancement panel for in-window sidebar editing."""

import logging
from typing import Optional
from PIL import Image

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QVBoxLayout

from resources.styles.theme_variables import SPACING_SM, px
from .base_tool_panel import BaseToolPanel
from ...application.result_types import CoreOperationError
from ..widgets.apple_button import AppleButton
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.parameter_row import ParameterRow, ParameterSpec
from ..widgets.status_badge import StatusBadge
from ...core.tools.beauty import (
    BEAUTY_PARAMETERS,
)

logger = logging.getLogger(__name__)

# 分组 -> (标题的文案键, 默认文案, 组内各参数的 (参数名, 文案键, 默认文案))
_BEAUTY_GROUPS = (
    ("skin", "beauty.skin_group", "Skin", (
        ("skin_smooth", "beauty.skin.smooth", "Skin Smooth"),
        ("skin_brighten", "beauty.skin.brighten", "Skin Brighten"),
        ("skin_rosy", "beauty.skin.rosy", "Rosy"),
        ("sharpen", "beauty.skin.sharpen", "Sharpen"))),
    ("eye", "beauty.eye_group", "Eye", (
        ("eye_enlarge", "beauty.eye.eye_enlarge", "Eye Enlarge"),
        ("eye_distance", "beauty.eye.eye_distance", "Eye Distance"))),
    ("shape", "beauty.shape_group", "Face Shape", (
        ("face_slim", "beauty.shape.face_slim", "Face Slim"),
        ("nose_slim", "beauty.shape.nose_slim", "Nose Slim"))),
    ("detail", "beauty.detail_group", "Detail Retouch", (
        ("teeth_whiten", "beauty.detail.teeth_whiten", "Teeth Whiten"),
        ("spot_remove", "beauty.detail.spot_remove", "Spot Remove"),
        ("eye_bag_remove", "beauty.detail.eye_bag_remove", "Eye Bag Remove"))),
)

_BEAUTY_FIELDS = {field.name: field for field in BEAUTY_PARAMETERS.fields}


class BeautyPanelBase(BaseToolPanel):
    """人像美颜单窗口侧栏面板。"""

    PARAMETER_CONTRACT = BEAUTY_PARAMETERS
    PARAMETER_LABELS = {key: (label_key, "int_signed" if _BEAUTY_FIELDS[key].minimum < 0 else "int")
                        for _group, _title_key, _title, items in _BEAUTY_GROUPS for key, label_key, _fallback in items}
    TOOL_ID = "beauty"
    TOOL_TITLE_KEY = "menu.edit.beauty"
    TOOL_TITLE_DEFAULT = "Beauty"

    def __init__(
        self,
        image_path: str,
        parent: Optional[QWidget] = None,
        edit_state_store=None,
        **services,
    ):
        self._group_cards: dict[str, CollapsibleCard] = {}
        self._parameter_rows: dict[str, ParameterRow] = {}
        self._undo_stack: list[dict] = []
        self._redo_stack: list[dict] = []
        self._auto_request_id = 0
        self.beauty_analysis_provider = None
        self._auto_guard = lambda: True
        self._auto_publication = None

        super().__init__(image_path, parent=parent, edit_state_store=edit_state_store, **services)

        self._restore_initial_params()
        self._load_source(image_path)

    def _load_source(self, path: str) -> None:
        if not path:
            return
        self.load_source_async(
            path,
            lambda bundle: self.update_preview(),
            self._on_source_error,
        )

    def _on_source_error(self, error: BaseException) -> None:
        logger.error("BeautyPanel: source load failed: %s", error)
        self.request_status.emit(self._tr("error.image.load", "图片加载失败"))

    def restore_params(self, params: dict) -> None:
        self._cancel_auto()
        super().restore_params(params)

    def _cancel_auto(self) -> None:
        self._auto_request_id += 1
        self.cancel_background()

    def cancel_beauty_analysis(self):
        self._cancel_auto()

    def build_ui(self) -> None:
        layout = self._root_layout

        actions = QHBoxLayout()
        actions.setSpacing(px(SPACING_SM))
        self._btn_auto_beauty = AppleButton("", self)
        self._btn_auto_beauty.clicked.connect(self._on_auto_beauty)
        actions.addWidget(self._btn_auto_beauty, 1)
        self._btn_preset = AppleButton("", self)
        self._btn_preset.clicked.connect(self._on_recommended_preset)
        actions.addWidget(self._btn_preset, 1)
        layout.addLayout(actions)

        # 人脸检测现在用的是什么；有参数因此用不了时，下面一行说明原因
        self._engine_badge = StatusBadge("pending", "", self)
        layout.addWidget(self._engine_badge, 0, Qt.AlignmentFlag.AlignLeft)
        self._engine_hint = QLabel(self)
        self._engine_hint.setObjectName("mutedText")
        self._engine_hint.setWordWrap(True)
        layout.addWidget(self._engine_hint)

        for group, _title_key, _title, items in _BEAUTY_GROUPS:
            card = CollapsibleCard("", self)
            self._group_cards[group] = card
            # 参数行自带上边距，行与行之间不再另加间距，与调色面板一致
            rows = QVBoxLayout()
            rows.setSpacing(0)
            for key, label_key, fallback in items:
                rows.addWidget(self._parameter_row(key, label_key, fallback))
            card.add_layout(rows)
            layout.addWidget(card)

        layout.addStretch()
        self.retranslate_ui()

    def _parameter_row(self, key: str, label_key: str, fallback: str) -> ParameterRow:
        field = _BEAUTY_FIELDS[key]
        row = ParameterRow(ParameterSpec(key, label_key, fallback, int(field.minimum), int(field.maximum),
                                         int(field.default), self.PARAMETER_LABELS[key][1]),
                           "", self)
        slider = row.slider
        self.track_edit_control(slider, started=slider.sliderPressed, finished=slider.sliderReleased,
                                is_active=slider.isSliderDown)
        row.typing_finished.connect(self.end_edit_group)
        row.value_edited.connect(lambda value, name=key: self._on_slider_changed(name, value))
        self._parameter_rows[key] = row
        return row

    def _project_engine(self) -> None:
        """把人脸检测的状态投影到标记、说明与各参数行：用不了的参数置灰，全都用不了时两个快捷操作也不可用。"""
        kind, text, hint, blocked = self._engine_state()
        self._engine_badge.set_status(kind, text)
        self._engine_badge.setVisible(bool(text))
        self._engine_hint.setText(hint)
        self._engine_hint.setVisible(bool(hint))
        for key, row in self._parameter_rows.items():
            row.setEnabled(key not in blocked)
        usable = len(blocked) < len(self._parameter_rows)
        self._btn_auto_beauty.setEnabled(usable)
        self._btn_preset.setEnabled(usable)

    def _on_auto_error(self, error: BaseException) -> None:
        if isinstance(error, CoreOperationError) and error.error.code == "CANCELLED":
            return
        logger.error("BeautyPanel: automatic analysis failed: %s", error)
        self._show_processing_error(error, "beauty.auto.failed")

    def _show_processing_error(self, error: BaseException, fallback_key: str) -> None:
        key = error.error.message_key if isinstance(error, CoreOperationError) else fallback_key
        self.request_status.emit(self._tr(key, self._tr(fallback_key)))

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        if not hasattr(self, "_engine_badge"):
            return
        self._btn_auto_beauty.setText(self._tr("beauty.action.auto", "Auto Beauty"))
        self._btn_auto_beauty.setToolTip(self._tr("beauty.action.auto_tip"))
        self._btn_preset.setText(self._tr("beauty.action.preset", "Recommended Preset"))
        for group, title_key, title, _items in _BEAUTY_GROUPS:
            self._group_cards[group].set_title(self._tr(title_key, title))
        for row in self._parameter_rows.values():
            row.set_label(self._tr(row.spec.label_key, row.spec.label_fallback))
        self._project_engine()

    def _on_slider_changed(self, key: str, value: int) -> None:
        self._cancel_auto()
        if self._commit_params({**self._params, key: value}):
            self.update_preview()

    def _sync_ui_from_params(self) -> None:
        for key, row in self._parameter_rows.items():
            row.set_value(int(self._params[key]))

    def _do_update_preview(self) -> None:
        self.preview_panel_image()

    def apply(self) -> Optional[Image.Image]:
        bundle = self.source_bundle
        if bundle is None or not self._image_source_service.matches_file(bundle.identity):
            self.request_status.emit(self._tr("beauty.source_unavailable", "图片尚未就绪，请稍后重试"))
            return None
        try:
            return self.render_panel_image()
        except Exception as error:
            logger.exception("BeautyPanel.apply failed")
            self._show_processing_error(error, "beauty.apply.failed")
            return None

    def reset(self) -> None:
        self._cancel_auto()
        self._apply_params(self._default_params())

    def on_image_path_changed(self, path: str) -> None:
        self._auto_request_id += 1
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._restore_initial_params()
        self._load_source(path)

    def cleanup(self) -> None:
        self._auto_request_id += 1
        super().cleanup()
        self._undo_stack.clear()
        self._redo_stack.clear()

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
        self._cancel_auto()
        self._redo_stack.append(dict(self._params))
        self._params = self._undo_stack.pop()
        self._sync_ui_from_params()
        self.notify_params_changed(self._params)
        self.update_preview()

    def redo(self) -> None:
        if not self._redo_stack:
            return
        self._cancel_auto()
        self._undo_stack.append(dict(self._params))
        self._params = self._redo_stack.pop()
        self._sync_ui_from_params()
        self.notify_params_changed(self._params)
        self.update_preview()


def __getattr__(name):
    if name == "BeautyPanel":
        from .managed_model_panels import BeautyPanel
        return BeautyPanel
    raise AttributeError(name)
