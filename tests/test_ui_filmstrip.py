"""胶片条：翻页、位置、筛选与收起的界面行为；外观取自全局样式表，不带内联样式。"""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtCore import QMimeData, QObject, QPoint, QPointF, Qt, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QAction, QColor, QDropEvent, QPainter, QPalette, QPixmap  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QSplitter, QWidget  # noqa: E402

from resources.styles.theme_variables import FILMSTRIP_HEIGHT, get_theme_qss  # noqa: E402
from src.application.image_source_service import ImageSourceService  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.ui.widgets import thumbnail_panel as module  # noqa: E402
from src.ui.widgets.action_button import ActionIconButton  # noqa: E402
from src.ui.widgets.apple_menu import AppleDropDown  # noqa: E402
from src.ui.widgets.rating_filters import RATING_FILTERS  # noqa: E402
from src.ui.widgets.thumbnail_panel import ThumbnailPanel  # noqa: E402
from tests.support import wait_until  # noqa: E402

THEMES = ("dark", "light", "beige")


class _Settings:
    """代替 QSettings，胶片条的大小与收起状态不写进本机设置。"""

    store: dict = {}

    def __init__(self, *_args):
        pass

    def value(self, key, default=None, type=None):
        return self.store.get(key, default)

    def setValue(self, key, value):
        self.store[key] = value


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
def filmstrip(theme, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "QSettings", _Settings)
    _Settings.store = {}
    sources = ImageSourceService()
    paths = []
    for index in range(15):
        path = tmp_path / f"photo_{index:02d}.png"
        Image.new("RGB", (48, 32), (40 + index * 10, 90, 140)).save(path)
        paths.append(str(path))
    host = QSplitter(Qt.Orientation.Vertical)
    host.setChildrenCollapsible(False)
    host.setHandleWidth(4)
    host.addWidget(QWidget())
    panel = ThumbnailPanel(host, image_source_service=sources)
    host.addWidget(panel)
    host.setStretchFactor(0, 1)
    host.setStretchFactor(1, 0)
    host.resize(900, 600)
    host.show()
    panel.load_images(paths, paths[0])
    for _ in range(200):
        QApplication.processEvents()
        if not panel.has_pending_items:
            break
        QTest.qWait(5)
    assert not panel.has_pending_items
    yield panel, paths, host
    panel.shutdown()
    host.close()
    sources.close()


def _thumbnails_fit(panel):
    """缩略图连同选中描边完整显示在列表的可见区域里。"""
    widget = panel.list_widget
    icon, grid = widget.iconSize().height(), widget.gridSize().height()
    return grid - icon >= 6 and widget.viewport().height() >= grid


def test_filmstrip_has_no_inline_style_and_starts_at_the_design_height(filmstrip):
    panel, _paths, _host = filmstrip
    assert not panel.styleSheet() and not hasattr(panel, "apply_theme")
    assert panel.height() == FILMSTRIP_HEIGHT and panel.minimumHeight() < FILMSTRIP_HEIGHT < panel.maximumHeight()
    assert panel.list_widget.iconSize().width() == panel._thumb_width == FILMSTRIP_HEIGHT - module.PANEL_CHROME
    assert _thumbnails_fit(panel)
    for name in ("_size_slider", "_fit_btn", "_collapse_btn", "_count_label"):
        assert not hasattr(panel, name), name


