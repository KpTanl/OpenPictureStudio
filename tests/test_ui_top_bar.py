"""动作表与顶栏：动作只建一次并带默认快捷键，顶栏只表达用户意图，程序回填不发信号。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QAction, QColor, QKeySequence, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from resources.styles.theme_variables import TOP_BAR_HEIGHT, get_theme_qss  # noqa: E402
from src.ui.shortcut_manager import ShortcutManager  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.ui.widgets.action_button import ActionIconButton, plain_action_text  # noqa: E402
from src.ui.widgets.top_bar import EDITOR, VIEWER, TopBar, TopBarActions  # noqa: E402
from src.ui.window_actions import ACTION_SPECS, WindowActions  # noqa: E402

THEMES = ("dark", "light", "beige")


@pytest.fixture
def theme(qapp):
    previous = qapp.styleSheet(), qapp.property("ops.theme"), QPalette(qapp.palette())
    manager = ThemeManager()
    manager.apply("dark")
    yield manager
    qapp.setStyleSheet(previous[0])
    qapp.setProperty("ops.theme", previous[1])
    qapp.setPalette(previous[2])


class _Settings:
    """代替 QSettings 的取值表，不触碰本机设置。"""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def value(self, key, default=None):
        return self._values.get(key, default)

    def setValue(self, key, value):
        self._values[key] = value

    def remove(self, key):
        self._values.pop(key, None)

    def sync(self):
        pass


def _shortcuts(values=None):
    manager = ShortcutManager("OpenPictureStudioTests", "TopBarShortcuts")
    manager._settings = _Settings(values)
    return manager


def _window_actions(window, shortcuts=None, calls=None, translate=None):
    calls = calls if calls is not None else []
    handlers = {spec.name: (lambda name=spec.name: calls.append(name)) for spec in ACTION_SPECS}
    return WindowActions(window, shortcuts or _shortcuts(), translate or (lambda key, default: default), handlers)


def _key(action):
    return action.shortcut().toString()


def test_actions_are_created_once_with_their_default_shortcuts(theme):
    window = QWidget()
    calls = []
    actions = _window_actions(window, calls=calls)
    assert len(window.actions()) == len(ACTION_SPECS) == len({spec.name for spec in ACTION_SPECS})
    settings_keys = [spec.settings_key for spec in ACTION_SPECS if spec.settings_key]
    assert len(settings_keys) == len(set(settings_keys))

    assert _key(actions.action("open_file")) == "Ctrl+O"
    assert _key(actions.action("rotate_cw")) == "Ctrl+R"
    assert _key(actions.action("flip_horizontal")) == "Ctrl+H"
    assert _key(actions.action("color_adjust")) == "Ctrl+L"
    assert _key(actions.action("undo")) == "Ctrl+Z" and _key(actions.action("redo")) == "Ctrl+Shift+Z"
    assert _key(actions.action("image_processing")) == "Ctrl+M"
    assert _key(actions.action("image_processing_alias")) == ""
    assert _key(actions.action("about")) == ""

    actions.action("open_file").trigger()
    actions.action("about").trigger()
    assert calls == ["open_file", "about"]

    with pytest.raises(KeyError):
        WindowActions(window, _shortcuts(), lambda key, default: default, {"open_file": lambda: None})
    window.deleteLater()


def test_saved_shortcuts_override_defaults_and_empty_means_none(theme):
    window = QWidget()
    shortcuts = _shortcuts({"File/Open File": "Ctrl+Alt+P", "Edit/Rotate 90 CW": "",
                            "Edit/Convert Format": "Ctrl+M"})
    actions = _window_actions(window, shortcuts)
    assert _key(actions.action("open_file")) == "Ctrl+Alt+P"
    assert _key(actions.action("rotate_cw")) == ""
    assert _key(actions.action("rotate_180")) == "Ctrl+Shift+R"
    # 别名与目标动作快捷键相同时只留目标的，避免歧义
    assert _key(actions.action("image_processing")) == "Ctrl+M"
    assert _key(actions.action("image_processing_alias")) == ""

    shortcuts._settings = _Settings({"Edit/Convert Format": "Ctrl+Alt+M"})
    shortcuts.reload()
    assert _key(actions.action("open_file")) == "Ctrl+O"
    assert _key(actions.action("image_processing_alias")) == "Ctrl+Alt+M"
    window.deleteLater()


def test_shortcut_rows_come_from_the_action_table(theme):
    window = QWidget()
    shortcuts = _shortcuts({"Edit/Rotate 90 CW": "", "文件/另存为": "Ctrl+Alt+S"})
    actions = _window_actions(window, shortcuts)
    rows = {row.key: row for row in actions.shortcut_rows()}
    specs = {spec.settings_key: spec for spec in ACTION_SPECS if spec.settings_key}
    # 设置里列出的就是动作表里可以自定义的那些，默认值只在动作表里声明
    assert list(rows) == list(specs) and len(rows) == 29
    assert all(row.default == specs[key].shortcut and row.group == key.split("/")[0] for key, row in rows.items())
    assert {row.group for row in rows.values()} == {"File", "Edit", "View"}
    assert (rows["File/Open File"].text, rows["File/Open File"].sequence) == ("Open Files", "Ctrl+O")
    assert rows["Edit/Rotate 90 CW"].sequence == "" and rows["Edit/Rotate 90 CW"].default == "Ctrl+R"
    # 旧键名下保存过的值照常读到
    assert rows["File/Save As"].sequence == "Ctrl+Alt+S"
    alias = rows["Edit/Convert Format"]
    assert alias.alias and alias.text == "Image Format Processing (alternate)" and alias.sequence == ""
    assert not rows["Edit/Compress Image"].alias
    window.deleteLater()


def test_shortcut_owner_set_and_reset(theme):
    window = QWidget()
    shortcuts = _shortcuts()
    actions = _window_actions(window, shortcuts)
    assert actions.shortcut_owner("") == ""
    assert actions.shortcut_owner("Ctrl+O") == "Open Files"
    assert actions.shortcut_owner("Ctrl+O", exclude="File/Open File") == ""
    # 不能自定义的快捷键同样算占用
    assert actions.shortcut_owner("Ctrl+S", exclude="Edit/Grid Crop") == "Save Project"
    assert actions.shortcut_owner("Ctrl+Alt+F9") == ""

    shortcuts.set_sequence("Edit/Grid Crop", "Ctrl+Alt+J")
    shortcuts.set_sequence("View/Zoom In", "")
    assert _key(actions.action("grid_crop")) == "Ctrl+Alt+J" and _key(actions.action("zoom_in")) == ""
    assert shortcuts.sequence("Edit/Grid Crop") == "Ctrl+Alt+J" and shortcuts.default("Edit/Grid Crop") == "Ctrl+Shift+G"
    assert actions.shortcut_owner("Ctrl+Shift+G") == "" and actions.shortcut_owner("Ctrl+Alt+J") == "Grid Crop"
    with pytest.raises(KeyError):
        shortcuts.set_sequence("Edit/Image Denoise", "Ctrl+Shift+N")

    shortcuts.reset()
    assert _key(actions.action("grid_crop")) == "Ctrl+Shift+G" and _key(actions.action("zoom_in")) == "Ctrl+="
    assert shortcuts._settings._values == {}
    window.deleteLater()


def test_actions_retranslate_in_place(theme):
    window = QWidget()
    language = {"current": "en"}

    def translate(key, default):
        return default if language["current"] == "en" else f"译:{key}"

    actions = _window_actions(window, translate=translate)
    action = actions.action("open_file")
    assert action.text() == "&Open Files..."
    language["current"] = "zh"
    actions.retranslate()
    assert actions.action("open_file") is action and action.text() == "译:menu.file.open_files"
    assert _key(action) == "Ctrl+O"
    window.deleteLater()


def test_action_availability_follows_image_tools_and_blocking(theme):
    window = QWidget()
    actions = _window_actions(window)
    export_pdf, crop, open_file, undo = (actions.action(name) for name in ("export_pdf", "crop", "open_file", "undo"))
    assert not export_pdf.isEnabled() and not actions.action("save_as").isEnabled()
    assert crop.isEnabled() and crop.isVisible() and open_file.isEnabled()

    actions.set_has_image(True)
    assert export_pdf.isEnabled() and actions.action("ai_enhance").isEnabled()

    actions.set_tool_check(lambda tool_id: tool_id != "crop")
    assert not crop.isVisible() and not crop.isEnabled()
    assert actions.action("lasso").isVisible() and actions.action("lasso").isEnabled()
    actions.set_tool_check(None)
    assert crop.isVisible() and crop.isEnabled()

    actions.set_enabled("undo", False)
    assert not undo.isEnabled()
    actions.set_enabled("undo", True)
    actions.set_blocked(True)
    assert not any(action.isEnabled() for action in window.actions())
    actions.set_blocked(False)
    assert undo.isEnabled() and export_pdf.isEnabled() and open_file.isEnabled()
    actions.set_has_image(False)
    assert not export_pdf.isEnabled() and undo.isEnabled()
    window.deleteLater()


def test_plain_action_text_strips_mnemonics_and_ellipsis():
    assert plain_action_text("&Open Files...") == "Open Files"
    assert plain_action_text("打开文件(&O)...") == "打开文件"
    assert plain_action_text("项目另存为…") == "项目另存为"
    assert plain_action_text("Rotate 90° CW") == "Rotate 90° CW"


def test_action_button_mirrors_its_action(theme):
    host = QWidget()
    triggered = []
    action = QAction("&Save Project...", host)
    action.setShortcut(QKeySequence("Ctrl+S"))
    action.triggered.connect(lambda: triggered.append(True))
    button = ActionIconButton(action, "device-floppy", host)
    host.show()
    assert button.toolTip() == "Save Project  Ctrl+S" and button.accessibleName() == "Save Project"
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert triggered == [True]

    action.setEnabled(False)
    assert not button.isEnabled()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert triggered == [True]
    action.setEnabled(True)
    action.setText("保存项目")
    action.setShortcut(QKeySequence())
    assert button.isEnabled() and button.toolTip() == "保存项目"
    host.close()


class _Bar:
    """独立搭一条顶栏：动作是普通的 QAction，触发时记下名字。"""

    def __init__(self, width=1100):
        self.host = QWidget()
        self.calls = []
        self.language = "en"
        names = ("open_file", "open_folder", "project_open", "project_save", "undo", "redo", "save_as",
                 "export_pdf", "project_export", "apply_all", "preferences", "about")
        self.actions = {}
        for name in names:
            action = QAction(name, self.host)
            action.triggered.connect(lambda _checked=False, name=name: self.calls.append(name))
            self.actions[name] = action
        a = self.actions
        self.bar = TopBar(TopBarActions(
            open_menu=(a["open_file"], a["open_folder"]),
            editor_buttons=((a["project_open"], "folder-open"), (a["project_save"], "device-floppy"), None,
                            (a["undo"], "arrow-back-up"), (a["redo"], "arrow-forward-up")),
            export_default=(a["save_as"], a["project_export"]),
            export_menus=((a["save_as"], a["export_pdf"]), (a["project_export"], None, a["apply_all"])),
            more_menu=(a["preferences"], None, a["about"]),
        ), self.translate, self.host)
        self.bar.setGeometry(0, 0, width, TOP_BAR_HEIGHT)
        self.host.resize(width, TOP_BAR_HEIGHT + 20)
        self.host.show()
        QApplication.processEvents()

    def translate(self, key, default):
        return default if self.language == "en" else f"译:{key}"


def test_top_bar_reports_workspace_clicks_but_not_programmatic_changes(theme):
    built = _Bar()
    bar = built.bar
    requests = []
    bar.workspace_requested.connect(requests.append)
    assert bar.workspace() == VIEWER and not bar.styleSheet() and bar.height() == TOP_BAR_HEIGHT
    assert not bar._open_button.isHidden() and all(widget.isHidden() for widget in bar._editor_widgets)

    bar.set_workspace(EDITOR)
    assert requests == [] and bar.workspace() == EDITOR and bar._switch.currentIndex() == EDITOR
    assert bar._open_button.isHidden() and not any(widget.isHidden() for widget in bar._editor_widgets)

    switch = bar._switch
    QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(8, switch.height() // 2))
    assert requests == [VIEWER]
    # 主窗口拒绝切换时把顶栏拨回实际所在的工作区，不再发出请求
    bar.set_workspace(EDITOR)
    assert requests == [VIEWER] and switch.currentIndex() == EDITOR
    with pytest.raises(ValueError):
        bar.set_workspace(2)
    built.host.close()


def test_export_button_runs_the_default_action_of_the_current_workspace(theme):
    built = _Bar()
    bar, actions = built.bar, built.actions
    QTest.mouseClick(bar._export_button, Qt.MouseButton.LeftButton)
    assert built.calls == ["save_as"]
    assert bar._export_options.menu().actions()[:2] == [actions["save_as"], actions["export_pdf"]]

    actions["save_as"].setEnabled(False)
    assert not bar._export_button.isEnabled()
    bar.set_workspace(EDITOR)
    assert bar._export_button.isEnabled()
    QTest.mouseClick(bar._export_button, Qt.MouseButton.LeftButton)
    assert built.calls == ["save_as", "project_export"]
    editor_menu = bar._export_options.menu().actions()
    assert editor_menu[0] is actions["project_export"] and editor_menu[1].isSeparator()
    assert editor_menu[2] is actions["apply_all"]
    actions["project_export"].setEnabled(False)
    assert not bar._export_button.isEnabled()

    assert bar._open_button.menu().actions() == [actions["open_file"], actions["open_folder"]]
    more = bar._more_button.menu().actions()
    assert more[0] is actions["preferences"] and more[1].isSeparator() and more[2] is actions["about"]
    built.host.close()


def test_editor_buttons_trigger_and_follow_their_actions(theme):
    built = _Bar()
    bar, actions = built.bar, built.actions
    bar.set_workspace(EDITOR)
    buttons = [widget for widget in bar._editor_widgets if isinstance(widget, ActionIconButton)]
    assert len(buttons) == 4
    actions["undo"].setEnabled(False)
    assert [button.isEnabled() for button in buttons] == [True, True, False, True]
    QTest.mouseClick(buttons[1], Qt.MouseButton.LeftButton)
    QTest.mouseClick(buttons[2], Qt.MouseButton.LeftButton)
    assert built.calls == ["project_save"]
    built.host.close()


def test_photo_info_toggle_reports_clicks_only(theme):
    built = _Bar()
    bar = built.bar
    toggles = []
    bar.photo_info_toggled.connect(toggles.append)
    bar.set_photo_info_state(True, True)
    assert toggles == [] and bar._info_toggle.isChecked()
    QTest.mouseClick(bar._info_toggle, Qt.MouseButton.LeftButton)
    assert toggles == [False]
    bar.set_photo_info_state(False, False)
    assert toggles == [False] and not bar._info_toggle.isEnabled()
    built.host.close()


def test_title_is_centered_and_gives_way_when_space_runs_out(theme):
    built = _Bar(width=1320)
    bar = built.bar
    bar.set_title("DSC_6674.NEF", "5568 × 3712")
    assert bar.title() == ("DSC_6674.NEF", "5568 × 3712")
    title = bar._title
    assert abs(title.geometry().center().x() - bar.width() // 2) <= 1
    assert title.width() == title.sizeHint().width()

    bar.set_title("一个非常非常长的文件名_" * 12 + ".tiff", "未保存")
    assert title.geometry().left() > bar._left.geometry().right()
    assert title.geometry().right() < bar._right.geometry().left()
    assert title.width() < title.sizeHint().width()

    bar.setGeometry(0, 0, 800, TOP_BAR_HEIGHT)
    QApplication.processEvents()
    assert title.width() > 0
    assert title.geometry().left() > bar._left.geometry().right()
    assert title.geometry().right() < bar._right.geometry().left()
    # 两侧的操作已经挨在一起时标题不占位
    bar.setGeometry(0, 0, bar._left.width() + bar._right.width() + 30, TOP_BAR_HEIGHT)
    QApplication.processEvents()
    assert title.width() == 0
    bar.set_title("")
    assert bar.title() == ("", "")
    built.host.close()


def test_top_bar_retranslates_in_place(theme):
    built = _Bar()
    bar = built.bar
    assert bar._switch._segments[VIEWER]["text"] == "View" and bar._export_button.text() == "Export"
    assert bar._info_toggle.text() == "Photo Info" and bar._more_button.text() == "More"
    switch, export = bar._switch, bar._export_button
    built.language = "zh"
    bar.retranslate_ui()
    assert bar._switch is switch and bar._export_button is export
    assert switch._segments[EDITOR]["text"] == "译:topbar.workspace.edit"
    assert export.text() == "译:toolbar.export" and bar._open_button.toolTip() == "译:toolbar.open"
    built.host.close()


@pytest.mark.parametrize("name", THEMES)
def test_top_bar_takes_its_surface_from_the_theme(theme, name):
    qss = get_theme_qss(name)
    for selector in ("QWidget#topBar {", "QFrame#topBarSeparator {", "QPushButton::menu-indicator {"):
        assert selector in qss, selector
    assert "QMenuBar" not in qss
    theme.apply(name)
    built = _Bar()
    bar = built.bar
    QApplication.processEvents()
    image = built.host.grab().toImage()
    tokens = get_theme_tokens()
    middle = bar._title.geometry().right() + 20
    assert image.pixelColor(middle, TOP_BAR_HEIGHT // 2).name() == QColor(tokens["surface"]).name()
    assert image.pixelColor(middle, TOP_BAR_HEIGHT - 1).name() == QColor(tokens["hairline_solid"]).name()
    built.host.close()


def test_main_window_mounts_the_top_bar_and_keeps_shortcuts_without_a_menu_bar(theme):
    from src.ui.main_window import MainWindow

    window = MainWindow()
    try:
        assert window.menuWidget() is window.top_bar and isinstance(window.top_bar, TopBar)
        assert not hasattr(window, "workspace_switcher") and not hasattr(window, "export_btn")
        registered = set(window.actions())
        assert all(window.window_actions.action(spec.name) in registered for spec in ACTION_SPECS)
        assert window.top_bar.workspace() == VIEWER and window.top_bar.title() == ("", "")
        assert not window.window_actions.action("save_as").isEnabled()
        assert not window.window_actions.action("undo").isEnabled()
        assert not window.top_bar._export_button.isEnabled()

        # 没有图片时切到编辑会被拒绝，顶栏回到看图
        window.top_bar._switch.setCurrentIndex(EDITOR)
        window.top_bar.workspace_requested.emit(EDITOR)
        assert window._current_workspace_index == VIEWER and window.top_bar.workspace() == VIEWER
        assert window.top_bar._switch.currentIndex() == VIEWER

        open_file = window.window_actions.action("open_file")
        window.retranslate_ui()
        assert window.window_actions.action("open_file") is open_file and window.menuWidget() is window.top_bar
    finally:
        window.shutdown_previews()
        window.deleteLater()
