from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from .apple_menu import AppleDropDown
from .setting_row import SettingRow, SettingSection


class EvaluationSettings(QWidget):
    settings_edited = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.section = SettingSection(self)
        layout.addWidget(self.section)
        self.backend = AppleDropDown(self)
        for key in ("main", "decisions"):
            self.backend.addItem(key, key)
        self.connection = AppleDropDown(self)
        self.rows = {}
        for key, control in (("backend", self.backend), ("connection", self.connection)):
            row = SettingRow(self.section)
            row.add_control(control)
            self.section.add_row(row)
            self.rows[key] = row
        self.backend.activated.connect(self._edited)
        self.connection.activated.connect(self._edited)
        layout.addStretch()

    def set_state(self, state, tr):
        self._settings = state.get("evaluations", {})
        self.section.set_title(tr("assistant.evaluation.settings"))
        self.rows["backend"].set_text(tr("assistant.evaluation.backend"), tr("assistant.evaluation.read_only"))
        self.rows["connection"].set_text(tr("assistant.evaluation.connection"), tr("assistant.evaluation.connection_help"))
        for index, key in enumerate(("main", "decisions")):
            self.backend.setItemText(index, tr("assistant.evaluation.backend." + key))
        self.backend.setItemEnabled(1, "decisions" in self._settings.get("backends", ()))
        self.backend.setCurrentIndex(max(0, self.backend.findData(self._settings.get("backend", "main"))))
        self.connection.clear()
        self.connection.addItem(tr("assistant.evaluation.unconfigured"), "")
        for connection in state.get("connections", ()):
            self.connection.addItem(connection["name"], connection["id"])
        self.connection.setCurrentIndex(max(0, self.connection.findData(self._settings.get("decisions_connection", ""))))
        self.rows["connection"].set_message(tr("assistant.evaluation.unavailable")
            if "decisions" not in self._settings.get("backends", ()) else "")

    def _edited(self, *_args):
        connection = self.connection.currentData() or ""
        backend = self.backend.currentData()
        if backend == "decisions" and connection not in self._settings.get("ready_connections", ()):
            backend = "main"
        self.settings_edited.emit({"backend": backend, "decisions_connection": connection})