def test_dragging_the_divider_resizes_the_strip_and_fits_the_thumbnails(filmstrip):
    panel, paths, host = filmstrip
    panel.select_image(paths[12])
    handle = host.handle(1)
    center = handle.rect().center()
    QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=center)
    QTest.mouseMove(handle, center + QPoint(0, -40))
    QTest.mouseMove(handle, center + QPoint(0, -50))
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=center + QPoint(0, -50))
    QTest.qWait(module.SAVE_DELAY_MS + 100)
    dragged = panel.height()
    assert dragged >= FILMSTRIP_HEIGHT + 50
    assert panel._thumb_width == panel.list_widget.iconSize().width() == panel.height() - module.PANEL_CHROME
    assert _thumbnails_fit(panel)
    assert _Settings.store["thumbSize"] == panel._thumb_width
    # 放大后当前这一张仍在可见范围内
    current = panel.list_widget.visualItemRect(panel.list_widget.currentItem())
    assert panel.list_widget.viewport().rect().intersects(current)

    # 拖到最小和最大都停在限度上，缩略图始终完整
    host.setSizes([590, 10])
    QApplication.processEvents()
    assert panel.height() == panel.minimumHeight() == module.MIN_THUMB_WIDTH + module.PANEL_CHROME
    assert panel.list_widget.iconSize().width() == module.MIN_THUMB_WIDTH and _thumbnails_fit(panel)
    host.setSizes([10, 590])
    QApplication.processEvents()
    assert panel.height() == panel.maximumHeight() == module.MAX_THUMB_WIDTH + module.PANEL_CHROME
    assert panel.list_widget.iconSize().width() == module.MAX_THUMB_WIDTH and _thumbnails_fit(panel)
    # 程序设置的高度只影响显示，不改写用户选过的大小
    assert _Settings.store["thumbSize"] == dragged - module.PANEL_CHROME


def test_position_shows_the_current_photo_among_the_visible_ones(filmstrip):
    panel, paths, _host = filmstrip
    assert (panel._position_current.text(), panel._position_total.text()) == ("1", "/ 15")
    panel.select_image(paths[6])
    assert (panel._position_current.text(), panel._position_total.text()) == ("7", "/ 15")
    selected = []
    panel.image_selected.connect(selected.append)
    item = panel.list_widget.item(11)
    panel.list_widget.setCurrentItem(item)
    assert panel._position_current.text() == "12" and selected == []  # 位置跟随当前项，选图只由点击发出


def test_navigation_buttons_trigger_the_given_actions(filmstrip):
    panel, _paths, host = filmstrip
    calls = []
    previous, following = QAction("&Previous Image", host), QAction("&Next Image", host)
    previous.triggered.connect(lambda: calls.append("previous"))
    following.triggered.connect(lambda: calls.append("next"))
    panel.set_navigation_actions(previous, following)
    panel.set_navigation_actions(previous, following)
    buttons = panel.findChildren(ActionIconButton)
    assert len(buttons) == 2
    QApplication.processEvents()
    before, after = sorted(buttons, key=lambda button: button.x())
    assert before.x() < panel.list_widget.x() < after.x() < panel._filter_dropdown.x()
    assert before.toolTip() == "Previous Image" and after.toolTip() == "Next Image"
    QTest.mouseClick(before, Qt.MouseButton.LeftButton)
    QTest.mouseClick(after, Qt.MouseButton.LeftButton)
    assert calls == ["previous", "next"]
    following.setEnabled(False)
    assert not after.isEnabled() and before.isEnabled()


def test_filter_dropdown_reports_user_choices_only(filmstrip):
    panel, _paths, _host = filmstrip
    dropdown = panel._filter_dropdown
    requests = []
    panel.rating_filter_requested.connect(requests.append)
    assert dropdown.count() == len(RATING_FILTERS) and dropdown.currentData() == "all"

    panel.apply_rating_filter("rated")
    assert dropdown.currentData() == "rated" and requests == []
    panel.apply_rating_filter("all")
    dropdown._on_action_triggered(2)
    assert requests == ["4+"]

    panel.apply_rating_filter("4+")
    panel.retranslate_ui()
    assert dropdown.currentData() == "4+" and dropdown.count() == len(RATING_FILTERS) and requests == ["4+"]


def test_collapsing_keeps_the_panel_alive_at_zero_height(filmstrip):
    panel, paths, host = filmstrip
    canvas = host.widget(0)
    changes = []
    panel.collapsed_changed.connect(changes.append)
    panel.select_image(paths[9])
    QApplication.processEvents()
    assert canvas.height() == host.height() - host.handleWidth() - FILMSTRIP_HEIGHT
    panel.set_collapsed(True)
    panel.set_collapsed(True)
    QApplication.processEvents()
    assert changes == [True] and panel.is_collapsed() and panel.height() == 0 and panel.isVisible()
    # 收起后画布接收让出的高度，下面不留一条空白
    assert host.sizes()[1] == 0 and canvas.height() == host.height() - host.handleWidth()
    assert _Settings.store["collapsed"] is True
    assert panel.list_widget.count() == 15 and panel.list_widget.currentItem().data(Qt.ItemDataRole.UserRole) == paths[9]

    panel.toggle_collapse()
    QApplication.processEvents()
    assert changes == [True, False] and panel.height() == FILMSTRIP_HEIGHT and _thumbnails_fit(panel)
    assert canvas.height() == host.height() - host.handleWidth() - FILMSTRIP_HEIGHT
    assert _Settings.store["collapsed"] is False


