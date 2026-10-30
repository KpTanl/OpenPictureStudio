"""缩略图面板及拖拽重排序单元测试。"""

import pytest
from PIL import Image
from PySide6.QtCore import Qt, QPoint, QPointF, QMimeData
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication

from src.ui.widgets.thumbnail_panel import ThumbnailPanel, _ReorderListWidget
from tests.support import wait_for_thumbnails, wait_until


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def temp_images(tmp_path):
    paths = []
    for i in range(4):
        p = str(tmp_path / f"img_{i}.png")
        img = Image.new("RGB", (64, 64), color=(i * 40, i * 40, i * 40))
        img.save(p)
        paths.append(p)
    return paths


@pytest.fixture
def panel(qapp, image_sources, temp_images):
    panel = ThumbnailPanel(image_source_service=image_sources)
    panel.resize(960, 200)
    panel.show()
    panel.load_images(temp_images, current_path=temp_images[0], version=1)
    wait_for_thumbnails(panel, temp_images, temp_images[0], version=1)
    try:
        yield panel
    finally:
        panel.shutdown()
        panel.close()


def test_thumbnail_panel_load_and_flags(qapp, temp_images, panel):
    """测试缩略图加载及列表项标志位配置（必须包含 ItemIsDropEnabled）。"""

    assert panel.list_widget.count() == len(temp_images)
    assert panel.get_all_paths() == temp_images

    for i in range(panel.list_widget.count()):
        item = panel.list_widget.item(i)
        flags = item.flags()
        assert flags & Qt.ItemFlag.ItemIsEnabled
        assert flags & Qt.ItemFlag.ItemIsSelectable
        assert flags & Qt.ItemFlag.ItemIsDragEnabled
        assert flags & Qt.ItemFlag.ItemIsDropEnabled


def test_calc_insert_index(qapp, temp_images, panel):
    """测试插入位置计算算法的精确性。"""
    list_widget = panel.list_widget

    panel.resize(600, 200)
    panel.show()
    QApplication.processEvents()

    rect_1 = list_widget.visualItemRect(list_widget.item(1))
    center_x = rect_1.center().x()
    center_y = rect_1.center().y()

    pos_left = QPoint(center_x - 5, center_y)
    assert list_widget._calc_insert_index(pos_left) == 1

    pos_right = QPoint(center_x + 5, center_y)
    assert list_widget._calc_insert_index(pos_right) == 2

    rect_0 = list_widget.visualItemRect(list_widget.item(0))
    pos_far_left = QPoint(max(0, rect_0.left() - 20), center_y)
    assert list_widget._calc_insert_index(pos_far_left) == 0

    rect_last = list_widget.visualItemRect(list_widget.item(len(temp_images) - 1))
    pos_far_right = QPoint(rect_last.right() + 50, center_y)
    assert list_widget._calc_insert_index(pos_far_right) == len(temp_images)

    panel.close()


def test_drag_and_drop_reorder(qapp, temp_images, panel):
    """测试拖拽放置（dropEvent）触发的重排序与信号通知。"""
    panel.resize(600, 200)
    panel.show()
    QApplication.processEvents()

    reordered_paths = []
    panel.order_changed.connect(lambda paths: reordered_paths.append(list(paths)))

    list_widget = panel.list_widget

    list_widget._drag_start_row = 0
    rect_2 = list_widget.visualItemRect(list_widget.item(2))
    drop_pos = QPointF(rect_2.center().x() + 5, rect_2.center().y())

    mime = QMimeData()
    event = QDropEvent(
        drop_pos,
        Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier
    )
    list_widget.dropEvent(event)

    expected_order_1 = [temp_images[1], temp_images[2], temp_images[0], temp_images[3]]
    assert panel.get_all_paths() == expected_order_1
    assert len(reordered_paths) == 1
    assert reordered_paths[0] == expected_order_1
    wait_until(lambda: not panel.has_pending_items and list_widget.dragEnabled(), "第一次排序完成")
    qapp.processEvents()

    list_widget._drag_start_row = 3
    rect_0 = list_widget.visualItemRect(list_widget.item(0))
    drop_pos_2 = QPointF(rect_0.center().x() - 5, rect_0.center().y())

    event_2 = QDropEvent(
        drop_pos_2,
        Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier
    )
    list_widget.dropEvent(event_2)

    expected_order_2 = [temp_images[3], temp_images[1], temp_images[2], temp_images[0]]
    assert panel.get_all_paths() == expected_order_2
    assert len(reordered_paths) == 2
    assert reordered_paths[1] == expected_order_2

    panel.close()


def test_drag_enter_and_move_internal(qapp):
    """测试 _ReorderListWidget 内部拖拽事件的接受与动作设置。"""
    list_widget = _ReorderListWidget()
    mime = QMimeData()

    enter_event = QDragEnterEvent(
        QPoint(10, 10),
        Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier
    )
    enter_event.accept = lambda: setattr(enter_event, "_accepted", True)
    setattr(enter_event, "source", lambda: list_widget)
    list_widget.dragEnterEvent(enter_event)
    assert getattr(enter_event, "_accepted", False) is True

    move_event = QDragMoveEvent(
        QPoint(10, 10),
        Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier
    )
    move_event.accept = lambda: setattr(move_event, "_accepted", True)
    setattr(move_event, "source", lambda: list_widget)
    list_widget.dragMoveEvent(move_event)
    assert getattr(move_event, "_accepted", False) is True


def test_thumbnail_panel_fit_button(qapp, temp_images, panel):
    """缩略图随面板实际高度适应，列表与选择保持。"""
    panel.resize(600, 160)
    panel.show()
    QApplication.processEvents()

    width = panel.list_widget.iconSize().width()
    panel.resize(600, 240)
    QApplication.processEvents()

    assert panel.list_widget.iconSize().width() > width
    assert panel.get_all_paths() == temp_images
    assert panel.get_selected_paths() == [temp_images[0]]

    panel.close()


def test_thumbnail_panel_collapse_toggle(qapp, temp_images, panel):
    """胶片条收起为零高度，展开后保留列表与选择。"""
    changes = []
    panel.collapsed_changed.connect(changes.append)
    try:
        assert panel.is_collapsed() is False

        panel.toggle_collapse()
        assert panel.is_collapsed() is True
        assert panel.maximumHeight() == 0 and panel.height() == 0
        assert panel.get_all_paths() == temp_images

        panel.toggle_collapse()
        assert panel.is_collapsed() is False
        assert panel.maximumHeight() > 0 and panel.height() > 0
        assert panel.get_selected_paths() == [temp_images[0]]
        assert changes == [True, False]
    finally:
        panel.close()
