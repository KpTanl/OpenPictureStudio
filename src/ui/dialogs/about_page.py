"""设置窗口里的“关于”页：应用图标、名称、版本、简介与许可。"""

from __future__ import annotations

from datetime import datetime
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_LG, SPACING_XS, px
from ..themed_mixin import ThemedI18nMixin
from ...utils.app_info import APP_NAME, APP_VERSION
from ...utils.resource_paths import resolve_app_icon_path
from ...application.version_check import UpdateState
from ..widgets.apple_button import AppleButton

_LOGO_SIZE = 64


class _LogoLabel(QLabel):
    def __init__(self, icon: QIcon, parent=None):
        super().__init__(parent)
        self._icon = icon

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        self._icon.paint(painter, self.contentsRect(), Qt.AlignmentFlag.AlignCenter)


class AboutPage(QWidget, ThemedI18nMixin):
    check_requested = Signal()
    download_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_themed_i18n()
        self.destroyed.connect(self._cleanup_themed_i18n)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_LG))

        header = QHBoxLayout()
        header.setSpacing(px(SPACING_LG))
        logo = _LogoLabel(QIcon(str(resolve_app_icon_path())), self)
        logo.setFixedSize(_LOGO_SIZE, _LOGO_SIZE)
        header.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(px(SPACING_XS))
        names.addStretch(1)
        self._name = QLabel(APP_NAME, self)
        self._name.setObjectName("dialogTitle")
        self._name.setIndent(0)
        names.addWidget(self._name)
        self._version = QLabel(self)
        self._version.setObjectName("settingRowDescription")
        names.addWidget(self._version)
        names.addStretch(1)
        header.addLayout(names, 1)
        layout.addLayout(header)

        self._description = QLabel(self)
        self._description.setWordWrap(True)
        self._description.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._description)
        self._license = QLabel(self)
        self._license.setObjectName("settingRowDescription")
        self._license.setWordWrap(True)
        layout.addWidget(self._license)
        self._update_state = UpdateState()
        self._source = QLabel(self)
        self._source.setWordWrap(True)
        self._source.setObjectName("settingRowDescription")
        self._source.setVisible(not getattr(sys, "frozen", False))
        layout.addWidget(self._source)
        self._update_status = QLabel(self)
        self._update_status.setWordWrap(True)
        self._update_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._update_status)
        self._update_time = QLabel(self)
        self._update_time.setObjectName("settingRowDescription")
        self._update_time.setWordWrap(True)
        layout.addWidget(self._update_time)
        actions = QHBoxLayout()
        self._check = AppleButton(parent=self)
        self._check.setAutoDefault(False)
        self._check.clicked.connect(self.check_requested.emit)
        actions.addWidget(self._check)
        self._download = AppleButton(parent=self)
        self._download.setAutoDefault(False)
        self._download.clicked.connect(self.download_requested.emit)
        actions.addWidget(self._download)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.retranslate_ui()

    def set_update_state(self, state):
        self._update_state = state
        self._project_update()

    def _project_update(self):
        state = self._update_state
        status = "checking" if state.busy else state.status
        if state.cached and state.status == "idle":
            status = "cached"
        text = self._tr("update.status." + status, version=state.version)
        if state.error:
            text = self._tr(state.error)
        if state.warning:
            text += "\n" + self._tr(state.warning)
        self._update_status.setText(text)
        self._update_time.setVisible(bool(state.checked_at))
        if state.checked_at:
            self._update_time.setText(self._tr("update.last_checked", time=datetime.fromtimestamp(
                state.checked_at).strftime("%Y-%m-%d %H:%M")))
        self._check.setEnabled(not state.busy)
        self._download.setVisible(bool(state.version) or state.error == "update.error.unavailable")

    def version_text(self) -> str:
        return self._version.text()

    def retranslate_ui(self) -> None:
        self._version.setText(self._tr("about.version", "Version v{version}", version=APP_VERSION))
        self._description.setText(self._tr(
            "about.description",
            "A free, open-source workspace for local image editing.\n"
            "The new Open Picture Studio brings together RAW color adjustment, AI editing, "
            "combined tools, and batch processing.\n"
            "Save projects and editing history, and pick up where you left off."))
        self._license.setText(self._tr("about.license", "Original code is licensed under the MIT License. Third-party licenses and source information are available in the installation folder."))
        self._source.setText(self._tr("update.source"))
        self._check.setText(self._tr("update.check"))
        self._download.setText(self._tr("update.download"))
        self._project_update()
