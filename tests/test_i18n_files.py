"""翻译文件：每种语言的键相同，同一个键的占位符与换行相同，设置里列出了每一种语言。"""

import json
import string
from pathlib import Path

from src.ui.dialogs.settings_dialog import SettingsDialog

I18N = Path(__file__).resolve().parents[1] / "resources" / "i18n"


def _load():
    return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(I18N.glob("*.json"))}


def _fields(text):
    return sorted(name for _, name, _, _ in string.Formatter().parse(text) if name is not None)


def test_every_language_has_the_same_keys_and_placeholders():
    languages = _load()
    reference = languages["en"]
    for code, table in languages.items():
        assert set(table) == set(reference), code
        mismatched = [key for key, text in table.items()
                      if _fields(text) != _fields(reference[key]) or text.count("\n") != reference[key].count("\n")]
        assert not mismatched, (code, mismatched[:10])


def test_settings_offers_every_language_file():
    languages = _load()
    offered = [code for code, _key, _name in SettingsDialog.LANGUAGE_OPTIONS]
    assert sorted(offered) == sorted(languages)
    for code, key, _name in SettingsDialog.LANGUAGE_OPTIONS:
        assert all(key in table for table in languages.values()), code
