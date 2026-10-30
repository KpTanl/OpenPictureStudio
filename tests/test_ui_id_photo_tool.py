"""证件照在编辑工作区里：左侧单独的入口、按人脸摆好的框、“调整 / 结果”两种状态、换规格与排版、
缺模型时的说明、处理顺序固定在最后，以及框与底部操作区本身的行为。"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

from src.core.tools.id_photo import (LAYOUT_SHEET, MANAGED_ID_PHOTO_PARAMETERS, PORTRAIT_GUIDE, SHEET_SIZE,
                                     SIZE_PRESETS)  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.adapters.studio_adapter import to_workflow  # noqa: E402
from src.ui.overlays.id_photo_overlay import IDPhotoOverlay  # noqa: E402
from src.ui.panels.canvas_result import ResultViewControl  # noqa: E402
from src.ui.panels.id_photo_panel import IDPhotoPanel  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from tests.support import wait_for_studio, wait_for_thumbnails, wait_until  # noqa: E402

ONE_INCH, TWO_INCH = SIZE_PRESETS["一寸"], SIZE_PRESETS["二寸"]
PHOTO = (900, 700)
# 人像在画面里的位置：宇航员 512×512 贴在灰底的 (194, 94)
OFFSET = (194, 94)


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
    """真实主窗口里打开一张灰底上的人像（900×700），进入指定的工具。"""
    data = pytest.importorskip("skimage.data")
    path = str(tmp_path / "portrait.png")
    image = Image.new("RGB", PHOTO, (150, 150, 150))
    image.paste(Image.fromarray(data.astronaut()), OFFSET)
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
               "预览渲染结束", timeout=60)


def _aligned(panel):
    wait_until(lambda: panel._align_job is None and panel._params["crop_rect"] is not None, "按人脸摆好框",
               timeout=30)


def _shown_size(studio):
    image = studio.viewer._pixmap.toImage()
    return image.width(), image.height()


def _point(studio, fx, fy) -> QPoint:
    rect = studio.viewer.get_image_display_rect()
    return QPoint(rect.left() + round(rect.width() * fx), rect.top() + round(rect.height() * fy))


def _drag(studio, start, end, steps=10):
    viewer = studio.viewer
    QTest.mousePress(viewer, Qt.MouseButton.LeftButton, pos=start)
    for step in range(1, steps + 1):
        QTest.mouseMove(viewer, QPoint(start.x() + (end.x() - start.x()) * step // steps,
                                       start.y() + (end.y() - start.y()) * step // steps))
    QTest.mouseRelease(viewer, Qt.MouseButton.LeftButton, pos=end)


def _is_sheet(size):
    return size[0] <= SHEET_SIZE[0] and size[0] / size[1] == pytest.approx(SHEET_SIZE[0] / SHEET_SIZE[1], abs=0.01)


def _pixel_ratio(rect, size=PHOTO):
    return (rect[2] - rect[0]) * size[0] / ((rect[3] - rect[1]) * size[1])


def test_id_photo_has_its_own_entry_and_frames_the_face_without_entering_the_edit(editor):
    studio, panel = editor("id_photo")
    assert isinstance(panel, IDPhotoPanel) and studio.tool_rail.current() == "id_photo"
    footer = panel.result_view.footer
    assert footer.parent() is studio.tool_sidebar._footer_host and footer.isVisible()
    assert studio.viewer._overlay is panel.overlay and isinstance(panel.overlay, IDPhotoOverlay)
    # 调整时画布上是这一步的输入：没有别的步骤，就是整幅照片
    assert _shown_size(studio) == PHOTO
    _aligned(panel)
    assert panel._step_input is not None and (panel._step_input.width(), panel._step_input.height()) == PHOTO
    # 框按人脸摆好：锁在一寸的比例上，左右以人脸居中，头顶与下巴大致落在参考线上
    rect = panel.overlay.norm_rect
    assert _pixel_ratio(rect) == pytest.approx(ONE_INCH[0] / ONE_INCH[1], abs=0.01)
    center_x = (rect[0] + rect[2]) / 2 * PHOTO[0]
    assert abs(center_x - (OFFSET[0] + 225)) < 40
    assert not panel._align_note
    # 只是摆在画布上：还没有进入本次编辑
    assert "id_photo" not in studio.modified_tools() and not studio.is_dirty()
    assert panel.result_view.footer.primary_button.text() == "查看结果"
    assert panel.result_view.footer.summary.text().startswith("一寸 · 原背景 · 单张 295 × 413")


def test_view_result_records_the_step_and_shows_the_photo(editor):
    studio, panel = editor("id_photo")
    _aligned(panel)
    rect = panel.overlay.norm_rect
    QTest.mouseClick(panel.result_view.footer.primary_button, Qt.MouseButton.LeftButton)
    _settle(studio)
    assert studio._result_view and studio.viewer._overlay is None
    assert "id_photo" in studio.modified_tools()
    assert studio.session.tool_params["id_photo"]["crop_rect"] == pytest.approx(rect)
    assert _shown_size(studio) == ONE_INCH
    assert panel.result_view.footer.primary_button.text() == "继续调整"
    assert not panel._align_button.isEnabled()
    # 在结果里换规格，直接重新渲染，仍在结果里
    panel._spec_chips._chips["二寸"].click()
    _settle(studio)
    assert studio._result_view and _shown_size(studio) == TWO_INCH
    assert _pixel_ratio(panel._params["crop_rect"]) == pytest.approx(TWO_INCH[0] / TWO_INCH[1], abs=0.01)
    # 排版：结果是整张相纸
    panel._layout_segments.setCurrentIndex(1)
    panel._layout_segments.activated.emit(1)
    _settle(studio)
    # 画布上的预览最长边不超过 1600，整张相纸按比例缩小显示
    assert panel._params["layout"] == LAYOUT_SHEET and _is_sheet(_shown_size(studio))
    # Esc 回到调整：框挂回画布，画面是这一步的输入
    QTest.keyClick(studio.viewer, Qt.Key.Key_Escape)
    _settle(studio)
    assert not studio._result_view and studio.viewer._overlay is panel.overlay
    assert _shown_size(studio) == PHOTO and panel._align_button.isEnabled()
    # 回车再看结果
    QTest.keyClick(studio.viewer, Qt.Key.Key_Return)
    _settle(studio)
    assert studio._result_view and _is_sheet(_shown_size(studio))


def test_dragging_keeps_the_ratio_and_is_one_undo_step(editor):
    studio, panel = editor("id_photo")
    _aligned(panel)
    QTest.mouseClick(panel.result_view.footer.primary_button, Qt.MouseButton.LeftButton)
    _settle(studio)
    QTest.keyClick(studio.viewer, Qt.Key.Key_Escape)
    _settle(studio)
    before = list(panel._params["crop_rect"])
    left, top, right, bottom = before
    # 拖右下角往外：比例不变，只记一步撤销
    _drag(studio, _point(studio, right, bottom), _point(studio, min(0.98, right + 0.08), min(0.98, bottom + 0.2)))
    _settle(studio)
    after = panel._params["crop_rect"]
    assert after != pytest.approx(before) and after[:2] == pytest.approx(before[:2])
    assert _pixel_ratio(after) == pytest.approx(ONE_INCH[0] / ONE_INCH[1], abs=0.01)
    studio.undo()
    _settle(studio)
    assert studio.session.tool_params["id_photo"]["crop_rect"] == pytest.approx(before)
    # 在框里拖：整体移动，大小不变
    _drag(studio, _point(studio, (left + right) / 2, (top + bottom) / 2),
          _point(studio, (left + right) / 2 - 0.05, (top + bottom) / 2))
    moved = panel._params["crop_rect"]
    assert moved[2] - moved[0] == pytest.approx(right - left, abs=1e-6) and moved[0] < left


def test_background_without_a_model_says_why_and_offers_the_original(editor):
    studio, panel = editor("id_photo")
    _aligned(panel)
    footer = panel.result_view.footer
    panel._background_chips._chips["蓝色"].click()
    wait_until(lambda: not panel.parameters_pending, "参数准备结束")
    # 测试环境里没有抠图模型：按钮不可点，写明是背景这一项缺模型；模型选择出现在背景一组里
    assert panel._params["background"] == [67, 142, 219]
    assert not footer.primary_button.isEnabled() and "背景" in footer.status.text()
    assert panel.model_selector.isVisibleTo(panel) and panel._swatch_box.isVisibleTo(panel)
    panel._background_chips._chips["keep"].click()
    assert panel._params["background"] is None and footer.primary_button.isEnabled()
    assert not panel.model_selector.isVisibleTo(panel) and not panel._swatch_box.isVisibleTo(panel)


def test_id_photo_stays_the_last_step(editor):
    studio, panel = editor("id_photo")
    _aligned(panel)
    QTest.mouseClick(panel.result_view.footer.primary_button, Qt.MouseButton.LeftButton)
    _settle(studio)
    # 之后再用形状裁切：它排在证件照前面，证件照仍是最后一步
    studio.switch_tool("shape_crop")
    _settle(studio)
    shape = studio.tool_sidebar.get_current_panel()
    shape.result_view.toggle()
    _settle(studio)
    order = [node.params.get("tool_id") for node in to_workflow(studio.session).nodes]
    assert order[-1] == "id_photo" and order.index("shape_crop") < order.index("id_photo")


def test_overlay_keeps_the_ratio_in_pixels_and_stays_inside(qapp):
    overlay = IDPhotoOverlay()
    host = QWidget()
    overlay.set_viewer(host)
    image_rect = QRect(0, 0, 900, 700)
    ratio = ONE_INCH[0] / ONE_INCH[1]
    span = ratio / (900 / 700)
    overlay.set_frame([0.3, 0.2, 0.3 + 0.5 * span, 0.7], ratio)
    changes = []
    overlay.geometry_changed.connect(lambda: changes.append(overlay.norm_rect))

    def drag(start, end):
        overlay.handle_mouse_press(_event(start, Qt.MouseButton.LeftButton), image_rect)
        overlay.handle_mouse_move(_event(end, Qt.MouseButton.NoButton), image_rect)
        overlay.handle_mouse_release(_event(end, Qt.MouseButton.LeftButton), image_rect)

    left, top, right, bottom = overlay.norm_rect
    drag(QPoint(round(right * 900), round(bottom * 700)), QPoint(round(right * 900) + 60, round(bottom * 700) + 10))
    new = overlay.norm_rect
    assert new[:2] == pytest.approx([left, top]) and _pixel_ratio(new) == pytest.approx(ratio, abs=1e-6)
    # 往外拖过了边：收在画面以内
    drag(QPoint(round(new[2] * 900), round(new[3] * 700)), QPoint(2000, 2000))
    clamped = overlay.norm_rect
    assert max(clamped) <= 1 + 1e-9 and _pixel_ratio(clamped) == pytest.approx(ratio, abs=1e-6)
    # 拖左上角：右下角不动
    drag(QPoint(round(clamped[0] * 900), round(clamped[1] * 700)),
         QPoint(round(clamped[0] * 900) + 40, round(clamped[1] * 700) + 40))
    shrunk = overlay.norm_rect
    assert shrunk[2:] == pytest.approx(clamped[2:]) and _pixel_ratio(shrunk) == pytest.approx(ratio, abs=1e-6)
    # 整体移动收在画面以内
    drag(QPoint(round((shrunk[0] + shrunk[2]) / 2 * 900), round((shrunk[1] + shrunk[3]) / 2 * 700)),
         QPoint(-500, -500))
    moved = overlay.norm_rect
    assert moved[0] == pytest.approx(0) and moved[1] == pytest.approx(0)
    assert len(changes) >= 4


def _event(point, button):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    kind = (QMouseEvent.Type.MouseButtonPress if button != Qt.MouseButton.NoButton else QMouseEvent.Type.MouseMove)
    return QMouseEvent(kind, QPointF(point), QPointF(point), button, button, Qt.KeyboardModifier.NoModifier)


def test_result_footer_shows_busy_only_after_a_moment_and_cancel_returns(qapp, language):
    host = QWidget()
    control = ResultViewControl(host)
    requests = []
    control.requested.connect(requests.append)
    control.describe("一寸")
    control.set_busy(True)
    assert not control.busy  # 不在看结果时不算忙
    control.set_viewing(True)
    control.set_busy(True)
    assert control.busy and control.footer.primary_button.text() == "继续调整"
    QTest.qWait(400)
    assert control.footer.summary.text() == "正在生成结果…" and control.footer.primary_button.text() == "取消"
    control.toggle()
    assert requests == [False]
    control.set_busy(False)
    assert control.footer.summary.text() == "正在查看结果"
    # 很快就出来时不闪
    control.set_busy(True)
    control.set_busy(False)
    QTest.qWait(400)
    assert control.footer.summary.text() == "正在查看结果"
    control.set_viewing(False)
    assert not control.busy and control.footer.primary_button.text() == "查看结果"


def test_panel_texts_follow_the_language(editor, language):
    studio, panel = editor("id_photo")
    _aligned(panel)
    language.set_language("en")
    assert panel._spec_card.title() == "Size" and panel._align_button.text() == "Auto Align Portrait"
    assert panel.result_view.footer.summary.text().startswith("1 inch · Original · single 295 × 413")
    assert panel._layout_segments.currentText() == "Single photo"
    language.set_language("zh-CN")
    assert panel._spec_card.title() == "规格"


def test_guides_follow_the_portrait_ratios():
    assert PORTRAIT_GUIDE["head_top_ratio"] < PORTRAIT_GUIDE["chin_ratio"] < 1
    params = MANAGED_ID_PHOTO_PARAMETERS.defaults(2)
    assert params["crop_rect"] is None and params["layout"] == "single" and np.isclose(
        ONE_INCH[0] / ONE_INCH[1], 295 / 413)
