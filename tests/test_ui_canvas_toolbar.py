"""底部工具条、评分、轻提示与画布的适应：只表达用户意图，程序回填不发信号，照片不被工具条遮住。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from PyQt6.QtGui import QAction, QColor, QPalette, QPixmap  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QScrollArea, QStatusBar, QWidget  # noqa: E402

from resources.styles.theme_variables import (CANVAS_TOOLBAR_HEIGHT, CANVAS_TOOLBAR_MARGIN,  # noqa: E402
                                              get_theme_qss)
from src.ui.image_viewer import ImageViewer  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.ui.widgets.bottom_dock import BottomDock  # noqa: E402
from src.ui.widgets.canvas_toolbar import EDITOR, VIEWER, CanvasToolbar, CanvasToolbarActions  # noqa: E402
from src.ui.widgets.segmented_control import AppleSegmentedControl  # noqa: E402
from src.ui.widgets.star_rating import StarRating  # noqa: E402
from src.ui.widgets.toast import Toast  # noqa: E402

THEMES = ("dark", "light", "beige")
INSET = CANVAS_TOOLBAR_HEIGHT + CANVAS_TOOLBAR_MARGIN


@pytest.fixture
def theme(qapp):
    previous = qapp.styleSheet(), qapp.property("ops.theme"), QPalette(qapp.palette())
    manager = ThemeManager()
    manager.apply("dark")
    yield manager
    qapp.setStyleSheet(previous[0])
    qapp.setProperty("ops.theme", previous[1])
    qapp.setPalette(previous[2])


def _token(key):
    return QColor(get_theme_tokens()[key]).name()


def _canvas(width=600, height=400, image=(400, 300)):
    area = QScrollArea()
    area.setObjectName("imageScrollArea")
    area.setWidgetResizable(False)
    area.setAlignment(Qt.AlignmentFlag.AlignCenter)
    viewer = ImageViewer()
    area.setWidget(viewer)
    area.resize(width, height)
    area.show()
    QApplication.processEvents()
    pixmap = QPixmap(*image)
    pixmap.fill(QColor("#557799"))
    viewer.set_pixmap(pixmap)
    QApplication.processEvents()
    return area, viewer


def _image_rect_in_viewport(area, viewer):
    rect = viewer.get_image_display_rect()
    top_left = viewer.mapTo(area.viewport(), rect.topLeft())
    return top_left.x(), top_left.y(), rect.width(), rect.height()


def test_fit_keeps_the_photo_above_the_reserved_bottom(theme):
    area, viewer = _canvas()
    viewport = area.viewport()
    _, top, _, height = _image_rect_in_viewport(area, viewer)
    assert viewer.is_fit_mode() and viewer.height() == height
    assert top + height > viewport.height() - INSET  # 没有保留高度时照片会伸到工具条的位置

    viewer.set_bottom_inset(INSET)
    QApplication.processEvents()
    left, top, width, height = _image_rect_in_viewport(area, viewer)
    assert viewer.is_fit_mode()
    assert top >= 8 and top + height <= viewport.height() - INSET - 8 + 1
    # 照片在工具条上方的区域里居中，宽高比不变
    assert abs((top + height / 2) - (viewport.height() - INSET) / 2) <= 1
    assert abs((left + width / 2) - viewport.width() / 2) <= 1
    assert abs(width / height - 400 / 300) < 0.01
    assert viewer.height() == height + INSET

    # 其余倍率不受影响：控件就是照片本身，倍率仍按原图尺寸计算
    viewer.set_zoom_percent(100)
    QApplication.processEvents()
    assert not viewer.is_fit_mode() and viewer.get_zoom_percent() == 100
    assert (viewer.width(), viewer.height()) == (400, 300)
    assert viewer.get_image_display_rect().height() == 300

    viewer.zoom_fit()
    viewer.set_bottom_inset(0)
    QApplication.processEvents()
    _, top, _, height = _image_rect_in_viewport(area, viewer)
    assert viewer.height() == height and top + height > viewport.height() - INSET
    area.close()


def test_fit_follows_the_viewport_with_the_reserve_in_place(theme):
    area, viewer = _canvas(width=900, height=300)
    viewer.set_bottom_inset(INSET)
    QApplication.processEvents()
    area.resize(500, 600)
    QTest.qWait(30)
    viewport = area.viewport()
    _, top, width, height = _image_rect_in_viewport(area, viewer)
    assert width <= viewport.width() - 16 + 1
    assert top + height <= viewport.height() - INSET - 8 + 1
    area.close()


def test_star_rating_reports_edits_and_toggles_the_current_star(theme):
    stars = StarRating()
    stars.resize(stars.sizeHint())
    stars.show()
    edits = []
    stars.rating_edited.connect(edits.append)
    step = stars.sizeHint().width() // 5

    stars.set_rating(4)
    assert stars.rating() == 4 and edits == []
    QTest.mouseClick(stars, Qt.MouseButton.LeftButton, pos=QPoint(step * 2 + 5, 10))
    assert stars.rating() == 3 and edits == [3]
    QTest.mouseClick(stars, Qt.MouseButton.LeftButton, pos=QPoint(step * 2 + 5, 10))
    assert stars.rating() == 0 and edits == [3, 0]

    stars.setFocus()
    QTest.keyClick(stars, Qt.Key.Key_Right)
    QTest.keyClick(stars, Qt.Key.Key_Right)
    QTest.keyClick(stars, Qt.Key.Key_Left)
    assert edits == [3, 0, 1, 2, 1]
    QTest.keyClick(stars, Qt.Key.Key_Left)
    QTest.keyClick(stars, Qt.Key.Key_Left)
    assert stars.rating() == 0 and edits[-1] == 0 and len(edits) == 6
    stars.set_rating(9)
    assert stars.rating() == 5
    stars.close()


def test_star_rating_paints_from_the_theme(theme):
    host = QWidget()
    stars = StarRating(host)
    stars.setGeometry(10, 10, stars.sizeHint().width(), stars.sizeHint().height())
    stars.set_rating(2)
    host.resize(stars.width() + 20, stars.height() + 20)
    host.show()
    pitch = (stars.width() + 4) // 5  # 每颗星连同间距的宽度
    for name in THEMES:
        theme.apply(name)
        QApplication.processEvents()
        image = host.grab().toImage()
        # 已评的星实心，未评的星只有描边，中心透出底色
        assert image.pixelColor(10 + pitch + 10, 20).name() == _token("rating")
        assert image.pixelColor(10 + pitch * 3 + 10, 20).name() == image.pixelColor(2, 2).name()
    host.close()


def test_segmented_control_can_show_no_selection(theme):
    control = AppleSegmentedControl()
    control.add_segments(["适应", "1:1"])
    changes = []
    control.currentIndexChanged.connect(changes.append)
    control.clear_selection()
    assert control.currentIndex() == -1 and control.currentText() == "" and changes == []
    control.setCurrentIndex(1)
    assert control.currentIndex() == 1 and changes == [1]
    control.deleteLater()


class _Toolbar:
    def __init__(self):
        self.host = QWidget()
        self.calls = []
        self.language = "en"
        self.actions = {}
        for name in ("zoom_out", "zoom_in", "zoom_fit", "zoom_actual", "rotate_cw", "rotate_ccw", "rotate_180",
                     "flip_horizontal", "flip_vertical"):
            action = QAction(name, self.host)
            action.triggered.connect(lambda _checked=False, name=name: self.calls.append(name))
            self.actions[name] = action
        a = self.actions
        self.toolbar = CanvasToolbar(CanvasToolbarActions(
            zoom_out=a["zoom_out"], zoom_in=a["zoom_in"], zoom_fit=a["zoom_fit"], zoom_actual=a["zoom_actual"],
            rotations=(a["rotate_cw"], a["rotate_ccw"], a["rotate_180"]),
            flips=(a["flip_horizontal"], a["flip_vertical"]),
        ), self.translate, self.host)
        self.toolbar.move(10, 10)
        self.toolbar.resize(self.toolbar.sizeHint())
        self.host.resize(self.toolbar.width() + 20, 80)
        self.host.show()
        QApplication.processEvents()

    def translate(self, key, default):
        return default if self.language == "en" else f"译:{key}"


def test_toolbar_shows_the_zoom_state_without_reporting_it(theme):
    built = _Toolbar()
    toolbar = built.toolbar
    edits = []
    toolbar.zoom_percent_edited.connect(edits.append)
    assert not toolbar.styleSheet() and toolbar.height() == CANVAS_TOOLBAR_HEIGHT

    toolbar.set_zoom_state(26, True)
    assert toolbar._readout.text() == "26%" and toolbar._fit_switch.currentIndex() == 0
    toolbar.set_zoom_state(100, False)
    assert toolbar._readout.text() == "100%" and toolbar._fit_switch.currentIndex() == 1
    toolbar.set_zoom_state(150, False)
    assert toolbar.zoom_percent() == 150 and toolbar._fit_switch.currentIndex() == -1
    assert edits == [] and built.calls == []

    switch = toolbar._fit_switch
    QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(6, switch.height() // 2))
    QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(switch.width() - 6, switch.height() // 2))
    assert built.calls == ["zoom_fit", "zoom_actual"]
    built.host.close()


def test_typing_a_zoom_level_commits_on_enter_only(theme):
    built = _Toolbar()
    toolbar, readout = built.toolbar, built.toolbar._readout
    edits = []
    toolbar.zoom_percent_edited.connect(edits.append)
    toolbar.set_zoom_state(26, True)

    readout.setFocus()
    readout.setText("150")
    toolbar.set_zoom_state(27, True)
    assert readout.text() == "150"  # 键入期间回填不覆盖正在输入的内容
    QTest.keyClick(readout, Qt.Key.Key_Return)
    assert edits == [150] and not readout.hasFocus() and readout.text() == "27%"

    for typed, expected in (("900%", 500), ("1", 5), (" 80 % ", 80)):
        readout.setFocus()
        readout.setText(typed)
        QTest.keyClick(readout, Qt.Key.Key_Return)
        assert edits[-1] == expected
    count = len(edits)
    readout.setFocus()
    readout.setText("abc")
    QTest.keyClick(readout, Qt.Key.Key_Return)
    readout.setFocus()
    readout.setText("300")
    QTest.keyClick(readout, Qt.Key.Key_Escape)
    assert len(edits) == count and readout.text() == "27%" and not readout.hasFocus()
    built.host.close()


def test_toolbar_buttons_trigger_actions_and_follow_the_workspace(theme):
    built = _Toolbar()
    toolbar, actions = built.toolbar, built.actions
    assert toolbar.mode() == VIEWER
    assert not toolbar._rotate_button.isHidden() and not toolbar._flip_button.isHidden()
    assert toolbar._compare_button.isHidden()
    QTest.mouseClick(toolbar._rotate_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(toolbar._flip_button, Qt.MouseButton.LeftButton)
    assert built.calls == ["rotate_cw", "flip_horizontal"]
    assert toolbar._rotate_button.menu_actions() == [actions["rotate_cw"], actions["rotate_ccw"], actions["rotate_180"]]
    assert toolbar._flip_button.menu_actions() == [actions["flip_horizontal"], actions["flip_vertical"]]

    actions["zoom_in"].setEnabled(False)
    # 缩小、读数、清晰度标记、放大
    zoom_buttons = [toolbar.layout().itemAt(index).widget() for index in (0, 3)]
    assert [button.isEnabled() for button in zoom_buttons] == [True, False]
    QTest.mouseClick(zoom_buttons[0], Qt.MouseButton.LeftButton)
    assert built.calls[-1] == "zoom_out"

    compare = []
    toolbar.compare_pressed.connect(lambda: compare.append("pressed"))
    toolbar.compare_released.connect(lambda: compare.append("released"))
    toolbar.set_mode(EDITOR)
    assert toolbar._rotate_button.isHidden() and toolbar._flip_button.isHidden()
    assert not toolbar._compare_button.isHidden()
    toolbar.set_compare_state(True)
    QTest.mousePress(toolbar._compare_button, Qt.MouseButton.LeftButton)
    assert compare == ["pressed"]
    QTest.mouseRelease(toolbar._compare_button, Qt.MouseButton.LeftButton)
    assert compare == ["pressed", "released"]
    toolbar.set_compare_state(False, "正在准备原图")
    assert not toolbar._compare_button.isEnabled() and toolbar._compare_button.toolTip() == "正在准备原图"
    toolbar.set_compare_state(True)
    assert toolbar._compare_button.toolTip() == "Press and hold to view original image"
    with pytest.raises(ValueError):
        toolbar.set_mode(3)
    built.host.close()


def test_rating_and_filmstrip_report_clicks_only(theme):
    built = _Toolbar()
    toolbar = built.toolbar
    ratings, filmstrip = [], []
    toolbar.rating_edited.connect(ratings.append)
    toolbar.filmstrip_toggled.connect(filmstrip.append)

    toolbar.set_rating(4)
    toolbar.set_filmstrip_visible(False)
    assert ratings == [] and filmstrip == [] and toolbar.rating() == 4
    assert toolbar._stars.toolTip() == "Rating: 4 stars" and not toolbar._filmstrip_button.isChecked()
    assert toolbar._filmstrip_button.toolTip() == "Expand filmstrip"

    QTest.mouseClick(toolbar._stars, Qt.MouseButton.LeftButton, pos=QPoint(5, 10))
    QTest.mouseClick(toolbar._filmstrip_button, Qt.MouseButton.LeftButton)
    assert ratings == [1] and filmstrip == [True]
    toolbar.set_rating(0)
    toolbar.set_filmstrip_visible(True)
    assert toolbar._stars.toolTip() == "Not rated" and toolbar._filmstrip_button.toolTip() == "Collapse filmstrip"
    assert ratings == [1] and filmstrip == [True]

    switch = toolbar._fit_switch
    built.language = "zh"
    toolbar.retranslate_ui()
    assert toolbar._fit_switch is switch and switch._segments[0]["text"] == "译:toolbar.zoom_fit"
    assert toolbar._readout.toolTip() == "译:canvas_toolbar.zoom_tip"
    built.host.close()


@pytest.mark.parametrize("name", THEMES)
def test_floating_widgets_keep_their_own_surface_on_the_canvas(theme, name):
    qss = get_theme_qss(name)
    for selector in ("QFrame#canvasToolbar {", "QLineEdit#canvasZoomReadout:focus {", "QLabel#toast {",
                     "QScrollArea#imageScrollArea > QWidget#qt_scrollarea_viewport,"):
        assert selector in qss, selector
    assert "floatingToolBar" not in qss and "zoomPillWidget" not in qss
    theme.apply(name)
    area, viewer = _canvas()
    built = _Toolbar()
    dock = BottomDock(built.toolbar, CANVAS_TOOLBAR_MARGIN)
    dock.attach(area)
    toast = Toast(area)
    toast_dock = BottomDock(toast, 2 * CANVAS_TOOLBAR_MARGIN + CANVAS_TOOLBAR_HEIGHT)
    toast_dock.attach(area)
    toast.show_message("已将 3 张图片评为 4 星")
    toast_dock.place()
    QApplication.processEvents()
    image = area.grab().toImage()
    toolbar = built.toolbar.geometry()
    # 画布的底色规则不能盖住浮在上面的工具条与轻提示
    assert image.pixelColor(toolbar.left() + 30, toolbar.top() + 3).name() == _token("surface")
    assert image.pixelColor(toolbar.center().x(), toolbar.top()).name() == _token("hairline_solid")
    assert image.pixelColor(toast.geometry().left() + 6, toast.geometry().center().y()).name() == _token("text_primary")
    assert image.pixelColor(4, 4).name() == _token("canvas")
    built.host.close()
    area.close()


def test_bottom_dock_follows_its_host(theme):
    first, second = QWidget(), QWidget()
    first.resize(900, 400)
    second.resize(300, 200)
    first.show()
    second.show()
    built = _Toolbar()
    toolbar = built.toolbar
    dock = BottomDock(toolbar, CANVAS_TOOLBAR_MARGIN)
    dock.attach(first)
    assert toolbar.parent() is first and not toolbar.isHidden() and dock.host() is first
    assert abs(toolbar.geometry().center().x() - 450) <= 1
    assert toolbar.geometry().bottom() == 400 - CANVAS_TOOLBAR_MARGIN - 1
    assert toolbar.height() == CANVAS_TOOLBAR_HEIGHT and toolbar.width() == toolbar.sizeHint().width()

    first.resize(1000, 500)
    QApplication.processEvents()
    assert abs(toolbar.geometry().center().x() - 500) <= 1
    assert toolbar.geometry().bottom() == 500 - CANVAS_TOOLBAR_MARGIN - 1

    toolbar.hide()
    dock.attach(second)
    assert toolbar.parent() is second and toolbar.isHidden()
    toolbar.show()
    assert toolbar.width() == 300 - 16 and toolbar.geometry().left() == 8  # 宿主太窄时两侧各留 8 像素
    first.resize(700, 500)
    QApplication.processEvents()
    assert toolbar.parent() is second and toolbar.geometry().left() == 8
    built.host.close()
    first.close()
    second.close()


def test_toast_replaces_and_dismisses_itself(theme):
    host = QWidget()
    host.resize(400, 200)
    host.show()
    toast = Toast(host)
    assert toast.isHidden() and toast.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    toast.show_message("第一条", 10_000)
    toast.show_message("<b>第二条</b>", 20)
    assert not toast.isHidden() and toast.text() == "<b>第二条</b>"
    assert toast.textFormat() == Qt.TextFormat.PlainText
    QTest.qWait(80)
    assert toast.isHidden()
    toast.show_message("第三条", 10_000)
    toast.show_message("")
    assert toast.isHidden()
    host.close()


def test_main_window_floats_the_toolbar_and_toast_over_the_canvas(theme):
    from src.ui.main_window import MainWindow

    window = MainWindow()
    try:
        assert not hasattr(window, "toolbar") and not hasattr(window, "zoom_spinbox")
        assert isinstance(window.canvas_toolbar, CanvasToolbar) and window.canvas_toolbar.isHidden()
        assert window.viewer._bottom_inset == INSET
        window.top_bar.workspace_requested.emit(EDITOR)
        # 没有图片时画布让给了打开图片的引导，轻提示浮在工作区上
        assert window.toast.parent() is window.workspace_stack and not window.toast.isHidden()
        assert window.toast.text() and window.findChild(QStatusBar) is None
    finally:
        window.shutdown_previews()
        window.deleteLater()
