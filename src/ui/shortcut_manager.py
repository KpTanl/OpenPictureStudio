"""Shortcut management for OpenPictureStudio."""

from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QKeySequence

from ..utils.app_info import APP_NAME


class ShortcutManager:
    """
    集中管理应用程序的快捷键注册与重载。

    负责维护 shortcut_key -> QAction 的映射；用户自定义的快捷键由它读写 QSettings，改动立即生效。
    """

    LEGACY_SHORTCUT_KEYS = {
        "File/Open File": "文件/打开文件",
        "File/Open Folder": "文件/打开文件夹",
        "File/Save As": "文件/另存为",
        "File/Set Wallpaper": "文件/设为壁纸",
        "File/Exit": "文件/退出",
        "Edit/Manual Crop": "编辑/手动裁切",
        "Edit/Grid Crop": "编辑/网格裁切",
        "Edit/Compress Image": "编辑/压缩图片",
        "Edit/Convert Format": "编辑/格式转换",
        "Edit/Export PDF": "编辑/导出PDF",
        "Edit/Remove Background": "编辑/移除背景",
        "Edit/Image Denoise": "编辑/图片降噪",
        "Edit/AI Enhance": "编辑/AI 修图",
        "View/Zoom In": "视图/放大",
        "View/Zoom Out": "视图/缩小",
        "View/100% Zoom": "视图/100% 缩放",
        "View/Fit to Window": "视图/适应窗口",
        "View/Next Image": "视图/下一张",
        "View/Previous Image": "视图/上一张",
    }

    def __init__(self, settings_group: str = APP_NAME, settings_key: str = "Shortcuts"):
        self._actions: dict[str, object] = {}
        self._defaults: dict[str, str] = {}
        self._aliases: dict[str, str] = {}
        self._settings = QSettings(settings_group, settings_key)

    def register(self, key: str, action, *, default: str | None = None) -> None:
        """注册一个 QAction 到指定的 shortcut key。"""
        self._actions[key] = action
        if default is not None:
            self._defaults[key] = default

    def register_alias(self, key: str, action, target_key: str) -> None:
        self.register(key, action)
        self._aliases[key] = target_key

    def clear(self) -> None:
        """清空已注册的 actions（通常在重建 UI 时调用）。"""
        self._actions.clear()
        self._defaults.clear()
        self._aliases.clear()

    def default(self, key: str) -> str:
        return self._defaults.get(key, "")

    def sequence(self, key: str) -> str:
        """设置里保存的快捷键；没有保存过时取旧键名下的值，再没有取默认值。保存为空表示不设。"""
        saved = self._settings.value(key, None)
        if saved is None:
            legacy_key = self.LEGACY_SHORTCUT_KEYS.get(key)
            if legacy_key:
                saved = self._settings.value(legacy_key, saved)
        if saved is None:
            saved = self.default(key)
        return str(saved or "")

    def set_sequence(self, key: str, sequence: str) -> None:
        """保存一项快捷键并立即生效；sequence 为空表示不设。"""
        if key not in self._actions:
            raise KeyError(key)
        self._settings.setValue(key, sequence)
        self._settings.sync()
        self.reload()

    def reset(self) -> None:
        """全部恢复默认：删掉已保存的值（含旧键名下的）并立即生效。"""
        for key in self._actions:
            self._settings.remove(key)
            legacy_key = self.LEGACY_SHORTCUT_KEYS.get(key)
            if legacy_key:
                self._settings.remove(legacy_key)
        self._settings.sync()
        self.reload()

    def reload(self) -> None:
        """从 QSettings 重新加载并应用快捷键。"""
        for key, action in self._actions.items():
            action.setShortcut(QKeySequence(self.sequence(key)))
        for key, target_key in self._aliases.items():
            action = self._actions[key]
            target = self._actions[target_key]
            if action.shortcut() == target.shortcut():
                action.setShortcut(QKeySequence())
