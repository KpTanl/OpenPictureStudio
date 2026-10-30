"""设置窗口：左侧导航，右侧是表单页，以及工具、模型、运行环境与关于。

每一项改了就生效：窗口只发出用户编辑的信号，读写与应用由打开它的一方承担，需要时再把结果投影回来。
缩略图缓存与编辑缓存的占用、清除经各自的维护运行器。工具、模型、运行环境三页要用编辑内核，
用到时才向打开它的一方请求，内核就绪前这三页只显示状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from PySide6.QtCore import QKeyCombination, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea,
                             QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from resources.styles.theme_variables import (CONTROL_HEIGHT, DIALOG_SIZES, SPACING_LG, SPACING_MD, SPACING_XL,
                                              SPACING_XS, px)
from ...application.image_display_settings import ImageDisplaySettings
from ...application.memory_settings import GIB, MemorySettings, MemoryState
from ...application.studio_preload import StudioPreloadSettings
from ...core.contracts.image_display import CanvasBackground, NonRawDisplayMode, RawDisplayMode
from ..base_window import STARTUP_LAST, STARTUP_MAXIMIZED, STARTUP_SIZES, BaseDialog
from .about_page import AboutPage
from ..theme import ThemeManager
from ..window_actions import ShortcutRow
from ..widgets.apple_button import AppleButton, AppleCompactButton
from ..widgets.apple_menu import AppleDropDown
from ..widgets.apple_spin_box import AppleSpinBox
from ..widgets.empty_state import EmptyState
from ..widgets.management_page import ManagementList, ManagementListGroup, ManagementListItem
from ..widgets.message_dialog import confirm_destructive
from ..widgets.notice_bar import NoticeBar
from ..widgets.segmented_control import AppleSegmentedControl
from ..widgets.setting_row import SettingRow, SettingSection
from ..widgets.usage_bar import UsageBar
from ..widgets.toggle_switch import ToggleSwitch

_NAVIGATION_WIDTH = 232
_CHOICE_WIDTH = 200
_SHORTCUT_WIDTH = 150
_USAGE_WIDTH = 200
# 连续点加减或逐位键入时，停下这么久才生效一次
_APPLY_DELAY_MS = 350
_CACHE_POLL_MS = 1500
_MIB = 1024 ** 2


def _gigabytes(size: int) -> str:
    return f"{size / GIB:.1f}".removesuffix(".0")


@dataclass(frozen=True)
class SettingsValues:
    """打开设置窗口时各项的当前取值。"""

    language: str = "en"
    theme: str = ThemeManager.THEME_DARK
    image_display: ImageDisplaySettings = ImageDisplaySettings()
    preload: StudioPreloadSettings = StudioPreloadSettings()
    memory: MemoryState = MemoryState()
    shortcuts: tuple[ShortcutRow, ...] = ()
    startup_window: str = STARTUP_LAST
    check_updates: bool = True


class ShortcutEdit(QPushButton):
    """快捷键录入框：点一下开始录，按下组合键即录入；录入中按 Esc 放弃，按退格或删除清空。

    只有用户录入才发出 sequence_edited，显示的内容由 set_sequence 投影。
    """

    sequence_edited = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("shortcutEdit")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoDefault(False)
        self.setMinimumHeight(CONTROL_HEIGHT)
        self.setFixedWidth(_SHORTCUT_WIDTH)
        self._sequence = ""
        self._recording = False
        self._recording_text = ""
        self._empty_text = ""
        self.clicked.connect(lambda: self._set_recording(not self._recording))
        self._refresh()

    def sequence(self) -> str:
        return self._sequence

    def is_recording(self) -> bool:
        return self._recording

    def set_sequence(self, sequence: str) -> None:
        self._sequence = sequence
        self._set_recording(False)

    def set_texts(self, recording: str, empty: str) -> None:
        self._recording_text, self._empty_text = recording, empty
        self._refresh()

    def _set_recording(self, recording: bool) -> None:
        self._recording = recording
        self._refresh()

    def _refresh(self) -> None:
        shown = QKeySequence(self._sequence).toString(QKeySequence.SequenceFormat.NativeText)
        text = self._recording_text if self._recording else shown or self._empty_text
        self.setText(text.replace("&", "&&"))
        state = "recording" if self._recording else "empty" if not shown else "set"
        if self.property("state") != state:
            self.setProperty("state", state)
            self.style().unpolish(self)
            self.style().polish(self)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._recording:
            super().keyPressEvent(event)
            return
        event.accept()
        key = event.key()
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_unknown):
            return
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        plain = modifiers == Qt.KeyboardModifier.NoModifier
        self._set_recording(False)
        if plain and key == Qt.Key.Key_Escape:
            return
        if plain and key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self.sequence_edited.emit("")
            return
        try:
            sequence = QKeySequence(QKeyCombination(modifiers, Qt.Key(key)))
        except ValueError:
            return
        self.sequence_edited.emit(sequence.toString(QKeySequence.SequenceFormat.PortableText))

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if self._recording:
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:
        self._set_recording(False)
        super().focusOutEvent(event)


class _PathLabel(QLabel):
    """一行路径：放不下时省略中间，可选中、可复制完整路径。"""

    def __init__(self, path: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("settingRowPath")
        self._path = path
        self.copy_text = ""
        self.setToolTip(path)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.update_display()

    def update_display(self) -> None:
        self.setText(self.fontMetrics().elidedText(self._path, Qt.TextElideMode.ElideMiddle, self.width()))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update_display()

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            QApplication.clipboard().setText(self._path)
            event.accept()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addAction(self.copy_text, lambda: QApplication.clipboard().setText(self._path))
        menu.exec(event.globalPos())
        menu.deleteLater()


class _Page(QScrollArea):
    """一页设置：页标题、右上角的操作、一句说明、出错时的提示条，下面是各组表单行。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(self)
        content.setObjectName("settingsPage")
        self._body = QVBoxLayout(content)
        self._body.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_XL))
        self._body.setSpacing(px(SPACING_LG))
        header = QHBoxLayout()
        header.setSpacing(px(SPACING_MD))
        self.title = QLabel(content)
        self.title.setObjectName("settingsPageTitle")
        header.addWidget(self.title, 1)
        self.actions = QHBoxLayout()
        header.addLayout(self.actions)
        self._body.addLayout(header)
        self.hint = QLabel(content)
        self.hint.setObjectName("settingRowDescription")
        self.hint.setWordWrap(True)
        self.hint.hide()
        self._body.addWidget(self.hint)
        self.notice = NoticeBar("warning", parent=content)
        self.notice.hide()
        self._body.addWidget(self.notice)
        self._body.addStretch(1)
        self.setWidget(content)

    def add(self, widget: QWidget) -> QWidget:
        self._body.insertWidget(self._body.count() - 1, widget)
        return widget

    def remove(self, widget: QWidget) -> None:
        self._body.removeWidget(widget)
        widget.deleteLater()

    def set_hint(self, text: str) -> None:
        self.hint.setText(text)
        self.hint.setVisible(bool(text))

    def set_notice(self, text: str = "") -> None:
        self.notice.set_text(text)
        self.notice.setVisible(bool(text))


