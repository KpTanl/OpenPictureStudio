import json
import logging
import sys
from pathlib import Path
from typing import Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)


def _resolve_default_translations_dir() -> Path:
    """Resolve i18n directory across source and packaged runtime layouts."""
    root_dir = Path(__file__).resolve().parents[2]
    fallback = root_dir / "resources" / "i18n"

    candidates = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundle_dir = Path(meipass)
        candidates.extend(
            [
                bundle_dir / "resources" / "i18n",
                bundle_dir / "i18n",
            ]
        )

    executable_dir = Path(sys.executable).resolve().parent
    candidates.extend(
        [
            executable_dir / "resources" / "i18n",
            executable_dir / "i18n",
            fallback,
            Path.cwd() / "resources" / "i18n",
        ]
    )

    visited = set()
    for path in candidates:
        normalized = str(path)
        if normalized in visited:
            continue
        visited.add(normalized)
        if path.exists() and path.is_dir():
            return path

    return fallback


class TranslationService:
    """JSON-based translation service with observer callbacks."""

    def __init__(
        self,
        translations_dir: Optional[Path] = None,
        default_language: str = "en",
        fallback_language: str = "en",
    ) -> None:
        self._translations_dir = Path(translations_dir or _resolve_default_translations_dir())
        self._translations: Dict[str, Dict[str, str]] = {}
        self._observers: Set[Callable[[str], None]] = set()
        self._missing_keys_reported: Set[str] = set()

        self._available_languages = self._discover_languages()
        self._fallback_language = self._resolve_language_code(fallback_language) or "en"
        self._current_language = self._resolve_language_code(default_language) or self._fallback_language

        self._load_language(self._fallback_language)
        self._load_language(self._current_language)

    def _discover_languages(self) -> Set[str]:
        if not self._translations_dir.exists():
            return set()
        return {path.stem for path in self._translations_dir.glob("*.json") if path.is_file()}

    def _resolve_language_code(self, code: Optional[str]) -> Optional[str]:
        if not code:
            return None
        normalized = code.strip()
        if normalized in self._available_languages:
            return normalized

        low = normalized.lower()
        for lang in self._available_languages:
            if lang.lower() == low:
                return lang
        return None

    def _load_language(self, language_code: str) -> bool:
        if language_code in self._translations:
            return True

        path = self._translations_dir / f"{language_code}.json"
        if not path.exists():
            logger.warning("Translation file not found: %s", path)
            self._translations[language_code] = {}
            return False

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Translation JSON root must be an object")
            self._translations[language_code] = data
            return True
        except Exception as exc:
            logger.warning("Failed to load translation file %s: %s", path, exc)
            self._translations[language_code] = {}
            return False

    def get_language(self) -> str:
        return self._current_language

    def set_language(self, language_code: str) -> str:
        resolved = self._resolve_language_code(language_code)
        if not resolved:
            logger.warning(
                "Unsupported language '%s', fallback to '%s'",
                language_code,
                self._fallback_language,
            )
            resolved = self._fallback_language

        self._load_language(resolved)
        if self._current_language != resolved:
            self._current_language = resolved
            self._notify_language_changed()
        return self._current_language

    def translate(self, key: str, default: Optional[str] = None, **kwargs) -> str:
        if not key:
            return default if default is not None else ""

        current_map = self._translations.get(self._current_language) or {}
        fallback_map = self._translations.get(self._fallback_language) or {}

        template = current_map.get(key)
        if template is None:
            template = fallback_map.get(key)

        if template is None:
            if key not in self._missing_keys_reported:
                logger.warning("Missing translation key: %s", key)
                self._missing_keys_reported.add(key)
            template = default if default is not None else key

        if kwargs:
            try:
                return template.format(**kwargs)
            except Exception as exc:
                logger.warning("Failed formatting translation key '%s': %s", key, exc)
        return template

    def add_observer(self, callback: Callable[[str], None]) -> None:
        self._observers.add(callback)

    def remove_observer(self, callback: Callable[[str], None]) -> None:
        self._observers.discard(callback)

    def _notify_language_changed(self) -> None:
        for callback in list(self._observers):
            try:
                callback(self._current_language)
            except Exception as exc:
                logger.warning("Translation observer error: %s", exc)


_translation_service: Optional[TranslationService] = None


def set_translation_service(service: Optional[TranslationService]) -> None:
    global _translation_service
    _translation_service = service


def get_translation_service() -> Optional[TranslationService]:
    return _translation_service


def tr(key: str, default: Optional[str] = None, **kwargs) -> str:
    service = get_translation_service()
    if not service:
        return default if default is not None else key
    return service.translate(key, default=default, **kwargs)
