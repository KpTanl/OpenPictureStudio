from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog


def pick_color(parent, initial_color, title: str):
    dialog = QColorDialog(QColor(*initial_color), parent)
    dialog.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
    dialog.setWindowTitle(title)
    if dialog.exec():
        color = dialog.currentColor()
        if color.isValid():
            return color
    return QColor()
