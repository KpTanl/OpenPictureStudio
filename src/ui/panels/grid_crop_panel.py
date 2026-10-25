"""Grid crop panel for in-window sidebar editing."""

import logging
from contextlib import contextmanager
from typing import Optional
from PIL import Image

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QFormLayout, QFileDialog, QLineEdit

from resources.styles.theme_variables import SPACING_SM, px
from .base_tool_panel import BaseToolPanel
from ...core.tools.geometry import GRID_PARAMETERS
from ..widgets.apple_button import ApplePrimaryButton
from ..widgets.apple_dropdown import AppleDropDown
from ..widgets.message_dialog import show_error, show_info
from ..widgets.apple_spin_box import AppleSpinBox
from ..widgets.chip import ChipGroup
from ..widgets.collapsible_card import CollapsibleCard
from ..overlays.grid_crop_overlay import GridCropOverlay

logger = logging.getLogger(__name__)

_PRESETS = ((3, 3), (2, 2), (1, 3), (3, 1))


def _preset_key(rows: int, cols: int) -> str:
    return f"{rows}x{cols}"


def _evenly_split(lines) -> bool:
    """分割线是否把这一边等分。"""
    count = len(lines) + 1
    return all(abs(line - (index + 1) / count) < 1e-6 for index, line in enumerate(lines))


