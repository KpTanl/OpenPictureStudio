"""照片信息：版式、缺失值的呈现、读取失败的提示条、右侧信息栏的显示与收起，以及主窗口的接入。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget  # noqa: E402

from resources.styles.theme_variables import INFO_SIDEBAR_WIDTH, get_theme_qss  # noqa: E402
from src.application.image_info import ImageInfo, ImageInfoRequest, ImageInfoResult, PhotoMetadata  # noqa: E402
from src.core.contracts.result import CoreError, ErrorCode  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.ui.widgets.photo_info_panel import PhotoInfoSidebar  # noqa: E402

THEMES = ("dark", "light", "beige")
PHOTO = PhotoMetadata(
    shutter="1/250 s", aperture="f/5.6", iso="200", focal_length="35 mm", focal_length_35mm="52 mm",
    exposure_bias="-0.3 EV", captured_at="2026-09-21 18:42:07", exposure_program=3, metering_mode=5,
    white_balance=0, flash=False, camera_make="NIKON CORPORATION", camera_model="NIKON Z 50",
    lens_model="NIKKOR Z DX 16-50mm f/3.5-6.3 VR")


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


@pytest.fixture
def sidebar(theme, language):
    """放在一个可见宿主右侧的信息栏；面板已启用，读取请求记在 queries 里。"""
    host = QWidget()
    layout = QHBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    layout.addWidget(QWidget(host), 1)
    bar = PhotoInfoSidebar(language.translate, host)
    layout.addWidget(bar)
    host.resize(900, 700)
    host.show()
    queries = []
    bar.panel.info_requested.connect(queries.append)
    bar.panel.set_active(True)
    yield bar, queries, host
    bar.panel.cleanup()
    host.close()


def _info(path, photo=PHOTO, **changes):
    values = dict(width=5568, height=3712, kind="RGB", camera=photo.camera, path=path, file_format="JPEG",
                  size_bytes=25_800_000, color_info="sRGB", photo=photo, details=True)
    values.update(changes)
    return ImageInfo(**values)


def _show(panel, path, info=None, error=None):
    """让面板展开并显示 path 的一次读取结果。"""
    panel.set_path(path)
    panel.set_expanded(True)
    request = ImageInfoRequest(1, path, None, True)
    panel.set_request(request)
    panel.set_result(ImageInfoResult(request, info if error is None else None, error))
    QApplication.processEvents()


def _wait_until(predicate, timeout_ms=5000):
    for _ in range(timeout_ms // 5):
        QApplication.processEvents()
        if predicate():
            return True
        QTest.qWait(5)
    return False


def test_panel_shows_headline_quad_and_three_groups(sidebar, tmp_path):
    bar, queries, _host = sidebar
    panel = bar.panel
    path = str(tmp_path / "DSC_6674.jpg")
    assert not panel.styleSheet() and bar.isHidden()

    _show(panel, path, _info(path))
    assert queries[-1].path == path and queries[-1].details
    assert panel._name.full_text() == "DSC_6674.jpg" and panel._name.toolTip() == path
    assert panel._time.text() == "2026-09-21 18:42:07" and not panel._time.isHidden()
    assert panel._status.isHidden() and panel._notice.isHidden()
    assert [panel._values[name].text() for name in ("shutter", "aperture", "iso", "focal_length")] == [
        "1/250 s", "f/5.6", "200", "35 mm"]
    assert [panel._labels[name].text() for name in ("shutter", "aperture", "iso", "focal_length")] == [
        "快门", "光圈", "ISO", "焦距"]
    assert [label.text() for label in panel._headings.values()] == ["拍摄参数", "设备", "源文件"]
    expected = {"focal_length_35mm": "52 mm", "exposure_bias": "-0.3 EV", "exposure_program": "光圈优先",
                "metering_mode": "多区域测光", "white_balance": "自动", "flash": "未触发",
                "camera_make": "NIKON CORPORATION", "camera_model": "NIKON Z 50", "lens_make": "未记录",
                "lens_model": "NIKKOR Z DX 16-50mm f/3.5-6.3 VR", "dimensions": "5568 × 3712",
                "size_bytes": "24.60 MB", "file_format": "JPEG", "color_info": "sRGB", "path": path}
    assert {name: panel._values[name].text() for name in expected} == expected
    assert set(panel._values) == set(expected) | {"shutter", "aperture", "iso", "focal_length"}
    assert panel._values["size_bytes"].toolTip() == "25,800,000 字节"
    assert panel._values["iso"].accessibleName() == "ISO: 200"

    # 读取中：只留文件名与路径，其余为占位
    panel.set_path(path)
    assert not panel._status.isHidden() and panel._time.isHidden()
    assert panel._values["shutter"].text() == "—" and panel._values["path"].text() == path


def test_missing_values_say_whether_they_were_absent_or_unreadable(sidebar, tmp_path):
    bar, _queries, _host = sidebar
    panel = bar.panel
    path = str(tmp_path / "scan.png")
    photo = PhotoMetadata(iso="400", unreadable=frozenset(("shutter", "exposure_bias")))
    _show(panel, path, _info(path, photo))

    # 四格放不下说明文字，缺失时显示占位，原因写在提示与无障碍名称里
    assert panel._values["shutter"].text() == "—" and "无法读取" in panel._values["shutter"].toolTip()
    assert panel._values["aperture"].text() == "—" and "未记录" in panel._values["aperture"].toolTip()
    assert panel._values["aperture"].accessibleName() == "光圈: 未记录"
    assert panel._values["iso"].text() == "400"
    assert panel._values["exposure_bias"].text() == "无法读取"
    assert panel._values["metering_mode"].text() == "未记录"
    assert panel._time.text() == "拍摄时间 — 未记录" and not panel._time.isHidden()

    _show(panel, path, _info(path, PhotoMetadata(metering_mode=9), kind="RAW", color_info="RAW"))
    assert panel._values["metering_mode"].text() == "未知（9）"
    assert panel._values["color_info"].text() == "RAW 原始数据"


def test_failures_use_a_notice_bar_and_retry_asks_again(sidebar, tmp_path):
    bar, queries, _host = sidebar
    panel = bar.panel
    path = str(tmp_path / "broken.jpg")

    _show(panel, path, error=CoreError(ErrorCode.DECODE_FAILED, "photo_info.read_failed", retryable=True))
    assert not panel._notice.isHidden() and panel._notice.text().startswith("照片信息读取失败")
    assert not panel._notice.action_button.isHidden() and panel._notice.action_button.text() == "重试"
    assert panel._values["dimensions"].text() == "—"
    before = len(queries)
    panel._notice.action_button.click()
    assert len(queries) == before + 1 and queries[-1].force and queries[-1].path == path

    # 只有部分信息读不到时给出说明，不提供重试
    _show(panel, path, _info(path, issue_key="photo_info.partial"))
    assert not panel._notice.isHidden() and panel._notice.action_button.isHidden()
    _show(panel, path, _info(path, issue_key="photo_info.metadata_failed"))
    assert not panel._notice.action_button.isHidden()
    _show(panel, path, _info(path))
    assert panel._notice.isHidden()


def test_sidebar_follows_the_panel_and_collapses_without_a_photo(sidebar, tmp_path):
    bar, queries, host = sidebar
    panel = bar.panel
    path = str(tmp_path / "a.jpg")
    states = []
    panel.expanded_changed.connect(states.append)
    assert bar.width() == INFO_SIDEBAR_WIDTH and bar.isHidden()

    panel.set_expanded(True)
    assert bar.isHidden() and not panel.expanded and states == []

    panel.set_path(path)
    assert bar.isHidden() and queries[-1].path == path and not queries[-1].details
    panel.set_expanded(True)
    QApplication.processEvents()
    assert not bar.isHidden() and bar.x() == host.width() - INFO_SIDEBAR_WIDTH and states == [True]
    assert queries[-1].details

    panel.set_expanded(False)
    assert bar.isHidden() and states == [True, False]
    panel.set_expanded(True)
    panel.set_path(None)
    assert bar.isHidden() and not panel.expanded and states == [True, False, True, False]


def test_long_path_wraps_inside_the_sidebar_and_stays_selectable(sidebar, tmp_path):
    bar, _queries, _host = sidebar
    panel = bar.panel
    path = "D:\\" + "\\".join(f"VeryLongFolderNameWithoutSpaces{index:02d}" for index in range(6)) + "\\DSC_6674.NEF"
    _show(panel, path, _info(path))
    value = panel._values["path"]
    line = value.fontMetrics().lineSpacing()

    assert value.fontMetrics().horizontalAdvance(path) > 2 * value.width()
    assert value.height() >= 3 * line and value.height() == round(value.document().size().height())
    assert value.document().idealWidth() <= value.viewport().width()
    assert value.x() >= 0 and value.x() + value.width() <= panel.width() <= bar.width()
    value.selectAll()
    assert value.textCursor().selectedText() == path and value.isReadOnly()

    # 滚轮不由这一项消费（交给整栏滚动），按住 Ctrl 也不缩放文字
    size = value.font().pointSizeF()
    for modifier in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier):
        wheel = QWheelEvent(QPointF(5, 5), QPointF(value.mapToGlobal(QPoint(5, 5))), QPoint(), QPoint(0, 120),
                            Qt.MouseButton.NoButton, modifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(value.viewport(), wheel)
        assert not wheel.isAccepted() and value.font().pointSizeF() == size


@pytest.mark.parametrize("name", THEMES)
def test_sidebar_takes_its_surface_and_left_hairline_from_the_theme(sidebar, theme, name, tmp_path):
    bar, _queries, host = sidebar
    qss = get_theme_qss(name)
    for selector in ("QFrame#infoSidebar {", "QLabel#infoSidebarTitle {", "QLabel#photoInfoFieldName {",
                     "QFrame#photoInfoRule {"):
        assert selector in qss, selector
    assert "sidebarInfoCard" not in qss and "sidebarPrimaryBtn" not in qss
    path = str(tmp_path / "a.jpg")
    _show(bar.panel, path, _info(path))
    theme.apply(name)
    QApplication.processEvents()
    tokens = get_theme_tokens()
    image = host.grab().toImage()
    assert image.pixelColor(bar.x(), 4).name() == QColor(tokens["hairline_solid"]).name()
    assert image.pixelColor(bar.x() + 4, 4).name() == QColor(tokens["surface"]).name()
    assert image.pixelColor(host.width() - 2, host.height() - 2).name() == QColor(tokens["surface"]).name()
    rule = next(child for child in bar.panel.children()
                if child.objectName() == "photoInfoRule" and child.height() == 1)
    point = rule.mapTo(host, rule.rect().center())
    assert image.pixelColor(point).name() == QColor(tokens["hairline_solid"]).name()


def test_language_switch_rewrites_the_text_in_place(sidebar, language, tmp_path):
    bar, _queries, _host = sidebar
    panel = bar.panel
    path = str(tmp_path / "a.jpg")
    _show(panel, path, _info(path))
    title = bar.findChild(QLabel, "infoSidebarTitle")
    value = panel._values["metering_mode"]
    assert title.text() == "照片信息"

    language.set_language("en")
    bar.retranslate_ui()
    assert bar.findChild(QLabel, "infoSidebarTitle") is title and title.text() == "Photo Info"
    assert panel._values["metering_mode"] is value and value.text() == "Multi Zone"
    assert panel._labels["path"].text() == "Location" and panel._headings["file"].text() == "Source File"
    assert panel._values["lens_make"].text() == "Not recorded"


def test_main_window_puts_photo_info_on_the_right_and_has_no_left_sidebar(theme, language, tmp_path):
    from src.ui.main_window import MainWindow

    path = str(tmp_path / "photo.png")
    Image.new("RGB", (400, 300), (60, 120, 180)).save(path)
    window = MainWindow()
    try:
        for name in ("viewer_sidebar", "sidebar", "h_splitter"):
            assert not hasattr(window, name), name
        bar = window.info_sidebar
        assert isinstance(bar, PhotoInfoSidebar) and bar.isHidden()
        assert window.photo_info_panel is bar.panel and not window.top_bar._info_toggle.isEnabled()
        window.resize(1280, 720)
        window.show()

        window.manager.open_file(path, load_siblings=True)
        window._refresh_all()
        # 信息栏收起时仍读取尺寸，供顶栏标题使用
        assert _wait_until(lambda: window.top_bar.title() == ("photo.png", "400 × 300"))
        assert bar.isHidden() and window.top_bar._info_toggle.isEnabled()
        assert not window.top_bar._info_toggle.isChecked()
        canvas_width = window.scroll_area.width()

        window.top_bar._info_toggle.click()
        assert _wait_until(lambda: bar.panel._values["dimensions"].text() == "400 × 300")
        assert not bar.isHidden() and window.top_bar._info_toggle.isChecked()
        assert bar.panel._name.full_text() == "photo.png" and bar.panel._values["path"].text() == path
        origin = bar.mapTo(window, bar.rect().topLeft())
        assert origin.x() + bar.width() == window.width()
        # 画布让出信息栏与分隔条的宽度
        assert window.scroll_area.width() == canvas_width - INFO_SIDEBAR_WIDTH - window.viewer_splitter.handleWidth()
        # 信息栏占满顶栏以下的右侧，胶片条只在画布下方
        assert window.thumbnail_panel.width() == window.scroll_area.width()
        assert bar.height() >= window.scroll_area.height() + window.thumbnail_panel.height()

        window.top_bar._info_toggle.click()
        QApplication.processEvents()
        assert bar.isHidden() and not window.top_bar._info_toggle.isChecked()
        assert window.scroll_area.width() == canvas_width
    finally:
        window.shutdown_previews()
        window.deleteLater()
