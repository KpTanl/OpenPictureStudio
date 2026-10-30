"""滑块与参数行：用户编辑与程序回填的区分、重置、键入、填充原点与主题取色。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from resources.styles.semantic_colors import MIXER_CHANNEL_COLORS, TRACK_GRADIENTS, mixer_track_colors  # noqa: E402
from resources.styles.theme_variables import get_theme_qss, get_theme_tokens_by_name  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens, to_qcolor  # noqa: E402
from src.ui.widgets.apple_slider import AppleSlider  # noqa: E402
from src.ui.widgets.color_mixer_widget import ColorMixerWidget  # noqa: E402
from src.ui.widgets.parameter_row import ParameterRow, ParameterSpec  # noqa: E402

BIPOLAR = ParameterSpec("contrast", "", "Contrast", -100, 100, 0, "int_signed")
EXPOSURE = ParameterSpec("exposure", "", "Exposure", -300, 300, 0, "ev")
UNIPOLAR = ParameterSpec("feather", "", "Feather", 0, 100, 0, "int")


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


def _mouse(kind, x, y, buttons=Qt.MouseButton.LeftButton):
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), Qt.MouseButton.LeftButton, buttons,
                       Qt.KeyboardModifier.NoModifier)


def _slider(minimum=-100, maximum=100, value=0, default=0, width=216):
    slider = AppleSlider(default_value=default)
    slider.setRange(minimum, maximum)
    slider.setValue(value)
    slider.resize(width, 20)
    edits = []
    slider.valueEdited.connect(edits.append)
    return slider, edits


def _host(widget):
    host = QWidget()
    widget.setParent(host)
    widget.move(20, 20)
    host.resize(widget.width() + 40, widget.height() + 40)
    host.show()
    QApplication.processEvents()
    return host


def _pixel(widget, x, y):
    return widget.window().grab().toImage().pixelColor(widget.x() + int(x), widget.y() + int(y)).name()


def test_to_qcolor_reads_both_token_forms():
    assert to_qcolor("#8098ff").name() == "#8098ff"
    color = to_qcolor("rgba(128, 152, 255, 0.20)")
    assert (color.red(), color.green(), color.blue(), color.alpha()) == (128, 152, 255, 51)
    with pytest.raises(ValueError):
        to_qcolor("not a color")


def test_programmatic_value_does_not_report_a_user_edit(theme):
    slider, edits = _slider()
    slider.setValue(40)
    assert slider.value() == 40 and edits == []
    slider.edit_value(55)
    slider.edit_value(55)
    assert edits == [55]


def test_clicking_the_track_jumps_there_and_keeps_dragging(theme):
    slider, edits = _slider()
    pressed, released = [], []
    slider.sliderPressed.connect(lambda: pressed.append(True))
    slider.sliderReleased.connect(lambda: released.append(True))
    slider.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, slider.width() - 8, 10))
    assert slider.value() == 100 and slider.isSliderDown() and pressed == [True]
    slider.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, 8, 10))
    assert slider.value() == -100
    slider.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, 8, 10, Qt.MouseButton.NoButton))
    assert not slider.isSliderDown() and released == [True]
    assert edits == [100, -100]


def test_pressing_the_knob_does_not_move_it(theme):
    slider, edits = _slider(value=30)
    center = slider._handle_rect().center()
    slider.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, center.x() + 3, center.y()))
    assert slider.value() == 30 and edits == []
    slider.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, center.x() + 3 + 20, center.y()))
    assert slider.value() == 50
    slider.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, 0, 0, Qt.MouseButton.NoButton))


def test_double_click_resets_only_on_the_knob(theme):
    slider, edits = _slider(value=80, default=10)
    resets = []
    slider.resetRequested.connect(lambda: resets.append(True))
    slider.mouseDoubleClickEvent(_mouse(QEvent.Type.MouseButtonDblClick, 8, 10))
    assert slider.value() == -100 and resets == []
    slider.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, 8, 10, Qt.MouseButton.NoButton))
    center = slider._handle_rect().center()
    slider.mouseDoubleClickEvent(_mouse(QEvent.Type.MouseButtonDblClick, center.x(), center.y()))
    assert slider.value() == 10 and resets == [True]
    assert edits == [-100, 10]


def test_arrow_keys_edit_and_the_wheel_is_ignored(theme):
    slider, edits = _slider()
    slider.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
    assert slider.value() == 1 and edits == [1]
    assert slider.focusPolicy() == Qt.FocusPolicy.StrongFocus


def test_slider_paints_fill_from_its_origin_in_theme_colors(theme):
    slider, _ = _slider(value=60)
    slider.set_fill_origin(0)
    host = _host(slider)
    for name in ("dark", "light", "beige"):
        theme.apply(name)
        QApplication.processEvents()
        tokens = get_theme_tokens()
        middle = slider.height() // 2
        assert _pixel(slider, slider._position(30), middle) == QColor(tokens["accent"]).name()
        assert _pixel(slider, slider._position(-60), middle) == QColor(tokens["track"]).name()
        assert _pixel(slider, slider._position(60), middle) == QColor(tokens["knob"]).name()
    slider.setValue(-60)
    QApplication.processEvents()
    assert _pixel(slider, slider._position(-30), slider.height() // 2) == QColor(get_theme_tokens()["accent"]).name()
    assert _pixel(slider, slider._position(40), slider.height() // 2) == QColor(get_theme_tokens()["track"]).name()
    host.close()


def test_colored_track_replaces_the_fill(theme):
    slider, _ = _slider(value=0)
    slider.set_track_colors(TRACK_GRADIENTS["temperature"])
    host = _host(slider)
    middle = slider.height() // 2
    left, right = QColor(_pixel(slider, slider._position(-95), middle)), QColor(_pixel(slider, slider._position(95), middle))
    assert left.blue() > left.red() and right.red() > right.blue()
    host.close()


def test_row_shows_formatted_value_and_marks_modification(theme):
    row = ParameterRow(EXPOSURE, "曝光度")
    edits = []
    row.value_edited.connect(edits.append)
    assert row.value_edit.text() == "+0.00" and not row.is_modified()
    assert row.value_edit.property("modified") is False
    row.set_value(65)
    assert row.value_edit.text() == "+0.65" and row.is_modified()
    assert row.value_edit.property("modified") is True and row._label.property("modified") is True
    assert edits == []


def test_row_reports_user_edits_from_slider_typing_and_reset(theme):
    row = ParameterRow(BIPOLAR, "对比度")
    edits, typed = [], []
    row.value_edited.connect(edits.append)
    row.typing_finished.connect(lambda: typed.append(True))
    row.slider.edit_value(25)
    row.value_edit.setText("-40")
    row.value_edit.editingFinished.emit()
    row.value_edit.setText("abc")
    row.value_edit.editingFinished.emit()
    assert row.value_edit.text() == "-40"
    row.value_edit.setText("500")
    row.value_edit.editingFinished.emit()
    assert row.value() == 100 and row.value_edit.text() == "+100"
    row._label.double_clicked.emit()
    assert row.value() == 0
    assert edits == [25, -40, 100, 0] and len(typed) == 3


def test_row_fills_from_the_middle_only_for_bipolar_parameters(theme):
    assert ParameterRow(BIPOLAR, "对比度").slider._fill_origin == 0
    assert ParameterRow(UNIPOLAR, "羽化").slider._fill_origin is None
    track = ParameterSpec("temperature", "", "Temperature", -100, 100, 0, "kelvin", TRACK_GRADIENTS["temperature"])
    assert ParameterRow(track, "色温").slider._track_colors == TRACK_GRADIENTS["temperature"]


def test_modified_value_is_drawn_in_the_accent_color(theme):
    for name in ("dark", "light", "beige"):
        qss = get_theme_qss(name)
        rule = qss[qss.index('QLineEdit#sliderValueEdit[modified="true"]'):]
        assert f"color: {get_theme_tokens_by_name(name)['accent']};" in rule[:rule.index("}")]


def test_mixer_rows_follow_the_channel_without_reporting_edits(theme):
    mixer = ColorMixerWidget(lambda key, fallback=None: fallback or key)
    changes = []
    mixer.values_changed.connect(lambda: changes.append(True))
    mixer.set_values({"blue": {"hue": 10, "sat": 20, "lum": -30}})
    mixer._on_channel_selected("blue")
    assert [mixer._rows[key].value() for key in ("hue", "sat", "lum")] == [10, 20, -30]
    assert mixer._rows["sat"].slider._track_colors == mixer_track_colors("blue")["sat"]
    assert changes == []
    mixer._rows["sat"].slider.edit_value(45)
    mixer._rows["hue"].reset()
    assert mixer.get_values()["blue"] == {"hue": 0, "sat": 45, "lum": -30}
    assert len(changes) == 2
    assert len(mixer.edit_controls()) == 3


def test_mixer_channel_dots_are_colored_by_the_stylesheet(theme):
    mixer = ColorMixerWidget(lambda key, fallback=None: fallback or key)
    qss = get_theme_qss("dark")
    for channel, color in MIXER_CHANNEL_COLORS.items():
        assert not mixer._channel_buttons[channel].styleSheet()
        assert f'QPushButton#mixerChannelDot[channel="{channel}"] {{ background-color: {color}; }}' in qss
