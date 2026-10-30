"""设置窗口：表单行、快捷键录入、只在用户编辑时发出信号，以及在主窗口里改了就生效。"""

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QGroupBox, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget

from src.application.image_display_settings import ImageDisplaySettings
from src.application.managed_cache import ManagedCacheStatus
from src.application.memory_settings import GIB, MemoryConfigurator, MemorySettings, MemoryState, resolve
from src.application.studio_preload import StudioPreloadSettings
from src.core.contracts.image_display import CanvasBackground, RawDisplayMode
from src.i18n import TranslationService, get_translation_service, set_translation_service
from src.ui.base_window import STARTUP_MODES
from src.ui.dialogs.settings_dialog import SettingsDialog, SettingsValues, ShortcutEdit
from src.ui.icons import themed_icon
from src.ui.theme import ThemeManager, get_theme_manager
from src.ui.widgets.apple_button import AppleButton
from src.ui.widgets.apple_spin_box import AppleSpinBox
from src.ui.widgets.message_dialog import MessageDialog
from src.ui.widgets.setting_row import SettingRow, SettingSection
from src.ui.widgets.usage_bar import UsageBar
from src.ui.window_actions import ACTION_SPECS, ShortcutRow
from tests.support import wait_until

MIB = 1024 ** 2
ROWS = (
    ShortcutRow("File/Open File", "File", "Open Files", "Ctrl+O", "Ctrl+O"),
    ShortcutRow("Edit/Manual Crop", "Edit", "Crop", "Ctrl+Alt+C", "Ctrl+Shift+C"),
    ShortcutRow("View/Zoom In", "View", "Zoom In", "", "Ctrl+="),
)


class _Runner(QObject):
    state_changed = Signal()
    clear_finished = Signal(int, str)
    maintenance_failed = Signal(str)

    def __init__(self, used, limit, *, writer=True):
        super().__init__()
        self.directory = "C:/cache/edit_cache"
        self.status_snapshot = ManagedCacheStatus(self.directory, used, limit, writer, True)
        self.is_busy = False
        self.can_clear = writer
        self.limit_mib = limit // MIB
        self.cleared = 0
        self.queries = 0

    def query_status(self):
        self.queries += 1

    def clear(self):
        self.cleared += 1


def _record(dialog):
    events = []
    dialog.language_edited.connect(lambda code: events.append(("language", code)))
    dialog.theme_edited.connect(lambda code: events.append(("theme", code)))
    dialog.image_display_edited.connect(lambda value: events.append(("images", value)))
    dialog.preload_edited.connect(lambda value: events.append(("preload", value)))
    dialog.memory_edited.connect(lambda value: events.append(("memory", value)))
    dialog.shortcut_edited.connect(lambda key, sequence: events.append(("shortcut", key, sequence)))
    dialog.shortcuts_reset_requested.connect(lambda: events.append(("reset",)))
    return events


def test_setting_row_puts_controls_on_the_next_line_when_narrow(qapp):
    host = QWidget()
    layout = QVBoxLayout(host)
    section = SettingSection(host)
    section.set_title("预加载")
    layout.addWidget(section)
    row = section.add_row(SettingRow(section))
    row.set_text("预加载范围", "优先加载选中图片，后台准备预加载范围内的图片。")
    button = AppleButton("一个比较宽的按钮文字")
    button.setFixedWidth(260)
    row.add_control(button)
    host.resize(720, 200)
    host.show()
    qapp.processEvents()
    assert (section.title(), row.title()) == ("预加载", "预加载范围") and row.description()
    assert not row.is_stacked() and button.y() < row._description.geometry().bottom()

    host.resize(400, 300)
    qapp.processEvents()
    assert row.is_stacked() and button.y() > row._description.geometry().bottom()
    assert button.x() == 0 and row.rect().contains(button.geometry())

    row.set_message("没有更改。", "warning")
    assert row.message() == "没有更改。" and row._message.property("kind") == "warning"
    row.set_message("")
    assert row.message() == ""
    host.close()