class GridCropPanel(BaseToolPanel):
    """九宫格/多格切图侧栏参数面板。"""

    PARAMETER_CONTRACT = GRID_PARAMETERS
    TOOL_ID = "grid_crop"
    TOOL_TITLE_KEY = "menu.edit.grid_crop"
    TOOL_TITLE_DEFAULT = "Grid Crop"

    def __init__(
        self,
        image_path: str,
        parent: Optional[QWidget] = None,
        edit_state_store=None,
        **services,
    ):
        self._original_image: Optional[Image.Image] = None
        self.export_snapshot_provider = None
        self.overlay = GridCropOverlay()
        self.overlay.geometry_changed.connect(self._on_geometry_changed)

        super().__init__(image_path, parent=parent, edit_state_store=edit_state_store, **services)

        self._load_source(image_path)
        self._sync_tiles_count()

    @staticmethod
    def _default_params():
        return GRID_PARAMETERS.defaults(GRID_PARAMETERS.version)

    def _load_source(self, path):
        self.load_panel_source(path, self._sync_ui_from_params)

    def build_ui(self) -> None:
        layout = self._root_layout

        self._grid_card = CollapsibleCard("", self)
        form = QFormLayout()

        # 行与列各占一行：并排时比侧栏宽
        self._rows_spin = AppleSpinBox(self)
        self._rows_spin.setRange(1, 10)
        self._rows_spin.setValue(3)
        self._rows_spin.valueEdited.connect(self._on_grid_changed)
        self._rows_label = QLabel(self)
        form.addRow(self._rows_label, self._rows_spin)

        self._cols_spin = AppleSpinBox(self)
        self._cols_spin.setRange(1, 10)
        self._cols_spin.setValue(3)
        self._cols_spin.valueEdited.connect(self._on_grid_changed)
        self._cols_label = QLabel(self)
        form.addRow(self._cols_label, self._cols_spin)

        # 常用的等分方式；分割线被拖动过之后都不选中，再点一次重新等分
        self._preset_chips = ChipGroup(len(_PRESETS), self)
        self._preset_chips.set_items([(_preset_key(rows, cols), f"{rows} × {cols}") for rows, cols in _PRESETS])
        self._preset_chips.current_changed.connect(self._on_preset_chosen)
        form.addRow(self._preset_chips)

        self._tip_label = QLabel(self)
        self._tip_label.setWordWrap(True)
        self._tip_label.setObjectName("mutedText")
        form.addRow(self._tip_label)

        self._grid_card.setContentLayout(form)
        layout.addWidget(self._grid_card)

        self._export_card = CollapsibleCard("", self)
        export_form = QFormLayout()

        self._prefix_edit = QLineEdit("grid", self)
        self._prefix_label = QLabel(self)
        export_form.addRow(self._prefix_label, self._prefix_edit)

        self._fmt_combo = AppleDropDown(self)
        self._fmt_combo.addItems(["PNG", "JPEG", "WEBP", "BMP"])
        self._fmt_label = QLabel(self)
        export_form.addRow(self._fmt_label, self._fmt_combo)

        self._export_card.setContentLayout(export_form)
        layout.addWidget(self._export_card)

        layout.addStretch()

        # 导出由用户发起：写明会导出多少张。宿主支持时固定在底部，不随上面的内容滚动
        self._footer = QWidget(self)
        footer = QVBoxLayout(self._footer)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(px(SPACING_SM))
        self._total_label = QLabel(self._footer)
        self._total_label.setObjectName("mutedText")
        footer.addWidget(self._total_label)
        self._export_btn = ApplePrimaryButton(parent=self._footer)
        self._export_btn.clicked.connect(self.export_tiles)
        footer.addWidget(self._export_btn)
        layout.addWidget(self._footer)
        self.retranslate_ui()

    def take_footer(self):
        self._root_layout.removeWidget(self._footer)
        return self._footer

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        if not hasattr(self, "_export_btn"):
            return
        self._grid_card.set_title(self._tr("grid_crop.settings", "Grid"))
        self._rows_label.setText(self._tr("grid_crop.rows", "Rows"))
        self._cols_label.setText(self._tr("grid_crop.cols", "Columns"))
        self._tip_label.setText(self._tr("grid_crop.tip", "Drag a split line on the photo to change the size of each tile."))
        self._export_card.set_title(self._tr("grid_crop.export_options", "Export"))
        self._prefix_label.setText(self._tr("grid_crop.prefix", "File name prefix"))
        self._fmt_label.setText(self._tr("grid_crop.format", "Format"))
        self._export_btn.setText(self._tr("grid_crop.export_button", "Export tiles…"))
        self._sync_tiles_count()

    def _on_preset_chosen(self, key: str) -> None:
        rows, cols = (int(part) for part in key.split("x"))
        self.overlay.set_grid(rows, cols)

    def _on_grid_changed(self) -> None:
        r = self._rows_spin.value()
        c = self._cols_spin.value()
        self.overlay.split_evenly(r, c)

    def _on_geometry_changed(self) -> None:
        rows, cols = len(self.overlay.h_lines) + 1, len(self.overlay.v_lines) + 1
        if self._commit_params({"rows": rows, "cols": cols,
                                "h_lines": list(self.overlay.h_lines), "v_lines": list(self.overlay.v_lines)}):
            self._sync_grid_display(rows, cols)

    def _sync_tiles_count(self) -> None:
        rows = len(self.overlay.h_lines) + 1
        cols = len(self.overlay.v_lines) + 1
        self._total_label.setText(self._tr("grid_crop.total", "{rows} × {cols}, {count} tiles",
                                           rows=rows, cols=cols, count=rows * cols))

    def _sync_grid_display(self, rows: int, cols: int) -> None:
        self._rows_spin.setValue(rows)
        self._cols_spin.setValue(cols)
        even = (_evenly_split(self.overlay.h_lines) and _evenly_split(self.overlay.v_lines)
                and (rows, cols) in _PRESETS)
        self._preset_chips.set_current(_preset_key(rows, cols) if even else "")
        self._sync_tiles_count()
        self.overlay.update_view()

    def _sync_ui_from_params(self) -> None:
        rows, cols = self._params.get("rows", 3), self._params.get("cols", 3)
        self.overlay.restore_geometry(rows, cols, self._params.get("h_lines"), self._params.get("v_lines"))
        self._sync_grid_display(rows, cols)

    def export_tiles(self) -> None:
        if self._render_service is None:
            from ...application.panel_render_service import PanelRenderService
            self._render_service = PanelRenderService()
        available = self._render_service.check_available(self.TOOL_ID)
        if not available.success:
            from ...application.result_types import CoreOperationError
            self._processing_error(CoreOperationError(available.error))
            return
        if not self._original_image:
            self._load_source(self._image_path)
        if not self._original_image:
            return

        output_dir = QFileDialog.getExistingDirectory(
            self,
            self._tr("grid_crop.select_output_folder", "Select Output Folder"),
        )
        if not output_dir:
            return

        prefix = self._prefix_edit.text().strip() or "grid"
        fmt = self._fmt_combo.currentText()
        try:
            import copy
            from ..services.background_operation import run_background
            from ...application.image_output_service import ImageOutputService
            params = copy.deepcopy(self._params)
            if self.export_snapshot_provider is not None:
                exporter = self.export_snapshot_provider()
                paths = run_background(self, lambda cancellation: exporter(params, output_dir, prefix, fmt, cancellation))
            else:
                identity, sources = self.source_bundle.identity, self._image_source_service
                service = self._output_service or ImageOutputService()
                runtime = self._render_service.runtime
                checked = runtime.validate_tool_params(self.TOOL_ID, params)
                if not checked.success:
                    from ...application.result_types import CoreOperationError
                    raise CoreOperationError(checked.error)
                params = checked.data
                snapshot = runtime.snapshot()
                tool_id = self.TOOL_ID
                def export(cancellation):
                    def current():
                        return (not snapshot.is_cancelled() and not cancellation.is_cancelled()
                                and runtime.check_enabled(tool_id).success
                                and sources.matches_file(identity))

                    @contextmanager
                    def publication():
                        with runtime.publication(snapshot) as available, cancellation.publication() as active:
                            yield available and active and current()

                    with sources.borrow_linear(identity.path, identity=identity,
                                               is_cancelled=lambda: not current()) as lease:
                        return service.grid(lease.value, params, output_dir, prefix, fmt,
                                            cancellation=cancellation, publication=publication)
                paths = run_background(self, export)
            from ..services.background_operation import batch_message
            msg = batch_message(paths)
            show_info(self, self._tr("grid_crop.export_complete.title", "Export Complete"), msg)
            self.request_status.emit(msg)
        except Exception as e:
            logger.exception("Failed to export grid crop results")
            from ...application.result_types import CoreOperationError
            key = e.error.message_key if isinstance(e, CoreOperationError) else str(e)
            show_error(self, self._tr("grid_crop.export_error.title", "Export Error"),
                       self._tr(key, self._tr("error.export.failed")))

    def apply(self) -> Optional[Image.Image]:
        # 九宫格属于导出型工具，直接触发目录选择并批量保存切片，返回 None 防止覆盖当前主图
        self.export_tiles()
        return None

    def reset(self):
        self._apply_params(self._default_params())

    def on_image_path_changed(self, path: str) -> None:
        self._load_source(path)
        self._sync_tiles_count()


    def _do_update_preview(self):
        return

    def cleanup(self):
        super().cleanup()
