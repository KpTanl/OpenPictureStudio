"""Base window and dialog classes for OpenPictureStudio.

Provides unified theming, translation, and lifecycle hooks so that
all major windows and dialogs share the same visual style and i18n
infrastructure.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QDialog, QMainWindow

from ..utils.app_info import APP_NAME
from ..i18n import get_translation_service
from ..utils.resource_paths import resolve_app_icon_path
from .dwm_helper import apply_title_bar_colors
from .window_chrome import WindowChrome

logger = logging.getLogger(__name__)


# 恢复窗口位置前的校验：标题栏这一条至少要有这么宽落在某块屏幕上，否则窗口拖不回来
_TITLE_STRIP = 32
_VISIBLE_TITLE = 120

# 打开时的窗口：上次的大小与位置、最大化，或几种固定大小之一（居中，不超过屏幕）
STARTUP_LAST, STARTUP_MAXIMIZED = "last", "maximized"
STARTUP_SIZES = {"1920x1080": (1920, 1080), "1600x900": (1600, 900), "1280x720": (1280, 720)}
STARTUP_MODES = (STARTUP_LAST, *STARTUP_SIZES, STARTUP_MAXIMIZED)


def _apply_title_bar(window) -> None:
    """系统标题栏与界面同色：底色取当前主题的面板色，文字取次要文字色。"""
    try:
        from .theme import get_theme_tokens, to_qcolor
        tokens = get_theme_tokens()
        caption, text = to_qcolor(tokens["surface"]), to_qcolor(tokens["text_secondary"])
        apply_title_bar_colors(window, dark_mode=caption.lightness() < 128,
                               caption=(caption.red(), caption.green(), caption.blue()),
                               text=(text.red(), text.green(), text.blue()))
    except Exception:
        logger.debug("标题栏外观未能应用", exc_info=True)


class BaseWindow(QMainWindow):
    """所有主窗口的基类。

    内置能力：
    - 统一应用图标自动加载
    - 翻译服务接入（``_tr``）
    - 语言切换时自动调用 ``retranslate_ui()``
    - 主题变更时自动调用 ``apply_theme()``
    - 系统标题栏与界面同色，随主题变化
    - 窗口尺寸持久化（可选）
    """

    def __init__(
        self,
        *,
        title_key: str = "",
        title_default: str = APP_NAME,
        min_width: int = 800,
        min_height: int = 600,
        default_width: int = 1600,
        default_height: int = 900,
        persist_geometry: bool = True,
        settings_group: str = APP_NAME,
        settings_key: str = "MainWindow",
        parent=None,
    ):
        super().__init__(parent)
        self._persist_geometry = persist_geometry
        self._settings_group = settings_group
        self._settings_key = settings_key
        self._title_key = title_key
        self._title_default = title_default

        self._chrome: Optional[WindowChrome] = None

        self.translation_service = get_translation_service()
        self._setup_icon()
        self.setWindowTitle(self._tr(self._title_key, self._title_default))
        self.setMinimumSize(min_width, min_height)

        if self._persist_geometry:
            self._restore_geometry(default_width, default_height)
        else:
            self.resize(default_width, default_height)

        if self.translation_service:
            self.translation_service.add_observer(self._on_language_changed)

        self._theme_subscribed = False
        self._subscribe_theme_manager()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        _apply_title_bar(self)

    # ------------------------------------------------------------------
    # 自己画标题栏
    # ------------------------------------------------------------------
    def install_window_chrome(self, region_at, maximize_state_changed) -> bool:
        """把系统标题栏的位置交给界面：region_at 回答窗口里一点属于哪种区域，见 window_chrome。

        只在 Windows 的真实窗口上生效；不生效时返回 False，窗口保持系统标题栏。在窗口显示之前调用：
        此前恢复的位置与大小是按带系统标题栏算的，接管之后客户区的上沿就是窗口的上沿，按原来的位置与大小
        重设一次，否则每次启动窗口都会下移、变高一个标题栏。
        """
        if QGuiApplication.platformName() != "windows" or self._chrome is not None:
            return self._chrome is not None
        position, size = self.pos(), self.size()
        chrome = WindowChrome(self, region_at, maximize_state_changed, self.toggle_maximized)
        self._chrome = chrome
        if not chrome.install():
            self._chrome = None
            return False
        if self.testAttribute(Qt.WidgetAttribute.WA_Moved):
            self.setGeometry(QRect(position, size))
        else:
            self.resize(size)
        return True

    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def nativeEvent(self, event_type, message):
        if self._chrome is not None:
            handled, result = self._chrome.native_event(message)
            if handled:
                return True, result
        return False, 0

    # ------------------------------------------------------------------
    # Translation helpers
    # ------------------------------------------------------------------
    def _tr(self, key: str, default: Optional[str] = None, **kwargs) -> str:
        if self.translation_service:
            return self.translation_service.translate(key, default=default, **kwargs)
        template = default if default is not None else key
        if kwargs and template:
            try:
                return template.format(**kwargs)
            except Exception:
                pass
        return template

    def _on_language_changed(self, _language: str) -> None:
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        """子类重写此方法以在语言切换时刷新 UI 文案。

        基类默认只刷新窗口标题。
        """
        self.setWindowTitle(self._tr(self._title_key, self._title_default))

    # ------------------------------------------------------------------
    # Theme helpers
    # ------------------------------------------------------------------
    def apply_theme(self, theme_name: str) -> None:
        """子类重写此方法以在主题切换时刷新特殊样式。

        基类默认让标题栏跟着换色。
        """
        _apply_title_bar(self)

    def _subscribe_theme_manager(self) -> None:
        """连接全局 ThemeManager 的 theme_changed 信号 → ``apply_theme``。"""
        from .theme import get_theme_manager  # lazy import to avoid cycle
        mgr = get_theme_manager()
        if mgr is not None:
            mgr.theme_changed.connect(self.apply_theme)
            self._theme_subscribed = True

    def _unsubscribe_theme_manager(self) -> None:
        if not self._theme_subscribed:
            return
        from .theme import get_theme_manager
        mgr = get_theme_manager()
        if mgr is not None:
            try:
                mgr.theme_changed.disconnect(self.apply_theme)
            except (TypeError, RuntimeError):
                pass
        self._theme_subscribed = False

    # ------------------------------------------------------------------
    # Icon
    # ------------------------------------------------------------------
    def _setup_icon(self) -> None:
        app_icon_path = resolve_app_icon_path()
        if app_icon_path.exists():
            self.setWindowIcon(QIcon(str(app_icon_path)))

    # ------------------------------------------------------------------
    # Geometry persistence
    # ------------------------------------------------------------------
    def startup_mode(self) -> str:
        """打开时的窗口，取值见 STARTUP_MODES；没有保存过或保存的值不认识时是上次的大小与位置。"""
        mode = QSettings(self._settings_group, self._settings_key).value("startupWindow", STARTUP_LAST)
        return mode if mode in STARTUP_MODES else STARTUP_LAST

    def set_startup_mode(self, mode: str) -> bool:
        """保存打开时的窗口，下次打开生效；返回是否保存成功。"""
        if mode not in STARTUP_MODES:
            raise ValueError(f"未知的窗口打开方式: {mode}")
        settings = QSettings(self._settings_group, self._settings_key)
        settings.setValue("startupWindow", mode)
        settings.sync()
        return settings.status() == QSettings.Status.NoError

    def _restore_geometry(self, default_width: int, default_height: int) -> None:
        """按“打开时的窗口”定下大小、位置与是否最大化。

        上次的大小与位置：上次所在的屏幕不在了就不恢复位置。固定大小：在上次所在的屏幕上居中。
        两种情况下大小都不超过所在屏幕。最大化：还原后是上次的大小与位置。
        """
        settings = QSettings(self._settings_group, self._settings_key)
        mode = self.startup_mode()
        size = settings.value("windowSize")
        pos = settings.value("windowPos")
        size = size if isinstance(size, QSize) and size.isValid() else QSize(default_width, default_height)
        screen = None
        if isinstance(pos, QPoint):
            title = QRect(pos, QSize(size.width(), _TITLE_STRIP))
            screen = next((item for item in QGuiApplication.screens()
                           if item.availableGeometry().intersected(title).width() >= _VISIBLE_TITLE), None)
        fixed = STARTUP_SIZES.get(mode)
        if fixed is not None:
            size = QSize(*fixed)
        available = (screen or QGuiApplication.primaryScreen())
        if available is not None:
            size = size.boundedTo(available.availableGeometry().size())
        self.resize(size.expandedTo(self.minimumSize()))
        if fixed is not None and available is not None:
            area = available.availableGeometry()
            pos = area.topLeft() + QPoint((area.width() - self.width()) // 2, (area.height() - self.height()) // 2)
        elif screen is None:
            pos = None
        if pos is not None:
            self.move(pos)
        self._normal_size, self._normal_pos = self.size(), pos
        maximized = mode == STARTUP_LAST and settings.value("windowMaximized", False, type=bool)
        if mode == STARTUP_MAXIMIZED or maximized:
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def _track_normal_geometry(self) -> None:
        """记下未最大化时的大小与位置；最大化时关闭窗口，保存的是这一份。

        只在窗口已经显示时记：显示之前 Qt 记的边框还是系统标题栏那一份，这时读到的位置比实际高出一个标题栏，
        保存下来窗口就会每打开一次上移一截。显示之前沿用恢复出来的值。
        """
        if (self._persist_geometry and self.isVisible()
                and not (self.isMaximized() or self.isMinimized() or self.isFullScreen())):
            self._normal_size, self._normal_pos = self.size(), self.pos()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._track_normal_geometry()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._track_normal_geometry()

    def _save_geometry(self) -> None:
        if not self._persist_geometry:
            return
        # 关闭时窗口还显示着：显示之前由程序定下、此后没有再变过的大小与位置在这里记下
        self._track_normal_geometry()
        settings = QSettings(self._settings_group, self._settings_key)
        settings.setValue("windowMaximized", self.isMaximized())
        settings.setValue("windowSize", self._normal_size)
        if self._normal_pos is not None:
            settings.setValue("windowPos", self._normal_pos)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        self._save_geometry()
        if self.translation_service:
            self.translation_service.remove_observer(self._on_language_changed)
        self._unsubscribe_theme_manager()
        super().closeEvent(event)

    def show_error(self, title: str, message: str, ok_text: str = "") -> None:
        from .widgets.message_dialog import show_error
        show_error(self, title, message, ok_text=ok_text)

    def show_warning(self, title: str, message: str, ok_text: str = "") -> None:
        from .widgets.message_dialog import show_warning
        show_warning(self, title, message, ok_text=ok_text)

    def show_info(self, title: str, message: str, ok_text: str = "") -> None:
        from .widgets.message_dialog import show_info
        show_info(self, title, message, ok_text=ok_text)

    def show_confirm(self, title: str, message: str, yes_text: str = "", no_text: str = "",
                     default_yes: bool = True) -> bool:
        from .widgets.message_dialog import show_confirm
        return show_confirm(self, title, message, yes_text=yes_text, no_text=no_text, default_yes=default_yes)

    def show_question(self, title: str, message: str, yes_text: str = "", no_text: str = "",
                      default_yes: bool = True) -> bool:
        from .widgets.message_dialog import show_question
        return show_question(self, title, message, yes_text=yes_text, no_text=no_text, default_yes=default_yes)


class BaseDialog(QDialog):
    """所有对话框的基类。

    内置能力：
    - 统一应用图标自动加载
    - 翻译服务接入（``_tr``）
    - 语言切换时自动调用 ``retranslate_ui()``
    - 统一边距、默认尺寸与模态行为
    - 系统标题栏与界面同色，随主题变化
    - Esc 键处理（默认拒绝/关闭）
    """

    def __init__(
        self,
        *,
        title_key: str = "",
        title_default: str = "",
        min_width: int = 320,
        min_height: int = 200,
        default_width: int = 0,
        default_height: int = 0,
        modal: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._title_key = title_key
        self._title_default = title_default

        self.translation_service = get_translation_service()
        self._observer_removed = False
        self.setWindowTitle(self._tr(self._title_key, self._title_default))
        # 允许对话框最小化/最大化，保留系统菜单与关闭按钮
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setMinimumSize(min_width, min_height)
        if default_width > 0 and default_height > 0:
            self.resize(default_width, default_height)
        if modal:
            self.setModal(True)

        self._setup_icon()

        if self.translation_service:
            self.translation_service.add_observer(self._on_language_changed)
            # 没有经过 done() 或关闭就被销毁时（如嵌在别的界面里）同样退订
            service, callback = self.translation_service, self._on_language_changed
            self.destroyed.connect(lambda: service.remove_observer(callback))

        self._theme_subscribed = False
        self._subscribe_theme_manager()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        _apply_title_bar(self)

    def _cleanup_translation_observer(self) -> None:
        if self.translation_service and not self._observer_removed:
            self.translation_service.remove_observer(self._on_language_changed)
            self._observer_removed = True

    # ------------------------------------------------------------------
    # Translation helpers
    # ------------------------------------------------------------------
    def _tr(self, key: str, default: Optional[str] = None, **kwargs) -> str:
        if self.translation_service:
            return self.translation_service.translate(key, default=default, **kwargs)
        template = default if default is not None else key
        if kwargs and template:
            try:
                return template.format(**kwargs)
            except Exception:
                pass
        return template

    def _on_language_changed(self, _language: str) -> None:
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        """子类重写此方法以在语言切换时刷新 UI 文案。

        基类默认只刷新窗口标题。
        """
        self.setWindowTitle(self._tr(self._title_key, self._title_default))

    # ------------------------------------------------------------------
    # Theme helpers
    # ------------------------------------------------------------------
    def apply_theme(self, theme_name: str) -> None:
        """子类重写此方法以在主题切换时刷新特殊样式。"""
        _apply_title_bar(self)

    def _subscribe_theme_manager(self) -> None:
        """连接全局 ThemeManager 的 theme_changed 信号 → ``apply_theme``。"""
        from .theme import get_theme_manager  # lazy import to avoid cycle
        mgr = get_theme_manager()
        if mgr is not None:
            mgr.theme_changed.connect(self.apply_theme)
            self._theme_subscribed = True

    def _unsubscribe_theme_manager(self) -> None:
        if not self._theme_subscribed:
            return
        from .theme import get_theme_manager
        mgr = get_theme_manager()
        if mgr is not None:
            try:
                mgr.theme_changed.disconnect(self.apply_theme)
            except (TypeError, RuntimeError):
                pass
        self._theme_subscribed = False

    # ------------------------------------------------------------------
    # Icon
    # ------------------------------------------------------------------
    def _setup_icon(self) -> None:
        app_icon_path = resolve_app_icon_path()
        if app_icon_path.exists():
            self.setWindowIcon(QIcon(str(app_icon_path)))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        self._cleanup_translation_observer()
        self._unsubscribe_theme_manager()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        """统一处理 accept/reject 结束路径，避免重复清理 observer。"""
        self._cleanup_translation_observer()
        self._unsubscribe_theme_manager()
        super().done(result)

    def show_error(self, title: str, message: str, ok_text: str = "") -> None:
        from .widgets.message_dialog import show_error
        show_error(self, title, message, ok_text=ok_text)

    def show_warning(self, title: str, message: str, ok_text: str = "") -> None:
        from .widgets.message_dialog import show_warning
        show_warning(self, title, message, ok_text=ok_text)

    def show_info(self, title: str, message: str, ok_text: str = "") -> None:
        from .widgets.message_dialog import show_info
        show_info(self, title, message, ok_text=ok_text)

    def show_confirm(self, title: str, message: str, yes_text: str = "", no_text: str = "",
                     default_yes: bool = True) -> bool:
        from .widgets.message_dialog import show_confirm
        return show_confirm(self, title, message, yes_text=yes_text, no_text=no_text, default_yes=default_yes)

    def show_question(self, title: str, message: str, yes_text: str = "", no_text: str = "",
                      default_yes: bool = True) -> bool:
        from .widgets.message_dialog import show_question
        return show_question(self, title, message, yes_text=yes_text, no_text=no_text, default_yes=default_yes)
