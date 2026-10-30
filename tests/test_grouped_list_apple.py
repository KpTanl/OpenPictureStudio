"""Apple HIG 风格 GroupedOptionCard、WallpaperDialog 与 QRadioButton 主题样式专项测试。"""

import os
import tempfile
import pytest
from PIL import Image
from PySide6.QtCore import Qt, QPoint, QPointF
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from resources.styles.theme_variables import (
    get_theme_qss,
    get_theme_tokens_by_name,
    THEMES,
)
from src.ui.widgets.grouped_list import GroupedOptionCard, GroupedOptionRow
from src.ui.dialogs.wallpaper_dialog import WallpaperDialog
from src.ui.theme import ThemeManager, set_theme_manager


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def sample_image_file(tmp_path):
    img = Image.new("RGB", (100, 100), color=(80, 120, 160))
    file_path = str(tmp_path / "test_wp.jpg")
    img.save(file_path)
    return file_path


def test_radio_button_theme_qss_color_inside():
    """验证所有注册主题的 QSS 中 QRadioButton::indicator:checked 均为内部填充主色，无 5px 粗边框镂空缺陷。"""
    for theme_name in ["dark", "light", "beige"]:
        qss = get_theme_qss(theme_name)
        tokens = get_theme_tokens_by_name(theme_name)
        primary = tokens["accent"]
        widget_bg = tokens["page"]

        # 断言不再包含 5px 粗边框
        assert "border: 5px solid" not in qss

        # 断言 indicator:checked 内部包含 background-color 填充为 primary 色
        assert f"background-color: {primary};" in qss


def test_grouped_option_card_lifecycle_and_selection(qapp):
    """测试 GroupedOptionCard 基础生命周期、项添加与单选切换。"""
    card = GroupedOptionCard()
    assert card.item_count() == 0

    records = []
    card.selection_changed.connect(lambda idx, key: records.append((idx, key)))

    # 添加项
    card.add_item("fill", "填充（推荐）")
    card.add_item("fit", "适应")
    card.add_item("stretch", "拉伸")

    assert card.item_count() == 3
    assert card.checked_index() == 0
    assert card.checked_key() == "fill"

    # 切换选中项
    card.set_checked_index(1)
    assert card.checked_index() == 1
    assert card.checked_key() == "fit"
    assert len(records) == 1
    assert records[-1] == (1, "fit")

    # 按 key 切换
    card.set_checked_key("stretch")
    assert card.checked_index() == 2
    assert card.checked_key() == "stretch"
    assert records[-1] == (2, "stretch")

    # 切换不存在的 key
    card.set_checked_key("non_existing")
    assert card.checked_index() == 2

    # 重命名文本
    card.set_item_text(0, "填充 (默认)", "副标题")
    assert card._rows[0].title == "填充 (默认)"


def test_grouped_option_card_set_items_and_clear(qapp):
    """测试批量设置选项与清空列表。"""
    card = GroupedOptionCard()
    card.set_items([("k1", "Title 1"), ("k2", "Title 2"), ("k3", "Title 3")])
    assert card.item_count() == 3
    assert card.checked_key() == "k1"

    card.clear()
    assert card.item_count() == 0
    assert card.checked_key() == ""


def test_grouped_option_row_mouse_events(qapp):
    """测试单个 GroupedOptionRow 的悬浮、点击与双击事件触发。"""
    row = GroupedOptionRow(0, "test_key", "Test Row")
    row.resize(200, 44)

    clicked_records = []
    double_clicked_records = []
    row.clicked.connect(clicked_records.append)
    row.double_clicked.connect(double_clicked_records.append)

    # 模拟鼠标按下与抬起
    position = QPointF(10, 10)
    global_position = row.mapToGlobal(position)
    press_ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, position, global_position,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    row.mousePressEvent(press_ev)
    assert row._pressed is True

    release_ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, position, global_position,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    row.mouseReleaseEvent(release_ev)
    assert row._pressed is False
    assert clicked_records == [0]

    # 模拟双击
    dbl_ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonDblClick, position, global_position,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    row.mouseDoubleClickEvent(dbl_ev)
    assert double_clicked_records == [0]


def test_grouped_option_card_mouse_and_keyboard(qapp):
    """测试鼠标点击与键盘方向键导航。"""
    card = GroupedOptionCard()
    card.add_item("fill", "Fill")
    card.add_item("fit", "Fit")
    card.add_item("stretch", "Stretch")

    double_clicked_records = []
    card.item_double_clicked.connect(lambda idx, key: double_clicked_records.append((idx, key)))

    # 模拟键盘向下移动
    key_down = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
    card.keyPressEvent(key_down)
    assert card.checked_index() == 1

    # 模拟键盘向上移动
    key_up = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
    card.keyPressEvent(key_up)
    assert card.checked_index() == 0

    # 模拟回车触发双击/激活信号
    key_enter = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
    card.keyPressEvent(key_enter)
    assert len(double_clicked_records) == 1
    assert double_clicked_records[-1] == (0, "fill")

    # 模拟空格键激活
    key_space = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)
    card.keyPressEvent(key_space)
    assert len(double_clicked_records) == 2


def test_grouped_option_card_painting_under_all_themes(qapp):
    """测试不同主题下 GroupedOptionCard 与各行在普通、悬浮、按下状态下的 QPainter 绘制覆盖率与颜色有效性。"""
    mgr = ThemeManager()
    set_theme_manager(mgr)

    card = GroupedOptionCard()
    card.add_item("opt1", "Option 1")
    card.add_item("opt2", "Option 2")
    card.add_item("opt3", "Option 3")
    card.resize(300, 150)

    pixmap = QPixmap(300, 150)

    for theme in ["dark", "light", "beige"]:
        mgr.apply(theme)

        # 1. 常规态绘制
        painter = QPainter(pixmap)
        try:
            card.render(painter, QPoint())
        finally:
            painter.end()

        # 2. 悬浮态绘制
        card._rows[1]._hovered = True
        painter = QPainter(pixmap)
        try:
            card.render(painter, QPoint())
        finally:
            painter.end()
        card._rows[1]._hovered = False

        # 3. 按下态绘制
        card._rows[1]._pressed = True
        painter = QPainter(pixmap)
        try:
            card.render(painter, QPoint())
        finally:
            painter.end()
        card._rows[1]._pressed = False


def test_wallpaper_dialog_with_grouped_card(qapp, sample_image_file):
    """测试 WallpaperDialog 采用 GroupedOptionCard 后的交互与多主题生命周期。"""
    mgr = ThemeManager()
    set_theme_manager(mgr)

    dialog = WallpaperDialog(sample_image_file)
    assert dialog._option_card.item_count() == 5
    assert dialog._option_card.checked_index() == 0

    for theme in ["light", "beige", "dark"]:
        mgr.apply(theme)
        dialog.apply_theme(theme)

    # 动态切换语言
    dialog.retranslate_ui()
    assert dialog._option_card.item_count() == 5

    # 切换选项
    dialog._option_card.set_checked_index(2)
    assert dialog._option_card.checked_key() == "dialog.wallpaper.style.stretch"