class _ResourceHolder(QWidget):
    """工具、模型、运行环境三页的位置：服务就绪前是忙碌的进度，准备不了或没有内容时是空状态，就绪后换成真正的页面。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._waiting = QWidget(self)
        waiting = QVBoxLayout(self._waiting)
        waiting.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_XL))
        self.status = UsageBar(self._waiting)
        self.status.set_progress(0, 0)
        waiting.addWidget(self.status, 0, Qt.AlignmentFlag.AlignTop)
        self.empty = EmptyState("alert-triangle", self._waiting)
        waiting.addWidget(self.empty, 1)
        self._layout.addWidget(self._waiting)
        self.page: Optional[QWidget] = None
        self.set_busy("")

    def set_busy(self, text: str) -> None:
        self.status.set_text(text)
        self.status.show()
        self.empty.hide()

    def set_empty(self, icon: str, text: str) -> None:
        self.empty.set_icon(icon)
        self.empty.set_text(text)
        self.empty.show()
        self.status.hide()

    def set_page(self, page: QWidget) -> None:
        self.page = page
        self._waiting.hide()
        self._layout.addWidget(page)


class SettingsDialog(BaseDialog):
    language_edited = Signal(str)
    theme_edited = Signal(str)
    startup_window_edited = Signal(str)
    image_display_edited = Signal(object)
    preload_edited = Signal(object)
    # 用户改了内存占用上限或保留的内存：新的 MemorySettings；生效后的结果经 set_memory() 投影回来
    memory_edited = Signal(object)
    shortcut_edited = Signal(str, str)
    shortcuts_reset_requested = Signal()
    # 用户第一次进入工具、模型或运行环境页：请打开它的一方经 set_resource_services() 给出服务
    resource_pages_requested = Signal()
    assistant_services_requested = Signal()
    # 工具页里启停了工具：停用的工具
    tools_changed = Signal(object)
    storage_manage_requested = Signal(str)
    update_enabled_edited = Signal(bool)

    PAGES = (
        ("general", "settings.tab.general", "General"),
        ("images", "settings.images.title", "Image display"),
        ("shortcuts", "settings.tab.shortcuts", "Shortcuts"),
        ("performance", "settings.tab.performance", "Performance & Cache"),
        ("storage", "storage.title", "Storage"),
        ("ai_services", "assistant.services_title", "AI Services"),
        ("file_association", "settings.tab.file_association", "File association"),
        ("tools", "tool_manager.tools", "Tools"),
        ("models", "models.title", "Models"),
        ("runtime", "runtime.title", "Runtime"),
        ("about", "settings.tab.about", "About"),
    )
    PAGE_ICONS = {"general": "adjustments-horizontal", "images": "photo", "shortcuts": "keyboard",
                  "performance": "gauge", "file_association": "folder-open", "tools": "sparkles",
                  "models": "stack-2", "runtime": "cpu", "about": "info-circle", "ai_services": "sparkles", "storage": "folder-open"}
    # 导航的分组：分组标题的文案键（空为不带标题）与其中的页
    NAVIGATION = (
        ("", "", ("general", "images", "shortcuts", "performance", "storage", "ai_services", "file_association")),
        ("tool_manager.title", "Tools & Models", ("tools", "models", "runtime")),
        ("", "", ("about",)),
    )
    RESOURCE_PAGES = ("tools", "models", "runtime")
    LANGUAGE_OPTIONS = (
        ("en", "settings.language.option.en", "English"),
        ("zh-CN", "settings.language.option.zh-CN", "简体中文"),
        ("fr", "settings.language.option.fr", "Français"),
    )
    THEME_OPTIONS = (
        (ThemeManager.THEME_DARK, "settings.theme.option.dark", "Dark"),
        (ThemeManager.THEME_LIGHT, "settings.theme.option.light", "Light"),
        (ThemeManager.THEME_BEIGE, "settings.theme.option.beige", "Beige"),
        (ThemeManager.THEME_SYSTEM, "settings.theme.option.system", "Follow System"),
    )
    # 打开时的窗口：固定大小的几项直接写尺寸，没有文案键
    STARTUP_WINDOWS = (
        (STARTUP_LAST, "settings.startup_window.last", "Last size and position"),
        *((mode, "", f"{width} × {height}") for mode, (width, height) in STARTUP_SIZES.items()),
        (STARTUP_MAXIMIZED, "settings.startup_window.maximized", "Maximized"),
    )
    RAW_DISPLAY_MODES = (
        (RawDisplayMode.CAMERA, "settings.images.raw_camera", "Camera preview"),
        (RawDisplayMode.ORIGINAL, "color_adjust.look.none", "Original color"),
        (RawDisplayMode.OPS, "color_adjust.look.ops_base", "OPS Look"),
    )
    NON_RAW_DISPLAY_MODES = (
        (NonRawDisplayMode.ORIGINAL, "color_adjust.look.none", "Original color"),
        (NonRawDisplayMode.OPS, "color_adjust.look.ops_base", "OPS Look"),
    )
    CANVAS_BACKGROUNDS = (
        (CanvasBackground.THEME, "settings.images.canvas.theme", "Follow theme"),
        (CanvasBackground.DARK, "settings.images.canvas.dark", "Dark"),
        (CanvasBackground.MID, "settings.images.canvas.mid", "Mid gray"),
        (CanvasBackground.LIGHT, "settings.images.canvas.light", "Light"),
        (CanvasBackground.BEIGE, "settings.images.canvas.beige", "Beige"),
    )
    PRELOAD_MODES = (
        ("off", "settings.ops.off", "Off"),
        ("nearby", "settings.ops.nearby", "Nearby images"),
        ("all", "settings.ops.all", "All images"),
    )
    CANVAS_MODES = (
        ("auto", "canvas.preview.mode.auto", "Automatic"),
        ("full", "canvas.preview.mode.full", "Always full resolution"),
        ("preview1600", "canvas.preview.mode.preview1600", "Always 1600 preview"),
    )
    PRELOAD_FIELDS = (
        ("before", "settings.ops.before", "Previous images"),
        ("after", "settings.ops.after", "Next images"),
        ("memory_mib", "settings.memory.preview", "Edit preview cache (memory, MiB)"),
        ("disk_mib", "settings.ops.disk", "Limit (MiB)"),
        ("thumbnail_mib", "settings.ops.disk", "Limit (MiB)"),
    )
    SHORTCUT_GROUPS = {
        "File": ("settings.shortcuts.group.file", "File"),
        "Edit": ("settings.shortcuts.group.edit", "Edit"),
        "View": ("settings.shortcuts.group.view", "View"),
    }

    def __init__(self, parent=None, *, values: Optional[SettingsValues] = None, thumbnail_cache_runner=None,
                 edit_cache_runner=None, page: str = "", tool_id: Optional[str] = None, model: str = "",
                 capability: str = "", storage_runner=None):
        """page 是打开时所在的页；tool_id、model、capability 是进入工具页或模型页后要定位的条目。"""
        size = DIALOG_SIZES["settings"]
        super().__init__(title_key="settings.window_title", title_default="Preferences",
                         min_width=size["min_w"], min_height=size["min_h"],
                         default_width=size["default_w"], default_height=size["default_h"],
                         modal=True, parent=parent)
        values = values or SettingsValues()
        self._thumbnail_cache_runner = thumbnail_cache_runner
        self._edit_cache_runner = edit_cache_runner
        self._storage_runner = storage_runner
        self._storage_close_pending = False
        self._storage_close_result = 0
        self._finished = False
        self._cache_message_key = ""
        self._edit_cache_message_key = ""
        self._cache_connections_active = True
        self._shortcut_rows: tuple[ShortcutRow, ...] = ()
        self._shortcut_widgets: dict[str, tuple[SettingRow, ShortcutEdit, AppleCompactButton]] = {}
        self._shortcut_sections: list[tuple[str, SettingSection]] = []
        self._resource_pages: dict[str, QWidget] = {}
        self.assistant_page = None
        self._assistant_requested = False
        # 文件关联页要读系统里的关联状态，第一次进入这一页时才建
        self.file_association_page: Optional[QWidget] = None
        self._resources_requested = False
        self._resource_error = ""
        self._tool_target = tool_id
        self._model_target = (model, capability) if model or capability else None
        self._runtime_target = ""
        # 运行环境页里要求重启：窗口关闭后由打开它的一方执行
        self.restart_pending = False
        self._preload_timer = QTimer(self)
        self._preload_timer.setSingleShot(True)
        self._preload_timer.setInterval(_APPLY_DELAY_MS)
        self._preload_timer.timeout.connect(self._emit_preload)
        self._memory_timer = QTimer(self)
        self._memory_timer.setSingleShot(True)
        self._memory_timer.setInterval(_APPLY_DELAY_MS)
        self._memory_timer.timeout.connect(self._emit_memory)
        self._memory_state = values.memory

        self._setup_ui()
        self.set_language(values.language)
        self.set_theme(values.theme)
        self.set_startup_window(values.startup_window)
        self.update_switch.set_checked_silent(values.check_updates)
        self.set_image_display(values.image_display)
        self.set_preload(values.preload)
        self.set_memory(values.memory)
        self.set_shortcuts(values.shortcuts)
        self.retranslate_ui()
        self.show_page(page or self.PAGES[0][0])
        self._clear_default_buttons()

        for runner, update, cleared, failed in self._cache_bindings():
            runner.state_changed.connect(update)
            runner.clear_finished.connect(cleared)
            runner.maintenance_failed.connect(failed)
        self.finished.connect(self._on_finished)
        self._cache_timer = QTimer(self)
        self._cache_timer.setInterval(_CACHE_POLL_MS)
        self._cache_timer.timeout.connect(self._refresh_cache_status)
        self._cache_timer.start()
        self._refresh_cache_status()

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        navigation = QFrame(self)
        navigation.setObjectName("settingsNavigation")
        navigation.setFixedWidth(_NAVIGATION_WIDTH)
        navigation_layout = QVBoxLayout(navigation)
        navigation_layout.setContentsMargins(px(SPACING_MD), px(SPACING_LG), px(SPACING_MD), px(SPACING_LG))
        self._navigation = ManagementList(navigation)
        self._navigation.selection_changed.connect(self._show_selected_page)
        navigation_layout.addWidget(self._navigation)
        navigation_layout.addStretch(1)
        layout.addWidget(navigation)
        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack, 1)
        self._pages = {key: (_ResourceHolder if key in (*self.RESOURCE_PAGES, "ai_services") else _Page)(self._stack)
                       for key, _, _ in self.PAGES}
        for page in self._pages.values():
            self._stack.addWidget(page)
        self._build_general(self._pages["general"])
        self._build_images(self._pages["images"])
        self._build_shortcuts(self._pages["shortcuts"])
        self._build_performance(self._pages["performance"])
        self.about_page = self._pages["about"].add(AboutPage(self._pages["about"]))

    @staticmethod
    def _choice(options) -> AppleDropDown:
        control = AppleDropDown()
        control.setAutoDefault(False)
        control.setFixedWidth(_CHOICE_WIDTH)
        for value, _, fallback in options:
            control.addItem(fallback, getattr(value, "value", value))
        return control

    @staticmethod
    def _segments(options) -> AppleSegmentedControl:
        control = AppleSegmentedControl()
        for value, _, fallback in options:
            control.add_segment(fallback, getattr(value, "value", value))
        return control

    @staticmethod
    def _button(row: SettingRow, slot) -> AppleButton:
        button = AppleButton(parent=row)
        button.setAutoDefault(False)
        button.clicked.connect(slot)
        row.add_control(button)
        return button

    def _clear_default_buttons(self) -> None:
        """窗口里没有“确定”：任何按钮都不当默认按钮，在输入框里按回车不会触发清除缓存之类的操作。"""
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

    def _build_general(self, page: _Page) -> None:
        section = page.add(SettingSection(page))
        self.language_row = section.add_row(SettingRow(section))
        self.language_combo = self._choice(self.LANGUAGE_OPTIONS)
        self.language_combo.activated.connect(lambda _index: self.language_edited.emit(self.language_combo.currentData()))
        self.language_row.add_control(self.language_combo)
        self.theme_row = section.add_row(SettingRow(section))
        self.theme_combo = self._choice(self.THEME_OPTIONS)
        self.theme_combo.activated.connect(lambda _index: self.theme_edited.emit(self.theme_combo.currentData()))
        self.theme_row.add_control(self.theme_combo)
        self.startup_window_row = section.add_row(SettingRow(section))
        self.startup_window_combo = self._choice(self.STARTUP_WINDOWS)
        self.startup_window_combo.activated.connect(self._emit_startup_window)
        self.startup_window_row.add_control(self.startup_window_combo)
        self.update_row = section.add_row(SettingRow(section))
        self.update_switch = ToggleSwitch(self.update_row)
        self.update_switch.clicked[bool].connect(self.update_enabled_edited.emit)
        self.update_row.add_control(self.update_switch)

    def _build_images(self, page: _Page) -> None:
        section = page.add(SettingSection(page))
        self.raw_display_row = section.add_row(SettingRow(section))
        self.raw_display_mode = self._segments(self.RAW_DISPLAY_MODES)
        self.raw_display_mode.activated.connect(self._emit_image_display)
        self.raw_display_row.add_control(self.raw_display_mode)
        self.non_raw_display_row = section.add_row(SettingRow(section))
        self.non_raw_display_mode = self._segments(self.NON_RAW_DISPLAY_MODES)
        self.non_raw_display_mode.activated.connect(self._emit_image_display)
        self.non_raw_display_row.add_control(self.non_raw_display_mode)
        self.canvas_background_row = section.add_row(SettingRow(section))
        self.canvas_background = self._choice(self.CANVAS_BACKGROUNDS)
        self.canvas_background.activated.connect(self._emit_image_display)
        self.canvas_background_row.add_control(self.canvas_background)

    def _build_shortcuts(self, page: _Page) -> None:
        self.reset_shortcuts = AppleButton(parent=page)
        self.reset_shortcuts.setAutoDefault(False)
        self.reset_shortcuts.clicked.connect(self._confirm_reset_shortcuts)
        page.actions.addWidget(self.reset_shortcuts)

    @staticmethod
    def _number(limits, timer: QTimer) -> AppleSpinBox:
        """数值输入框：用户改动后等 timer 走完才生效一次。"""
        control = AppleSpinBox()
        control.setRange(*limits)
        control.setKeyboardTracking(False)
        control.valueEdited.connect(lambda _value: timer.start())
        return control

    def _build_performance(self, page: _Page) -> None:
        self.canvas_section = page.add(SettingSection(page))
        self.canvas_mode_row = self.canvas_section.add_row(SettingRow(self.canvas_section))
        self.canvas_mode = self._choice(self.CANVAS_MODES)
        self.canvas_mode.activated.connect(lambda _index: self._emit_preload())
        self.canvas_mode_row.add_control(self.canvas_mode)
        self.memory_section = page.add(SettingSection(page))
        self.memory_usage_row = self.memory_section.add_row(SettingRow(self.memory_section))
        self.memory_usage = self._number(MemorySettings.USAGE_RANGE, self._memory_timer)
        self.memory_usage_row.add_control(self.memory_usage)
        self.memory_seat_row = self.memory_section.add_row(SettingRow(self.memory_section))
        self.memory_seat = self._number((0, 0), self._memory_timer)
        self.memory_seat_row.add_control(self.memory_seat)

        self.preload_section = page.add(SettingSection(page))
        self.preload_mode_row = self.preload_section.add_row(SettingRow(self.preload_section))
        self.preload_mode = self._segments(self.PRELOAD_MODES)
        self.preload_mode.activated.connect(self._on_preload_mode_edited)
        self.preload_mode_row.add_control(self.preload_mode)
        self._preload_inputs: dict[str, AppleSpinBox] = {}
        self._preload_rows: dict[str, SettingRow] = {}
        for name, _, _ in self.PRELOAD_FIELDS:
            control = self._preload_inputs[name] = self._number(StudioPreloadSettings.RANGES[name],
                                                                self._preload_timer)
            row = self._preload_rows[name] = SettingRow()
            row.add_control(control)
            if name == "memory_mib":
                self.memory_section.add_row(row)
            elif name in ("before", "after"):
                self.preload_section.add_row(row)

        from .storage_page import StoragePage
        disk_page = self._pages["storage"]
        self.storage_page = disk_page.add(StoragePage(self._storage_runner, self._tr, disk_page,
                                                    header_actions=disk_page.actions))
        self.storage_page.manage_requested.connect(self.storage_manage_requested)
        self.storage_page.discard_limits_requested.connect(self._discard_storage_limits)
        self.thumbnail_section = SettingSection(self.storage_page)
        self.thumbnail_section.add_row(self._preload_rows["thumbnail_mib"])
        self.thumbnail_usage = self._usage_bar()
        self.thumbnail_row = self.thumbnail_section.add_row(
            SettingRow(self.thumbnail_section, leading=self.thumbnail_usage))
        self.thumbnail_clear = self._button(self.thumbnail_row, self._clear_thumbnail_cache)
        self.storage_page.add_cache_section("thumbnails", self.thumbnail_section, self.thumbnail_row)

        self.edit_cache_section = SettingSection(self.storage_page)
        location = QWidget()
        location_layout = QVBoxLayout(location)
        location_layout.setContentsMargins(0, 0, 0, 0)
        location_layout.setSpacing(px(SPACING_XS) // 2)
        self.edit_cache_location_label = QLabel(location)
        self.edit_cache_location_label.setObjectName("settingRowTitle")
        location_layout.addWidget(self.edit_cache_location_label)
        self.edit_cache_path = _PathLabel(
            self._edit_cache_runner.directory if self._edit_cache_runner is not None else "", location)
        location_layout.addWidget(self.edit_cache_path)
        self.edit_cache_location_row = self.edit_cache_section.add_row(
            SettingRow(self.edit_cache_section, leading=location))
        self.edit_cache_open = self._button(self.edit_cache_location_row, self._open_edit_cache_folder)
        self.edit_cache_section.add_row(self._preload_rows["disk_mib"])
        self.edit_cache_usage = self._usage_bar()
        self.edit_cache_row = self.edit_cache_section.add_row(
            SettingRow(self.edit_cache_section, leading=self.edit_cache_usage))
        self.edit_cache_clear = self._button(self.edit_cache_row, self._clear_edit_cache)
        self.storage_page.add_cache_section("edit_cache", self.edit_cache_section, self.edit_cache_row)

    @staticmethod
    def _usage_bar() -> UsageBar:
        bar = UsageBar()
        bar.setMinimumWidth(_USAGE_WIDTH)
        return bar

    # ------------------------------------------------------------------
    # 文案
    # ------------------------------------------------------------------
    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        if not hasattr(self, "_pages"):
            return
        selected = self._navigation.selected_key()
        titles = {key: self._tr(text_key, fallback) for key, text_key, fallback in self.PAGES}
        self._navigation.set_groups([
            ManagementListGroup("+".join(keys), self._tr(title_key, fallback) if title_key else "", tuple(
                ManagementListItem(key, titles[key], icon=self.PAGE_ICONS[key]) for key in keys))
            for title_key, fallback, keys in self.NAVIGATION], preferred_key=selected or None)
        for key, page in self._pages.items():
            if isinstance(page, _Page):
                page.title.setText(titles[key])
        self._project_resource_status()

        self.language_row.set_text(self._tr("settings.language.group", "Language"))
        self.update_row.set_text(self._tr("update.setting"), self._tr("update.setting_hint"))
        self.update_switch.setAccessibleName(self._tr("update.setting"))
        self.theme_row.set_text(self._tr("settings.theme.group", "Theme"))
        self.startup_window_row.set_text(
            self._tr("settings.startup_window.title", "Window at startup"),
            self._tr("settings.startup_window.hint", "A fixed size is centered and never larger than the screen. "
                                                     "Applies the next time the app opens."))
        for index, (_, text_key, fallback) in enumerate(self.STARTUP_WINDOWS):
            self.startup_window_combo.setItemText(index, self._tr(text_key, fallback) if text_key else fallback)
        self.raw_display_row.set_text(self._tr("settings.images.raw_mode", "RAW display mode"))
        self.non_raw_display_row.set_text(self._tr("settings.images.non_raw_mode", "Display mode for other formats"))
        self.canvas_background_row.set_text(self._tr("settings.images.canvas_background", "Canvas background"))
        for control, options in ((self.language_combo, self.LANGUAGE_OPTIONS), (self.theme_combo, self.THEME_OPTIONS),
                                 (self.canvas_background, self.CANVAS_BACKGROUNDS)):
            for index, (_, text_key, fallback) in enumerate(options):
                control.setItemText(index, self._tr(text_key, fallback))
        for control, options in ((self.raw_display_mode, self.RAW_DISPLAY_MODES),
                                 (self.non_raw_display_mode, self.NON_RAW_DISPLAY_MODES),
                                 (self.preload_mode, self.PRELOAD_MODES)):
            for index, (_, text_key, fallback) in enumerate(options):
                control.set_segment_text(index, self._tr(text_key, fallback))
            control.updateGeometry()

        self._pages["shortcuts"].set_hint(self._tr(
            "settings.shortcut.hint",
            "Click a shortcut, then press the new keys. Esc cancels; Backspace clears it."))
        self.reset_shortcuts.setText(self._tr("settings.confirm.reset_all.title", "Reset All Shortcuts"))
        self._project_shortcuts()

        self.preload_section.set_title(self._tr("settings.ops.title", "Preloading"))
        self.canvas_section.set_title(self._tr("canvas.preview.settings.title", "Editing canvas"))
        self.canvas_mode_row.set_text(self._tr("canvas.preview.settings.mode", "Canvas preview mode"),
                                     self._tr("canvas.preview.settings.hint"))
        for index, (_, key, fallback) in enumerate(self.CANVAS_MODES):
            self.canvas_mode.setItemText(index, self._tr(key, fallback))
        self.preload_mode_row.set_text(
            self._tr("settings.ops.mode", "Preload range"),
            self._tr("settings.ops.hint", "The selected image loads first. Images in the preload range are "
                                          "prepared in the background."))
        for name, text_key, fallback in self.PRELOAD_FIELDS:
            self._preload_rows[name].set_text(self._tr(text_key, fallback))
        self._preload_rows["memory_mib"].set_text(
            self._tr("settings.memory.preview", "Edit preview cache (memory, MiB)"),
            self._tr("settings.memory.preview_hint", "Stores processed previews for faster photo switching and returning "
                     "to edit states. Counts toward the app memory limit and is released when the app closes."))
        self._project_memory()
        self.thumbnail_section.set_title(self._tr("settings.thumbnails.title", "Thumbnail cache (disk)"))
        self._preload_rows["thumbnail_mib"].set_text(
            self._tr("settings.ops.disk", "Limit (MiB)"),
            self._tr("settings.thumbnails.hint", "Stores small images for photo lists and filmstrips for faster browsing. "
                     "Thumbnails are rebuilt when needed after clearing. Older unused entries are removed when space runs low."))
        self.thumbnail_clear.setText(self._tr("settings.thumbnails.clear", "Clear thumbnail cache"))
        self._update_thumbnail_cache()
        self.edit_cache_section.set_title(self._tr("settings.edit_cache.title", "Edit proxy cache (disk)"))
        self._preload_rows["disk_mib"].set_text(
            self._tr("settings.ops.disk", "Limit (MiB)"),
            self._tr("settings.edit_cache.hint", "Stores editing sources with a 1600-pixel long edge to reduce original "
                     "image decoding. Clearing keeps source files, projects and edit history. Older unused entries "
                     "are removed when space runs low."))
        self.edit_cache_location_label.setText(self._tr("settings.edit_cache.location", "Location"))
        self.edit_cache_path.copy_text = self._tr("settings.edit_cache.copy_path", "Copy path")
        self.edit_cache_open.setText(self._tr("settings.edit_cache.open_folder", "Open folder"))
        self.edit_cache_clear.setText(self._tr("settings.edit_cache.clear", "Clear edit proxy cache"))
        self._update_edit_cache()
        self.storage_page.retranslate()

    def apply_theme(self, theme_name: str) -> None:
        super().apply_theme(theme_name)
        if hasattr(self, "edit_cache_path"):
            self.edit_cache_path.update_display()

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------
    def current_page(self) -> str:
        return self._navigation.selected_key()

    def show_page(self, key: str) -> None:
        if key in self._pages:
            self._navigation.select_key(key)
            self._show_selected_page(key)

    def _show_selected_page(self, key: str) -> None:
        if key == "file_association" and self.file_association_page is None:
            from .file_association_page import FileAssociationPage
            page = self._pages[key]
            self.file_association_page = page.add(FileAssociationPage(page))
            self._clear_default_buttons()
        self._stack.setCurrentWidget(self._pages[key])
        self._request_resources()
        self._request_assistant()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._request_resources()
        self._request_assistant()

    def _request_assistant(self):
        if self.isVisible() and self.current_page() == "ai_services" and self.assistant_page is None and not self._assistant_requested:
            self._assistant_requested = True
            self._pages["ai_services"].set_busy(self._tr("assistant.status.preparing"))
            self.assistant_services_requested.emit()

    def set_assistant_runner(self, runner):
        if self.assistant_page is None:
            from .assistant_settings_page import AssistantSettingsPage
            self.assistant_page = AssistantSettingsPage(runner, self._pages["ai_services"])
            self._pages["ai_services"].set_page(self.assistant_page)
            self._clear_default_buttons()

    # ------------------------------------------------------------------
    # 工具、模型与运行环境
    # ------------------------------------------------------------------
    def _request_resources(self) -> None:
        """进入这三页而服务还没给出时请求一次；窗口还没显示时不请求，那时还没有人在听。"""
        if (self.isVisible() and self.current_page() in self.RESOURCE_PAGES and not self._resource_pages
                and not self._resources_requested):
            self._resources_requested = True
            self._resource_error = ""
            self._project_resource_status()
            self.resource_pages_requested.emit()

    def _project_resource_status(self) -> None:
        for key in self.RESOURCE_PAGES:
            holder = self._pages[key]
            if holder.page is not None:
                continue
            if self._resource_error:
                holder.set_empty("alert-triangle", self._resource_error)
            else:
                holder.set_busy(self._tr("settings.resources.loading", "Preparing tools and models…"))
        if self._resource_pages and "runtime" not in self._resource_pages:
            self._pages["runtime"].set_empty(
                self.PAGE_ICONS["runtime"], self._tr("management.runtimes_empty", "No runtimes available"))

    def set_resource_services(self, core, runtime_service=None, context_provider=None) -> None:
        """给出工具、模型、运行环境三页要用的服务并建好这三页；只建一次。"""
        if self._resource_pages:
            return
        from .model_manager_page import ModelManagerPage
        from .tool_manager_page import ToolManagerPage
        tools = ToolManagerPage(core, runtime_service=runtime_service, context_provider=context_provider,
                                tool_id=self._tool_target)
        tools.tools_changed.connect(self.tools_changed)
        tools.model_view_requested.connect(self.show_model)
        tools.runtime_view_requested.connect(self.show_runtime)
        self._resource_pages = {"tools": tools,
                                "models": ModelManagerPage(core.models, backend_status=core.inference.status)}
        if runtime_service is not None:
            from .runtime_manager_page import RuntimeManagerPage
            runtime = RuntimeManagerPage(runtime_service)
            runtime.restart_requested.connect(self._restart)
            self._resource_pages["runtime"] = runtime
        for key, page in self._resource_pages.items():
            self._pages[key].set_page(page)
        self._tool_target = None
        self._project_resource_status()
        self._apply_resource_targets()
        self._clear_default_buttons()

    def show_resource_error(self, text: str) -> None:
        """这三页要用的服务没能准备好；下次进入时会再请求一次。"""
        self._resource_error = text
        self._resources_requested = False
        self._project_resource_status()

    def resource_page(self, key: str) -> Optional[QWidget]:
        return self._resource_pages.get(key)

    def show_tool(self, tool_id: str) -> None:
        self._tool_target = tool_id
        self.show_page("tools")
        self._apply_resource_targets()

    def show_model(self, token: str = "", capability: str = "") -> None:
        """切到模型页并定位到某个模型；没有给出模型或找不到时只列出这一类能力的模型。"""
        self._model_target = (token, capability)
        self.show_page("models")
        self._apply_resource_targets()

    def show_runtime(self, token: str) -> None:
        self._runtime_target = token
        self.show_page("runtime")
        self._apply_resource_targets()

    def _apply_resource_targets(self) -> None:
        pages = self._resource_pages
        if not pages:
            return
        if self._tool_target:
            pages["tools"].select_tool(self._tool_target)
            self._tool_target = None
        if self._model_target is not None:
            token, capability = self._model_target
            self._model_target = None
            if not (token and pages["models"].select_model(token)):
                pages["models"].show_capability(capability)
        if self._runtime_target and "runtime" in pages:
            pages["runtime"].select_runtime(self._runtime_target)
        self._runtime_target = ""

    def _restart(self) -> None:
        self.restart_pending = True
        self.done(0)

    def show_page_error(self, page: str, text: str) -> None:
        """在某一页顶部显示没能保存之类的问题；空文字表示收起。"""
        self._pages[page].set_notice(text)

    # ------------------------------------------------------------------
    # 常规与图片显示
    # ------------------------------------------------------------------
    @staticmethod
    def _select(control, value) -> None:
        index = control.findData(value)
        if index >= 0:
            control.setCurrentIndex(index)

    def set_language(self, code: str) -> None:
        self._select(self.language_combo, code)

    def set_theme(self, code: str) -> None:
        self._select(self.theme_combo, code)

    def set_startup_window(self, mode: str) -> None:
        self._select(self.startup_window_combo, mode)

    def set_update_state(self, state):
        self.update_switch.set_checked_silent(state.enabled)
        self.update_switch.setEnabled(not state.saving)
        error = "update.error.settings" if "update.error.settings" in (state.warning, state.error) else ""
        message = self._tr(error) if error else self._tr("update.saving") if state.saving else ""
        self.update_row.set_message(message, "error" if error else "info")
        self.about_page.set_update_state(state)

    def _emit_startup_window(self, _index: int = 0) -> None:
        self.show_page_error("general", "")
        self.startup_window_edited.emit(self.startup_window_combo.currentData())

    def set_image_display(self, settings: ImageDisplaySettings) -> None:
        self.raw_display_mode.setCurrentIndex(
            [mode for mode, _, _ in self.RAW_DISPLAY_MODES].index(settings.raw_mode))
        self.non_raw_display_mode.setCurrentIndex(
            [mode for mode, _, _ in self.NON_RAW_DISPLAY_MODES].index(settings.non_raw_mode))
        self._select(self.canvas_background, settings.canvas_background.value)

    def _emit_image_display(self, _index: int = 0) -> None:
        self.show_page_error("images", "")
        self.image_display_edited.emit(ImageDisplaySettings(
            raw_mode=RawDisplayMode(self.raw_display_mode.currentData()),
            non_raw_mode=NonRawDisplayMode(self.non_raw_display_mode.currentData()),
            canvas_background=CanvasBackground(self.canvas_background.currentData())))

    # ------------------------------------------------------------------
    # 快捷键
    # ------------------------------------------------------------------
    def set_shortcuts(self, rows: Sequence[ShortcutRow]) -> None:
        """投影快捷键各行；行的集合变了才重建，否则原位更新。行下的冲突说明随之收起。"""
        rows = tuple(rows)
        if tuple(row.key for row in rows) != tuple(row.key for row in self._shortcut_rows):
            self._rebuild_shortcuts(rows)
        self._shortcut_rows = rows
        self._project_shortcuts()
        self._clear_default_buttons()

    def _rebuild_shortcuts(self, rows: tuple[ShortcutRow, ...]) -> None:
        page = self._pages["shortcuts"]
        for _, section in self._shortcut_sections:
            page.remove(section)
        self._shortcut_sections = []
        self._shortcut_widgets = {}
        sections: dict[str, SettingSection] = {}
        for item in rows:
            section = sections.get(item.group)
            if section is None:
                section = sections[item.group] = page.add(SettingSection(page))
                self._shortcut_sections.append((item.group, section))
            row = section.add_row(SettingRow(section))
            edit = ShortcutEdit(row)
            edit.sequence_edited.connect(lambda sequence, key=item.key: self.shortcut_edited.emit(key, sequence))
            row.add_control(edit)
            reset = AppleCompactButton(parent=row)
            reset.setAutoDefault(False)
            reset.clicked.connect(lambda _checked=False, key=item.key: self._reset_shortcut(key))
            row.add_control(reset)
            self._shortcut_widgets[item.key] = (row, edit, reset)

    def _project_shortcuts(self) -> None:
        for group, section in self._shortcut_sections:
            text_key, fallback = self.SHORTCUT_GROUPS.get(group, ("", group))
            section.set_title(self._tr(text_key, fallback) if text_key else fallback)
        recording = self._tr("settings.shortcut.recording", "Press keys...")
        empty = self._tr("settings.shortcut.none", "Not set")
        reset_text = self._tr("settings.button.reset", "Reset")
        for item in self._shortcut_rows:
            row, edit, reset = self._shortcut_widgets[item.key]
            row.set_text(item.text)
            row.set_message("")
            edit.set_texts(recording, empty)
            edit.set_sequence(item.sequence)
            reset.setText(reset_text)
            reset.setEnabled(QKeySequence(item.sequence) != QKeySequence(item.default))

    def show_shortcut_conflict(self, key: str, sequence: str, owner: str) -> None:
        """这一项想用的快捷键已经被别的动作占用，没有改动。"""
        shown = QKeySequence(sequence).toString(QKeySequence.SequenceFormat.NativeText)
        self._shortcut_widgets[key][0].set_message(self._tr(
            "settings.shortcut.conflict", "{sequence} is already used by “{name}”. Nothing was changed.",
            sequence=shown, name=owner), "warning")

    def _reset_shortcut(self, key: str) -> None:
        default = next(row.default for row in self._shortcut_rows if row.key == key)
        self.shortcut_edited.emit(key, default)

    def _confirm_reset_shortcuts(self) -> None:
        if confirm_destructive(
                self, self._tr("settings.confirm.reset_all.title", "Reset All Shortcuts"),
                self._tr("settings.confirm.reset_all.message",
                         "Are you sure you want to reset all shortcuts to default?"),
                self._tr("settings.button.reset_all", "Reset All")):
            self.shortcuts_reset_requested.emit()

    # ------------------------------------------------------------------
    # 预加载
    # ------------------------------------------------------------------
    def set_preload(self, settings: StudioPreloadSettings) -> None:
        self.canvas_mode.setCurrentIndex(StudioPreloadSettings.CANVAS_MODES.index(settings.canvas_preview_mode))
        self.preload_mode.setCurrentIndex([mode for mode, _, _ in self.PRELOAD_MODES].index(settings.mode))
        for name, control in self._preload_inputs.items():
            control.setValue(getattr(settings, name))
        self._update_preload_controls()

    def _update_preload_controls(self) -> None:
        nearby = self.preload_mode.currentData() == "nearby"
        for name in ("before", "after"):
            self._preload_inputs[name].setEnabled(nearby)

    def _on_preload_mode_edited(self, _index: int) -> None:
        self._update_preload_controls()
        self._emit_preload()

    def _emit_preload(self) -> None:
        self._preload_timer.stop()
        self.show_page_error("performance", "")
        self.preload_edited.emit(StudioPreloadSettings(mode=self.preload_mode.currentData(),
            canvas_preview_mode=self.canvas_mode.currentData(), **{
            name: control.value() for name, control in self._preload_inputs.items()}))

    # ------------------------------------------------------------------
    # 内存
    # ------------------------------------------------------------------
    def set_memory(self, state: MemoryState) -> None:
        """投影已生效的内存配置：两个数值、换算出的上限与保留量，以及座位是否得到系统批准。"""
        self._memory_state = state
        readable = state.budget.total_bytes is not None
        self.memory_usage.setValue(state.settings.usage_percent)
        self.memory_usage.setEnabled(readable)
        self.memory_seat.setRange(0, state.budget.seat_limit_gib)
        self.memory_seat.setValue(state.settings.seat_gib)
        self.memory_seat.setEnabled(readable)
        self._project_memory()

    def _project_memory(self) -> None:
        state = self._memory_state
        budget = state.budget
        self.memory_section.set_title(self._tr("settings.memory.title", "Memory"))
        self.memory_usage_row.set_text(
            self._tr("settings.memory.usage", "Memory usage limit (%)"),
            self._tr("settings.memory.usage_hint",
                     "The most memory image processing may register, as a share of physical memory. Requests "
                     "beyond the reserved amount are also checked against the memory currently available."))
        self.memory_seat_row.set_text(
            self._tr("settings.memory.seat", "Memory reserved for this app (GiB)"),
            self._tr("settings.memory.seat_hint",
                     "Stays available to this app even when other programs fill the memory. The part already in "
                     "use is not handed back while the app idles in the background. 0 reserves nothing."))
        if budget.total_bytes is None:
            self.memory_usage_row.set_message(self._tr(
                "settings.memory.unreadable", "System memory could not be read. The limit is {limit} GiB.",
                limit=_gigabytes(budget.limit_bytes)), "warning")
            self.memory_seat_row.set_message("")
            return
        self.memory_usage_row.set_message(self._tr(
            "settings.memory.usage_result", "Uses up to {limit} GiB and leaves {reserve} GiB for the system.",
            limit=_gigabytes(budget.limit_bytes), reserve=_gigabytes(budget.reserve_bytes)))
        if state.seat_refused:
            self.memory_seat_row.set_message(self._tr(
                "settings.memory.seat_refused",
                "The system did not approve this reservation. {seat} GiB is reserved now.",
                seat=_gigabytes(state.seat_bytes)), "warning")
        elif state.seat_bytes:
            self.memory_seat_row.set_message(self._tr(
                "settings.memory.seat_result", "{seat} GiB is reserved for this app.",
                seat=_gigabytes(state.seat_bytes)))
        else:
            self.memory_seat_row.set_message("")

    def _emit_memory(self) -> None:
        self._memory_timer.stop()
        self.show_page_error("performance", "")
        self.memory_edited.emit(MemorySettings(self.memory_usage.value(), self.memory_seat.value()))

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    def _cache_bindings(self):
        if self._thumbnail_cache_runner is not None:
            yield (self._thumbnail_cache_runner, self._update_thumbnail_cache, self._on_thumbnail_cache_cleared,
                   self._on_thumbnail_cache_failed)
        if self._edit_cache_runner is not None:
            yield (self._edit_cache_runner, self._update_edit_cache, self._on_edit_cache_cleared,
                   self._on_edit_cache_failed)

    def _refresh_cache_status(self) -> None:
        for runner in (self._thumbnail_cache_runner, self._edit_cache_runner):
            if runner is not None:
                runner.query_status()

    def _project_usage(self, bar: UsageBar, status, text_key: str, fallback: str, waiting: str) -> None:
        if status is None:
            bar.set_progress(0, 1)
            bar.set_text(waiting)
            return
        limit = max(1, status.max_bytes // _MIB)
        bar.set_progress(min(limit, round(status.used_bytes / _MIB)), limit)
        bar.set_text(self._tr(text_key, fallback, used=f"{status.used_bytes / _MIB:.1f}", limit=limit))

    def _update_thumbnail_cache(self) -> None:
        runner = self._thumbnail_cache_runner
        status = runner.status_snapshot if runner is not None else None
        self.thumbnail_clear.setEnabled(runner is not None and runner.can_clear)
        self._preload_inputs["thumbnail_mib"].setEnabled(status is not None and status.is_writer)
        self._project_usage(
            self.thumbnail_usage, status, "settings.thumbnails.usage", "{used} of {limit} MiB used.",
            self._tr("settings.thumbnails.checking", "Checking…") if runner is not None else "")
        key = ("settings.thumbnails.busy" if runner is not None and runner.is_busy else
               self._cache_message_key if self._cache_message_key else
               "settings.thumbnails.unavailable" if runner is None else
               "" if status is None else
               "settings.thumbnails.read_only" if not status.is_writer else
               "settings.thumbnails.unavailable" if not status.capacity_ready else "")
        self.thumbnail_row.set_message(self._tr(key) if key else "")

    def _clear_thumbnail_cache(self) -> None:
        self._cache_message_key = ""
        self._thumbnail_cache_runner.clear()

    def _on_thumbnail_cache_cleared(self, count, error_key) -> None:
        self._cache_message_key = error_key or "settings.thumbnails.cleared"
        self._update_thumbnail_cache()

    def _on_thumbnail_cache_failed(self, key) -> None:
        self._cache_message_key = key
        self._update_thumbnail_cache()

    def _update_edit_cache(self) -> None:
        runner = self._edit_cache_runner
        status = runner.status_snapshot if runner is not None else None
        self.edit_cache_clear.setEnabled(runner is not None and runner.can_clear)
        self.edit_cache_open.setEnabled(runner is not None)
        self._preload_inputs["disk_mib"].setEnabled(status is not None and status.is_writer)
        self._project_usage(
            self.edit_cache_usage, status, "settings.edit_cache.usage_text",
            "{used} of {limit} MiB used.",
            self._tr("settings.edit_cache.checking", "Checking…") if runner is not None else "")
        key = ("settings.edit_cache.busy" if runner is not None and runner.is_busy else
               self._edit_cache_message_key if self._edit_cache_message_key else
               "settings.edit_cache.unavailable" if runner is None else
               "" if status is None else
               "settings.edit_cache.read_only" if not status.is_writer else
               "settings.edit_cache.unavailable" if not status.capacity_ready else "")
        self.edit_cache_row.set_message(self._tr(key) if key else "")

    def _clear_edit_cache(self) -> None:
        self._edit_cache_message_key = ""
        if self._edit_cache_runner is not None:
            self._edit_cache_runner.clear()

    def _on_edit_cache_cleared(self, count, error_key) -> None:
        self._edit_cache_message_key = error_key or "settings.edit_cache.cleared"
        self._update_edit_cache()

    def _on_edit_cache_failed(self, key) -> None:
        self._edit_cache_message_key = key
        self._update_edit_cache()

    def _open_edit_cache_folder(self) -> None:
        if self._edit_cache_runner is not None:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(self._edit_cache_runner.directory)):
                self._edit_cache_message_key = "settings.edit_cache.open_failed"
                self._update_edit_cache()

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        if ((self.assistant_page is not None and self.assistant_page.has_unsaved_changes())
                or (self._storage_runner is not None and
                    self.storage_page.has_pending_limits)):
            event.ignore()
            self.done(0)
            return
        super().closeEvent(event)

    def done(self, result: int) -> None:
        if self._finished:
            return
        if self._storage_runner is not None and self._storage_runner.discarding_limits:
            return
        if self.assistant_page is not None and self.assistant_page.has_unsaved_changes():
            self.show_page("ai_services")
            self.assistant_page.save_before_close(lambda: self.done(result))
            return
        if self._storage_runner is not None:
            self.storage_page.flush_limits()
            if self._storage_runner.has_pending_limits:
                self._storage_close_result = result
                self.storage_page.set_limits_close_pending(True)
                self.show_page("storage")
                if not self._storage_close_pending:
                    self._storage_close_pending = True
                    def flushed(success):
                        self._storage_close_pending = False
                        self.storage_page.set_limits_close_pending(False)
                        if success:
                            self.done(self._storage_close_result)
                        else:
                            self.show_page("storage")
                    self._storage_runner.flush_limits(flushed)
                return
        super().done(result)

    def _discard_storage_limits(self):
        if self._finished or self._storage_runner is None or self._storage_runner.discarding_limits:
            return
        if not confirm_destructive(self, self._tr("storage.discard_limits_close"),
                self._tr("storage.discard_limits_note"), self._tr("storage.discard_limits_close")):
            return
        if self._finished or self._storage_runner.discarding_limits or not self.storage_page.has_pending_limits:
            return
        self.storage_page.discard_limits()
        self._storage_close_pending = True
        self.storage_page.set_limits_close_pending(True)
        def discarded():
            self._storage_close_pending = False
            self.storage_page.set_limits_close_pending(False)
            self.done(self._storage_close_result)
        self._storage_runner.discard_limits(discarded)

    def _on_finished(self, _result: int) -> None:
        """关闭时把还在等的数值改动发出去，并断开缓存运行器与三个资源页的订阅。"""
        self._finished = True
        if self._preload_timer.isActive():
            self._emit_preload()
        if self._memory_timer.isActive():
            self._emit_memory()
        if self._storage_runner is not None:
            self._storage_runner.shutdown()
        if self.assistant_page is not None:
            self.assistant_page.cleanup()
        if not self._cache_connections_active:
            return
        self._cache_connections_active = False
        self._cache_timer.stop()
        for page in self._resource_pages.values():
            page.close_subscription()
        for runner, update, cleared, failed in self._cache_bindings():
            runner.state_changed.disconnect(update)
            runner.clear_finished.disconnect(cleared)
            runner.maintenance_failed.disconnect(failed)
