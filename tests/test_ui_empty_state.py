"""空状态组件，以及看图工作区没有图片时的引导。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtCore import QMimeData, QPoint, Qt, QUrl  # noqa: E402
from PyQt6.QtGui import QDragEnterEvent, QDragLeaveEvent, QPalette  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens, to_qcolor  # noqa: E402
from src.ui.widgets.apple_button import ApplePrimaryButton  # noqa: E402
from src.ui.widgets.empty_state import EmptyState  # noqa: E402

THEMES = ("dark", "light", "beige")


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


def _wait_until(predicate, timeout_ms=5000):
    for _ in range(timeout_ms // 5):
        QApplication.processEvents()
        if predicate():
            return True
        QTest.qWait(5)
    return False


def _shown(widget, width=600, height=360):
    widget.resize(width, height)
    widget.show()
    QApplication.processEvents()
    return widget


def test_shows_a_sentence_and_hides_what_it_was_not_given(theme):
    empty = _shown(EmptyState("cpu"))
    empty.set_text("暂无可用运行环境")
    QApplication.processEvents()
    assert empty.title() == "暂无可用运行环境" and empty.description() == ""
    assert empty._description.isHidden() and empty._actions.isHidden()
    # 居中
    card = empty._card.geometry()
    assert abs(card.center().x() - empty.rect().center().x()) <= 1
    assert abs(card.center().y() - empty.rect().center().y()) <= 1

    empty.set_text("还没有预览", "选好规格后点“生成预览”")
    assert not empty._description.isHidden() and empty.description() == "选好规格后点“生成预览”"
    empty.set_icon("photo")
    assert empty._icon._name == "photo"
    empty.close()


def test_actions_report_their_keys_and_only_the_first_is_filled(theme):
    empty = _shown(EmptyState("photo", actions=("open_file", "open_folder"), drop_target=True))
    empty.set_text("把图片或文件夹拖到这里")
    empty.set_action_text("open_file", "打开图片")
    empty.set_action_text("open_folder", "打开文件夹")
    seen = []
    empty.action_triggered.connect(seen.append)
    first, second = empty.action_button("open_file"), empty.action_button("open_folder")
    assert (first.text(), second.text()) == ("打开图片", "打开文件夹")
    assert isinstance(first, ApplePrimaryButton) and not isinstance(second, ApplePrimaryButton)
    assert first.geometry().right() < second.geometry().left()
    QTest.mouseClick(second, Qt.MouseButton.LeftButton)
    QTest.mouseClick(first, Qt.MouseButton.LeftButton)
    assert seen == ["open_folder", "open_file"]
    empty.close()

    with pytest.raises(ValueError):
        EmptyState("photo", actions=("a", "b", "c"))
    with pytest.raises(ValueError):
        EmptyState("photo", actions=("a", "a"))


def test_long_text_wraps_inside_a_narrow_host(theme):
    empty = _shown(EmptyState("photo", actions=("open_file", "open_folder"), drop_target=True), 320, 420)
    empty.set_text("Drop images or a folder here", "JPEG, PNG, WebP, TIFF and common RAW formats")
    empty.set_action_text("open_file", "Open Images")
    empty.set_action_text("open_folder", "Open Folder")
    QApplication.processEvents()
    card = empty._card.geometry()
    assert card.left() >= 0 and card.right() <= empty.width() and card.bottom() <= empty.height()
    for child in (empty._title, empty._description, empty.action_button("open_file"),
                  empty.action_button("open_folder")):
        top_left = child.mapTo(empty._card, QPoint(0, 0))
        assert top_left.x() >= 0 and top_left.x() + child.width() <= card.width()
    assert empty._description.height() > empty._description.fontMetrics().lineSpacing() * 1.5
    empty.close()


@pytest.mark.parametrize("name", THEMES)
def test_drop_target_takes_its_colors_from_the_theme(theme, name):
    theme.apply(name)
    tokens = get_theme_tokens()
    empty = _shown(EmptyState("photo", drop_target=True, on_canvas=True))
    empty.set_text("把图片或文件夹拖到这里")
    QApplication.processEvents()
    image = empty.grab().toImage()
    card = empty._card.geometry()
    assert image.pixelColor(4, 4) == to_qcolor(tokens["canvas"])
    inside = QPoint(card.left() + 12, card.top() + 12)
    assert image.pixelColor(inside) == to_qcolor(tokens["canvas"])

    assert not empty.is_drag_active()
    empty.set_drag_active(True)
    QApplication.processEvents()
    assert empty.is_drag_active()
    assert empty.grab().toImage().pixelColor(inside) == to_qcolor(tokens["canvas"])
    empty.set_drag_active(False)
    QApplication.processEvents()
    assert empty.grab().toImage().pixelColor(inside) == to_qcolor(tokens["canvas"])
    empty.close()


def test_viewer_without_an_image_shows_the_guide_instead_of_canvas_and_filmstrip(theme, language, tmp_path):
    from src.ui.main_window import MainWindow

    win = MainWindow()
    win.resize(1280, 800)
    win.show()
    QApplication.processEvents()
    try:
        empty = win.viewer_empty
        assert not empty.isHidden() and win.viewer_splitter.isHidden()
        assert not win.thumbnail_panel.isVisible() and not win.scroll_area.isVisible()
        assert win.canvas_toolbar.isHidden()
        assert empty.size() == win.viewer_workspace.size()
        assert empty.title() == "把图片或文件夹拖到这里" and "RAW" in empty.description()
        assert empty.action_button("open_file").text() == "打开图片"
        assert empty.action_button("open_folder").text() == "打开文件夹"

        # 两个按钮就是动作表里的“打开图片”“打开文件夹”
        triggered = []
        for name in ("open_file", "open_folder"):
            action = win.window_actions.action(name)
            action.triggered.disconnect()
            action.triggered.connect(lambda _checked=False, name=name: triggered.append(name))
        QTest.mouseClick(empty.action_button("open_folder"), Qt.MouseButton.LeftButton)
        QTest.mouseClick(empty.action_button("open_file"), Qt.MouseButton.LeftButton)
        assert triggered == ["open_folder", "open_file"]

        # 一句话的消息仍然看得到
        win._on_workspace_index_changed(1)
        assert win._current_workspace_index == 0
        assert win.toast.isVisible() and win.toast.text() == win._tr("status.no_image_for_studio")

        language.set_language("en")
        QApplication.processEvents()
        assert empty.title() == "Drop images or a folder here"
        assert empty.action_button("open_file").text() == "Open Images"
        language.set_language("zh-CN")

        # 拖入与离开时更新拖放状态
        path = str(tmp_path / "photo.png")
        Image.new("RGB", (400, 300), (60, 120, 180)).save(path)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path)])
        enter = QDragEnterEvent(QPoint(100, 100), Qt.DropAction.CopyAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        win.dragEnterEvent(enter)
        assert enter.isAccepted() and empty.is_drag_active()
        win.dragLeaveEvent(QDragLeaveEvent())
        assert not empty.is_drag_active()

        # 打开图片后换回画布、胶片条与底部工具条
        assert win.open_image_path(path, load_siblings=False)
        assert _wait_until(lambda: win.viewer.has_image)
        QApplication.processEvents()
        assert empty.isHidden() and win.viewer_splitter.isVisible()
        assert win.scroll_area.isVisible() and win.thumbnail_panel.isVisible()
        assert win.canvas_toolbar.isVisible() and win.canvas_toolbar.parentWidget() is win.scroll_area
        assert win.scroll_area.width() == win.viewer_workspace.width()
        assert win.viewer.is_fit_mode()
        shown = win.viewer.get_image_display_rect()
        viewport = win.scroll_area.viewport()
        assert 0 < shown.width() <= viewport.width() and 0 < shown.height() <= viewport.height()
    finally:
        win.shutdown_previews()
        win.deleteLater()