def test_usage_bar_shows_usage_and_busy_state(qapp):
    bar = UsageBar()
    bar.set_progress(186, 512)
    bar.set_text("占用 186 / 512 MiB")
    assert (bar.value(), bar.maximum(), bar.text()) == (186, 512, "占用 186 / 512 MiB")
    bar.set_progress(900, 512)
    assert bar.value() == 512
    bar.set_progress(3, 0)
    assert (bar.value(), bar.maximum()) == (0, 0) or bar.maximum() == 0


def test_shortcut_edit_records_cancels_and_clears(qapp):
    edit = ShortcutEdit()
    edit.set_texts("请按下快捷键...", "未设置")
    edit.set_sequence("Ctrl+O")
    edit.show()
    edited = []
    edit.sequence_edited.connect(edited.append)
    assert edit.text() == "Ctrl+O" and edit.property("state") == "set" and not edit.styleSheet()

    QTest.mouseClick(edit, Qt.MouseButton.LeftButton)
    assert edit.is_recording() and edit.text() == "请按下快捷键..." and edit.property("state") == "recording"
    QTest.keyClick(edit, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    # 录入后发出改动，显示仍是原来的，等所属方投影
    assert edited == ["Ctrl+Shift+K"] and not edit.is_recording() and edit.text() == "Ctrl+O"

    QTest.mouseClick(edit, Qt.MouseButton.LeftButton)
    QTest.keyClick(edit, Qt.Key.Key_Escape)
    assert edited == ["Ctrl+Shift+K"] and not edit.is_recording()

    QTest.mouseClick(edit, Qt.MouseButton.LeftButton)
    QTest.keyClick(edit, Qt.Key.Key_Backspace)
    assert edited == ["Ctrl+Shift+K", ""]

    edit.set_sequence("")
    assert edit.text() == "未设置" and edit.property("state") == "empty"
    edit.close()


def test_settings_dialog_structure_and_navigation(qapp):
    dialog = SettingsDialog(values=SettingsValues(shortcuts=ROWS))
    dialog.show()
    assert not dialog.findChildren(QTabWidget) and not dialog.findChildren(QGroupBox)
    assert all(isinstance(box, AppleSpinBox) for box in dialog.findChildren(QSpinBox))
    assert not dialog.styleSheet()
    # 没有默认按钮：在数值框里按回车不会触发“清除缓存”这类按钮
    assert not any(button.autoDefault() or button.isDefault() for button in dialog.findChildren(QPushButton))
    assert [key for key, _, _ in dialog.PAGES] == [
        "general", "images", "shortcuts", "performance", "storage", "ai_services", "file_association", "tools",
        "models", "runtime", "about"]
    assert [key for _, _, keys in dialog.NAVIGATION for key in keys] == [key for key, _, _ in dialog.PAGES]
    assert set(dialog.PAGE_ICONS) == {key for key, _, _ in dialog.PAGES}
    assert all(themed_icon(name) for name in dialog.PAGE_ICONS.values())
    assert dialog.current_page() == "general"
    for key, _, _ in dialog.PAGES:
        dialog.show_page(key)
        assert dialog.current_page() == key and dialog._stack.currentWidget() is dialog._pages[key]
    dialog.close()

    dialog = SettingsDialog(page="shortcuts")
    assert dialog.current_page() == "shortcuts"
    dialog.close()


def test_settings_dialog_projection_is_silent_and_user_edits_emit(qapp, qtbot):
    values = SettingsValues(
        language="zh-CN", theme="beige",
        image_display=ImageDisplaySettings(RawDisplayMode.ORIGINAL, CanvasBackground.MID),
        preload=StudioPreloadSettings(mode="all", before=2, after=5, memory_mib=128, disk_mib=4096),
        shortcuts=ROWS)
    dialog = SettingsDialog(values=values, edit_cache_runner=_Runner(100 * MIB, 4096 * MIB))
    events = _record(dialog)
    dialog.show()
    assert dialog.language_combo.currentData() == "zh-CN" and dialog.theme_combo.currentData() == "beige"
    assert dialog.raw_display_mode.currentData() == RawDisplayMode.ORIGINAL.value
    assert dialog.canvas_background.currentData() == CanvasBackground.MID.value
    assert dialog.preload_mode.currentData() == "all"
    assert {name: box.value() for name, box in dialog._preload_inputs.items()} == {
        "before": 2, "after": 5, "memory_mib": 128, "disk_mib": 4096, "thumbnail_mib": 512}
    assert not dialog._preload_inputs["before"].isEnabled() and dialog._preload_inputs["memory_mib"].isEnabled()

    dialog.set_language("en")
    dialog.set_theme("dark")
    dialog.set_image_display(ImageDisplaySettings())
    dialog.set_preload(StudioPreloadSettings())
    dialog.set_shortcuts(ROWS)
    qtbot.wait(500)
    assert events == []
    assert dialog._preload_inputs["before"].isEnabled()

    dialog.language_combo._on_action_triggered(dialog.language_combo.findData("zh-CN"))
    dialog.theme_combo._on_action_triggered(dialog.theme_combo.findData("light"))
    assert events == [("language", "zh-CN"), ("theme", "light")]
    events.clear()

    dialog.show_page("images")
    QTest.keyClick(dialog.raw_display_mode, Qt.Key.Key_Left)
    dialog.canvas_background._on_action_triggered(dialog.canvas_background.findData("beige"))
    assert events == [("images", ImageDisplaySettings(RawDisplayMode.ORIGINAL, CanvasBackground.THEME)),
                      ("images", ImageDisplaySettings(RawDisplayMode.ORIGINAL, CanvasBackground.BEIGE))]
    dialog.close()


def test_update_switch_reports_user_clicks_and_keys_only(qapp):
    dialog = SettingsDialog(values=SettingsValues(check_updates=True))
    dialog.show()
    edited = []
    dialog.update_enabled_edited.connect(edited.append)
    switch = dialog.update_switch
    switch.set_checked_silent(False)
    assert edited == []
    switch.click()
    assert edited == [True]
    QTest.mouseClick(switch, Qt.MouseButton.LeftButton)
    assert edited == [True, False]
    QTest.keyClick(switch, Qt.Key.Key_Space)
    QTest.keyClick(switch, Qt.Key.Key_Return)
    assert edited == [True, False, True, False]
    switch.set_checked_silent(True)
    switch.setEnabled(False)
    QTest.mouseClick(switch, Qt.MouseButton.LeftButton)
    QTest.keyClick(switch, Qt.Key.Key_Space)
    assert edited == [True, False, True, False]
    dialog.close()


def test_startup_window_row_lists_last_fixed_sizes_and_maximized(qapp):
    dialog = SettingsDialog(values=SettingsValues(startup_window="1600x900"))
    events = []
    dialog.startup_window_edited.connect(events.append)
    dialog.show()
    combo = dialog.startup_window_combo
    assert tuple(combo.itemData(index) for index in range(combo.count())) == STARTUP_MODES
    texts = [combo.itemText(index) for index in range(combo.count())]
    assert texts[1:4] == ["1920 × 1080", "1600 × 900", "1280 × 720"] and texts[0] and texts[4]
    assert combo.currentData() == "1600x900"
    assert dialog.startup_window_row.title() and dialog.startup_window_row.description()

    # 投影不发信号，用户选了才发
    dialog.set_startup_window("maximized")
    assert combo.currentData() == "maximized" and events == []
    combo._on_action_triggered(combo.findData("1280x720"))
    assert events == ["1280x720"]
    dialog.close()


def test_main_window_saves_the_startup_window_when_it_is_chosen(window_factory, monkeypatch):
    monkeypatch.setattr("src.ui.theme._theme_manager_instance", ThemeManager())
    win = window_factory()
    win.show()
    seen = {}

    def session(dialog):
        dialog.show()
        combo = dialog.startup_window_combo
        seen["shown"] = combo.currentData()
        combo._on_action_triggered(combo.findData("1280x720"))
        seen["saved"] = QSettings("OpenPictureStudio", "MainWindow").value("startupWindow")
        # 没能保存：在这一页说明，显示回到实际生效的那一项
        monkeypatch.setattr(win, "set_startup_mode", lambda mode: False)
        combo._on_action_triggered(combo.findData("maximized"))
        seen["after_failure"] = (combo.currentData(), not dialog._pages["general"].notice.isHidden())
        dialog.done(0)
        return 0

    monkeypatch.setattr(SettingsDialog, "exec", session)
    win.window_actions.action("preferences").trigger()
    assert seen["shown"] == "last" and seen["saved"] == "1280x720"
    assert seen["after_failure"] == ("1280x720", True)
    assert win.startup_mode() == "1280x720"

    # “更多”里不再有默认查看器，它是设置里的一页；证件照是编辑工作区左侧的工具，也不在“更多”里
    names = [spec.name for spec in ACTION_SPECS]
    assert "default_viewer" not in names
    action = win.window_actions.action
    more = win.top_bar._more_button.menu().actions()
    assert [item for item in more if not item.isSeparator()] == [
        action("photo_browser"), action("photo_library"), action("photo_batch"), action("photo_calibration"),
        action("preferences"), action("clear_ratings"), action("reset_all"), action("about")]


def test_preload_edits_are_coalesced_and_flushed_on_close(qapp, qtbot):
    dialog = SettingsDialog(edit_cache_runner=_Runner(100 * MIB, 2048 * MIB))
    events = _record(dialog)
    dialog.show()
    dialog.show_page("performance")
    after = dialog._preload_inputs["after"]
    for _ in range(3):
        after.stepBy(1)
    assert events == []
    qtbot.waitUntil(lambda: bool(events), timeout=3000)
    assert events == [("preload", StudioPreloadSettings(after=6))]
    events.clear()

    QTest.keyClick(dialog.preload_mode, Qt.Key.Key_Left)
    assert events == [("preload", StudioPreloadSettings(mode="off", after=6))]
    assert not after.isEnabled()
    events.clear()

    dialog._preload_inputs["memory_mib"].stepBy(1)
    dialog.close()
    assert events == [("preload", StudioPreloadSettings(mode="off", after=6, memory_mib=65))]


def test_memory_section_projects_the_effective_budget_and_emits_user_edits(qapp, qtbot):
    state = MemoryConfigurator(32 * GIB, lambda size: True).apply(MemorySettings(usage_percent=80, seat_gib=4))
    dialog = SettingsDialog(values=SettingsValues(memory=state), page="performance")
    events = _record(dialog)
    dialog.show()
    assert dialog.memory_section.title() and dialog.memory_usage_row.description()
    assert (dialog.memory_usage.value(), dialog.memory_usage.minimum(), dialog.memory_usage.maximum()) == (80, 30, 90)
    assert (dialog.memory_seat.value(), dialog.memory_seat.minimum(), dialog.memory_seat.maximum()) == (4, 0, 16)
    assert "25.6" in dialog.memory_usage_row.message() and "3.2" in dialog.memory_usage_row.message()
    assert "4" in dialog.memory_seat_row.message()
    # 编辑预览缓存归在内存这一组，仍是预加载偏好里的字段
    assert dialog._preload_rows["memory_mib"].parent() is dialog.memory_section
    assert dialog._preload_rows["memory_mib"].description()

    # 程序投影不发信号；不保留时不写保留了多少，系统没有批准时写明当前生效的值
    idle = MemorySettings(usage_percent=60, seat_gib=0)
    dialog.set_memory(MemoryState(idle, resolve(idle, 32 * GIB), 0))
    assert dialog.memory_seat_row.message() == "" and "19.2" in dialog.memory_usage_row.message()
    wanted = MemorySettings(usage_percent=60, seat_gib=8)
    dialog.set_memory(MemoryState(wanted, resolve(wanted, 32 * GIB), 4 * GIB, seat_refused=True))
    assert dialog.memory_seat.value() == 8 and "4" in dialog.memory_seat_row.message()
    qtbot.wait(500)
    assert events == []

    # 连续的改动合并成一次；关闭时把还没发出的补发
    dialog.memory_usage.stepBy(1)
    dialog.memory_usage.stepBy(1)
    assert events == []
    qtbot.waitUntil(lambda: bool(events), timeout=3000)
    assert events == [("memory", MemorySettings(usage_percent=62, seat_gib=8))]
    events.clear()
    dialog.memory_seat.stepBy(-1)
    dialog.close()
    assert events == [("memory", MemorySettings(usage_percent=62, seat_gib=7))]


def test_memory_controls_are_disabled_when_physical_memory_is_unreadable(qapp):
    dialog = SettingsDialog(page="performance")
    dialog.show()
    assert not dialog.memory_usage.isEnabled() and not dialog.memory_seat.isEnabled()
    assert dialog.memory_usage_row.message() and dialog.memory_seat_row.message() == ""
    assert dialog._preload_inputs["memory_mib"].isEnabled()
    dialog.close()


def test_main_window_applies_memory_settings_immediately(window_factory, monkeypatch):
    calls = []
    approve = [True]

    def register(size):
        calls.append(size)
        return approve[0]

    win = window_factory(memory=MemoryConfigurator(32 * GIB, register))
    win.show()
    pool = win._image_sources.resources
    assert len(calls) == 1 and pool.policy.memory_seat_bytes == 4 * GIB
    assert pool.policy.memory_bytes == 32 * GIB * 90 // 100 and pool.policy.memory_reserve_bytes == 32 * GIB // 10
    assert win._image_sources.policy is pool.policy
    seen = {}

    def session(dialog):
        dialog.show()
        dialog.show_page("performance")
        seen["shown"] = (dialog.memory_usage.value(), dialog.memory_seat.value(), dialog.memory_seat.maximum())
        dialog.memory_edited.emit(MemorySettings(usage_percent=50, seat_gib=2))
        seen["applied"] = (pool.policy.memory_bytes, pool.policy.memory_seat_bytes, pool.policy.memory_reserve_bytes)
        seen["result"] = dialog.memory_usage_row.message()
        # 系统不批准：座位沿用原来的，界面写明
        approve[0] = False
        dialog.memory_edited.emit(MemorySettings(usage_percent=50, seat_gib=6))
        seen["refused"] = (pool.policy.memory_seat_bytes, dialog.memory_seat.value(), dialog.memory_seat_row.message())
        # 预览缓存的容量在编辑工作区建立之前也要生效
        dialog.preload_edited.emit(StudioPreloadSettings(memory_mib=128))
        seen["preview"] = pool.policy.preview_cache_bytes
        dialog.done(0)
        return 0

    monkeypatch.setattr(SettingsDialog, "exec", session)
    win.window_actions.action("preferences").trigger()

    assert seen["shown"] == (90, 4, 16)
    assert seen["applied"] == (16 * GIB, 2 * GIB, 32 * GIB // 10) and "16" in seen["result"]
    assert seen["refused"][:2] == (2 * GIB, 6) and "2" in seen["refused"][2]
    assert seen["preview"] == 128 * MIB
    assert MemorySettings.load(QSettings("OpenPictureStudio", "Memory")) == MemorySettings(usage_percent=50, seat_gib=6)
    assert win._memory_state.seat_refused and len(calls) == 3


def test_shortcut_rows_reset_conflict_and_reset_all(qapp, monkeypatch):
    dialog = SettingsDialog(values=SettingsValues(shortcuts=ROWS), page="shortcuts")
    events = _record(dialog)
    dialog.show()
    assert [section.title() for _, section in dialog._shortcut_sections] == ["File", "Edit", "View"]
    row, edit, reset = dialog._shortcut_widgets["Edit/Manual Crop"]
    assert (row.title(), edit.sequence()) == ("Crop", "Ctrl+Alt+C") and reset.isEnabled()
    assert not dialog._shortcut_widgets["File/Open File"][2].isEnabled()
    assert dialog._shortcut_widgets["View/Zoom In"][1].property("state") == "empty"

    reset.click()
    QTest.mouseClick(edit, Qt.MouseButton.LeftButton)
    QTest.keyClick(edit, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
    assert events == [("shortcut", "Edit/Manual Crop", "Ctrl+Shift+C"), ("shortcut", "Edit/Manual Crop", "Ctrl+O")]

    dialog.show_shortcut_conflict("Edit/Manual Crop", "Ctrl+O", "Open Files")
    assert "Ctrl+O" in row.message() and "Open Files" in row.message()
    widgets = dict(dialog._shortcut_widgets)
    dialog.set_shortcuts(ROWS)
    # 同一组行原位更新，冲突说明随投影收起
    assert dialog._shortcut_widgets == widgets and row.message() == ""

    events.clear()
    answers = ["cancel", "confirm"]
    monkeypatch.setattr(MessageDialog, "run", lambda self: answers.pop(0))
    dialog.reset_shortcuts.click()
    assert events == []
    dialog.reset_shortcuts.click()
    assert events == [("reset",)]

    dialog.set_shortcuts(ROWS[:2])
    assert list(dialog._shortcut_widgets) == ["File/Open File", "Edit/Manual Crop"]
    assert [group for group, _ in dialog._shortcut_sections] == ["File", "Edit"]
    dialog.close()


def test_cache_rows_show_usage_clear_and_disconnect_on_close(qapp):
    thumbnails, edits = _Runner(186 * MIB, 512 * MIB), _Runner(1188 * MIB, 2048 * MIB, writer=False)
    dialog = SettingsDialog(thumbnail_cache_runner=thumbnails, edit_cache_runner=edits)
    dialog.show()
    dialog.show_page("performance")
    assert thumbnails.queries >= 1 and edits.queries >= 1
    assert (dialog.thumbnail_usage.value(), dialog.thumbnail_usage.maximum()) == (186, 512)
    assert "186.0" in dialog.thumbnail_usage.text() and "512" in dialog.thumbnail_usage.text()
    assert (dialog.edit_cache_usage.value(), dialog.edit_cache_usage.maximum()) == (1188, 2048)
    # 只读的缓存不能清除、不能改上限，并说明原因
    assert not dialog.edit_cache_clear.isEnabled() and not dialog._preload_inputs["disk_mib"].isEnabled()
    assert dialog.edit_cache_row.message()

    dialog.thumbnail_clear.click()
    assert thumbnails.cleared == 1
    thumbnails.clear_finished.emit(12, "")
    assert dialog.thumbnail_row.message()

    dialog.close()
    thumbnails.status_snapshot = ManagedCacheStatus("x", 0, 512 * MIB, True, True)
    thumbnails.state_changed.emit()
    assert dialog.thumbnail_usage.value() == 186


def test_settings_dialog_retranslates_in_place(qapp, monkeypatch):
    previous = get_translation_service()
    service = TranslationService(default_language="en", fallback_language="en")
    service.set_language("en")
    set_translation_service(service)
    try:
        dialog = SettingsDialog(values=SettingsValues(shortcuts=ROWS), edit_cache_runner=_Runner(MIB, 2048 * MIB))
        dialog.show()
        dialog.show_page("performance")
        row = dialog._shortcut_widgets["File/Open File"][0]
        assert dialog._pages["performance"].title.text() == "Performance & memory"
        assert dialog.preload_mode_row.title() == "Preload range"
        service.set_language("zh-CN")
        assert dialog.current_page() == "performance"
        assert dialog._pages["performance"].title.text() == "性能与内存"
        assert dialog.preload_mode_row.title() == "预加载范围" and dialog.preload_section.title() == "预加载"
        assert dialog.theme_combo.itemText(0) == "深色" and dialog.edit_cache_clear.text() == "清除编辑代理缓存"
        assert dialog._shortcut_sections[0][1].title() == "文件"
        assert dialog._shortcut_widgets["File/Open File"][0] is row
        dialog.close()
    finally:
        set_translation_service(previous)


def test_main_window_applies_each_setting_immediately(window_factory, monkeypatch, qtbot):
    monkeypatch.setattr("src.ui.theme._theme_manager_instance", ThemeManager())
    service = TranslationService(default_language="en", fallback_language="en")
    service.set_language("en")
    monkeypatch.setattr("src.i18n.translation_service._translation_service", service)
    win = window_factory()
    win.show()
    seen = {}

    def session(dialog):
        dialog.show()
        seen["rows"] = len(dialog._shortcut_widgets)
        seen["theme"] = dialog.theme_combo.currentData()

        dialog.theme_combo._on_action_triggered(dialog.theme_combo.findData("light"))
        seen["applied_theme"] = get_theme_manager().current_theme()

        dialog.canvas_background._on_action_triggered(dialog.canvas_background.findData("mid"))
        seen["canvas"] = get_theme_manager()._canvas_background

        edit = dialog._shortcut_widgets["Edit/Grid Crop"][1]
        edit.sequence_edited.emit("Ctrl+Alt+J")
        seen["grid"] = win.window_actions.action("grid_crop").shortcut().toString()
        seen["grid_shown"] = edit.sequence()
        # Ctrl+S 是“保存项目”固定的快捷键：不写入，说明原因，显示保持原值
        edit.sequence_edited.emit("Ctrl+S")
        seen["conflict"] = dialog._shortcut_widgets["Edit/Grid Crop"][0].message()
        seen["grid_after_conflict"] = win.window_actions.action("grid_crop").shortcut().toString()

        monkeypatch.setattr(MessageDialog, "run", lambda self: "confirm")
        dialog.reset_shortcuts.click()
        seen["grid_reset"] = win.window_actions.action("grid_crop").shortcut().toString()

        dialog.show_page("performance")
        QTest.keyClick(dialog.preload_mode, Qt.Key.Key_Left)
        seen["preload"] = win._preload_settings.mode

        dialog.language_combo._on_action_triggered(dialog.language_combo.findData("zh-CN"))
        seen["title"] = dialog._pages["shortcuts"].title.text()
        seen["row_text"] = dialog._shortcut_widgets["File/Open File"][0].title()
        dialog.done(0)
        return 0

    monkeypatch.setattr(SettingsDialog, "exec", session)
    win.window_actions.action("preferences").trigger()

    customizable = [spec for spec in ACTION_SPECS if spec.settings_key]
    assert seen["rows"] == len(customizable) == 29
    assert seen["applied_theme"] == "light" and seen["canvas"] == "mid"
    assert (seen["grid"], seen["grid_shown"]) == ("Ctrl+Alt+J", "Ctrl+Alt+J")
    assert seen["conflict"] and seen["grid_after_conflict"] == "Ctrl+Alt+J"
    assert seen["grid_reset"] == "Ctrl+Shift+G"
    assert seen["preload"] == "off"
    assert (seen["title"], seen["row_text"]) == ("快捷键", "打开文件")

    assert QSettings("OpenPictureStudio", "Theme").value("current") == "light"
    assert QSettings("OpenPictureStudio", "Language").value("current") == "zh-CN"
    assert ImageDisplaySettings.load(QSettings("OpenPictureStudio", "ImageDisplay")).canvas_background \
        is CanvasBackground.MID
    assert StudioPreloadSettings.load(QSettings("OpenPictureStudio", "OPSPreload")).mode == "off"
    assert QSettings("OpenPictureStudio", "Shortcuts").value("Edit/Grid Crop") is None
    assert win.window_actions.action("grid_crop").shortcut() == QKeySequence("Ctrl+Shift+G")


def test_resource_pages_are_requested_once_when_first_entered(qapp):
    dialog = SettingsDialog()
    requests = []
    dialog.resource_pages_requested.connect(lambda: requests.append(dialog.current_page()))
    # 还没显示时不请求：这时还没有人在听
    dialog.show_page("tools")
    assert requests == []
    dialog.show_page("general")
    dialog.show()
    assert requests == []
    dialog.show_page("models")
    dialog.show_page("runtime")
    dialog.show_page("tools")
    assert requests == ["models"]
    holder = dialog._pages["tools"]
    assert holder.page is None and holder.status.text() and holder.status.maximum() == 0

    # 没准备好时写出原因，再次进入会重新请求
    dialog.show_resource_error("OPS 初始化失败")
    assert holder.status.isHidden() and not holder.empty.isHidden()
    assert holder.empty.title() == "OPS 初始化失败"
    dialog.show_page("general")
    dialog.show_page("tools")
    assert requests == ["models", "tools"]

    # 关于页不需要这些服务
    dialog.show_page("about")
    assert dialog.about_page.version_text() and requests == ["models", "tools"]
    dialog.close()


def test_main_window_merges_tools_models_and_about_into_settings(window_factory, monkeypatch):
    monkeypatch.setattr("src.ui.theme._theme_manager_instance", ThemeManager())
    win = window_factory()
    win.show()
    names = [spec.name for spec in ACTION_SPECS]
    assert "tool_manager" not in names and "preferences" in names and "about" in names
    assert win.studio_workspace is None
    seen, restarts = {}, []
    monkeypatch.setattr(win, "request_restart", lambda: restarts.append(True))

    def session(dialog):
        dialog.show()
        seen["page"] = dialog.current_page()
        seen["waiting"] = dialog.resource_page("tools") is None
        if dialog.current_page() != "tools":
            dialog.show_page("tools")
        # 编辑内核按需装配，就绪后三页才建出来
        wait_until(lambda: dialog.resource_page("tools") is not None, "设置里的工具页就绪", timeout=60)
        tools = dialog.resource_page("tools")
        core = win.studio_workspace.core
        seen["pages"] = sorted(dialog._resource_pages)
        seen["selected"] = tools.selected_tool()

        # 工具的开关点了就提交
        tools.select_tool("beauty")
        assert core.tools.check_enabled("beauty").success
        tools._toggle.setChecked(False)
        seen["disabled"] = not core.tools.check_enabled("beauty").success
        seen["saved"] = list(QSettings("OpenPictureStudio", "Tools").value("disabled") or [])
        seen["action_hidden"] = not win.window_actions.action("beauty").isVisible()
        tools._toggle.setChecked(True)
        seen["enabled_again"] = core.tools.check_enabled("beauty").success
        seen["saved_after"] = list(QSettings("OpenPictureStudio", "Tools").value("disabled") or [])
        # 调色是基础工具，不能停用
        tools.select_tool("color_adjust")
        seen["color_toggle"] = tools._toggle.isVisible()

        # 从工具页查看某个模型，切到模型页并定位
        token = dialog.resource_page("models")._rows[0].descriptor.key.token
        tools.model_view_requested.emit(token)
        seen["model_page"] = (dialog.current_page(), dialog.resource_page("models")._page.navigation.selected_key() == token)

        if seen.get("restart"):
            dialog.resource_page("runtime").restart_requested.emit()
        else:
            dialog.done(0)
        return 0

    monkeypatch.setattr(SettingsDialog, "exec", session)
    win.window_actions.action("preferences").trigger()
    assert (seen["page"], seen["waiting"]) == ("general", True)
    assert seen["pages"] == ["models", "runtime", "tools"]
    assert seen["disabled"] and seen["saved"] == ["beauty"] and seen["action_hidden"]
    assert seen["enabled_again"] and seen["saved_after"] == []
    assert seen["color_toggle"] is False
    assert seen["model_page"] == ("models", True)
    assert restarts == [] and win._settings_dialog is None

    # 运行环境页要求重启：窗口关闭后才执行
    seen["restart"] = True
    win.window_actions.action("preferences").trigger()
    wait_until(lambda: restarts == [True], "设置窗口关闭后执行重启")


def test_main_window_routes_about_missing_resources_and_model_install_to_settings(window_factory, monkeypatch):
    monkeypatch.setattr("src.ui.theme._theme_manager_instance", ThemeManager())
    win = window_factory()
    win.show()
    opened = []

    def session(dialog):
        dialog.show()
        entry = {"page": dialog.current_page(), "parent": dialog.parent()}
        if dialog.current_page() in dialog.RESOURCE_PAGES:
            wait_until(lambda: dialog.resource_page("tools") is not None, "设置里的资源页就绪", timeout=60)
            entry["tool"] = dialog.resource_page("tools").selected_tool()
            entry["filter"] = dialog.resource_page("models").filter.currentData()
        opened.append(entry)
        dialog.done(0)
        return 0

    monkeypatch.setattr(SettingsDialog, "exec", session)
    win.window_actions.action("about").trigger()
    assert opened[-1]["page"] == "about" and opened[-1]["parent"] is win

    # 缺资源提示条的入口：定位到所在的工具
    win._show_settings("tools", tool_id="bg_remove")
    assert (opened[-1]["page"], opened[-1]["tool"]) == ("tools", "bg_remove")

    # 面板里的“安装模型…”：没有选中模型时只列出这一类能力的模型
    win._show_model_settings("segmentation", "")
    assert (opened[-1]["page"], opened[-1]["filter"]) == ("models", "segmentation")
    win.studio_workspace.model_management_requested.emit("denoise", "")
    assert (opened[-1]["page"], opened[-1]["filter"]) == ("models", "denoise")
