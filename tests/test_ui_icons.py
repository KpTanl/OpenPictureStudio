"""界面图标：资源完整性、按主题取色、状态与指定颜色。"""

import os
import re

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QColor, QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.ui.icons import themed_icon  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.utils.resource_paths import resolve_resource  # noqa: E402

ICON_DIR = resolve_resource("icons/ui/LICENSE.txt").parent
NAMES = sorted(path.stem for path in ICON_DIR.glob("*.svg"))


@pytest.fixture
def theme():
    app = QApplication.instance() or QApplication([])
    previous_sheet, previous_theme = app.styleSheet(), app.property("ops.theme")
    manager = ThemeManager()
    manager.apply("dark")
    yield manager
    app.setStyleSheet(previous_sheet)
    app.setProperty("ops.theme", previous_theme)


def _ink(icon, mode=QIcon.Mode.Normal, size=48):
    """图标中不透明度最高的像素的颜色。"""
    image = icon.pixmap(QSize(size, size), mode).toImage()
    pixels = (image.pixelColor(x, y) for y in range(image.height()) for x in range(image.width()))
    strongest = max(pixels, key=lambda color: color.alpha())
    assert strongest.alpha() > 200
    return strongest.name()


def test_icon_set_ships_with_its_license_and_source():
    assert len(NAMES) >= 40
    assert "MIT License" in (ICON_DIR / "LICENSE.txt").read_text(encoding="utf-8")
    assert "Tabler Icons" in (ICON_DIR / "SOURCE.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", NAMES)
def test_icon_files_only_use_the_current_color(name):
    text = (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8")
    assert set(re.findall(r'(?:stroke|fill)="([^"]+)"', text)) <= {"currentColor", "none"}
    assert not re.search(r"<script|<foreignObject|href=|<image", text, re.IGNORECASE)


@pytest.mark.parametrize("name", NAMES)
def test_every_icon_renders(theme, name):
    pixmap = themed_icon(name).pixmap(QSize(20, 20))
    assert pixmap.size() == QSize(20, 20)
    assert _ink(themed_icon(name)) == get_theme_tokens()["text_secondary"]


def test_icon_color_follows_the_state(theme):
    tokens, icon = get_theme_tokens(), themed_icon("crop")
    assert _ink(icon, QIcon.Mode.Normal) == tokens["text_secondary"]
    assert _ink(icon, QIcon.Mode.Active) == tokens["text_primary"]
    assert _ink(icon, QIcon.Mode.Disabled) == tokens["text_disabled"]


def test_existing_icon_follows_a_theme_change(theme):
    icon = themed_icon("settings")
    dark = _ink(icon)
    theme.apply("light")
    assert _ink(icon) == get_theme_tokens()["text_secondary"] != dark


def test_role_overrides_the_color_except_when_disabled(theme):
    tokens, icon = get_theme_tokens(), themed_icon("trash", role="danger")
    assert _ink(icon) == QColor(tokens["danger"]).name() == _ink(icon, QIcon.Mode.Active)
    assert _ink(icon, QIcon.Mode.Disabled) == tokens["text_disabled"]


def test_copied_icon_keeps_rendering(theme):
    assert _ink(QIcon(themed_icon("star"))) == get_theme_tokens()["text_secondary"]


@pytest.mark.parametrize("name", ["", "Crop", "../app", "crop.svg", "no-such-icon"])
def test_unknown_or_malformed_names_are_rejected(theme, name):
    with pytest.raises((ValueError, FileNotFoundError)):
        themed_icon(name)


def test_unknown_role_is_rejected(theme):
    with pytest.raises(KeyError):
        themed_icon("crop", role="not_a_token")
