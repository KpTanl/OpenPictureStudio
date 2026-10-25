"""输入框的照片菜单：发出的预览只缩小不放大，原始画面写照片实际的长边，当前画面只写上限。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.i18n import TranslationService  # noqa: E402
from src.ui.widgets.assistant_composer import AssistantComposer  # noqa: E402

_TRANSLATIONS = TranslationService(default_language="zh-CN", fallback_language="en")


def _tr(key, default=None, **values):
    return _TRANSLATIONS.translate(key, default=default, **values)


def test_photo_menu_names_the_real_original_edge_and_a_limit_for_the_current_view(qapp):
    composer = AssistantComposer()
    try:
        composer.set_state({}, _tr, photo_edge=1448)
        assert composer._photo_label("original") == "原始画面（不含调整，长边 1448）"
        assert composer._photo_label("current") == "当前画面（含已做的调整，长边最多 1536）"
        composer.set_state({}, _tr, photo_edge=6720)
        assert composer._photo_label("original") == "原始画面（不含调整，长边 2048）"
        composer.set_state({}, _tr)
        assert composer._photo_label("original") == "原始画面（不含调整，长边最多 2048）"
        assert composer._photo_label("") == "不发送照片"
    finally:
        composer.deleteLater()
