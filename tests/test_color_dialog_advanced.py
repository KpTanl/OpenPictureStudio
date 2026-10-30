"""折叠卡片、混色器控件与直方图测试。"""

import os
import tempfile
import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from src.ui.widgets.collapsible_card import CollapsibleCard
from src.ui.widgets.color_mixer_widget import ColorMixerWidget
from src.core.color_adjuster import ColorAdjuster
from src.core.photo_engine import RenderSource


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_collapsible_card(qapp):
    """测试 CollapsibleCard 的展开/折叠与标题设置。"""
    card = CollapsibleCard("基础调整", is_expanded=True)
    assert card.is_expanded() is True
    assert card._content_widget.isHidden() is False

    card.set_expanded(False)
    assert card.is_expanded() is False
    assert card._content_widget.isHidden() is True

    card.toggle()
    assert card.is_expanded() is True
    assert card._content_widget.isHidden() is False

    card.set_title("新标题")
    assert card.title() == "新标题"


def test_color_mixer_widget_ui(qapp):
    """测试 ColorMixerWidget 控件各通道切换与数值读写。"""
    w = ColorMixerWidget(lambda k, fb: fb)
    assert w.get_values()["red"] == {"hue": 0, "sat": 0, "lum": 0}

    # 切换到蓝色通道并修改
    w._on_channel_selected("blue")
    w._on_slider_changed("sat", 45)
    w._on_slider_changed("hue", -20)
    vals = w.get_values()
    assert vals["blue"]["sat"] == 45
    assert vals["blue"]["hue"] == -20
    assert vals["red"]["sat"] == 0

    # 重置蓝色通道
    w._on_reset_current_channel()
    assert w.get_values()["blue"]["sat"] == 0

    # 批量设置与重置全部
    w.set_values({"green": {"hue": 10, "sat": 20, "lum": 30}})
    assert w.get_values()["green"]["sat"] == 20
    w.reset_all()
    assert w.get_values()["green"]["sat"] == 0


def test_histogram_accuracy_and_speed():
    """测试优化后的 compute_histogram 的形态准确性与极速表现。"""
    adjuster = ColorAdjuster()
    # 构造一张渐变测试图像
    arr = np.linspace(0, 255, 600 * 800 * 3, dtype=np.uint8).reshape((600, 800, 3))
    img = Image.fromarray(arr, "RGB")

    hist = adjuster.compute_histogram(img)
    assert set(hist.keys()) == {"r", "g", "b", "luma", "max_count"}
    assert len(hist["r"]) == 256
    assert len(hist["g"]) == 256
    assert len(hist["b"]) == 256
    assert len(hist["luma"]) == 256
    assert all(isinstance(v, int) for v in hist["r"])
    assert sum(hist["r"]) > 0


def test_color_mixer_apply():
    """测试 ColorAdjuster 正确应用 color_mixer 与 masks。"""
    adj = ColorAdjuster()
    img = Image.new("RGB", (32, 32), (220, 50, 50))  # 红色块
    params = {
        "color_mixer": {
            "red": {"hue": 100, "sat": 0, "lum": 0}
        }
    }
    out = adj.apply_chain(img, params)
    orig_px = img.getpixel((16, 16))
    out_px = out.getpixel((16, 16))
    assert out_px != orig_px
    # 色相顺时针偏向橙黄色（绿色分量提升，红色下降）
    assert out_px[1] > orig_px[1]

    # 测试 ui_to_engine_params
    ui_dict = {"contrast_amt": 10, "color_mixer": params["color_mixer"]}
    eng_dict = adj.ui_to_engine_params(ui_dict)
    assert eng_dict["color_mixer"] == params["color_mixer"]


def test_color_mixer_eyedropper_and_channel_selection(qapp):
    """测试 ColorMixerWidget 色相自动定标与吸色通道切换。"""
    w = ColorMixerWidget(lambda k, fb: fb)
    assert w.select_channel_for_hue(302.0) == "magenta"
    assert w._current_channel == "magenta"

    assert w.select_channel_for_hue(32.0) == "orange"
    assert w._current_channel == "orange"

    assert w.select_channel_for_hue(115.0) == "green"
    assert w._current_channel == "green"

    assert w.select_channel_for_hue(5.0) == "red"
    assert w._current_channel == "red"

    assert w.select_channel_for_hue(355.0) == "red"
    assert w._current_channel == "red"

    assert w.select_channel_for_hue(240.0) == "blue"
    assert w._current_channel == "blue"

    assert w.select_channel_for_hue(180.0) == "cyan"
    assert w._current_channel == "cyan"

    assert w.select_channel_for_hue(60.0) == "yellow"
    assert w._current_channel == "yellow"

    assert w.select_channel_for_hue(275.0) == "purple"
    assert w._current_channel == "purple"