def test_ctrl_wheel_resizes_the_thumbnails_and_the_strip(filmstrip):
    panel, _paths, _host = filmstrip
    width = panel._thumb_width
    panel._on_thumb_zoom(1)
    QTest.qWait(module.SAVE_DELAY_MS + 100)
    assert panel._thumb_width == width + module.THUMB_ZOOM_STEP
    assert panel.height() == FILMSTRIP_HEIGHT + module.THUMB_ZOOM_STEP
    assert panel.list_widget.iconSize().width() == panel._thumb_width and _thumbnails_fit(panel)
    assert _Settings.store["thumbSize"] == panel._thumb_width
    panel._on_thumb_zoom(100)
    QApplication.processEvents()
    assert panel._thumb_width == module.MAX_THUMB_WIDTH and panel.height() == panel.maximumHeight()
    panel._on_thumb_zoom(-100)
    QApplication.processEvents()
    assert panel._thumb_width == module.MIN_THUMB_WIDTH and panel.height() == module.MIN_THUMB_WIDTH + module.PANEL_CHROME
    panel.set_collapsed(True)
    panel._on_thumb_zoom(1)
    QApplication.processEvents()
    assert panel.height() == 0  # 收起时调整大小不把胶片条撑开


def test_saved_state_is_restored(theme, monkeypatch):
    monkeypatch.setattr(module, "QSettings", _Settings)
    _Settings.store = {"thumbSize": 96, "collapsed": True, "thumbWidth": 300}
    sources = ImageSourceService()
    panel = ThumbnailPanel(image_source_service=sources)
    assert panel._thumb_width == 96 and panel.is_collapsed() and panel.height() == 0
    panel.set_collapsed(False)
    assert panel.height() == 96 + module.PANEL_CHROME
    panel.shutdown()
    panel.deleteLater()
    sources.close()


@pytest.mark.parametrize("name", THEMES)
def test_filmstrip_takes_its_surface_from_the_theme(filmstrip, theme, name):
    panel, _paths, host = filmstrip
    qss = get_theme_qss(name)
    for selector in ("ThumbnailPanel {", "ThumbnailPanel QListWidget::item:selected {",
                     "QScrollBar#thumbnailHorizontalScrollBar:horizontal {", "QLabel#filmstripCurrent {"):
        assert selector in qss, selector
    theme.apply(name)
    QApplication.processEvents()
    tokens = get_theme_tokens()
    image = host.grab().toImage()
    top = panel.y()
    assert image.pixelColor(4, top).name() == QColor(tokens["hairline_solid"]).name()
    assert image.pixelColor(4, top + 3).name() == QColor(tokens["surface"]).name()
    assert image.pixelColor(4, top + panel.height() - 2).name() == QColor(tokens["surface"]).name()


def test_dropdown_shows_an_ampersand_as_written(theme):
    dropdown = AppleDropDown()
    dropdown.addItem("4 Stars & Up", "4+")
    dropdown.setCurrentIndex(0)
    dropdown.resize(320, dropdown.sizeHint().height())
    assert dropdown.currentText() == "4 Stars & Up"
    assert dropdown._button_option().text == "4 Stars && Up"
    dropdown.deleteLater()


def _settle(panel):
    """等胶片条把重排后的列表整理完。"""
    for _ in range(200):
        QApplication.processEvents()
        if not panel.has_pending_items:
            return
        QTest.qWait(5)


