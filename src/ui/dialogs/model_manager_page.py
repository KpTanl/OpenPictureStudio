from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QFileDialog, QGridLayout, QProgressBar, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_MD, px
from ..themed_mixin import ThemedI18nMixin
from ..widgets.apple_button import AppleButton, AppleDestructiveButton, ApplePrimaryButton
from ..widgets.apple_menu import AppleDropDown
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.management_page import (ManagementInfo, ManagementListGroup, ManagementListItem,
                                       ManagementPage, management_label, status_kind)
from ..widgets.status_badge import StatusBadge
from ..services.model_task_runner import ModelTaskRunner


class ModelManagerPage(QWidget, ThemedI18nMixin):
    def __init__(self, service, capability=None, parent=None, *, backend_status=None):
        super().__init__(parent)
        self.service = service
        self._backend_status = backend_status
        self._rows = ()
        self._detail_key = ""
        self._init_themed_i18n()
        self.destroyed.connect(self._cleanup_themed_i18n)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._page = ManagementPage(self)
        layout.addWidget(self._page)
        self.filter = AppleDropDown(self)
        self.filter.addItem("", None)
        for cap in ("super_resolution", "denoise", "deblur", "segmentation", "face_detection", "face_localization"):
            self.filter.addItem("", cap)
        self.filter.setCurrentIndex(max(0, self.filter.findData(capability)))
        self._page.navigation_layout.insertWidget(0, self.filter)
        self._default_badge = StatusBadge("success", parent=self)
        self._page.header_actions.addWidget(self._default_badge)
        self._info = ManagementInfo(("version", "download", "installed", "runtime", "license",
                                     "source", "references", "temporary"), self)
        self._page.body_layout.addWidget(self._info)
        self._files = CollapsibleCard("", self, is_expanded=False)
        self.details = management_label(self, "managementCaption", selectable=True)
        self._files.add_widget(self.details)
        self._page.body_layout.addWidget(self._files)
        self.progress = QProgressBar(self)
        self._page.footer_layout.addWidget(self.progress)
        buttons = QGridLayout()
        buttons.setSpacing(px(SPACING_MD))
        self.actions = {}
        for index, action in enumerate(("download", "import", "default", "uninstall", "cancel", "clear")):
            button_type = ApplePrimaryButton if action == "download" else AppleDestructiveButton if action == "uninstall" else AppleButton
            button = button_type(parent=self)
            button.clicked.connect(lambda checked=False, name=action: self._act(name))
            buttons.addWidget(button, index // 2, index % 2)
            self.actions[action] = button
        self._page.footer_layout.addLayout(buttons)
        self.filter.currentIndexChanged.connect(self.refresh)
        self._page.navigation.selection_changed.connect(self._details)
        self.runner = ModelTaskRunner(service, self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh)
        self.runner.changed.connect(lambda: self._timer.start() if not self._timer.isActive() else None)
        self.retranslate_ui()

    def retranslate_ui(self):
        blocked = self.filter.blockSignals(True)
        for index in range(self.filter.count()):
            cap = self.filter.itemData(index)
            self.filter.setItemText(index, self._tr("models.capability." + cap if cap else "models.all"))
        self.filter.blockSignals(blocked)
        self.filter.setAccessibleName(self._tr("management.model_filter"))
        self._files.set_title(self._tr("management.files"))
        self._default_badge.set_status("success", self._tr("management.default_model"))
        for name, button in self.actions.items():
            button.setText(self._tr("models.action." + name))
        self.refresh()

    def _selected(self):
        key = self._page.navigation.selected_key()
        return next((item for item in self._rows if item.descriptor.key.token == key), None)

    def select_model(self, token):
        self.filter.setCurrentIndex(max(0, self.filter.findData(None)))
        self.refresh()
        return self._page.select_key(token)

    def show_capability(self, capability):
        """只列出某一类能力的模型；不认识的能力时列出全部。"""
        self.filter.setCurrentIndex(max(0, self.filter.findData(capability)))
        self.refresh()

    def refresh(self):
        self._rows = self.service.list(self.filter.currentData())
        items = tuple(ManagementListItem(item.descriptor.key.token,
            item.descriptor.name + (" ★" if item.default else ""),
            self._tr("models.state." + item.state) + " · " + item.descriptor.key.revision,
            "sparkles") for item in self._rows)
        if self._page.navigation.set_groups((ManagementListGroup("models",
                self._tr("management.model_count", count=len(items)), items),)):
            self._page.ensure_selection_visible()
        self._details()

    def _details(self):
        item = self._selected()
        self.progress.hide()
        self.actions["cancel"].setVisible(item is not None and item.state in ("queued", "downloading", "verifying", "waiting"))
        self.actions["clear"].setVisible(item is not None and item.temporary_bytes > 0)
        key = item.descriptor.key.token if item is not None else ""
        if key != self._detail_key:
            self._detail_key = key
            self._files.set_expanded(False)
            self._page.scroll.verticalScrollBar().setValue(0)
        self._info.setVisible(item is not None)
        self._files.setVisible(item is not None)
        self._page.badge.setVisible(item is not None)
        self._default_badge.setVisible(item is not None and item.default)
        self._page.set_notice()
        if item is None:
            for button in self.actions.values():
                button.setEnabled(False)
            self.details.clear()
            self._page.title.setText(self._tr("models.title"))
            self._page.subtitle.setText(self._tr("management.models_empty"))
            return
        descriptor = item.descriptor
        self._page.title.setText(descriptor.name)
        self._page.subtitle.setText(self._tr("models.capability." + descriptor.capability))
        self._page.badge.set_status(status_kind(item.state), self._tr("models.state." + item.state))
        readonly = item.descriptor.adapter in ("bundled", "legacy")
        active = item.state in ("queued", "downloading", "verifying", "waiting", "uninstalling", "discovering", "cleaning")
        self.actions["download"].setEnabled(not readonly and not active and item.state != "installed")
        self.actions["import"].setEnabled(not readonly and not active and not item.managed)
        self.actions["cancel"].setEnabled(item.state in ("queued", "downloading", "verifying", "waiting"))
        self.actions["uninstall"].setEnabled(item.managed and item.state == "installed")
        self.actions["default"].setEnabled(not readonly and not active)
        self.actions["clear"].setEnabled(not readonly and not active and item.temporary_bytes > 0)
        runtime = self.service.runtime_status(item.descriptor)
        if not item.descriptor.runtime:
            backend = self._tr("models.runtime.bundled")
        elif not runtime.available:
            state_key = "runtime.library.state." + runtime.state if runtime.state in ("restart", "update") else "runtime.state." + runtime.state
            backend = self._tr("runtime.library." + runtime.runtime) + " · " + self._tr(state_key)
        elif item.descriptor.runtime == "onnxruntime" and self._backend_status is not None:
            backend = self._tr("inference.backend." + self._backend_status().backend)
        else:
            backend = self._tr("runtime.library." + runtime.runtime)
        if item.descriptor.runtime:
            backend += " · " + self._tr("runtime.library.source." + runtime.source)
            if runtime.version:
                backend += " " + runtime.version
        values = {"version": descriptor.key.revision,
            "download": f"{descriptor.download_size / 1e6:.1f} MB",
            "installed": f"{item.installed_bytes / 1e6:.1f} MB", "runtime": backend,
            "license": descriptor.license, "source": descriptor.source,
            "references": self._tr("management.reference_count", count=self.service.reference_count(descriptor.key)),
            "temporary": f"{item.temporary_bytes / 1e6:.1f} MB"}
        for name, value in values.items():
            label = self._tr("models.column." + name) if name in ("version", "download", "installed") else self._tr("management." + name)
            self._info.set_row(name, label, value, visible=name != "temporary" or item.temporary_bytes > 0)
        text = descriptor.key.token
        for file in descriptor.files:
            text += f"\n{file.name}\nSHA-256: {file.digest}"
        if item.paths:
            text += "\n" + "\n".join(path for _, path in item.paths)
        self.details.setText(text)
        if item.error:
            self._page.set_notice(self._tr(item.error), "warning")
        elif readonly:
            self._page.set_notice(self._tr("management.model_readonly"))
        elif descriptor.runtime and not runtime.available:
            self._page.set_notice(backend, "warning")
        if active:
            self.progress.setVisible(True)
            if item.state == "downloading":
                self.progress.setRange(0, item.descriptor.download_size)
                self.progress.setValue(item.downloaded)
                self.progress.setFormat(f"{item.downloaded / 1e6:.1f} / {item.descriptor.download_size / 1e6:.1f} MB")
            else:
                self.progress.setRange(0, 0)

    def _act(self, name):
        item = self._selected()
        if item is None:
            return
        key = item.descriptor.key
        if name == "download":
            self.service.install(key)
        elif name == "cancel":
            self.service.cancel(key)
        elif name == "clear":
            self.service.clear_temporary(key)
        elif name == "default":
            self.service.set_default(key)
        elif name == "import":
            path, _ = QFileDialog.getOpenFileName(self, self._tr("models.action.import"), "",
                                                 self._tr("models.import_filter"))
            if path:
                self.service.install(key, import_path=path)
        elif name == "uninstall":
            from ..widgets.message_dialog import show_confirm
            if show_confirm(self, self._tr("models.action.uninstall"), self._tr("models.uninstall_confirm",
                            name=item.descriptor.name, version=key.revision,
                            size=f"{item.installed_bytes / 1e6:.1f}",
                            count=self.service.reference_count(key)), default_yes=False):
                self.service.uninstall(key)

    def close_subscription(self):
        self._timer.stop()
        self.runner.close()
        self._cleanup_themed_i18n()
