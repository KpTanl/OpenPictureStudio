"""Crop Panel for in-window sidebar editing."""

import logging
from typing import Optional
from PIL import Image

from PySide6.QtWidgets import QWidget, QLabel, QFormLayout

from .base_tool_panel import BaseToolPanel
from .canvas_result import ResultViewControl
from ...core.tools.geometry import CROP_PARAMETERS
from ..widgets.chip import ChipGroup
from ..widgets.collapsible_card import CollapsibleCard
from ..overlays.crop_overlay import CropOverlay

logger = logging.getLogger(__name__)

# 标签的键与宽高比；“自由”没有约束
_FREE = "free"
_ASPECT_RATIOS = (
    (_FREE, None),
    ("1:1", 1.0),
    ("4:3", 4.0 / 3.0),
    ("3:2", 3.0 / 2.0),
    ("16:9", 16.0 / 9.0),
    ("2:3", 2.0 / 3.0),
    ("9:16", 9.0 / 16.0),
)


def _ratio_key(ratio) -> str:
    """参数里的宽高比对应哪个标签；不在预设里时返回空字符串。"""
    if ratio is None:
        return _FREE
    return next((key for key, value in _ASPECT_RATIOS if value is not None and abs(value - ratio) < 1e-6), "")


class CropPanel(BaseToolPanel):
    """矩形裁剪侧栏参数面板。

    调整时画布上是裁切之前的整幅画面与裁切框；底部的“查看结果”请宿主显示裁切之后的画面。
    """

    PARAMETER_CONTRACT = CROP_PARAMETERS
    TOOL_ID = "crop"
    TOOL_TITLE_KEY = "menu.edit.crop"
    TOOL_TITLE_DEFAULT = "Crop"

    def __init__(
        self,
        image_path: str,
        parent: Optional[QWidget] = None,
        edit_state_store=None,
        **services,
    ):
        self._original_image: Optional[Image.Image] = None
        self.overlay = CropOverlay()
        self.overlay.geometry_changed.connect(self._on_geometry_changed)

        super().__init__(image_path, parent=parent, edit_state_store=edit_state_store, **services)
        # 一次拖动记一步撤销
        self.track_edit_control(self.overlay, started=self.overlay.drag_started,
                                finished=self.overlay.drag_finished, is_active=self.overlay.is_dragging)

        self._load_source(image_path)
        self._sync_size_label()

    @staticmethod
    def _default_params():
        return CROP_PARAMETERS.defaults(CROP_PARAMETERS.version)

    def _load_source(self, path):
        self.load_panel_source(path, self._sync_ui_from_params)

    def build_ui(self) -> None:
        layout = self._root_layout

        self._ratio_card = CollapsibleCard("", self)
        form = QFormLayout()
        self._ratio_chips = ChipGroup(4, self)
        self._ratio_chips.set_items([(key, key) for key, _ in _ASPECT_RATIOS])
        self._ratio_chips.current_changed.connect(self._on_ratio_changed)
        form.addRow(self._ratio_chips)

        self._size_title = QLabel(self)
        self._size_label = QLabel(self)
        form.addRow(self._size_title, self._size_label)
        self._ratio_card.setContentLayout(form)
        layout.addWidget(self._ratio_card)

        self._tip_label = QLabel(self)
        self._tip_label.setWordWrap(True)
        self._tip_label.setObjectName("mutedText")
        layout.addWidget(self._tip_label)

        layout.addStretch()

        # 宿主支持时固定在底部：查看裁切之后的画面，或回到拖框
        self.result_view = ResultViewControl(self)
        self.result_view.footer.setVisible(self.external_preview)
        layout.addWidget(self.result_view.footer)
        self.retranslate_ui()

    def take_footer(self):
        self._root_layout.removeWidget(self.result_view.footer)
        return self.result_view.footer

    def _project_result(self) -> None:
        """底部写出将裁成多大；框还是整幅时没有结果可看。"""
        if not hasattr(self, "result_view"):
            return
        checked = CROP_PARAMETERS.normalize(self._params, CROP_PARAMETERS.version)
        if not checked.success or CROP_PARAMETERS.is_neutral(checked.data, CROP_PARAMETERS.version):
            self.result_view.describe(self._tr("crop.result.none"), self._tr("crop.result.reason"))
            return
        if self.source_bundle is None:
            self.result_view.describe("")
            return
        width, height = self.source_size
        box = self.overlay.get_crop_box(width, height)
        self.result_view.describe(self._tr("crop.result.summary", width=box[2] - box[0], height=box[3] - box[1]))

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        if not hasattr(self, "result_view"):
            return
        self._ratio_card.set_title(self._tr("crop.aspect_ratio", "Aspect ratio"))
        self._ratio_chips.set_text(_FREE, self._tr("crop.ratio.free", "Free"))
        self._size_title.setText(self._tr("crop.output_size", "Size"))
        self._tip_label.setText(self._tr("crop.tip", "Drag the handles to resize; drag inside the frame to move it."))
        self._project_result()

    def _on_ratio_changed(self, key: str) -> None:
        ratio = dict(_ASPECT_RATIOS)[key]
        w, h = self.source_size if self.source_bundle else (1, 1)
        self.overlay.set_aspect_ratio(ratio, w, h)

    def _on_geometry_changed(self) -> None:
        self._sync_size_label()
        if self._original_image:
            self._commit_params({
                "aspect_ratio": self.overlay.aspect_ratio,
                "crop_rect": self.overlay.norm_rect,
                "rotation_angle": self._params.get("rotation_angle", 0),
            })
            self._project_result()

    def _sync_ui_from_params(self) -> None:
        box = self._params.get("crop_rect")
        ratio = self._params.get("aspect_ratio")
        self.overlay.restore_geometry(box, ratio)
        self._ratio_chips.set_current(_ratio_key(ratio))
        self._sync_size_label()
        self.overlay.update_view()

    def _sync_size_label(self) -> None:
        self._project_result()
        if not self._original_image:
            return
        w, h = self.source_size
        box = self.overlay.get_crop_box(w, h)
        bw = box[2] - box[0]
        bh = box[3] - box[1]
        # 裁掉了一部分时写成“裁切前 → 裁切后”
        self._size_label.setText(f"{w} × {h}" if (bw, bh) == (w, h) else f"{w} × {h} → {bw} × {bh}")

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
        self._sync_size_label()

    def _do_update_preview(self):
        self.preview_panel_image()

    def cleanup(self):
        super().cleanup()