def _drop(list_widget, source_row, x):
    """模拟把第 source_row 张拖到横坐标 x 处松手。"""
    list_widget._drag_start_row = source_row
    event = QDropEvent(QPointF(x, list_widget.viewport().height() / 2), Qt.DropAction.MoveAction, QMimeData(),
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    list_widget.dropEvent(event)


def test_dragging_a_thumbnail_moves_it_between_its_neighbours(filmstrip):
    panel, paths, _host = filmstrip
    orders = []
    panel.order_changed.connect(lambda order: orders.append(list(order)))
    widget = panel.list_widget
    assert widget.dragEnabled() and widget.acceptDrops()

    # 把第 1 张拖到第 3 张的右半边：落在第 3 张之后，其余依次前移
    target = widget.visualItemRect(widget.item(2))
    _drop(widget, 0, target.center().x() + 5)
    expected = [paths[1], paths[2], paths[0]] + paths[3:]
    assert panel.get_all_paths() == expected and orders == [expected]
    assert widget.currentItem().data(Qt.ItemDataRole.UserRole) == paths[0]
    _settle(panel)
    assert panel._position_current.text() == "3"

    # 再把它拖回最前面
    first = widget.visualItemRect(widget.item(0))
    _drop(widget, 2, first.center().x() - 5)
    assert panel.get_all_paths() == paths and len(orders) == 2
    _settle(panel)
    assert panel._position_current.text() == "1"

    # 松手在原位不算移动
    own = widget.visualItemRect(widget.item(4))
    _drop(widget, 4, own.center().x() - 3)
    assert panel.get_all_paths() == paths and len(orders) == 2


def test_edited_thumbnails_are_cropped_to_squares_like_source_thumbnails(filmstrip):
    """接入选片后胶片条显示编辑结果：结果按原比例渲染，胶片条与源缩略图一样居中裁成正方形铺满格子；
    编辑结果就是源画面的照片直接用源缩略图，不向编辑结果的渲染请求。"""
    panel, paths, _host = filmstrip
    unedited = set(paths[1::2])
    requested = []
    images = {}
    for index, path in enumerate(paths):
        wide = index % 2 == 0
        pixmap = QPixmap(300, 100) if wide else QPixmap(100, 300)
        pixmap.fill(QColor("red"))
        painter = QPainter(pixmap)
        painter.fillRect(100 if wide else 0, 0 if wide else 100, 100, 100, QColor("green"))
        painter.end()
        images[path] = SimpleNamespace(pixmap=pixmap, error="")

    # 与选片的渲染一样：要求正方形时按中心裁成正方形
    squares = {}
    for path, image in images.items():
        side = min(image.pixmap.width(), image.pixmap.height())
        square = image.pixmap.copy((image.pixmap.width() - side) // 2, (image.pixmap.height() - side) // 2, side, side)
        squares[path] = SimpleNamespace(pixmap=square, error="")

    class _Runner(QObject):
        ready = pyqtSignal(str)
        clearing = pyqtSignal(str)

        def request(self, channel, paths, maximum=300, *, square=False):
            requested.append((tuple(paths), square))

        def forget(self, channel):
            pass

        def image(self, path, maximum=300, *, square=False):
            return squares[path] if square else images[path]

    service = SimpleNamespace(results=tuple(paths), filters=SimpleNamespace(rating="all"), revision=1,
                              selected=(paths[0],), key=os.path.normcase, sessions=object(),
                              metadata=SimpleNamespace(get_mark=lambda path: "unmarked"),
                              photo_state=lambda path: {"edited": path not in unedited},
                              shows_source=lambda path: path in unedited)
    panel.set_browser(service, _Runner())
    shown = lambda: panel._filtered_paths[:6]
    wait_until(lambda: len(shown()) == 6 and all(
        isinstance(panel._icon_keys.get(path), module.ImageLoadKey) if path in unedited
        else panel._icon_keys.get(path) is squares[path] for path in shown()), "编辑结果与源缩略图")
    assert requested and requested[-1][1] and not unedited.intersection(requested[-1][0])
    for path in shown():
        icon = panel._path_to_item[path].icon()
        sizes = [(size.width(), size.height()) for size in icon.availableSizes()]
        if path in unedited:
            assert sizes == [(module.MAX_THUMB_WIDTH, module.MAX_THUMB_WIDTH)]
            continue
        assert sizes == [(100, 100)]
        assert icon.pixmap(100, 100).toImage().pixelColor(50, 50) == QColor("green")
