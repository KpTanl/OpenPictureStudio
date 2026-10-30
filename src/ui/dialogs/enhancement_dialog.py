from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout

from ..base_window import BaseDialog
from ..panels.neural_enhance_panel import AIEnhancePanel
from resources.styles.theme_variables import SPACING_LG, SPACING_SM, SPACING_XL, px
from ..widgets.apple_button import AppleBrandButton, AppleButton
from ..services.background_operation import run_background, batch_message
from ..widgets.message_dialog import show_error, show_info
from ...application.enhancement_batch_service import EnhancementBatchService
from ...application.panel_render_service import PanelRenderService


class EnhancementDialog(BaseDialog):
    def __init__(self, image_paths, parent=None, output_service=None, *, runtime=None, source_service=None,
                 manage_models=None):
        """manage_models(能力, 模型标识) 由打开它的一方给出，用来打开模型管理页。"""
        from ...application.tool_runtime import ToolRuntime
        from ...application.image_source_service import ImageSourceService
        from ...application.image_output_service import ImageOutputService
        super().__init__(title_key="menu.edit.ai_enhance", min_width=600, min_height=440,
                         default_width=1000, default_height=700, parent=parent)
        self.paths = tuple(image_paths)
        if not self.paths:
            raise ValueError("error.image.load")
        self.sources = source_service or ImageSourceService()
        self._owns_runtime = runtime is None
        self.runtime = runtime or ToolRuntime(compute_budget=self.sources.compute,
            resource_budget=self.sources.resources, execution_threads=self.sources.policy.operator_threads)
        self.outputs = output_service or ImageOutputService(compute_budget=self.sources.compute)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_LG))
        layout.setSpacing(px(SPACING_LG))
        row = QHBoxLayout()
        row.setSpacing(px(SPACING_LG))
        self.preview = QLabel(self)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(240, 240)
        row.addWidget(self.preview, 1)
        self.panel = AIEnhancePanel(self.paths[0], self, image_source_service=self.sources,
                                   render_service=PanelRenderService(self.runtime))
        self.panel.preview_ready.connect(self._preview)
        self.panel.availability_changed.connect(self._update_availability)
        if manage_models is not None:
            self.panel.model_manager_requested.connect(manage_models)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.panel)
        scroll.setMinimumWidth(360)
        row.addWidget(scroll, 1)
        layout.addLayout(row, 1)
        footer = QHBoxLayout()
        footer.setSpacing(px(SPACING_SM))
        footer.addStretch()
        self.cancel = AppleButton(parent=self)
        self.cancel.clicked.connect(self.reject)
        footer.addWidget(self.cancel)
        self.save = AppleBrandButton(parent=self)
        self.save.clicked.connect(self._export)
        footer.addWidget(self.save)
        layout.addLayout(footer)
        self.retranslate_ui()
        self._update_availability()

    def _update_availability(self):
        self.save.setEnabled(bool(self.panel.models_ready))

    def retranslate_ui(self):
        super().retranslate_ui()
        self.cancel.setText(self._tr("button.cancel", "Cancel"))
        self.save.setText(self._tr("enhance.export_batch", count=len(self.paths)))

    def _preview(self, image):
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _export(self):
        directory = QFileDialog.getExistingDirectory(self, self._tr("enhance.output_folder"))
        if not directory:
            return
        params = self.panel.parameters
        service = EnhancementBatchService(self.runtime, self.sources, self.outputs)
        try:
            result = run_background(self, lambda cancellation: service.export(self.paths, params,
                                     directory, cancellation=cancellation))
        except Exception as error:
            key = getattr(getattr(error, "error", None), "message_key", str(error))
            show_error(self, self._tr("menu.edit.ai_enhance"), self._tr(key, self._tr("enhance.failed")))
            return
        show_info(self, self._tr("menu.edit.ai_enhance"), batch_message(result))

    def done(self, result):
        self.panel.close_panel()
        if self._owns_runtime:
            self.runtime.close()
        super().done(result)
