"""导出 PDF 对话框：列出要放进去的图片，右下角是取消与导出。"""

from __future__ import annotations

import os

from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel,
    QFileDialog, QListWidget,
)

from resources.styles.theme_variables import SPACING_LG, SPACING_MD, SPACING_SM, SPACING_XL, px
from ..base_window import BaseDialog
from ..widgets.apple_button import AppleBrandButton, AppleButton
from ...core.pdf_exporter import PDFExporter


class PDFDialog(BaseDialog):
    """PDF 导出对话框。"""

    def __init__(self, input_paths: list[str], parent=None):
        self.input_paths = input_paths
        self.exporter = PDFExporter()
        super().__init__(
            min_width=450,
            modal=True,
            parent=parent,
        )
        self._setup_ui()
        self.retranslate_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_LG))
        layout.setSpacing(px(SPACING_MD))

        self.images_label = QLabel()
        layout.addWidget(self.images_label)
        self.file_list = QListWidget()
        for p in self.input_paths:
            self.file_list.addItem(os.path.basename(p))
        layout.addWidget(self.file_list)

        self.hint = QLabel()
        self.hint.setObjectName("settingRowDescription")
        layout.addWidget(self.hint)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, px(SPACING_SM), 0, 0)
        btn_layout.setSpacing(px(SPACING_SM))
        btn_layout.addStretch()
        self.cancel_btn = AppleButton()
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)

        self.export_btn = AppleBrandButton()
        self.export_btn.setDefault(True)
        self.export_btn.clicked.connect(self.start_export)
        btn_layout.addWidget(self.export_btn)
        layout.addLayout(btn_layout)

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        self.setWindowTitle(
            self._tr(
                "pdf.window_title",
                "Export to PDF ({count} images)",
                count=len(self.input_paths),
            )
        )
        self.images_label.setText(
            self._tr("pdf.images_label", "Images to include (in order):")
        )
        self.hint.setText(
            self._tr("pdf.hint", "Note: Drag and drop in list to reorder coming soon.")
        )
        self.cancel_btn.setText(self._tr("pdf.cancel", "Cancel"))
        self.export_btn.setText(self._tr("pdf.export", "Export PDF"))

    def start_export(self) -> None:
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            self._tr("pdf.select_output_dir", "Save PDF As"),
            "",
            "PDF Files (*.pdf)",
        )
        if not output_path:
            return

        if self.exporter.export(self.input_paths, output_path):
            self.show_info(
                self._tr("pdf.success_title", "Success"),
                self._tr("pdf.success_message", "PDF exported successfully!"),
            )
            self.accept()
        else:
            self.show_error(
                self._tr("pdf.error_title", "Error"),
                self._tr("pdf.error_message", "Failed to export PDF."),
            )
