"""Unified theme and i18n mixin for windows, dialogs, and tool panels."""

from typing import Optional
from ..i18n import get_translation_service


class ThemedI18nMixin:
    """提供统一的翻译服务接入、主题变更订阅与清理生命周期。"""

    def _init_themed_i18n(self) -> None:
        self.translation_service = get_translation_service()
        if self.translation_service:
            self.translation_service.add_observer(self._on_language_changed)
        self._theme_subscribed = False
        self._subscribe_theme_manager()

    def _cleanup_themed_i18n(self) -> None:
        if getattr(self, "translation_service", None):
            self.translation_service.remove_observer(self._on_language_changed)
        self._unsubscribe_theme_manager()

    def _tr(self, key: str, default: Optional[str] = None, **kwargs) -> str:
        svc = getattr(self, "translation_service", None) or get_translation_service()
        if svc:
            return svc.translate(key, default=default, **kwargs)
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
        """子类重写以刷新文案。"""
        pass

    def apply_theme(self, theme_name: str) -> None:
        """子类重写以更新样式或组件状态。"""
        pass

    def _subscribe_theme_manager(self) -> None:
        from .theme import get_theme_manager
        mgr = get_theme_manager()
        if mgr is not None:
            mgr.theme_changed.connect(self.apply_theme)
            self._theme_subscribed = True

    def _unsubscribe_theme_manager(self) -> None:
        if not getattr(self, "_theme_subscribed", False):
            return
        from .theme import get_theme_manager
        mgr = get_theme_manager()
        if mgr is not None:
            try:
                mgr.theme_changed.disconnect(self.apply_theme)
            except (TypeError, RuntimeError):
                pass
        self._theme_subscribed = False
