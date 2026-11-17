from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QGridLayout, QHBoxLayout, QProgressBar, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_MD, px
from ..themed_mixin import ThemedI18nMixin
from ..widgets.apple_button import AppleButton, AppleDestructiveButton, ApplePrimaryButton
from ..widgets.apple_menu import AppleDropDown
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.management_page import (ManagementInfo, ManagementListGroup, ManagementListItem,
                                       ManagementPage, management_label, status_kind)
from ..widgets.notice_bar import NoticeBar
from ..services.model_task_runner import ModelTaskRunner
from ...application.runtime_bootstrap import python_tag
from ...application.system_profile import memory_status
from ...core.contracts.inference import CPU


class RuntimeManagerPage(QWidget, ThemedI18nMixin):
    restart_requested = Signal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._rows = ()
        self._libraries = ()
        self._detail_key = ""
        self._init_themed_i18n()
        self.destroyed.connect(self._cleanup_themed_i18n)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._page = ManagementPage(self)
        layout.addWidget(self._page)
        self._advice = []
        for _ in range(2):
            notice = NoticeBar("info", parent=self)
            notice.action_triggered.connect(lambda target=notice.action_button: self._advice_action(target))
            self._page.body_layout.addWidget(notice)
            self._advice.append(notice)
        preference = QHBoxLayout()
        self._preference_label = management_label(self, "managementCaption", wrap=False)
        self.preference = AppleDropDown(self)
        for value in ("auto", CPU):
            self.preference.addItem("", value)
        preference.addWidget(self._preference_label)
        preference.addWidget(self.preference, 1)
        self._page.body_layout.addLayout(preference)
        self._info = ManagementInfo(("version", "download", "installed", "backend", "license", "source"), self)
        self._page.body_layout.addWidget(self._info)
        self._profile_card = CollapsibleCard("", self, is_expanded=False)
        self.profile = management_label(self, "managementCaption", selectable=True)
        self._profile_card.add_widget(self.profile)
        self._page.body_layout.addWidget(self._profile_card)
        self._files = CollapsibleCard("", self, is_expanded=False)
        self.details = management_label(self, "managementCaption", selectable=True)
        self._files.add_widget(self.details)
        self._page.body_layout.addWidget(self._files)
        self.progress = QProgressBar(self)
        self._page.footer_layout.addWidget(self.progress)
        buttons = QGridLayout()
        buttons.setSpacing(px(SPACING_MD))
        self.actions = {}
        for index, action in enumerate(("download", "import", "toggle", "uninstall", "cancel", "restart")):
            button_type = ApplePrimaryButton if action == "download" else AppleDestructiveButton if action == "uninstall" else AppleButton
            button = button_type(parent=self)
            button.clicked.connect(lambda checked=False, name=action: self._act(name))
            buttons.addWidget(button, index // 2, index % 2)
            self.actions[action] = button
        self._page.footer_layout.addLayout(buttons)
        self.preference.currentIndexChanged.connect(self._preference_changed)
        self._page.navigation.selection_changed.connect(self._details)
        self.runner = ModelTaskRunner(service, self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh)
        self.runner.changed.connect(lambda: self._timer.start() if not self._timer.isActive() else None)
        self.retranslate_ui()
        service.refresh_profile()

    def retranslate_ui(self):
        self._preference_label.setText(self._tr("runtime.preference"))
        self.preference.setAccessibleName(self._tr("runtime.preference"))
        blocked = self.preference.blockSignals(True)
        for index in range(self.preference.count()):
            self.preference.setItemText(index, self._tr("runtime.preference." + self.preference.itemData(index)))
        self.preference.blockSignals(blocked)
        self._profile_card.set_title(self._tr("management.system_profile"))
        self._files.set_title(self._tr("management.files"))
        for name, button in self.actions.items():
            if name != "toggle":
                button.setText(self._tr("runtime.action." + name))
        self.refresh()

    @staticmethod
    def _megabytes(size):
        return f"{size / 1e6:.1f} MB"

    def _state_key(self, item):
        if item.descriptor.file_for(python_tag()) is None and item.state == "missing":
            return "runtime.state.unsupported"
        if item.state == "installed":
            if item.active and item.enabled:
                return "runtime.state.active"
            if item.active:
                return "runtime.state.restart_disable"
            return "runtime.state.restart_enable" if item.enabled else "runtime.state.disabled"
        return "runtime.state." + item.state

    def refresh(self):
        selected = self._page.navigation.selected_key()
        self._rows = self.service.list()
        self._libraries = self.service.libraries(("onnxruntime", "mediapipe"))
        if not selected and self._rows:
            selected = "runtime:" + self._rows[0].descriptor.token
        profile = self.service.profile()
        self._show_profile(profile)
        self._show_advice(profile)
        blocked = self.preference.blockSignals(True)
        self.preference.setCurrentIndex(max(0, self.preference.findData(self.service.preference())))
        self.preference.blockSignals(blocked)
        libraries = []
        for library in self._libraries:
            state_key = "runtime.library.state." + library.state if library.state in ("restart", "update") else "runtime.state." + library.state
            libraries.append(ManagementListItem("library:" + library.runtime,
                self._tr("runtime.library." + library.runtime),
                self._tr(state_key) + " · " + self._tr("runtime.library.source." + library.source), "settings"))
        runtimes = []
        for item in self._rows:
            runtimes.append(ManagementListItem("runtime:" + item.descriptor.token,
                item.descriptor.name, self._tr(self._state_key(item)), "settings"))
        groups = []
        if libraries:
            groups.append(ManagementListGroup("libraries", self._tr("management.libraries"), tuple(libraries)))
        if runtimes:
            groups.append(ManagementListGroup("runtimes", self._tr("management.runtimes"), tuple(runtimes)))
        if self._page.navigation.set_groups(tuple(groups), selected):
            self._page.ensure_selection_visible()
        self._details()
        if self.service.catalog_error:
            self._page.set_notice(self._tr(self.service.catalog_error), "warning")
        elif profile is not None and profile.error:
            self._page.set_notice(self._tr(profile.error), "warning")

    def _show_profile(self, profile):
        if self.service.catalog_error:
            self.profile.setText(self._tr(self.service.catalog_error))
            return
        if profile is None:
            self.profile.setText(self._tr("runtime.profile.detecting"))
            return
        if profile.error:
            self.profile.setText(self._tr(profile.error))
            return
        gib = 1024 ** 3
        lines = [self._tr("runtime.profile.cpu_cores" if profile.physical_cores else "runtime.profile.cpu",
                          name=profile.cpu, cores=profile.physical_cores, threads=profile.logical_processors)]
        # 占用与可用是打开这一页时的读数
        memory = memory_status()
        if memory is None:
            lines.append(self._tr("runtime.profile.memory", size=f"{profile.memory_bytes / gib:.1f}"))
        else:
            lines.append(self._tr("runtime.profile.memory_live", size=f"{memory.total_bytes / gib:.1f}",
                                  used=f"{memory.used_bytes / gib:.1f}", free=f"{memory.available_bytes / gib:.1f}"))
        for adapter in profile.adapters:
            lines.append(self._tr("runtime.profile.gpu" if adapter.directx12 else "runtime.profile.gpu_legacy",
                                  name=adapter.name, memory=f"{adapter.dedicated_bytes / 1024 ** 3:.1f}"))
        if not profile.adapters:
            lines.append(self._tr("runtime.profile.no_gpu"))
        status = self.service.status()
        if status is not None:
            lines.append(self._tr("inference.backend", name=self._tr("inference.backend." + status.backend)))
        if self.service.activation.reason:
            lines.append(self._tr(self.service.activation.reason))
        self.profile.setText("\n".join(lines))

    def _show_advice(self, profile):
        advice = self.service.advice() if profile is not None and not self.service.catalog_error else ()
        for index, notice in enumerate(self._advice):
            item = advice[index] if index < len(advice) else None
            notice.setVisible(item is not None)
            if item is not None:
                notice.set_text(self._tr(item.message_key, **dict(item.params)))
                notice.action_button.setProperty("advice_action", item.action)
                notice.set_action_text(self._tr("runtime.action." + ("download" if item.action == "install" else item.action)) if item.action else "")

    def _selected(self):
        key = self._page.navigation.selected_key()
        return next((item for item in self._rows if "runtime:" + item.descriptor.token == key), None)

    def select_runtime(self, token_or_name):
        self.refresh()
        for library in self._libraries:
            if library.runtime == token_or_name:
                return self._page.select_key("library:" + library.runtime)
        for item in self._rows:
            if item.descriptor.token == token_or_name:
                return self._page.select_key("runtime:" + item.descriptor.token)
        return False

    def _details(self):
        item = self._selected()
        self.actions["restart"].setEnabled(self.service.restart_required)
        self.actions["restart"].setVisible(self.service.restart_required)
        self.actions["cancel"].setVisible(item is not None and item.state in ("queued", "downloading", "verifying", "removing"))
        for name in ("download", "import", "toggle", "uninstall"):
            self.actions[name].setVisible(item is not None)
        self.actions["toggle"].setText(self._tr("runtime.action.disable" if item is not None and item.enabled
                                                else "runtime.action.enable"))
        self.progress.hide()
        key = self._page.navigation.selected_key()
        if key != self._detail_key:
            self._detail_key = key
            self._files.set_expanded(False)
            self._page.scroll.verticalScrollBar().setValue(0)
        self._page.set_notice()
        self._files.setVisible(item is not None)
        if item is None:
            for name, button in self.actions.items():
                if name != "restart":
                    button.setEnabled(False)
            library = next((value for value in self._libraries if "library:" + value.runtime == key), None)
            self._info.setVisible(library is not None)
            self._page.badge.setVisible(library is not None)
            self.details.clear()
            if library is not None:
                state_key = "runtime.library.state." + library.state if library.state in ("restart", "update") else "runtime.state." + library.state
                name = self._tr("runtime.library." + library.runtime)
                source = self._tr("runtime.library.source." + library.source)
                self._page.title.setText(name)
                self._page.subtitle.setText(source)
                self._page.badge.set_status(status_kind(library.state), self._tr(state_key))
                for name in ("version", "download", "installed", "backend", "license", "source"):
                    label = self._tr("models.column." + name) if name in ("version", "download", "installed") else self._tr("management." + name)
                    value = (library.version or "—") if name == "version" else source
                    self._info.set_row(name, label, value, visible=name in ("version", "source"))
                if library.available:
                    self._page.set_notice(self._tr("runtime.library.readonly", name=self._tr("runtime.library." + library.runtime), version=library.version or "—"))
                if library.error:
                    self._page.set_notice(self._tr(library.error), "warning")
                elif not library.available:
                    self._page.set_notice(self._tr(state_key), "warning")
            else:
                self._page.title.setText(self._tr("runtime.title"))
                self._page.subtitle.setText(self._tr("management.runtimes_empty"))
            return
        self._info.show()
        self._page.badge.show()
        descriptor = item.descriptor
        state_key = self._state_key(item)
        self._page.title.setText(descriptor.name)
        self._page.subtitle.setText(self._tr("inference.backend." + descriptor.backend))
        self._page.badge.set_status(status_kind(state_key.rsplit(".", 1)[-1]), self._tr(state_key))
        file = item.descriptor.file_for(python_tag())
        busy = item.state in ("queued", "downloading", "verifying", "uninstalling")
        installable = file is not None and not busy and not item.active and item.state not in ("installed", "removing")
        self.actions["download"].setEnabled(installable)
        self.actions["import"].setEnabled(installable)
        self.actions["cancel"].setEnabled(item.state in ("queued", "downloading", "verifying", "removing"))
        self.actions["uninstall"].setEnabled(item.state in ("installed", "corrupt", "failed"))
        self.actions["toggle"].setEnabled(item.state == "installed")
        values = {"version": descriptor.version,
            "download": self._megabytes(file.size) if file is not None else "—",
            "installed": self._megabytes(item.installed_bytes) if item.installed_bytes else "—",
            "backend": self._tr("inference.backend." + descriptor.backend),
            "license": descriptor.license, "source": descriptor.source}
        for name, value in values.items():
            label = self._tr("models.column." + name) if name in ("version", "download", "installed") else self._tr("management." + name)
            self._info.set_row(name, label, value)
        text = descriptor.token
        if file is not None:
            text += f"\n{file.name}\nSHA-256: {file.sha256}"
        if item.path:
            text += "\n" + item.path
        self.details.setText(text)
        if file is None:
            self._page.set_notice(self._tr("runtime.state.unsupported"), "warning")
        if item.error:
            self._page.set_notice(self._tr(item.error), "warning")
        elif item.state in ("installed", "removing") and (item.enabled != item.active or item.state == "removing"):
            self._page.set_notice(self._tr(state_key))
        if busy:
            self.progress.setVisible(True)
            if item.state == "downloading" and file is not None:
                self.progress.setRange(0, file.size)
                self.progress.setValue(item.downloaded)
                self.progress.setFormat(f"{item.downloaded / 1e6:.1f} / {self._megabytes(file.size)}")
            else:
                self.progress.setRange(0, 0)

    def _advice_action(self, button):
        action = button.property("advice_action")
        if action == "restart":
            self.restart_requested.emit()
        elif action == "install":
            item = next((row for row in self._rows if row.descriptor.file_for(python_tag()) is not None), None)
            if item is not None:
                self.service.install(item.descriptor.token)

    def _preference_changed(self, index):
        if not self.service.set_preference(self.preference.itemData(index)):
            self.refresh()

    def _act(self, name):
        if name == "restart":
            self.restart_requested.emit()
            return
        item = self._selected()
        if item is None:
            return
        token = item.descriptor.token
        if name == "download":
            self.service.install(token)
        elif name == "cancel":
            if item.state == "removing":
                self.service.cancel_removal(token)
            else:
                self.service.cancel(token)
        elif name == "toggle":
            self.service.set_enabled(token, not item.enabled)
        elif name == "import":
            path, _ = QFileDialog.getOpenFileName(self, self._tr("runtime.action.import"), "",
                                                 self._tr("runtime.import_filter"))
            if path:
                self.service.install(token, import_path=path)
        elif name == "uninstall":
            from ..widgets.message_dialog import show_confirm
            if show_confirm(self, self._tr("runtime.action.uninstall"), self._tr(
                    "runtime.uninstall_confirm", name=item.descriptor.name, version=item.descriptor.version,
                    size=f"{(item.installed_bytes or item.descriptor.installed_size) / 1e6:.1f}"), default_yes=False):
                self.service.uninstall(token)

    def close_subscription(self):
        self._timer.stop()
        self.runner.close()
        self._cleanup_themed_i18n()
