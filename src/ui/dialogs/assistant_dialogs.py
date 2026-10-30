from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QVBoxLayout

from resources.styles.theme_variables import SPACING_LG, SPACING_XL, px
from ..base_window import BaseDialog
from ..widgets.apple_button import AppleButton, ApplePrimaryButton
from ..widgets.apple_choice import AppleCheckBox
from ..widgets.setting_row import SettingRow


class AssistantNameDialog(BaseDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent=parent, title_key="assistant.rename_conversation")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_XL))
        layout.setSpacing(px(SPACING_LG))
        self.row = SettingRow(self)
        self.name = QLineEdit(title, self)
        self.name.setMaxLength(120)
        self.row.add_control(self.name)
        layout.addWidget(self.row)
        actions = QHBoxLayout()
        self.cancel = AppleButton(parent=self)
        self.save = ApplePrimaryButton(parent=self)
        self.cancel.clicked.connect(self.reject)
        self.save.clicked.connect(self.accept)
        self.name.textEdited.connect(lambda text: self.save.setEnabled(bool(text.strip())))
        actions.addStretch()
        actions.addWidget(self.cancel)
        actions.addWidget(self.save)
        layout.addLayout(actions)
        self.retranslate_ui()
        self.name.selectAll()
        self.name.setFocus()

    def retranslate_ui(self):
        if hasattr(self, "row"):
            self.row.set_text(self._tr("assistant.conversation_name"))
            self.cancel.setText(self._tr("common.cancel"))
            self.save.setText(self._tr("common.save"))


class AssistantExportDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__(parent=parent, title_key="assistant.export_conversation")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_XL))
        layout.setSpacing(px(SPACING_LG))
        self.changes = AppleCheckBox("", self)
        self.changes.setChecked(True)
        self.images = AppleCheckBox("", self)
        layout.addWidget(self.changes)
        layout.addWidget(self.images)
        actions = QHBoxLayout()
        self.cancel = AppleButton(parent=self)
        self.export = ApplePrimaryButton(parent=self)
        self.cancel.clicked.connect(self.reject)
        self.export.clicked.connect(self.accept)
        actions.addStretch()
        actions.addWidget(self.cancel)
        actions.addWidget(self.export)
        layout.addLayout(actions)
        self.retranslate_ui()

    def retranslate_ui(self):
        if hasattr(self, "changes"):
            self.changes.setText(self._tr("assistant.export_changes"))
            self.images.setText(self._tr("assistant.export_images"))
            self.cancel.setText(self._tr("common.cancel"))
            self.export.setText(self._tr("common.export"))
