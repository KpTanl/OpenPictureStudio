"""设置窗口里的“工具”页，以及安装前确认固定清单的对话框。"""

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QProgressBar, QScrollArea, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_SM, SPACING_LG, SPACING_XL, px
from ...application.tool_resource_service import ToolResourceService
from ...core.contracts.models import ModelKey
from ..base_window import BaseDialog
from ..panels.registry import PANEL_TYPES, tool_entry_for
from ..services.model_task_runner import ModelTaskRunner
from ..themed_mixin import ThemedI18nMixin
from ..widgets.apple_button import AppleButton, AppleCompactButton, AppleGhostButton, ApplePrimaryButton
from ..widgets.action_button import plain_action_text
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.management_page import (ManagementListGroup, ManagementListItem, ManagementPage,
                                       management_label, status_kind)
from ..widgets.message_dialog import show_error, show_warning
from ..widgets.notice_bar import NoticeBar
from ..widgets.status_badge import StatusBadge
from ..widgets.toggle_switch import ToggleSwitch


def _text_label(text, parent):
    label = management_label(parent, selectable=True)
    label.setText(text)
    return label


class _ResourceRow(QWidget):
    view_requested = pyqtSignal(object)
    cancel_requested = pyqtSignal(object)

    def __init__(self, resource, parent=None):
        super().__init__(parent)
        self._resource = resource
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        row = QHBoxLayout()
        self._name = management_label(self, "managementSubheading")
        row.addWidget(self._name, 1)
        self._view = AppleCompactButton(parent=self)
        self._view.clicked.connect(lambda checked=False: self.view_requested.emit(self._resource))
        row.addWidget(self._view)
        layout.addLayout(row)
        summary = QHBoxLayout()
        self._badge = StatusBadge(parent=self)
        summary.addWidget(self._badge)
        self._summary = management_label(self, "managementCaption")
        summary.addWidget(self._summary, 1)
        layout.addLayout(summary)
        self._error = NoticeBar("warning", parent=self)
        layout.addWidget(self._error)
        self._progress_row = QWidget(self)
        progress_layout = QHBoxLayout(self._progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        self._progress = QProgressBar(self._progress_row)
        progress_layout.addWidget(self._progress, 1)
        self._cancel = AppleButton(parent=self._progress_row)
        self._cancel.clicked.connect(lambda checked=False: self.cancel_requested.emit(self._resource))
        progress_layout.addWidget(self._cancel)
        layout.addWidget(self._progress_row)

    def set_resource(self, resource, *, name, state, summary, error,
                     view_text, cancel_text, view_enabled, progress_format):
        self._resource = resource
        self._name.setText(name)
        self._badge.set_status(status_kind(resource.state), state)
        self._summary.setText(summary)
        self._error.set_text(error)
        self._error.setVisible(bool(error))
        self._view.setText(view_text)
        self._view.setEnabled(view_enabled)
        self._cancel.setText(cancel_text)
        active = resource.state in ("queued", "downloading", "verifying", "waiting")
        self._cancel.setEnabled(active)
        self._progress_row.setVisible(active)
        if resource.state == "downloading" and resource.download_size:
            self._progress.setRange(0, 10000)
            self._progress.setValue(min(10000, resource.downloaded * 10000 // resource.download_size))
            self._progress.setFormat(progress_format)
        else:
            self._progress.setRange(0, 0)


class ResourceInstallDialog(BaseDialog):
    def __init__(self, plan, parent=None):
        super().__init__(title_key="tool_manager.install_title", min_width=480, min_height=320,
                         default_width=640, default_height=480, parent=parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_XL))
        layout.setSpacing(px(SPACING_LG))
        scroll = QScrollArea(self)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        entries = QVBoxLayout(content)
        for item in plan.items:
            text = self._tr("tool_manager.install_item", name=item.name, token=item.token,
                version=item.version, digest=item.digest, source=item.source,
                license=item.license, size=f"{item.download_size / 1e6:.1f}")
            if item.requires_restart:
                text += "\n" + self._tr("tool_manager.restart_required")
            entries.addWidget(_text_label(text, content))
        entries.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        layout.addWidget(_text_label(self._tr("tool_manager.install_total",
            size=f"{sum(item.download_size for item in plan.items) / 1e6:.1f}"), self))
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = AppleButton(self._tr("button.cancel"), self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        install = ApplePrimaryButton(self._tr("tool_manager.install_confirm"), self)
        install.setDefault(True)
        install.clicked.connect(self.accept)
        buttons.addWidget(install)
        layout.addLayout(buttons)


class ToolManagerPage(QWidget, ThemedI18nMixin):
    """左侧是工具列表，右侧是所选工具的功能、所需资源与安装入口。

    工具的启停点了就提交，停用的工具经 tools_changed 报告；安装前确认固定清单并复核当前图片、
    参数与资源身份。查看某个模型或运行环境时发出请求，由所在的设置窗口切到对应的页。
    """

    tools_changed = pyqtSignal(tuple)
    model_view_requested = pyqtSignal(str)
    runtime_view_requested = pyqtSignal(str)

    def __init__(self, core, parent=None, *, runtime_service=None, context_provider=None, tool_id=None):
        super().__init__(parent)
        self._core = core
        self._runtime_service = runtime_service
        self._resources = ToolResourceService(core.tools, core.models, runtime_service)
        self._context_provider = context_provider
        self._context_identity = ()
        self._workflow = None
        self._rows = ()
        self._detail_signature = None
        self._detail_tool = None
        self._detail_headers = []
        self._detail_features = []
        self._detail_runtime_entries = {}
        self._detail_resource_card = None
        self._detail_resource_text = None
        self._detail_scroll_value = 0
        self._refreshing = False
        self._requested_tool = tool_id
        self._init_themed_i18n()
        self.destroyed.connect(self._cleanup_themed_i18n)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._tool_page = ManagementPage(self)
        layout.addWidget(self._tool_page)
        self._tool_page.navigation.selection_changed.connect(self._details)
        self._enable_label = management_label(self, "managementCaption", wrap=False)
        self._toggle = ToggleSwitch(self)
        self._toggle.stateChanged.connect(self._enabled_changed)
        self._tool_page.header_actions.addWidget(self._enable_label)
        self._tool_page.header_actions.addWidget(self._toggle)
        self._install_button = ApplePrimaryButton(parent=self)
        self._install_button.clicked.connect(self._install_selected)
        self._tool_page.footer_layout.addWidget(self._install_button)
        # 列表下方：合计占用、推理后端与数据目录
        self._total = management_label(self, "managementCaption")
        self._backend = management_label(self, "managementCaption")
        self._directory = AppleGhostButton(parent=self)
        self._directory.clicked.connect(self._open_directory)
        self._tool_page.navigation_layout.addWidget(self._total)
        self._tool_page.navigation_layout.addWidget(self._backend)
        self._tool_page.navigation_layout.addWidget(self._directory, 0, Qt.AlignmentFlag.AlignLeft)
        self._detail_scroll = self._tool_page.scroll
        self._detail_restore_timer = QTimer(self)
        self._detail_restore_timer.setSingleShot(True)
        self._detail_restore_timer.timeout.connect(self._restore_detail_scroll)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh)
        self._runners = [ModelTaskRunner(core.models, self)]
        if runtime_service is not None:
            self._runners.append(ModelTaskRunner(runtime_service, self))
        for runner in self._runners:
            runner.changed.connect(self._schedule_refresh)
        self._context_timer = QTimer(self)
        self._context_timer.setInterval(400)
        self._context_timer.timeout.connect(self._check_context)
        self._context_timer.start()
        self.retranslate_ui()

    def select_tool(self, tool_id):
        """定位到某个工具；列表里没有它时保持当前选择。"""
        self._requested_tool = tool_id
        return self._tool_page.select_key(tool_id)

    def selected_tool(self):
        row = self._selected()
        return row.tool_id if row else None

    def _name(self, tool_id):
        key = PANEL_TYPES.title_key(tool_id)
        return plain_action_text(self._tr(key, tool_id)) if key else tool_id

    def _context(self):
        return self._context_provider() if self._context_provider else ((), None)

    def _schedule_refresh(self):
        if not self._timer.isActive():
            self._timer.start()

    def _check_context(self):
        identity, _ = self._context()
        if identity != self._context_identity:
            self._schedule_refresh()

    def retranslate_ui(self):
        self._install_button.setText(self._tr("tool_manager.install_required"))
        self._directory.setText(self._tr("tool_manager.open_directory"))
        self.refresh()

    def _selected(self):
        key = self._tool_page.navigation.selected_key()
        return next((row for row in self._rows if row.tool_id == key), None)

    def refresh(self):
        previous = self._selected()
        selected_id = previous.tool_id if previous else self._requested_tool
        self._context_identity, self._workflow = self._context()
        overview = self._resources.overview(self._workflow)
        self._rows = overview.rows
        if not selected_id:
            selected_id = next((row.tool_id for row in self._rows
                if any(feature.state != "ready" for feature in row.contextual)), None)
        self._refreshing = True
        groups = {"editing": [], "intelligent": []}
        for row in self._rows:
            state = self._tr("tool_manager.state." + row.state)
            entry = tool_entry_for(row.tool_id)
            item = ManagementListItem(row.tool_id, self._name(row.tool_id),
                state + f" · {row.installed_bytes / 1e6:.1f} MB", entry.icon if entry else "settings")
            group = "intelligent" if any(feature.resources for feature in row.features) else "editing"
            groups[group].append(item)
        if self._tool_page.navigation.set_groups(tuple(ManagementListGroup(key,
                self._tr("management.group." + key), tuple(items)) for key, items in groups.items() if items), selected_id):
            self._tool_page.ensure_selection_visible()
        self._refreshing = False
        self._total.setText(self._tr("tool_manager.total_size", size=f"{overview.total_bytes / 1e6:.1f}")
                            + "\n" + self._tr("management.shared_resources"))
        self._backend.setText(self._tr("inference.backend", name=self._tr("inference.backend." + self._core.inference.status().backend)))
        self._details()

    def _enabled_changed(self, enabled):
        """启停点了就提交：把这次改动后的停用清单交给内核，成功后报告给所属方。"""
        row = self._selected()
        if self._refreshing or row is None or not row.can_disable:
            return
        disabled = tuple(item.tool_id for item in self._rows if item.can_disable
                         and not (enabled if item.tool_id == row.tool_id else item.enabled))
        result = self._core.restore_disabled_tools(list(disabled))
        if result.success:
            self.tools_changed.emit(disabled)
        else:
            show_error(self, self._tr("tool_manager.title"), self._tr(result.error.message_key))
        self.refresh()

    def _install_selected(self):
        row = self._selected()
        if row is not None:
            self._install(row.tool_id)

    def _resource_state(self, resource):
        if resource.state in ("legacy_unpinned", "incompatible"):
            return self._tr("tool_manager.state." + resource.state)
        prefix = "runtime.library.state." if resource.kind == "runtime" and resource.state in ("restart", "update") else (
            "runtime.state." if resource.kind == "runtime" else "models.state.")
        return self._tr(prefix + resource.state)

    def _details(self):
        if self._refreshing:
            return
        row = self._selected()
        self._toggle.setVisible(row is not None and row.can_disable)
        self._enable_label.setVisible(row is not None and row.can_disable)
        self._tool_page.badge.setVisible(row is not None)
        self._tool_page.set_notice()
        groups = []
        if row is not None:
            enabled = row.enabled
            self._tool_page.title.setText(self._name(row.tool_id))
            self._tool_page.subtitle.setText(" · ".join(self._tr("tool_manager.feature." + feature.feature_id, feature.feature_id) for feature in row.features))
            self._toggle.set_checked_silent(enabled)
            self._toggle.setAccessibleName(self._tr("management.enable_tool", name=self._name(row.tool_id)))
            self._enable_label.setText(self._tr("management.enabled" if enabled else "management.disabled"))
            state = self._tr("management.always_enabled") if not row.can_disable else self._tr("tool_manager.state." + row.state)
            self._tool_page.badge.set_status(status_kind(row.state), state)
            installable = any(resource.installable for feature in self._resources.features_for_install(row) for resource in feature.resources)
            self._install_button.setEnabled(row.enabled and installable and self._context_identity is not None)
            if self._context_identity is None:
                self._tool_page.set_notice(self._tr("tool_manager.invalid_context"), "warning")
            elif not row.can_disable:
                self._tool_page.set_notice(self._tr("management.native_hint"))
            elif not enabled:
                self._tool_page.set_notice(self._tr("management.disabled_hint"))
            groups = [("tool_manager.current_features", row.features)]
            if row.contextual:
                groups.append(("tool_manager.request_features", row.contextual))
        else:
            self._install_button.setEnabled(False)
            self._tool_page.title.setText(self._tr("tool_manager.tools"))
            self._tool_page.subtitle.clear()
        tool_id = row.tool_id if row else None
        signature = (tool_id, tuple((key, tuple((feature.feature_id,
            feature.implementation_id, feature.implementation_version,
            tuple((resource.kind, resource.identity, resource.role) for resource in feature.resources))
            for feature in features)) for key, features in groups))
        content = None
        if signature != self._detail_signature:
            content = self._build_details(tool_id, groups)
            self._detail_signature = signature
        for (_, card), (title_key, _) in zip(self._detail_headers, groups):
            card.set_title(self._tr(title_key))
        features = (feature for _, items in groups for feature in items)
        for (label, badge, info, free, entries), feature in zip(self._detail_features, features):
            title = self._tr("tool_manager.feature." + feature.feature_id, feature.feature_id)
            badge.set_status(status_kind(feature.state), self._tr("tool_manager.state." + feature.state))
            details = []
            if feature.issue:
                details.append(self._issue_text(feature.issue))
            label.setText(title)
            info.setText("\n".join(details))
            info.setVisible(bool(details))
            if free is not None:
                free.setText(self._tr("tool_manager.resource_free"))
            for entry, resource in zip(entries, (value for value in feature.resources if value.kind != "runtime")):
                self._project_resource(entry, resource)
        identities = []
        for group_key, features in groups:
            runtimes = {resource.identity: resource for feature in features for resource in feature.resources if resource.kind == "runtime"}
            runtime_card, entries = self._detail_runtime_entries[group_key]
            if runtime_card is not None:
                runtime_card.set_title(self._tr("runtime.title"))
            for entry, resource in zip(entries, runtimes.values()):
                self._project_resource(entry, resource)
            for feature in features:
                if not feature.resources and not feature.implementation_id:
                    continue
                text = self._tr(group_key) + " · " + self._tr("tool_manager.feature." + feature.feature_id, feature.feature_id)
                if feature.implementation_id:
                    text += f"\n{feature.implementation_id} {feature.implementation_version}"
                text += "".join("\n" + self._resource_text(resource, tool_id) for resource in feature.resources)
                identities.append(text)
        self._detail_resource_card.set_title(self._tr("management.resource_details"))
        self._detail_resource_card.setVisible(bool(identities))
        self._detail_resource_text.setText("\n\n".join(identities))
        if content is not None:
            self._tool_page.replace_body(content)
            content.layout().activate()
            self._detail_restore_timer.start(0)

    def _project_resource(self, entry, resource):
        name = self._tr("runtime.library." + resource.name, resource.name) if resource.kind == "runtime" else resource.name
        summary = f"{resource.installed_bytes / 1e6:.1f} MB"
        if resource.version:
            summary = resource.version + " · " + summary
        if resource.kind == "runtime" and resource.source:
            summary += " · " + self._tr("runtime.library.source." + resource.source, resource.source)
        entry.set_resource(resource, name=name, state=self._resource_state(resource), summary=summary,
            error=self._issue_text(resource.error) if resource.error else "",
            view_text=self._tr("tool_manager.view_" + resource.kind),
            cancel_text=self._tr("models.action.cancel"),
            view_enabled=bool(resource.token or resource.runtime)
                and (resource.kind == "model" or self._runtime_service is not None),
            progress_format=f"{resource.downloaded / 1e6:.1f} / {resource.download_size / 1e6:.1f} MB")

    def _build_details(self, tool_id, groups):
        same_tool = tool_id == self._detail_tool
        self._detail_scroll_value = self._detail_scroll.verticalScrollBar().value() if same_tool else 0
        sections = {key: card.is_expanded() for key, card in self._detail_headers} if same_tool else {}
        runtime_sections = {key: card.is_expanded() for key, (card, _) in self._detail_runtime_entries.items() if card is not None} if same_tool else {}
        resources_expanded = same_tool and self._detail_resource_card is not None and self._detail_resource_card.is_expanded()
        self._detail_tool = tool_id
        self._detail_headers = []
        self._detail_features = []
        self._detail_runtime_entries = {}
        content = QWidget(self._detail_scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_LG))
        for group_key, features in groups:
            header = CollapsibleCard("", content, is_expanded=sections.get(group_key, True))
            layout.addWidget(header)
            self._detail_headers.append((group_key, header))
            for feature in features:
                title = QHBoxLayout()
                label = management_label(content, "managementSubheading")
                title.addWidget(label, 1)
                badge = StatusBadge(parent=content)
                title.addWidget(badge)
                header.add_layout(title)
                info = management_label(content, "managementCaption", selectable=True)
                header.add_widget(info)
                free = None
                if not feature.resources:
                    free = management_label(content, "managementCaption")
                    header.add_widget(free)
                entries = []
                for resource in feature.resources:
                    if resource.kind == "runtime":
                        continue
                    entry = _ResourceRow(resource, content)
                    entry.view_requested.connect(self._view_resource)
                    entry.cancel_requested.connect(self._cancel_resource)
                    header.add_widget(entry)
                    entries.append(entry)
                self._detail_features.append((label, badge, info, free, entries))
            runtimes = {resource.identity: resource for feature in features for resource in feature.resources if resource.kind == "runtime"}
            runtime_card = None
            entries = []
            if runtimes:
                runtime_card = CollapsibleCard("", content, is_expanded=runtime_sections.get(group_key, True))
                layout.addWidget(runtime_card)
                for resource in runtimes.values():
                    entry = _ResourceRow(resource, content)
                    entry.view_requested.connect(self._view_resource)
                    entry.cancel_requested.connect(self._cancel_resource)
                    runtime_card.add_widget(entry)
                    entries.append(entry)
            self._detail_runtime_entries[group_key] = (runtime_card, entries)
        self._detail_resource_card = CollapsibleCard("", content, is_expanded=bool(resources_expanded))
        self._detail_resource_text = management_label(content, "managementCaption", selectable=True)
        self._detail_resource_card.add_widget(self._detail_resource_text)
        layout.addWidget(self._detail_resource_card)
        return content

    def _restore_detail_scroll(self):
        self._detail_scroll.verticalScrollBar().setValue(self._detail_scroll_value)

    def _issue_text(self, issue):
        key = {"TOOL_LEGACY_UNPINNED": "error.tool.legacy_unpinned", "TOOL_INCOMPATIBLE": "error.tool.incompatible",
               "PARAMETER_INVALID": "tool_manager.invalid_context", "TOOL_DISABLED": "error.tool.disabled",
               "legacy_resource": "tool_manager.legacy_restore"}.get(issue, issue)
        return self._tr(key)

    def _resource_text(self, resource, tool_id):
        name = self._tr("runtime.library." + resource.name, resource.name) if resource.kind == "runtime" else resource.name
        text = f"{name} · {self._resource_state(resource)} · {resource.installed_bytes / 1e6:.1f} MB"
        if resource.role:
            text += "\n" + self._tr("tool_manager.resource_role", role=self._tr("tool_manager.role." + resource.role, resource.role))
        if resource.version:
            text += "\n" + self._tr("tool_manager.resource_version", version=resource.version)
        if resource.token:
            text += "\n" + resource.token
        if resource.digest:
            text += "\nSHA-256: " + resource.digest
        if resource.source:
            source = self._tr("runtime.library.source." + resource.source, resource.source) if resource.kind == "runtime" else resource.source
            text += "\n" + source
        if resource.license:
            text += "\n" + resource.license
        others = tuple(value for value in resource.shared_with if value != tool_id)
        if others:
            text += "\n" + self._tr("tool_manager.shared", names=", ".join(self._name(value) for value in others))
        if resource.path:
            text += "\n" + resource.path
        if resource.temporary_bytes:
            text += "\n" + self._tr("models.temporary", size=f"{resource.temporary_bytes / 1e6:.1f}")
        if resource.error:
            text += "\n" + self._issue_text(resource.error)
        return text

    def _view_resource(self, resource):
        if resource.kind == "model":
            self.model_view_requested.emit(resource.token)
        elif self._runtime_service is not None:
            self.runtime_view_requested.emit(resource.token or resource.runtime)

    def _cancel_resource(self, resource):
        if resource.kind == "model":
            self._core.models.cancel(ModelKey.parse(resource.token))
        elif self._runtime_service is not None and resource.token:
            self._runtime_service.cancel(resource.token)

    def _install(self, tool_id):
        identity, workflow = self._context()
        if identity is None:
            show_error(self, self._tr("tool_manager.title"), self._tr("tool_manager.invalid_context"))
            return
        plan = self._resources.plan(tool_id, workflow, identity)
        if not plan.items:
            show_warning(self, self._tr("tool_manager.install_title"), self._tr("tool_manager.no_install_items"))
            return
        dialog = ResourceInstallDialog(plan, self)
        confirmed = dialog.exec()
        dialog.deleteLater()
        if not confirmed:
            return
        current_identity, current_workflow = self._context()
        if current_identity is None:
            show_error(self, self._tr("tool_manager.title"), self._tr("tool_manager.context_changed"))
            return
        result = self._resources.install(plan, current_workflow, current_identity)
        if not result.success:
            show_error(self, self._tr("tool_manager.title"), self._tr(result.error.message_key))
        self.refresh()

    def _open_directory(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._core.models.root.parent)))

    def close_subscription(self):
        self._timer.stop()
        self._detail_restore_timer.stop()
        self._context_timer.stop()
        for runner in self._runners:
            runner.close()
        self._cleanup_themed_i18n()
