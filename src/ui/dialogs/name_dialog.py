"""输入一个名称的对话框：标题、一行说明、输入框与“取消 / 确定”，名称为空时不能确定。"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_LG, SPACING_SM, SPACING_XL, SPACING_XS, px
from ..base_window import BaseDialog
from ..widgets.apple_button import AppleButton, ApplePrimaryButton
from ..widgets.management_page import management_label
from ..widgets.message_dialog import fit_dialog_height

_WIDTH = 420
_MAX_LENGTH = 120


class NameDialog(BaseDialog):
    def __init__(self, title: str, label: str, value: str = "", action: str = "",
                 parent: Optional[QWidget] = None):
        super().__init__(title_default=title, min_width=_WIDTH, min_height=0, parent=parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_LG))
        layout.setSpacing(px(SPACING_XS))
        caption = management_label(self, "settingRowTitle")
        caption.setText(label)
        layout.addWidget(caption)
        self.name = QLineEdit(value, self)
        self.name.setMaxLength(_MAX_LENGTH)
        layout.addWidget(self.name)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, px(SPACING_LG), 0, 0)
        actions.setSpacing(px(SPACING_SM))
        actions.addStretch()
        cancel = AppleButton(self._tr("common.cancel"), self)
        self.confirm = ApplePrimaryButton(action or self._tr("common.ok"), self)
        cancel.clicked.connect(self.reject)
        self.confirm.clicked.connect(self.accept)
        self.confirm.setDefault(True)
        actions.addWidget(cancel)
        actions.addWidget(self.confirm)
        layout.addLayout(actions)
        self.name.textChanged.connect(lambda text: self.confirm.setEnabled(bool(text.strip())))
        self.confirm.setEnabled(bool(value.strip()))
        self.setFixedWidth(_WIDTH)
        fit_dialog_height(self)
        self.name.selectAll()
        self.name.setFocus()

    def value(self) -> str:
        return self.name.text()


def ask_name(parent: Optional[QWidget], title: str, label: str, value: str = "", action: str = "") -> Optional[str]:
    """返回输入的名称；取消时返回 None。"""
    dialog = NameDialog(title, label, value, action, parent)
    accepted = dialog.exec()
    name = dialog.value()
    dialog.deleteLater()
    return name if accepted else None
