from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel

from resources.styles.theme_variables import SPACING_SM, px
from ...core.tools.color import OPS_LOOK, color_look_options
from ..themed_mixin import ThemedI18nMixin
from .apple_button import AppleButton
from .apple_menu import AppleDropDown

_LOOKS = ("none", OPS_LOOK)


class ColorLookControls(QWidget, ThemedI18nMixin):
    """观感选择器，保留旧观感状态并提供旧曝光的升级入口。"""

    look_edited = pyqtSignal(str)
    upgrade_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        row = QHBoxLayout()
        row.setSpacing(px(SPACING_SM))
        self._label = QLabel(self)
        self._choice = AppleDropDown(self)
        for look in _LOOKS:
            self._choice.addItem("", userData=look)
        self._choice.activated.connect(lambda index: self.look_edited.emit(self._choice.itemData(index)))
        row.addWidget(self._label)
        row.addWidget(self._choice, 1)
        layout.addLayout(row)
        self._legacy_row = QWidget(self)
        legacy_layout = QHBoxLayout(self._legacy_row)
        legacy_layout.setContentsMargins(0, 0, 0, 0)
        legacy_layout.setSpacing(px(SPACING_SM))
        self._legacy_label = QLabel(self._legacy_row)
        self._legacy_label.setObjectName("mutedText")
        self._legacy_label.setWordWrap(True)
        self._upgrade_button = AppleButton(parent=self._legacy_row)
        self._upgrade_button.clicked.connect(lambda checked=False: self.upgrade_requested.emit())
        legacy_layout.addWidget(self._legacy_label, 1)
        legacy_layout.addWidget(self._upgrade_button)
        layout.addWidget(self._legacy_row)
        self._init_themed_i18n()
        self.retranslate_ui()
        self.set_state("none", 2)

    def look(self) -> str:
        return self._choice.currentData()

    def set_state(self, look, process_version, *, version=None):
        """程序回填：不发出 look_edited。"""
        looks = color_look_options(look, process_version, version=version)
        if looks != tuple(self._choice.itemData(index) for index in range(self._choice.count())):
            self._choice.clear()
            for value in looks:
                self._choice.addItem("", userData=value)
            self.retranslate_ui()
        self._choice.setCurrentIndex(self._choice.findData(look))
        self._legacy_row.setVisible(process_version == 1)

    def label_width_hint(self) -> int:
        """“观感”二字所需的宽度，供宿主把它与相邻几行的名称对齐。"""
        return self._label.sizeHint().width()

    def set_label_width(self, width: int) -> None:
        self._label.setFixedWidth(width)

    def cleanup(self):
        self._cleanup_themed_i18n()

    def retranslate_ui(self):
        self._label.setText(self._tr("color_adjust.look.label", "Look"))
        self._choice.setItemText(0, self._tr("color_adjust.look.none", "Original Color"))
        self._choice.setItemText(1, self._tr("color_adjust.look.ops_base", "OPS Look"))
        if self._choice.count() > len(_LOOKS):
            self._choice.setItemText(2, self._tr("color_adjust.look.legacy", "OPS Look (Legacy)"))
        self._legacy_label.setText(self._tr("color_adjust.exposure.legacy", "This photo uses the previous exposure version"))
        self._upgrade_button.setText(self._tr("color_adjust.exposure.upgrade", "Upgrade"))
