"""主窗口的动作表：每个动作只创建一次，登记在窗口与快捷键管理器上。

顶栏、工具条和菜单从这里取动作；文字随语言原位更新，可用状态由所属方经本模块设置。
快捷键的键名与默认值在 ``ACTION_SPECS`` 唯一定义；带 ``settings_key`` 的可在设置里改，
设置页列出的各行与“这个快捷键是否已被占用”的查询也由本模块给出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QWidget

from .shortcut_manager import ShortcutManager
from .widgets.action_button import plain_action_text


@dataclass(frozen=True)
class ActionSpec:
    name: str
    text_key: str
    text_default: str
    shortcut: str = ""
    settings_key: str = ""
    needs_image: bool = False
    tool_id: str = ""
    alias_of: str = ""


ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec("open_file", "menu.file.open_files", "&Open Files...", "Ctrl+O", "File/Open File"),
    ActionSpec("open_folder", "menu.file.open_folder", "Open &Folder...", "Ctrl+Shift+O", "File/Open Folder"),
    ActionSpec("project_open", "studio.project_open", "Open Project…", "Ctrl+Alt+O"),
    ActionSpec("project_save", "studio.project_save", "Save Project", "Ctrl+S"),
    ActionSpec("project_save_as", "studio.project_save_as", "Save Project As…"),
    ActionSpec("project_export", "studio.project_export", "Export Image…", "Ctrl+Alt+E"),
    ActionSpec("save_as", "menu.file.save_as", "&Save As...", "Ctrl+Shift+S", "File/Save As", needs_image=True),
    ActionSpec("set_wallpaper", "menu.file.set_wallpaper", "Set as &Wallpaper...", "", "File/Set Wallpaper",
               needs_image=True),
    ActionSpec("exit", "menu.file.exit", "E&xit", "Ctrl+Q", "File/Exit"),
    ActionSpec("undo", "studio.undo", "Undo", "Ctrl+Z"),
    ActionSpec("redo", "studio.redo", "Redo", "Ctrl+Shift+Z"),
    ActionSpec("reset_all", "studio.reset_all", "Reset All"),
    ActionSpec("apply_all", "topbar.apply_to_original", "Apply to Original…"),
    ActionSpec("assistant", "assistant.title", "AI Assistant", "Ctrl+Alt+A"),
    ActionSpec("photo_batch", "batch.title", "Batch Adjustments…", needs_image=True),
    ActionSpec("photo_export", "export_queue.title", "Batch Export…"),
    ActionSpec("photo_browser", "browser.title", "Photo Selection", "Ctrl+Alt+G", "View/Photo Selection"),
    ActionSpec("photo_calibration", "calibration.title", "Photo Groups and Calibration…"),
    ActionSpec("photo_library", "library.title", "Photo Library…"),
    ActionSpec("photo_keep", "browser.keep", "Keep", needs_image=True),
    ActionSpec("photo_reject", "browser.reject", "Reject", needs_image=True),
    ActionSpec("photo_unmark", "browser.unmarked", "Clear mark", needs_image=True),
    ActionSpec("crop", "menu.edit.crop", "&Crop...", "Ctrl+Shift+C", "Edit/Manual Crop", tool_id="crop"),
    ActionSpec("grid_crop", "menu.edit.grid_crop", "&Grid Crop...", "Ctrl+Shift+G", "Edit/Grid Crop",
               tool_id="grid_crop"),
    ActionSpec("shape_crop", "menu.edit.shape_crop", "Sha&pe Crop...", "Ctrl+Shift+P", "Edit/Shape Crop",
               tool_id="shape_crop"),
    ActionSpec("image_processing", "menu.edit.compress", "Image For&mat Processing...", "Ctrl+M",
               "Edit/Compress Image", needs_image=True),
    ActionSpec("image_processing_alias", "menu.edit.compress", "Image For&mat Processing...", "",
               "Edit/Convert Format", needs_image=True, alias_of="image_processing"),
    ActionSpec("export_pdf", "menu.edit.export_pdf", "Export to &PDF...", "", "Edit/Export PDF", needs_image=True),
    ActionSpec("bg_remove", "menu.edit.remove_bg", "Remove &Background...", "", "Edit/Remove Background",
               tool_id="bg_remove"),
    ActionSpec("lasso", "menu.edit.lasso", "&Lasso Cutout...", "Ctrl+Shift+L", "Edit/Lasso Cutout", tool_id="lasso"),
    ActionSpec("id_photo", "menu.edit.id_photo", "ID Photo &Maker...", "", "Edit/ID Photo Maker", tool_id="id_photo"),
    ActionSpec("color_adjust", "menu.edit.color_lab", "Color &Lab...", "Ctrl+L", "Edit/Color Lab",
               tool_id="color_adjust"),
    ActionSpec("ai_enhance", "menu.edit.ai_enhance", "Image Enhancement...", "", "Edit/AI Enhance",
               needs_image=True, tool_id="ai_enhance"),
    ActionSpec("beauty", "menu.edit.beauty", "&Beauty...", "", "Edit/Beauty", tool_id="beauty"),
    ActionSpec("rotate_cw", "menu.edit.rotate_90_cw", "Rotate 90° CW", "Ctrl+R", "Edit/Rotate 90 CW"),
    ActionSpec("rotate_180", "menu.edit.rotate_180", "Rotate 180°", "Ctrl+Shift+R", "Edit/Rotate 180"),
    ActionSpec("rotate_ccw", "menu.edit.rotate_90_ccw", "Rotate 90° CCW", "Ctrl+Alt+R", "Edit/Rotate 90 CCW"),
    ActionSpec("flip_horizontal", "menu.edit.flip_h", "Flip Horizontal", "Ctrl+H", "Edit/Flip Horizontal"),
    ActionSpec("flip_vertical", "menu.edit.flip_v", "Flip Vertical", "Ctrl+Shift+H", "Edit/Flip Vertical"),
    ActionSpec("zoom_in", "menu.view.zoom_in", "Zoom &In", "Ctrl+=", "View/Zoom In"),
    ActionSpec("zoom_out", "menu.view.zoom_out", "Zoom &Out", "Ctrl+-", "View/Zoom Out"),
    ActionSpec("zoom_100", "menu.view.zoom_100", "Zoom &100%", "Ctrl+0", "View/100% Zoom"),
    ActionSpec("zoom_fit", "menu.view.zoom_fit", "Fit to &Window", "Ctrl+W", "View/Fit to Window"),
    ActionSpec("next_image", "menu.view.next", "&Next Image", "Right", "View/Next Image"),
    ActionSpec("previous_image", "menu.view.prev", "&Previous Image", "Left", "View/Previous Image"),
    ActionSpec("preferences", "menu.settings.preferences", "&Preferences..."),
    ActionSpec("clear_ratings", "menu.settings.clear_ratings", "Clear All &Ratings..."),
    ActionSpec("about", "menu.help.about", "&About"),
)


@dataclass(frozen=True)
class ShortcutRow:
    """设置里快捷键页的一行。key 是设置键名，group 是它的前缀；alias 表示同一动作的备用快捷键。"""

    key: str
    group: str
    text: str
    sequence: str
    default: str
    alias: bool = False


class WindowActions:
    """按 ``ACTION_SPECS`` 创建动作并统一管理它们的文字与可用状态。"""

    def __init__(self, window: QWidget, shortcuts: ShortcutManager,
                 translate: Callable[[str, str], str], handlers: Mapping[str, Callable[[], None]]):
        names = {spec.name for spec in ACTION_SPECS}
        if names != set(handlers):
            raise KeyError(f"动作与处理函数不一致: {sorted(names ^ set(handlers))}")
        self._translate = translate
        self._shortcuts = shortcuts
        self._actions: dict[str, QAction] = {}
        self._wanted: dict[str, bool] = {}
        self._has_image = False
        self._tool_check: Optional[Callable[[str], bool]] = None
        self._blocked = False

        for spec in ACTION_SPECS:
            action = QAction(window)
            action.triggered.connect(lambda _checked=False, handler=handlers[spec.name]: handler())
            window.addAction(action)
            self._actions[spec.name] = action
            self._wanted[spec.name] = True
        for spec in ACTION_SPECS:
            action = self._actions[spec.name]
            if spec.alias_of:
                shortcuts.register_alias(spec.settings_key, action, self._spec(spec.alias_of).settings_key)
            elif spec.settings_key:
                shortcuts.register(spec.settings_key, action, default=spec.shortcut)
            elif spec.shortcut:
                action.setShortcut(QKeySequence(spec.shortcut))
        shortcuts.reload()
        self.retranslate()
        self._refresh()

    @staticmethod
    def _spec(name: str) -> ActionSpec:
        return next(spec for spec in ACTION_SPECS if spec.name == name)

    def action(self, name: str) -> QAction:
        return self._actions[name]

    def retranslate(self) -> None:
        for spec in ACTION_SPECS:
            self._actions[spec.name].setText(self._translate(spec.text_key, spec.text_default))

    def _shortcut_text(self, spec: ActionSpec) -> str:
        text = plain_action_text(self._actions[spec.name].text())
        if spec.alias_of:
            return self._translate("settings.shortcuts.alternate", "{name} (alternate)").replace("{name}", text)
        return text

    def shortcut_rows(self) -> tuple[ShortcutRow, ...]:
        """可在设置里改的快捷键，按动作表的顺序；sequence 是动作上当前生效的那一个。"""
        return tuple(
            ShortcutRow(spec.settings_key, spec.settings_key.split("/", 1)[0], self._shortcut_text(spec),
                        self._actions[spec.name].shortcut().toString(QKeySequence.SequenceFormat.PortableText),
                        self._shortcuts.default(spec.settings_key), bool(spec.alias_of))
            for spec in ACTION_SPECS if spec.settings_key)

    def shortcut_owner(self, sequence: str, *, exclude: str = "") -> str:
        """已经在用这个快捷键的动作的显示名，没有则为空；exclude 是正在改的那一项的设置键名。"""
        wanted = QKeySequence(sequence)
        if wanted.isEmpty():
            return ""
        return next((self._shortcut_text(spec) for spec in ACTION_SPECS
                     if not (exclude and spec.settings_key == exclude)
                     and self._actions[spec.name].shortcut() == wanted), "")

    def set_enabled(self, name: str, enabled: bool) -> None:
        """设置一个不随图片或工具状态变化的动作是否可用。"""
        self._wanted[name] = bool(enabled)
        self._apply(self._spec(name))

    def set_has_image(self, has_image: bool) -> None:
        self._has_image = bool(has_image)
        self._refresh()

    def set_tool_check(self, check: Optional[Callable[[str], bool]]) -> None:
        """设置或刷新工具是否启用的查询；为空时视为全部启用。"""
        self._tool_check = check
        self._refresh()

    def set_blocked(self, blocked: bool) -> None:
        """提交等不可打断的操作进行时，暂时禁用全部动作。"""
        if self._blocked != bool(blocked):
            self._blocked = bool(blocked)
            self._refresh()

    def _refresh(self) -> None:
        for spec in ACTION_SPECS:
            self._apply(spec)

    def _apply(self, spec: ActionSpec) -> None:
        action = self._actions[spec.name]
        available = not spec.tool_id or self._tool_check is None or self._tool_check(spec.tool_id)
        action.setVisible(available)
        action.setEnabled(available and self._wanted[spec.name] and not self._blocked
                          and (self._has_image or not spec.needs_image))
