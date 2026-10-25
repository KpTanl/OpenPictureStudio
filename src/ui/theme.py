"""Shared UI theme helpers for OpenPictureStudio.

从 v1.3 开始，QSS 由 ``resources/styles/theme_variables.py`` 统一生成，
便于维护配色 token 并支持未来自定义主题色。
本模块继续作为对外统一入口，保持所有公共 API 向后兼容。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

try:
    from resources.styles.theme_variables import (
        DARK,
        THEMES,
        get_dark_qss,
        get_light_qss,
        get_theme_qss,
        get_theme_tokens_by_name,
    )
except ImportError:  # pragma: no cover
    # 如果资源路径不在 PYTHONPATH 中，尝试相对导入
    import sys
    from pathlib import Path
    _repo_root = Path(__file__).resolve().parents[3]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from resources.styles.theme_variables import (
        DARK,
        THEMES,
        get_dark_qss,
        get_light_qss,
        get_theme_qss,
        get_theme_tokens_by_name,
    )

from resources.styles.semantic_colors import color_channels  # noqa: E402

# 模块级 QSS 字符串，向后兼容直接引用
try:
    DARK_THEME_QSS = get_dark_qss()
    LIGHT_THEME_QSS = get_light_qss()
except Exception:  # pragma: no cover
    DARK_THEME_QSS = ""
    LIGHT_THEME_QSS = ""

_ACTIVE_THEME_NAME = "dark"
_ACTIVE_THEME_TOKENS = DARK


def _set_active_theme(theme_name: str) -> None:
    global _ACTIVE_THEME_NAME, _ACTIVE_THEME_TOKENS
    normalized = str(theme_name).lower()
    if normalized not in THEMES:
        normalized = "dark"
    _ACTIVE_THEME_NAME = normalized
    _ACTIVE_THEME_TOKENS = get_theme_tokens_by_name(normalized)


def current_theme_name() -> str:
    """返回当前生效的主题名 —— 优先取 QApplication 属性，回退到 ``_ACTIVE_THEME_NAME``。"""
    try:
        app = QApplication.instance()
        if app is not None:
            name = app.property("ops.theme")
            if isinstance(name, str) and name in THEMES:
                return name
    except Exception:
        pass
    return _ACTIVE_THEME_NAME


def get_theme_tokens() -> dict:
    """返回当前实际生效的主题 token 字典，供对话框构建 QSS 片段使用。"""
    return get_theme_tokens_by_name(current_theme_name())


def to_qcolor(value: str) -> QColor:
    """把主题取值（#rrggbb 或带不透明度的写法）转为 QColor，供自绘控件使用。"""
    red, green, blue, alpha = color_channels(value)
    return QColor(red, green, blue, round(alpha * 255))


def _palette(tokens: dict) -> QPalette:
    """样式表未覆盖的区域（普通容器的底色、自绘控件的默认文字）也跟随主题。"""
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: "surface",
        QPalette.ColorRole.WindowText: "text_primary",
        QPalette.ColorRole.Base: "page",
        QPalette.ColorRole.AlternateBase: "surface",
        QPalette.ColorRole.Text: "text_primary",
        QPalette.ColorRole.Button: "surface",
        QPalette.ColorRole.ButtonText: "text_primary",
        QPalette.ColorRole.PlaceholderText: "text_caption",
        QPalette.ColorRole.Highlight: "accent",
        QPalette.ColorRole.HighlightedText: "on_accent",
        QPalette.ColorRole.ToolTipBase: "text_primary",
        QPalette.ColorRole.ToolTipText: "surface",
    }
    for role, key in roles.items():
        palette.setColor(role, QColor(tokens[key]))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(tokens["text_disabled"]))
    return palette


class ThemeManager(QObject):
    """
    主题管理器，支持 Dark / Light / Beige / System 四种模式。

    通过 Qt 6.5+ 的 QStyleHints.colorScheme() 检测系统主题。
    """

    theme_changed = pyqtSignal(str)

    THEME_DARK = "dark"
    THEME_LIGHT = "light"
    THEME_BEIGE = "beige"
    THEME_SYSTEM = "system"

    def __init__(self):
        super().__init__()
        self._current = self.THEME_DARK
        self._canvas_background = "theme"

    def set_canvas_background(self, name: str) -> None:
        """设定画布底色（"theme" 为跟随主题）；主题已应用时立即刷新样式，切换主题不改变该选择。"""
        name = str(name)
        if name == self._canvas_background:
            return
        self._canvas_background = name
        app = QApplication.instance()
        if app is not None and app.property("ops.theme"):
            app.setStyleSheet(get_theme_qss(self.current_theme(), canvas_background=name))

    @property
    def available_themes(self) -> list[str]:
        """所有已注册主题名（来自 ``THEMES`` registry）。"""
        return list(THEMES.keys())

    def current_theme(self) -> str:
        """返回当前实际生效的主题名称（system 会解析为已注册主题）。"""
        if self._current == self.THEME_SYSTEM:
            return self._detect_system_theme()
        return self._current if self._current in THEMES else self.THEME_DARK

    def apply(self, theme_name: str) -> None:
        """应用指定主题。未注册的主题名会回退到 dark。"""
        self._current = theme_name
        resolved_theme = self.current_theme()
        _set_active_theme(resolved_theme)
        qss = get_theme_qss(resolved_theme, canvas_background=self._canvas_background)
        app = QApplication.instance()
        if app is not None:
            app.setPalette(_palette(get_theme_tokens_by_name(resolved_theme)))
            app.setStyleSheet(qss)
            app.setProperty("ops.theme", resolved_theme)
        self.theme_changed.emit(resolved_theme)

    def _detect_system_theme(self) -> str:
        """检测系统当前主题偏好。"""
        app = QApplication.instance()
        if app is not None:
            try:
                scheme = app.styleHints().colorScheme()
                if scheme == Qt.ColorScheme.Dark:
                    return self.THEME_DARK
                elif scheme == Qt.ColorScheme.Light:
                    return self.THEME_LIGHT
            except AttributeError:
                pass
        return self.THEME_DARK


def build_dark_theme(extra_qss: str = "") -> str:
    """向后兼容：直接返回 Dark Theme QSS，可选追加额外样式。"""
    return get_dark_qss(extra_qss)


# =============================================================================
# 全局 ThemeManager 单例（供 BaseWindow / BaseDialog 自动订阅 theme_changed）
# =============================================================================
# 设计与 ``src.i18n.translation_service`` 的 ``set/get_translation_service``
# 对齐：应用启动时在 ``main.py`` 调用 ``set_theme_manager(mgr)``，之后
# 任何 BaseWindow / BaseDialog 都能通过 ``get_theme_manager()`` 找到它
# 并在 __init__ 中自动接入 ``theme_changed`` 信号，避免每个对话框各自
# 手动 connect/disconnect。

_theme_manager_instance: "ThemeManager | None" = None


def set_theme_manager(manager: "ThemeManager | None") -> None:
    """注册全局 ThemeManager 实例。应用启动时调用一次。"""
    global _theme_manager_instance
    _theme_manager_instance = manager


def get_theme_manager() -> "ThemeManager | None":
    """获取已注册的全局 ThemeManager（未注册时返回 None）。"""
    return _theme_manager_instance
