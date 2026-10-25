from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QLabel, QLineEdit


class ClickableLabel(QLabel):
    """支持双击信号的标签，用于「双击重置参数」交互。"""

    double_clicked = pyqtSignal()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class EditableValueLabel(QLineEdit):
    """滑块右侧读数框：平时像标签，点击即可手动键入数值，回车/失焦提交。"""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("sliderValueEdit")
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setFixedWidth(60)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)
