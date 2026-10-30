from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from resources.styles.theme_variables import ASSISTANT_REFERENCE_EDGE, CONTROL_HEIGHT_COMPACT, SPACING_SM, px
from ..icons import themed_icon
from .apple_button import AppleIconButton
from .tooltip import set_tooltip


def measurement_text(analysis, tr):
    if not analysis or not analysis.get("spec"):
        return ""
    if analysis.get("status") != "available":
        return tr("assistant.measurement.empty")
    tone = analysis["luma_percentiles"]
    chroma = analysis["chroma_percentiles"]
    lines = [tr("assistant.measurement.sample", version=analysis["spec"]["version"], count=analysis["sample_count"]),
             tr("assistant.measurement.tone", p5=f'{tone["p5"]:.3f}', p50=f'{tone["p50"]:.3f}', p95=f'{tone["p95"]:.3f}'),
             tr("assistant.measurement.chroma", p50=f'{chroma["p50"]:.1f}', p90=f'{chroma["p90"]:.1f}'),
             tr("assistant.measurement.clipping", black=f'{analysis["near_black_share"] * 100:.1f}',
                white=f'{analysis["near_white_share"] * 100:.1f}')]
    if analysis.get("face_rectangle"):
        lines.append(tr("assistant.measurement.face"))
    return "\n".join(lines)


class AssistantReference(QWidget):
    removed = Signal()

    def __init__(self, parent=None, *, removable=False):
        super().__init__(parent)
        self._name, self._thumbnail = "", None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        self.preview = QLabel(self)
        self.preview.setObjectName("previewWell")
        self.preview.setFixedSize(ASSISTANT_REFERENCE_EDGE, ASSISTANT_REFERENCE_EDGE)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.preview)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        self.name = QLabel(self)
        self.name.setTextFormat(Qt.TextFormat.PlainText)
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.note = QLabel(self)
        self.note.setObjectName("mutedText")
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setWordWrap(True)
        column.addWidget(self.name)
        column.addWidget(self.note)
        layout.addLayout(column, 1)
        self.remove = AppleIconButton(themed_icon("x"), CONTROL_HEIGHT_COMPACT, self)
        self.remove.clicked.connect(self.removed)
        self.remove.setVisible(removable)
        layout.addWidget(self.remove)
        self.hide()

    def set_reference(self, reference, tr, *, status="", title=""):
        self.setVisible(bool(reference))
        if not reference:
            return
        self._name = title or tr("assistant.reference.label") + " · " + reference["name"]
        set_tooltip(self.name, self._name)
        self._elide()
        thumbnail = reference.get("thumbnail") or {}
        if thumbnail != self._thumbnail:
            self._thumbnail = thumbnail
            if thumbnail:
                width, height = thumbnail["size"]
                data = bytes(QByteArray.fromBase64(thumbnail["rgb"].encode("ascii")))
                image = QImage(data, width, height, width * 3, QImage.Format.Format_RGB888).copy()
                picture = QPixmap.fromImage(image)
            else:
                picture = QPixmap()
            self.preview.setPixmap(themed_icon("photo").pixmap(self.preview.size()) if picture.isNull() else
                picture.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.note.setText(tr("assistant.reference.missing") if reference.get("missing") else status)
        self.note.setVisible(bool(self.note.text()))
        set_tooltip(self.remove, tr("assistant.reference.remove"))
        self.remove.setAccessibleName(tr("assistant.reference.remove"))

    def _elide(self):
        self.name.setText(self.name.fontMetrics().elidedText(self._name, Qt.TextElideMode.ElideRight, self.name.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()
