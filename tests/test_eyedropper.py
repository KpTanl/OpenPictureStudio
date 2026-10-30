"""混色器吸管：色相计算、覆盖层的点击处理、面板的取样状态与通道图。"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.application.services import SamplingHandle  # noqa: E402
from src.core.mixer_sampling import (MIXER_CHANNELS, NO_SAMPLE, MixerSampling,  # noqa: E402
                                     MixerSamplingRequest)
from src.ui.image_viewer import ImageViewer  # noqa: E402
from src.ui.overlays.eyedropper_overlay import EyedropperOverlay  # noqa: E402
from src.ui.panels.color_adjust_panel import ColorAdjustPanel  # noqa: E402
from src.ui.widgets.color_mixer_widget import ColorMixerWidget, hue_of_rgb  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("rgb, hue", [
    ((255, 0, 0), 0.0), ((255, 255, 0), 60.0), ((0, 255, 0), 120.0),
    ((0, 255, 255), 180.0), ((0, 0, 255), 240.0), ((255, 0, 255), 300.0),
])
def test_hue_of_primary_and_secondary_colors(rgb, hue):
    assert hue_of_rgb(*rgb) == pytest.approx(hue)


@pytest.mark.parametrize("rgb", [(0, 0, 0), (128, 128, 128), (255, 255, 255), (120, 124, 127)])
def test_near_gray_has_no_reliable_hue(rgb):
    assert hue_of_rgb(*rgb) == 0.0


@pytest.mark.parametrize("rgb, channel", [
    ((230, 30, 30), "red"), ((240, 140, 30), "orange"), ((240, 230, 40), "yellow"),
    ((40, 200, 60), "green"), ((40, 210, 210), "cyan"), ((40, 60, 230), "blue"),
    ((150, 60, 230), "purple"), ((230, 50, 210), "magenta"),
])
def test_picked_color_selects_the_matching_mixer_channel(qapp, rgb, channel):
    mixer = ColorMixerWidget(lambda key, fallback: fallback)
    assert mixer.select_channel_for_hue(hue_of_rgb(*rgb)) == channel


def test_mixer_reports_eyedropper_state_changes(qapp):
    mixer = ColorMixerWidget(lambda key, fallback: fallback)
    states = []
    mixer.eyedropper_toggled.connect(states.append)
    mixer.set_eyedropper_active(True)
    mixer.set_eyedropper_active(False)
    assert states == [True, False]


def test_sampling_state_does_not_report_a_user_toggle(qapp):
    mixer = ColorMixerWidget(lambda key, fallback=None: fallback or key)
    states = []
    mixer.eyedropper_toggled.connect(states.append)
    mixer.set_eyedropper_state("preparing")
    mixer.set_eyedropper_state("ready")
    mixer.set_eyedropper_state("closed")
    assert states == []


def test_mixer_selects_a_channel_by_index(qapp):
    mixer = ColorMixerWidget(lambda key, fallback=None: fallback or key)
    assert [mixer.select_channel(index) for index in range(8)] == list(MIXER_CHANNELS)


class _Viewer:
    def __init__(self):
        self.cursor = None

    def setCursor(self, cursor):
        self.cursor = cursor

    def unsetCursor(self):
        self.cursor = None

    def update(self):
        pass


def _press(x, y, button=Qt.MouseButton.LeftButton):
    return QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(x, y), QPointF(x, y), button, button,
                       Qt.KeyboardModifier.NoModifier)


IMAGE_RECT = QRect(100, 50, 400, 200)


def _overlay(active=True, viewer=None):
    overlay, picks = EyedropperOverlay(), []
    overlay.set_viewer(viewer or _Viewer())
    overlay.picked.connect(lambda *args: picks.append(args))
    overlay.set_active(active)
    return overlay, picks


def test_inactive_overlay_leaves_mouse_events_to_the_viewer(qapp):
    overlay, picks = _overlay(active=False)
    assert overlay.handle_mouse_press(_press(200, 100), IMAGE_RECT) is False
    assert overlay.handle_mouse_move(_press(200, 100), IMAGE_RECT) is False
    assert picks == []


def test_active_overlay_reports_normalized_position(qapp):
    overlay, picks = _overlay()
    assert overlay.handle_mouse_press(_press(200, 100), IMAGE_RECT) is True
    assert picks == [(0.25, 0.25)]


def test_click_outside_the_image_is_consumed_without_picking(qapp):
    overlay, picks = _overlay()
    assert overlay.handle_mouse_press(_press(20, 20), IMAGE_RECT) is True
    assert picks == []


def test_other_buttons_do_not_pick(qapp):
    overlay, picks = _overlay()
    assert overlay.handle_mouse_press(_press(200, 100, Qt.MouseButton.RightButton), IMAGE_RECT) is True
    assert picks == []


def test_cursor_follows_the_active_state_and_is_restored_on_detach(qapp):
    viewer = _Viewer()
    overlay, _ = _overlay(active=False, viewer=viewer)
    assert viewer.cursor is None
    overlay.set_active(True)
    assert viewer.cursor == Qt.CursorShape.CrossCursor
    overlay.set_viewer(None)
    assert viewer.cursor is None


def test_viewer_samples_the_displayed_pixel(qapp):
    image = QImage(4, 2, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 255))
    image.setPixelColor(0, 0, QColor(255, 0, 0))
    viewer = ImageViewer()
    assert viewer.display_color_at(0.1, 0.1) is None
    viewer.set_pixmap(QPixmap.fromImage(image))
    assert viewer.display_color_at(0.1, 0.1) == (255, 0, 0)
    assert viewer.display_color_at(0.9, 0.9) == (0, 0, 255)
    assert viewer.display_color_at(1.0, 0.5) is None
    assert viewer.display_color_at(-0.1, 0.5) is None


class _Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, *args))


def _panel(state):
    # 调色面板在蒙版页与吸管之间换画布覆盖层，吸管的状态只投影到吸管覆盖层
    return SimpleNamespace(_sampling_state=state, sampling_picked=_Recorder(),
                           _mixer_widget=_Recorder(), _eyedropper_overlay=_Recorder())


@pytest.mark.parametrize("state, forwarded", [("ready", True), ("preparing", False), ("closed", False)])
def test_panel_forwards_a_pick_only_when_the_channel_map_is_ready(state, forwarded):
    panel = _panel(state)
    ColorAdjustPanel._on_eyedropper_picked(panel, 0.25, 0.75)
    assert panel.sampling_picked.calls == ([("emit", 0.25, 0.75)] if forwarded else [])


@pytest.mark.parametrize("state, clickable", [("ready", True), ("preparing", False), ("closed", False)])
def test_overlay_accepts_clicks_only_in_the_ready_state(state, clickable):
    panel = _panel("closed")
    ColorAdjustPanel.set_sampling_state(panel, state)
    assert panel._sampling_state == state
    assert panel._mixer_widget.calls == [("set_eyedropper_state", state)]
    assert panel._eyedropper_overlay.calls == [("set_active", clickable)]


def _srgb(*rows):
    return np.array(rows, dtype=np.float32)


RED, GREEN, BLUE, GRAY = (0.9, 0.1, 0.1), (0.1, 0.8, 0.2), (0.1, 0.2, 0.9), (0.5, 0.5, 0.5)


def _sampling(pixels, alpha=None):
    sampling = MixerSampling(MixerSamplingRequest(target_node="color"))
    sampling.capture(pixels, alpha, "python")
    return sampling


def test_channel_map_identifies_the_captured_colors():
    sampling = _sampling(_srgb([RED, GREEN], [BLUE, GRAY]))
    channels = sampling.to_channel_map((2, 2))
    assert channels.tolist() == [[MIXER_CHANNELS.index("red"), MIXER_CHANNELS.index("green")],
                                 [MIXER_CHANNELS.index("blue"), NO_SAMPLE]]
    assert not channels.flags.writeable


def test_capture_copies_the_pipeline_buffer():
    pixels = _srgb([RED, RED])
    sampling = _sampling(pixels)
    pixels[:] = GREEN
    assert sampling.to_channel_map((2, 1)).tolist() == [[MIXER_CHANNELS.index("red")] * 2]


def test_crop_and_quarter_turn_carry_the_colors_to_their_new_positions():
    sampling = _sampling(_srgb([RED, GREEN, BLUE]))
    sampling.crop((1, 0, 3, 1))
    assert sampling.to_channel_map((2, 1)).tolist() == [
        [MIXER_CHANNELS.index("green"), MIXER_CHANNELS.index("blue")]]
    sampling.rotate(90, output_size=(1, 2))
    assert sampling.to_channel_map((1, 2)).tolist() == [
        [MIXER_CHANNELS.index("blue")], [MIXER_CHANNELS.index("green")]]


def test_masked_and_transparent_pixels_have_no_channel():
    sampling = _sampling(_srgb([RED, RED, RED]), alpha=np.array([[1, 0, 1]], dtype=np.float32))
    sampling.apply_mask(np.array([[1, 1, 0]], dtype=np.float32))
    assert sampling.to_channel_map((3, 1)).tolist() == [[MIXER_CHANNELS.index("red"), NO_SAMPLE, NO_SAMPLE]]


def test_padding_adds_area_without_a_channel():
    sampling = _sampling(_srgb([GREEN]))
    sampling.pad((3, 1), (1, 0, 1, 0))
    assert sampling.to_channel_map((3, 1)).tolist() == [[NO_SAMPLE, MIXER_CHANNELS.index("green"), NO_SAMPLE]]


def test_channel_map_requires_a_capture():
    with pytest.raises(ValueError):
        MixerSampling(MixerSamplingRequest()).to_channel_map((1, 1))


class _Hold:
    closed = 0

    def close(self):
        self.closed += 1


def test_handle_reads_the_channel_at_a_normalized_position_and_releases_its_memory():
    hold = _Hold()
    channels = np.array([[0, 3], [5, NO_SAMPLE]], dtype=np.uint8)
    handle = SamplingHandle(channels, MixerSamplingRequest(target_node="color"), hold)
    assert handle.size == (2, 2)
    assert [handle.channel_at(x, y) for x, y in ((0.1, 0.1), (0.9, 0.1), (0.1, 0.9))] == [0, 3, 5]
    assert handle.channel_at(0.9, 0.9) is None
    assert handle.channel_at(1.0, 0.5) is None and handle.channel_at(0.5, -0.1) is None
    handle.close()
    handle.close()
    assert hold.closed == 1
    assert handle.size == (0, 0) and handle.channel_at(0.1, 0.1) is None


def test_replacing_and_closing_a_capture_releases_each_reservation():
    holds = []

    def reserve(size):
        holds.append(_Hold())
        return holds[-1]

    sampling = MixerSampling(MixerSamplingRequest(target_node="color"), reserve_memory=reserve)
    sampling.capture(_srgb([RED, GREEN]), None, "python")
    sampling.crop((0, 0, 1, 1))
    assert [hold.closed for hold in holds] == [1, 0]
    sampling.close()
    assert [hold.closed for hold in holds] == [1, 1]
    assert not sampling.active
