
import os
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QSettings, QTranslator, QLibraryInfo, QTimer

from PyQt6.QtGui import QImageReader, QIcon
from .utils.app_info import APP_NAME, APP_VERSION
from .utils.logging_setup import configure_logging
from .utils.resource_paths import resolve_app_icon_path


def main():
    """
    应用程序入口函数。
    负责初始化 Qt 环境、设置应用属性并显示主窗口。
    """
    import atexit
    from .application.runtime_bootstrap import activate
    from .utils.model_paths import get_runtime_root
    if "--ops-plugin-runner" in sys.argv:
        runtime_activation = activate(get_runtime_root())
    else:
        app = QApplication(sys.argv)
        app.setApplicationName(APP_NAME)
        app.setOrganizationName(APP_NAME)
        app.setApplicationVersion(APP_VERSION)
        from .utils.storage_bootstrap import prepare_storage
        storage_session, runtime_activation = prepare_storage(app)
        atexit.register(storage_session.close)
    from .application.image_resources import system_resource_policy
    resource_policy = system_resource_policy()
    os.environ["RAYON_NUM_THREADS"] = str(resource_policy.operator_threads)
    from .core.photo_engine import configure_hardware_acceleration
    configure_hardware_acceleration(resource_policy.operator_threads)

    if "--ops-plugin-runner" in sys.argv:
        from .application.plugin_runner import run
        return run(sys.argv[sys.argv.index("--ops-plugin-runner") + 1:])

    # 解除 Qt 加载图片 256MB 的内存限制，允许加载超大图片
    QImageReader.setAllocationLimit(0)
    
    logger = configure_logging(APP_NAME)
    from PyQt6.QtCore import QThreadPool
    from resources.styles.theme_variables import THREAD_POOL_MAX_THREADS
    logger.info("计算资源策略 capacity=%s slots=%s interactive_threads=%s operator_threads=%s thumbnail_slots=%s",
                resource_policy.thread_capacity, resource_policy.compute_slots, resource_policy.interactive_threads,
                resource_policy.operator_threads, resource_policy.thumbnail_slots)
    logger.info("推理运行环境 token=%s reason=%s", runtime_activation.token or "builtin", runtime_activation.reason)
    QThreadPool.globalInstance().setMaxThreadCount(THREAD_POOL_MAX_THREADS)

    # 配置高质量抗锯齿系统字体，字体栈与样式表共用同一份定义
    from PyQt6.QtGui import QFont, QFontDatabase
    from resources.styles.theme_variables import FONT_FAMILIES
    installed_families = set(QFontDatabase.families())
    matched_families = [f for f in FONT_FAMILIES if f in installed_families]
    default_font = QFont()
    default_font.setPointSize(11)
    default_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    if matched_families:
        default_font.setFamilies(matched_families)
    app.setFont(default_font)
    # 菜单与下拉即点即现，不跟随系统的淡入或滑出效果
    from PyQt6.QtCore import Qt
    for effect in (Qt.UIEffect.UI_AnimateMenu, Qt.UIEffect.UI_FadeMenu, Qt.UIEffect.UI_AnimateCombo):
        app.setEffectEnabled(effect, False)

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_NAME)
        except Exception:
            logger.debug("Failed to set AppUserModelID", exc_info=True)

    icon_path = resolve_app_icon_path()
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    else:
        logger.warning("Application icon not found: %s", icon_path)

    try:
        from .ui.theme import ThemeManager, set_theme_manager
        from .ui.main_window import MainWindow
        from .i18n import TranslationService, set_translation_service

        theme_settings = QSettings(APP_NAME, "Theme")
        saved_theme = theme_settings.value("current", ThemeManager.THEME_DARK)
        theme_manager = ThemeManager()
        from .application.image_display_settings import ImageDisplaySettings
        theme_manager.set_canvas_background(
            ImageDisplaySettings.load(QSettings(APP_NAME, "ImageDisplay")).canvas_background.value)
        theme_manager.apply(saved_theme)
        # 注册全局 ThemeManager —— BaseWindow / BaseDialog 会自动接入
        # theme_changed 信号，无需各自手动 connect。
        set_theme_manager(theme_manager)

        language_settings = QSettings(APP_NAME, "Language")
        saved_language = language_settings.value("current", "en")
        translation_service = TranslationService(default_language=saved_language, fallback_language="en")
        active_language = translation_service.set_language(saved_language)
        language_settings.setValue("current", active_language)
        set_translation_service(translation_service)

        qt_catalog = {"zh-CN": "qtbase_zh_CN", "fr": "qtbase_fr"}.get(active_language)
        if qt_catalog:
            qt_translator = QTranslator(app)
            translations_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
            if qt_translator.load(qt_catalog, translations_path):
                app.installTranslator(qt_translator)
                app._qt_translator = qt_translator

        # 创建并显示主窗口
        window = MainWindow(resource_policy=resource_policy, runtime_activation=runtime_activation)
        app.aboutToQuit.connect(window.shutdown_previews)

        window.show()
        if len(sys.argv) > 1:
            file_path = sys.argv[1]
            QTimer.singleShot(0, lambda: window.open_image_path(file_path, origin="startup"))

        QTimer.singleShot(800, window.prewarm_studio)

        # 进入事件循环，等待用户交互
        exit_code = app.exec()
        storage_session.close()
        if app.property("ops_restart"):
            from PyQt6.QtCore import QProcess
            QProcess.startDetached(sys.executable, sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv)
        return exit_code
    except Exception:
        logger.exception("Application startup failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
