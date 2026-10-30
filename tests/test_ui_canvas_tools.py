"""画布工具（自由裁切、形状裁切、套索）在编辑工作区里的“调整 / 结果”两种状态、键盘操作与撤销步数。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageDraw  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPalette, QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.image_viewer import ImageViewer  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens, to_qcolor  # noqa: E402
from tests.support import wait_for_studio, wait_for_thumbnails, wait_until  # noqa: E402


@pytest.fixture
def theme(qapp, monkeypatch):
    previous = qapp.styleSheet(), qapp.property("ops.theme"), QPalette(qapp.palette())
    manager = ThemeManager()
    monkeypatch.setattr("src.ui.theme._theme_manager_instance", manager)
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
def editor(theme, language, tmp_path, window_factory):
    """在真实主窗口里打开一张 800×600 的照片（灰底，右侧有一个红色方块），进入指定的工具。"""
    path = str(tmp_path / "photo.png")
    image = Image.new("RGB", (800, 600), (128, 128, 128))
    ImageDraw.Draw(image).rectangle((500, 200, 599, 299), fill=(220, 30, 30))
    image.save(path)
    window = window_factory()
    window.resize(1440, 900)
    window.show()
    window.manager.open_file(path)
    window._refresh_all()
    wait_for_thumbnails(window.thumbnail_panel, [path], path, version=window.manager.list_version)

    def enter(tool_id):
        studio = wait_for_studio(window, path, tool_id)
        _settle(studio)
        return studio, studio.tool_sidebar.get_current_panel()

    return enter


def _settle(studio):
    wait_until(lambda: studio._preview_queue.active is None and studio._preview_queue.pending is None,
               "预览渲染结束", timeout=30)


def _shown(studio) -> QImage:
    return studio.viewer._pixmap.toImage()


def _size(studio):
    image = _shown(studio)
    return image.width(), image.height()


def _point(studio, fx, fy) -> QPoint:
    """画面上按比例的一点，换成画布控件里的坐标。"""
    rect = studio.viewer.get_image_display_rect()
    return QPoint(rect.left() + round(rect.width() * fx), rect.top() + round(rect.height() * fy))


def _drag(studio, start, end, steps=10):
    viewer = studio.viewer
    QTest.mousePress(viewer, Qt.MouseButton.LeftButton, pos=start)
    for step in range(1, steps + 1):
        QTest.mouseMove(viewer, QPoint(start.x() + (end.x() - start.x()) * step // steps,
                                       start.y() + (end.y() - start.y()) * step // steps))
    QTest.mouseRelease(viewer, Qt.MouseButton.LeftButton, pos=end)


def _undo_steps(studio):
    count = 0
    while studio.session.can_undo() and count < 100:
        studio.undo()
        count += 1
    return count


def _key(studio, key):
    QTest.keyClick(studio.viewer, key)


def test_crop_adjusts_then_views_the_result_in_place(editor):
    studio, panel = editor("crop")
    control, footer = panel.result_view, panel.result_view.footer
    button = footer.primary_button
    # 底部操作区固定在侧栏的滚动区之外；框还是整幅时没有结果可看
    assert footer.parent() is studio.tool_sidebar._footer_host and footer.isVisible()
    assert button.text() == "查看结果" and not button.isEnabled()
    assert footer.status.text() == "拖动边框或选一个比例后可以查看结果"
    _key(studio, Qt.Key.Key_Return)
    assert not studio._result_view and studio.viewer._overlay is panel.overlay

    # 拖一次右下角：途中鼠标动了十下，只记一步撤销
    _drag(studio, _point(studio, 1.0, 1.0) - QPoint(1, 1), _point(studio, 0.5, 0.5))
    _settle(studio)
    assert "crop" in studio.modified_tools() and button.isEnabled() and footer.status.isHidden()
    box = panel.overlay.get_crop_box(800, 600)
    cropped = (box[2] - box[0], box[3] - box[1])
    assert abs(cropped[0] - 400) <= 2 and abs(cropped[1] - 300) <= 2
    summary = f"裁切为 {cropped[0]} × {cropped[1]}"
    assert footer.summary.text() == summary
    # 调整时画布上仍是裁切之前的整幅
    assert _size(studio) == (800, 600) and not control.viewing
    studio.undo()
    _settle(studio)
    assert not studio.session.can_undo() and "crop" not in studio.modified_tools() and not button.isEnabled()
    studio.redo()
    _settle(studio)
    assert footer.summary.text() == summary

    # 查看结果：画布上是裁切之后的画面，框收起，人还在这个工具里
    button.click()
    wait_until(lambda: _size(studio) == cropped, "显示裁切之后的画面", timeout=30)
    _settle(studio)
    assert studio._result_view and control.viewing and studio.viewer._overlay is None
    assert studio.active_tool_id == "crop" and button.text() == "继续调整" and footer.summary.text() == "正在查看结果"
    result = _shown(studio)

    # 在结果里改面板上的参数，直接重新渲染
    from PySide6.QtWidgets import QPushButton
    square = next(chip for chip in panel.findChildren(QPushButton) if chip.text() == "1:1")
    QTest.mouseClick(square, Qt.MouseButton.LeftButton)
    wait_until(lambda: _size(studio)[0] == _size(studio)[1] != cropped[0], "换比例后重新渲染", timeout=30)
    assert studio._result_view and button.text() == "继续调整"
    studio.undo()
    wait_until(lambda: _size(studio) == cropped, "撤销后仍在看结果", timeout=30)
    assert studio._result_view

    # Esc 回到调整：整幅加框
    _key(studio, Qt.Key.Key_Escape)
    wait_until(lambda: _size(studio) == (800, 600), "回到调整", timeout=30)
    assert not studio._result_view and not control.viewing and studio.viewer._overlay is panel.overlay
    assert button.text() == "查看结果" and studio.tool_sidebar.isVisible()

    # 回车等同于点按钮；看到的与离开工具后看到的是同一幅画面
    _key(studio, Qt.Key.Key_Return)
    wait_until(lambda: _size(studio) == cropped, "回车查看结果", timeout=30)
    _settle(studio)
    assert _shown(studio) == result
    studio.switch_tool("color_adjust")
    _settle(studio)
    assert _shown(studio) == result and not studio._result_view

    # 再进来总是从调整开始；在结果里重置也回到调整
    studio.switch_tool("crop")
    wait_until(lambda: _size(studio) == (800, 600), "再进入时从调整开始", timeout=30)
    assert not control.viewing and studio.viewer._overlay is panel.overlay and button.text() == "查看结果"
    button.click()
    wait_until(lambda: _size(studio) == cropped, "再看一次结果", timeout=30)
    studio.reset_current_tool()
    wait_until(lambda: _size(studio) == (800, 600), "重置后回到调整", timeout=30)
    assert not studio._result_view and not control.viewing and not button.isEnabled()
    assert studio.viewer._overlay is panel.overlay and "crop" not in studio.modified_tools()


def test_lasso_keys_close_and_view_and_feather_is_visible(editor):
    studio, panel = editor("lasso")
    control, footer = panel.result_view, panel.result_view.footer
    button = footer.primary_button
    assert not button.isEnabled() and footer.status.text() == "先在照片上圈出一个区域"

    def click(fx, fy):
        QTest.mouseClick(studio.viewer, Qt.MouseButton.LeftButton, pos=_point(studio, fx, fy))

    # 退格删掉上一个点；Esc 放弃正在画的轮廓，侧栏不会因此收起
    for spot in ((0.2, 0.2), (0.4, 0.2), (0.4, 0.5)):
        click(*spot)
    assert len(panel.overlay.draft_points) == 3 and footer.summary.text() == "轮廓还没有闭合，查看结果时会先闭合"
    _key(studio, Qt.Key.Key_Backspace)
    assert len(panel.overlay.draft_points) == 2 and not button.isEnabled()
    _key(studio, Qt.Key.Key_Escape)
    assert panel.overlay.draft_points == [] and studio.tool_sidebar.isVisible()
    assert "lasso" not in studio.modified_tools()

    # 圈住红色方块，回车：先闭合，再显示抠完之后的画面
    for spot in ((0.6, 0.3), (0.78, 0.3), (0.78, 0.53), (0.6, 0.53)):
        click(*spot)
    _settle(studio)
    assert _shown(studio).pixelColor(10, 10).alpha() == 255
    _key(studio, Qt.Key.Key_Return)
    wait_until(lambda: _shown(studio).pixelColor(10, 10).alpha() == 0, "显示抠完之后的画面", timeout=30)
    _settle(studio)
    assert len(panel.overlay.regions) == 1 and panel.overlay.draft_points == []
    assert studio._result_view and control.viewing and studio.viewer._overlay is None
    assert button.text() == "继续调整"
    shown = _shown(studio)
    assert shown.pixelColor(550, 250).alpha() == 255 and shown.pixelColor(550, 250).red() > 200
    # 选区的边是硬的
    assert shown.pixelColor(470, 250).alpha() == 0 and shown.pixelColor(490, 250).alpha() == 255

    # 羽化不再是盲调：在结果里拖它，边缘当场变软
    panel._on_feather_changed(20)
    wait_until(lambda: 0 < _shown(studio).pixelColor(482, 250).alpha() < 255, "羽化后边缘是半透明的", timeout=30)
    assert studio._result_view
    panel._keep_segments.activated.emit(1)
    wait_until(lambda: _shown(studio).pixelColor(550, 250).alpha() == 0, "换成去掉选区内", timeout=30)
    assert _shown(studio).pixelColor(10, 10).alpha() == 255

    # 正在数值框里键入时，回车是提交数值，不切换状态
    edit = panel._feather_row.value_edit
    edit.setFocus()
    QTest.keyClick(edit, Qt.Key.Key_Return)
    assert studio._result_view and button.text() == "继续调整"
    studio.viewer.setFocus()

    # 按住对比显示原图，松开回到结果
    studio.start_compare()
    assert _shown(studio).pixelColor(10, 10).alpha() == 255
    studio.stop_compare()
    assert _shown(studio).pixelColor(550, 250).alpha() == 0 and studio._result_view

    # 结果里画布不接收圈选；回到调整后轮廓还在
    click(0.1, 0.9)
    assert panel.overlay.draft_points == []
    button.click()
    wait_until(lambda: _shown(studio).pixelColor(550, 250).alpha() == 255, "回到调整", timeout=30)
    assert not studio._result_view and studio.viewer._overlay is panel.overlay and len(panel.overlay.regions) == 1


def test_shape_crop_view_result_takes_the_shape_into_the_edit(editor):
    studio, panel = editor("shape_crop")
    control, footer = panel.result_view, panel.result_view.footer
    button = footer.primary_button
    # 进入形状裁切不算使用
    assert "shape_crop" not in studio.modified_tools() and not studio.is_dirty()
    assert button.text() == "查看结果" and button.isEnabled() and footer.summary.text() == "形状：圆形"
    assert _size(studio) == (800, 600)

    # 没改过参数就点：把当前形状记入，并显示裁切之后的画面
    button.click()
    wait_until(lambda: _size(studio) == (560, 420), "显示形状裁切之后的画面", timeout=30)
    _settle(studio)
    assert "shape_crop" in studio.modified_tools() and studio.is_dirty() and control.viewing
    shown = _shown(studio)
    assert shown.pixelColor(2, 2).alpha() == 0 and shown.pixelColor(280, 210).alpha() == 255
    result = shown

    # 继续调整：回到这一步之前的画面与形状框；拖一次框只记一步撤销
    button.click()
    wait_until(lambda: _size(studio) == (800, 600), "回到调整", timeout=30)
    assert studio.viewer._overlay is panel.overlay and _shown(studio).pixelColor(2, 2).alpha() == 255
    before = panel.parameters["rect"]
    _drag(studio, _point(studio, 0.5, 0.5), _point(studio, 0.45, 0.45))
    _settle(studio)
    assert panel.parameters["rect"] != before
    studio.undo()
    _settle(studio)
    assert panel.parameters["rect"] == before and "shape_crop" in studio.modified_tools()

    # 离开后看到的与刚才查看的结果相同
    studio.switch_tool("color_adjust")
    wait_until(lambda: _size(studio) == (560, 420), "离开后的画面", timeout=30)
    _settle(studio)
    assert _shown(studio) == result

    # 重置后回到还没使用
    studio.switch_tool("shape_crop")
    _settle(studio)
    studio.reset_current_tool()
    _settle(studio)
    assert "shape_crop" not in studio.modified_tools() and button.text() == "查看结果"


def test_keys_go_elsewhere_when_no_canvas_tool_is_active(editor):
    studio, panel = editor("color_adjust")
    assert not hasattr(panel, "result_view")
    _key(studio, Qt.Key.Key_Return)
    _key(studio, Qt.Key.Key_Backspace)
    assert not studio._result_view
    # 历史与 AI 助手两页里画布工具的按键也不生效
    studio.switch_tool("lasso")
    _settle(studio)
    for page in (1, 2):
        studio.sidebar_tabs.setCurrentIndex(page)
        assert studio._canvas_panel() is None and studio.viewer._overlay is None
        studio.sidebar_tabs.setCurrentIndex(0)
        _settle(studio)
        assert studio._canvas_panel() is not None


def test_viewer_shows_a_checkerboard_only_through_transparent_pixels(theme, qapp):
    tokens = get_theme_tokens()
    cells = {to_qcolor(tokens["surface"]).rgb(), to_qcolor(tokens["track"]).rgb()}
    image = QImage(200, 100, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)
    for y in range(100):
        for x in range(100, 200):
            image.setPixelColor(x, y, Qt.GlobalColor.red)
    viewer = ImageViewer()
    viewer.resize(400, 300)
    viewer.show()
    viewer.set_pixmap(QPixmap.fromImage(image))
    qapp.processEvents()
    rect = viewer.get_image_display_rect()
    shot = viewer.grab().toImage()
    step = max(1, rect.width() // 40)
    left = {shot.pixelColor(rect.left() + x, rect.center().y()).rgb() for x in range(2, rect.width() // 2 - 2, step)}
    right = {shot.pixelColor(rect.left() + x, rect.center().y()).rgb()
             for x in range(rect.width() // 2 + 2, rect.width() - 2, step)}
    # 透明的一半露出两种格子，不透明的一半只有照片本身
    assert left == cells and len(right) == 1 and not right & cells

    # 没有透明通道的照片不垫棋盘格
    opaque = QImage(200, 100, QImage.Format.Format_RGB888)
    opaque.fill(Qt.GlobalColor.blue)
    viewer.set_pixmap(QPixmap.fromImage(opaque))
    qapp.processEvents()
    shot = viewer.grab().toImage()
    rect = viewer.get_image_display_rect()
    assert {shot.pixelColor(rect.left() + x, rect.center().y()).rgb() for x in range(2, rect.width() - 2, step)} \
        == {opaque.pixelColor(0, 0).rgb()}
    viewer.close()
