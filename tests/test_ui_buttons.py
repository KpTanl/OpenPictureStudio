"""按钮与分段开关：外观只由全局样式表与主题取值决定，并随主题切换。"""

import importlib.util
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from resources.styles.semantic_colors import BRAND_GRADIENT  # noqa: E402
from resources.styles.theme_variables import (CONTROL_HEIGHT, CONTROL_HEIGHT_COMPACT, TOOL_BUTTON_SIZE,  # noqa: E402
                                              get_theme_qss)
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.ui.widgets import (AppleBrandButton, AppleButton, AppleCompactButton, AppleDestructiveButton,  # noqa: E402
                            AppleGhostButton, AppleIconButton, ApplePrimaryButton, AppleSegmentedControl)

THEMES = ("dark", "light", "beige")
TEXT_BUTTONS = (AppleBrandButton, ApplePrimaryButton, AppleButton, AppleGhostButton, AppleDestructiveButton)
ALL_BUTTONS = (*TEXT_BUTTONS, AppleCompactButton, AppleIconButton)


@pytest.fixture
def theme():
    app = QApplication.instance() or QApplication([])
    previous = app.styleSheet(), app.property("ops.theme"), QPalette(app.palette())
    manager = ThemeManager()
    manager.apply("dark")
    yield manager
    app.setStyleSheet(previous[0])
    app.setProperty("ops.theme", previous[1])
    app.setPalette(previous[2])


def _shown(widget, width=None):
    """放进一个按主题底色铺底的容器里并完成布局，便于取像素。"""
    host = QWidget()
    widget.setParent(host)
    widget.move(20, 20)
    if width is not None:
        widget.resize(width, widget.sizeHint().height())
    else:
        widget.adjustSize()
    host.resize(widget.width() + 40, widget.height() + 40)
    host.show()
    QApplication.processEvents()
    return host


def _pixel(widget, x, y):
    """控件内 (x, y) 处连同其背后容器一起绘制出的颜色。"""
    return widget.window().grab().toImage().pixelColor(widget.x() + x, widget.y() + y).name()


@pytest.mark.parametrize("name", THEMES)
@pytest.mark.parametrize("cls", ALL_BUTTONS)
def test_stylesheet_covers_every_button_kind_and_state(name, cls):
    qss = get_theme_qss(name)
    for state in ("", ":hover", ":pressed", ":focus", ":disabled"):
        assert re.search(rf"QPushButton#{cls.OBJECT_NAME}{state}[,\s]", qss), (cls.OBJECT_NAME, state)


@pytest.mark.parametrize("cls", ALL_BUTTONS)
def test_buttons_carry_no_inline_style(theme, cls):
    button = cls()
    assert button.objectName() == cls.OBJECT_NAME
    assert not button.styleSheet()


def test_button_heights_follow_the_scale(theme):
    hosts = [_shown(cls("示例")) for cls in TEXT_BUTTONS]
    assert [host.children()[0].height() for host in hosts] == [CONTROL_HEIGHT] * len(TEXT_BUTTONS)
    assert AppleCompactButton("示例").height() == CONTROL_HEIGHT_COMPACT
    icon = AppleIconButton()
    assert (icon.width(), icon.height()) == (TOOL_BUTTON_SIZE, TOOL_BUTTON_SIZE)


@pytest.mark.parametrize("cls, key", [(ApplePrimaryButton, "accent"), (AppleDestructiveButton, "danger")])
def test_filled_buttons_take_their_color_from_the_theme_and_follow_changes(theme, cls, key):
    button = cls("示例")
    host = _shown(button, 120)
    seen = []
    for name in THEMES:
        theme.apply(name)
        QApplication.processEvents()
        assert _pixel(button, 8, button.height() // 2) == QColor(get_theme_tokens()[key]).name()
        seen.append(_pixel(button, 8, button.height() // 2))
    assert len(set(seen)) == len(THEMES)
    host.close()


def test_secondary_and_text_buttons_show_the_surface_behind_them(theme):
    for cls in (AppleButton, AppleGhostButton):
        button = cls("示例")
        host = _shown(button, 120)
        assert _pixel(button, 8, button.height() // 2) == QColor(get_theme_tokens()["surface"]).name()
        host.close()


def test_brand_button_uses_the_brand_gradient(theme):
    button = AppleBrandButton("导出")
    host = _shown(button, 200)
    left, right = QColor(_pixel(button, 4, 16)), QColor(_pixel(button, 195, 16))
    first, last = QColor(BRAND_GRADIENT[0]), QColor(BRAND_GRADIENT[2])
    assert left.blue() > left.red() and right.red() > right.blue()
    assert abs(left.blue() - first.blue()) < 40 and abs(right.red() - last.red()) < 60
    assert all(color in get_theme_qss("dark") for color in BRAND_GRADIENT)
    host.close()


def test_palette_follows_the_theme(theme):
    app = QApplication.instance()
    for name in THEMES:
        theme.apply(name)
        tokens = get_theme_tokens()
        assert app.palette().color(QPalette.ColorRole.Window).name() == tokens["surface"]
        assert app.palette().color(QPalette.ColorRole.WindowText).name() == tokens["text_primary"]


def test_segmented_control_paints_from_the_theme(theme):
    control = AppleSegmentedControl()
    control.add_segments(["看图", "编辑"])
    host = _shown(control, 200)
    assert control.height() == CONTROL_HEIGHT
    for name in THEMES:
        theme.apply(name)
        QApplication.processEvents()
        tokens = get_theme_tokens()
        assert _pixel(control, 10, 16) == QColor(tokens["segment_selected"]).name()
        assert _pixel(control, 190, 16) == QColor(tokens["well"]).name()
    control.setCurrentIndex(1)
    QApplication.processEvents()
    assert _pixel(control, 190, 16) == QColor(get_theme_tokens()["segment_selected"]).name()
    host.close()


def test_segmented_control_is_wide_enough_for_its_labels(theme):
    control = AppleSegmentedControl()
    control.add_segments(["Adjust", "History", "AI assistant"])
    widest = control.fontMetrics().horizontalAdvance("AI assistant")
    assert control.sizeHint().width() >= 3 * widest
    assert control.sizeHint().height() == CONTROL_HEIGHT


def test_gallery_builds_in_every_theme_and_language(theme):
    path = Path(__file__).resolve().parents[1] / "tools" / "ui_gallery.py"
    spec = importlib.util.spec_from_file_location("ui_gallery", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    gallery = module.Gallery(theme)
    for name, _, _ in module.THEMES:
        for language, _ in module.LANGUAGES:
            gallery.set_theme(name)
            gallery.set_language(language)
            assert gallery.content_height() > 400
    gallery.close()
