"""调色面板的顶部与分组、曲线的通道切换与取色、直方图的参考线与取色。"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from resources.styles.semantic_colors import CHANNEL_COLORS, color_channels  # noqa: E402
from src.core.photo_engine import _apply_curves  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.ui.widgets.curves_widget import CURVE_CHANNELS, CurvesWidget  # noqa: E402
from src.ui.widgets.histogram_widget import HistogramWidget  # noqa: E402
from tests.support import wait_for_panel_source  # noqa: E402

_IDENTITY = [(0, 0), (255, 255)]


@pytest.fixture
def theme(qapp):
    previous = qapp.styleSheet(), qapp.property("ops.theme"), QPalette(qapp.palette())
    manager = ThemeManager()
    manager.apply("dark")
    yield manager
    qapp.setStyleSheet(previous[0])
    qapp.setProperty("ops.theme", previous[1])
    qapp.setPalette(previous[2])


@pytest.fixture
def language(qapp):
    previous = get_translation_service()
    service = TranslationService(default_language="zh-CN", fallback_language="en")
    set_translation_service(service)
    yield service
    set_translation_service(previous)


@pytest.fixture
def panel(theme, language, panel_factory, tmp_path):
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (160, 120), (120, 160, 200)).save(path)
    widget = panel_factory("color_adjust", path)
    widget.resize(308, 1200)
    widget.show()
    wait_for_panel_source(widget, path)
    changes = []
    widget.params_changed.connect(changes.append)
    return widget, changes


def _points(curve):
    return [tuple(point) for point in curve]


def test_only_the_common_groups_start_expanded(panel):
    widget, _ = panel
    cards = widget._group_cards
    assert list(cards) == ["tone", "presence", "curves", "color_mixer"]
    assert [card.title() for card in cards.values()] == ["影调与白平衡", "质感与饱和度", "色调曲线", "色彩混合"]
    assert [card.is_expanded() for card in cards.values()] == [True, True, False, False]
    assert widget._curves_widget.isHidden() or not widget._curves_widget.isVisible()
    cards["curves"].set_expanded(True)
    cards["color_mixer"].set_expanded(True)
    assert widget._curves_widget.isVisible() and widget._mixer_widget.isVisible()


def test_look_and_preset_rows_line_up_and_follow_the_language(panel, language):
    widget, changes = panel
    look = widget._look_controls

    def control_edges():
        return (look._choice.mapTo(widget, QPoint(0, 0)).x(), widget._preset_combo.mapTo(widget, QPoint(0, 0)).x())

    assert widget._preset_label.text() == "预设" and look._label.text() == "观感"
    assert [button.text() for button in (widget._btn_auto, widget._btn_bw, widget._btn_hdr)] == ["自动", "黑白", "HDR"]
    left, right = control_edges()
    assert left == right

    language.set_language("en")
    widget.retranslate_ui()
    QApplication.processEvents()
    assert widget._preset_label.text() == language.translate("color_adjust.presets")
    assert not widget._preset_label.text().endswith((":", "："))
    assert widget._btn_auto.text() == language.translate("color_adjust.mode.auto")
    assert widget._group_cards["curves"].title() == language.translate("color_adjust.group.curves")
    assert widget._curve_channels._chips["g"].text() == language.translate("color_adjust.curves.green")
    assert widget._parameter_rows["contrast_amt"].label() == language.translate("color_adjust.slider.contrast_amt")
    left, right = control_edges()
    assert left == right
    assert not changes
    top = [widget, widget._preset_label, widget._preset_combo, look, *look.findChildren(QWidget),
           widget._btn_auto, widget._btn_bw, widget._btn_hdr, widget._histogram_widget, widget._curves_widget,
           widget._curve_channels]
    assert not any(child.styleSheet() for child in top)


def test_curve_channel_chips_choose_which_curve_is_edited(panel):
    widget, changes = panel
    widget._group_cards["curves"].set_expanded(True)
    chips, curve = widget._curve_channels, widget._curves_widget
    assert chips.current() == "rgb" and curve.active_channel() == "rgb"
    assert [chips._chips[channel].text() for channel in CURVE_CHANNELS] == ["RGB", "红", "绿", "蓝"]

    # 切换通道是界面状态，不是参数
    QTest.mouseClick(chips._chips["r"], Qt.MouseButton.LeftButton)
    assert curve.active_channel() == "r" and not changes

    rect = curve._graph_rect()
    QTest.mouseClick(curve, Qt.MouseButton.LeftButton,
                     pos=QPoint(int(rect.center().x()), int(rect.top() + rect.height() * 0.25)))
    curves = changes[-1]["curves"]
    assert len(curves["r"]) == 3 and _points(curves["rgb"]) == _IDENTITY and _points(curves["g"]) == _IDENTITY
    assert widget.edit_group is None

    count = len(changes)
    widget.restore_params({**widget.parameters, "curves": None})
    assert len(changes) == count and curve.get_curves()["r"] == _IDENTITY
    # 回填不改变正在看的通道
    assert curve.active_channel() == "r" and chips.current() == "r"


def test_a_single_channel_curve_only_moves_that_channel():
    """分通道曲线在引擎里确实生效：只给红色通道一条上抬的曲线，绿、蓝不变。"""
    pixels = np.full((2, 2, 3), 0.5, np.float32)
    lifted = _apply_curves(pixels, {"r": [(0, 0), (128, 200), (255, 255)]})
    assert float(lifted[..., 0].min()) > 0.6
    assert np.allclose(lifted[..., 1:], 0.5)


def test_curves_take_colors_from_the_theme_and_the_channel_palette(theme):
    widget = CurvesWidget()
    widget.resize(280, 180)
    center = QPoint(140, 90)
    theme.apply("light")
    assert widget.grab().toImage().pixelColor(center).lightness() < 110
    theme.apply("dark")
    assert widget.grab().toImage().pixelColor(center).lightness() > 150

    # 红色曲线压成一条水平线，线上的颜色就是通道色
    widget.set_curves({"r": [(0, 128), (255, 128)]})
    widget.set_active_channel("r")
    rect = widget._graph_rect()
    sample = widget.grab().toImage().pixelColor(int(rect.left() + rect.width() * 0.25), round(rect.center().y()))
    red, green, blue, _ = color_channels(CHANNEL_COLORS["r"])
    assert red > green + 60 and red > blue + 60
    assert sample.red() > sample.green() + 60 and sample.red() > sample.blue() + 60
    assert widget.get_curves()["rgb"] == _IDENTITY
    widget.deleteLater()


def test_histogram_shows_quartile_guides_and_channel_colors(theme):
    widget = HistogramWidget()
    widget.resize(260, widget.height())
    empty = widget.grab().toImage()
    assert not widget.has_data()
    middle = round(widget._graph_rect().left() + widget._graph_rect().width() * 0.5)
    # 四分位参考线与旁边的底色不同
    assert empty.pixelColor(middle, 40) != empty.pixelColor(middle + 8, 40)
    quarter = round(widget._graph_rect().left() + widget._graph_rect().width() * 0.25)
    assert empty.pixelColor(quarter, 40) != empty.pixelColor(quarter + 8, 40)

    red = [0] * 256
    for index in range(150, 200):
        red[index] = 1000
    red[255] = 4000
    widget.set_histogram_data({"r": red, "g": [0] * 256, "b": [0] * 256, "luma": [0] * 256})
    assert widget.has_data()
    image = widget.grab().toImage()
    body = image.pixelColor(round(260 * 175 / 255), widget.height() - 6)
    assert body.red() > body.green() + 30 and body.red() > body.blue() + 30
    # 红色通道在最亮一格裁切：右上角的指示是红色
    corner = image.pixelColor(widget.width() - 6, 5)
    assert corner.red() > 200 and corner.green() < 80 and corner.blue() < 80
    # 左上角没有裁切，是不起眼的底色
    idle = image.pixelColor(5, 5)
    track = color_channels(get_theme_tokens()["track"])
    assert abs(idle.red() - track[0]) < 30 and abs(idle.green() - track[1]) < 30

    widget.clear()
    assert not widget.has_data() and widget.grab().toImage() == empty
    widget.deleteLater()


def test_restored_changes_open_the_collapsed_group_once(panel):
    """回填进来的参数让收起的分组有了改动时展开它；用户再收起后，同一份改动不会反复把它展开。"""
    widget, changes = panel
    curves, mixer = widget._group_cards["curves"], widget._group_cards["color_mixer"]
    assert not curves.is_expanded() and not mixer.is_expanded()
    lifted = {"rgb": _IDENTITY, "r": [(0, 0), (128, 170), (255, 255)], "g": _IDENTITY, "b": _IDENTITY}
    widget.restore_params({**widget.parameters, "curves": lifted})
    assert curves.is_expanded() and not mixer.is_expanded() and not changes

    curves.set_expanded(False)
    widget.restore_params({**widget.parameters, "contrast_amt": 12})
    assert not curves.is_expanded()

    # 改动消失后再出现，重新展开
    widget.restore_params({**widget.parameters, "curves": None})
    widget.restore_params({**widget.parameters, "curves": lifted,
                           "color_mixer": {"red": {"hue": 0, "sat": 25, "lum": 0}}})
    assert curves.is_expanded() and mixer.is_expanded()
    # 四条都是直线的曲线不算改动
    curves.set_expanded(False)
    widget.restore_params({**widget.parameters, "curves": None})
    widget.restore_params({**widget.parameters, "curves": {key: _IDENTITY for key in ("rgb", "r", "g", "b")}})
    assert not curves.is_expanded()
