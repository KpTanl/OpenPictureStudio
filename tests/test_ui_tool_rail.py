"""编辑工作区的外壳：工具栏入口清单、工具栏、侧栏容器里的工具切换，以及主窗口里的装配。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget  # noqa: E402

from resources.styles.theme_variables import INFO_SIDEBAR_WIDTH, TOOL_RAIL_WIDTH  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.icons import themed_icon  # noqa: E402
from src.ui.panels.registry import PANEL_TYPES, TOOL_ENTRIES, tool_entry_for  # noqa: E402
from src.ui.theme import ThemeManager, get_theme_tokens  # noqa: E402
from src.ui.widgets.tool_rail import ToolRail  # noqa: E402
from src.ui.widgets.tool_sidebar import ToolSidebar  # noqa: E402

THEMES = ("dark", "light", "beige")
ENTRIES = (("color", "adjustments-horizontal", "调色"), ("crop", "crop", "裁切"), ("cutout", "lasso", "抠图"))


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


def _rail(entries=ENTRIES):
    """放在一个可见宿主左侧的工具栏，点击请求记在 requests 里。"""
    host = QWidget()
    layout = QHBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    rail = ToolRail(host)
    rail.set_entries(entries)
    layout.addWidget(rail)
    layout.addWidget(QWidget(host), 1)
    requests = []
    rail.entry_requested.connect(requests.append)
    host.resize(400, 500)
    host.show()
    QApplication.processEvents()
    return host, rail, requests


def test_tool_entries_place_each_editor_tool_under_one_entry(theme):
    assert [entry.entry_id for entry in TOOL_ENTRIES] == ["color", "crop", "cutout", "repair", "beauty", "id_photo"]
    tool_ids = [tool_id for entry in TOOL_ENTRIES for tool_id in entry.tool_ids]
    assert len(tool_ids) == len(set(tool_ids)) and set(tool_ids) <= set(PANEL_TYPES)
    for entry in TOOL_ENTRIES:
        assert not themed_icon(entry.icon).isNull()
        assert all(tool_entry_for(tool_id) is entry for tool_id in entry.tool_ids)
    assert tool_entry_for("crop").tool_ids == ("crop", "grid_crop", "shape_crop")
    assert tool_entry_for("lasso").entry_id == "cutout"
    # 证件照是单独的一个入口；旧降噪只为旧项目保留，不在工具栏里
    assert tool_entry_for("id_photo").tool_ids == ("id_photo",) and tool_entry_for("denoise") is None


def test_rail_reports_clicks_and_leaves_the_selection_to_its_owner(theme):
    host, rail, requests = _rail()
    assert rail.entry_ids() == ("color", "crop", "cutout") and rail.width() == TOOL_RAIL_WIDTH
    assert [rail.button(name).text() for name in rail.entry_ids()] == ["调色", "裁切", "抠图"]

    rail.set_current("color")
    assert requests == [] and rail.button("color").isChecked() and not rail.button("crop").isChecked()

    # 点击只报告请求；所属方没有回填时，选中项仍是原来的入口
    QTest.mouseClick(rail.button("crop"), Qt.MouseButton.LeftButton)
    assert requests == ["crop"] and rail.current() == "color"
    assert rail.button("color").isChecked() and not rail.button("crop").isChecked()

    rail.set_current("crop")
    assert requests == ["crop"] and rail.button("crop").isChecked() and not rail.button("color").isChecked()

    # 键盘同样可以触发
    rail.button("cutout").setFocus()
    QTest.keyClick(rail.button("cutout"), Qt.Key.Key_Space)
    assert requests == ["crop", "cutout"]

    rail.set_entry_text("crop", "Crop")
    assert rail.button("crop").text() == "Crop"
    host.close()


def test_rail_hides_unavailable_entries_and_paints_a_dot_on_modified_ones(theme):
    host, rail, requests = _rail()
    rail.set_available(["color", "cutout"])
    QApplication.processEvents()
    assert rail.button("crop").isHidden() and not rail.button("color").isHidden()
    assert rail.button("cutout").y() < rail.button("color").y() + 2 * rail.button("color").height()

    button = rail.button("cutout")
    corner = QPoint(button.width() - 8, 8)
    plain = button.grab().toImage().pixelColor(corner)
    rail.set_modified(["cutout"])
    QApplication.processEvents()
    assert rail.modified() == frozenset({"cutout"}) and requests == []
    marked = button.grab().toImage().pixelColor(corner)
    assert marked.name() == QColor(get_theme_tokens()["accent"]).name() != plain.name()

    rail.set_modified([])
    QApplication.processEvents()
    assert rail.modified() == frozenset()
    assert button.grab().toImage().pixelColor(corner).name() == plain.name()
    host.close()


@pytest.mark.parametrize("name", THEMES)
def test_rail_takes_its_surface_hairline_and_selection_from_the_theme(theme, name):
    theme.apply(name)
    host, rail, _requests = _rail()
    rail.set_current("crop")
    QApplication.processEvents()
    assert not rail.styleSheet() and not rail.button("crop").styleSheet()
    tokens = get_theme_tokens()
    image = host.grab().toImage()
    assert image.pixelColor(4, host.height() - 4).name() == QColor(tokens["surface"]).name()
    assert image.pixelColor(TOOL_RAIL_WIDTH - 1, host.height() - 4).name() == QColor(tokens["hairline_solid"]).name()
    checked, plain = rail.button("crop"), rail.button("color")
    assert checked.grab().toImage().pixelColor(4, checked.height() // 2).name() != \
        plain.grab().toImage().pixelColor(4, plain.height() // 2).name()
    host.close()


class _FakePanel(QWidget):
    TOOL_TITLE_KEY = "menu.edit.crop"
    TOOL_TITLE_DEFAULT = "&Crop..."

    def __init__(self, content_width=0, content_height=40):
        super().__init__()
        self._content_size = QSize(content_width, content_height)

    def minimumSizeHint(self):
        return QSize(self._content_size)

    sizeHint = minimumSizeHint

    def take_footer(self):
        return None

    def retranslate_ui(self):
        pass


def test_tool_sidebar_switches_tools_on_user_choice_only(theme, language):
    sidebar = ToolSidebar()
    sidebar.resize(360, 500)
    sidebar.show()
    requests = []
    sidebar.tool_requested.connect(requests.append)

    variants = (("crop", "自由"), ("grid_crop", "网格"), ("shape_crop", "形状"))
    sidebar.set_variants(variants, "grid_crop")
    QApplication.processEvents()
    assert sidebar.variants() == ("crop", "grid_crop", "shape_crop") and sidebar.current_variant() == "grid_crop"
    assert not sidebar._variants_row.isHidden() and requests == []

    # 用户点第三段
    control = sidebar._variants
    QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=QPoint(control.width() - 12, control.height() // 2))
    assert requests == ["shape_crop"] and sidebar.current_variant() == "shape_crop"

    # 回填与换文字都不发信号
    sidebar.set_variants(variants, "crop")
    sidebar.set_variants((("crop", "Free"), ("grid_crop", "Grid"), ("shape_crop", "Shape")), "crop")
    assert requests == ["shape_crop"] and sidebar.current_variant() == "crop"
    assert control.currentText() == "Free"

    # 只有一个工具时不显示分段开关
    sidebar.set_variants((("color_adjust", "调色"),), "color_adjust")
    QApplication.processEvents()
    assert sidebar._variants_row.isHidden() and requests == ["shape_crop"]
    sidebar.close()


def test_tool_sidebar_shows_a_new_panel_from_its_top(theme, language):
    sidebar = ToolSidebar()
    tall, short = _FakePanel(120, 3000), _FakePanel(120, 200)
    sidebar.register_panel("color_adjust", tall)
    sidebar.register_panel("ai_enhance", short)
    sidebar.resize(360, 500)
    sidebar.show()
    sidebar.switch_to_panel("color_adjust")
    QApplication.processEvents()
    bar = sidebar._scroll_area.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum())

    # 较矮的面板不沿用上一个面板的滚动位置，也不被较高的面板撑出一段空白
    sidebar.switch_to_panel("ai_enhance")
    QApplication.processEvents()
    assert bar.value() == 0 and bar.maximum() == 0
    assert short.mapTo(sidebar._scroll_area.viewport(), QPoint(0, 0)).y() == 0

    # 回到原来的面板也从顶部看起
    sidebar.switch_to_panel("color_adjust")
    QApplication.processEvents()
    assert bar.value() == 0 and bar.maximum() > 0
    sidebar.close()


def test_tool_sidebar_keeps_background_panels_from_widening_the_current_one(theme, language):
    sidebar = ToolSidebar()
    wide, narrow = _FakePanel(700), _FakePanel(120)
    sidebar.register_panel("grid_crop", wide)
    sidebar.register_panel("crop", narrow)
    sidebar.resize(360, 500)
    sidebar.show()

    assert sidebar.switch_to_panel("grid_crop") is wide
    QApplication.processEvents()
    assert sidebar._stack.minimumSizeHint().width() >= 700
    assert sidebar.switch_to_panel("crop") is narrow and sidebar._title_label.text() == "裁切"
    QApplication.processEvents()
    assert sidebar._stack.minimumSizeHint().width() < 700
    assert narrow.width() <= sidebar._scroll_area.viewport().width()
    sidebar.close()


def test_main_window_lays_out_the_editor_with_a_rail_and_one_right_column(theme, language, tmp_path):
    from src.ui.main_window import MainWindow
    from src.ui.widgets.top_bar import EDITOR

    path = str(tmp_path / "photo.png")
    Image.new("RGB", (400, 300), (60, 120, 180)).save(path)
    window = MainWindow()
    try:
        window.resize(1600, 900)
        window.show()
        window.manager.open_file(path, load_siblings=True)
        window._refresh_all()
        window.top_bar.workspace_requested.emit(EDITOR)
        assert _wait_until(lambda: window.studio_workspace is not None
                           and window.studio_workspace.session is not None
                           and window.studio_workspace.tool_sidebar.get_current_panel() is not None, 60000)
        studio = window.studio_workspace
        QApplication.processEvents()

        # 左侧栏、项目按钮行、底部操作栏与过渡入口都已撤销
        for name in ("sidebar", "project_bar", "_btn_apply", "_btn_back", "_project_save", "_tool_resources"):
            assert not hasattr(studio, name), name
        assert not hasattr(window, "_toggle_left_sidebar")
        more = [action.text() for action in window.top_bar._more_button.menu().actions()]
        assert not any("侧边栏" in text for text in more)

        # 自左至右：工具栏、画布与胶片条、右侧栏
        rail, right = studio.tool_rail, studio.right_sidebar
        assert rail.mapTo(studio, QPoint(0, 0)).x() == 0 and rail.height() == studio.height()
        assert rail.entry_ids() == ("color", "crop", "cutout", "repair", "beauty", "id_photo")
        assert rail.current() == "color"
        assert right.mapTo(studio, QPoint(0, 0)).x() + right.width() == studio.width()
        assert studio.thumbnail_panel.width() == studio.scroll_area.width()
        assert studio.sidebar_tabs.count() == 3 and studio._notices.isHidden()
        assert studio.tool_sidebar.variants() == ("color_adjust",) and studio.tool_sidebar._variants_row.isHidden()

        # 点入口进入该入口的第一个工具；入口内用分段开关切换；再点入口回到上次用过的工具
        QTest.mouseClick(rail.button("crop"), Qt.MouseButton.LeftButton)
        assert studio.active_tool_id == "crop" and rail.current() == "crop"
        assert studio.tool_sidebar.variants() == ("crop", "grid_crop", "shape_crop")
        assert not studio.tool_sidebar._variants_row.isHidden()
        studio.tool_sidebar.tool_requested.emit("grid_crop")
        assert studio.active_tool_id == "grid_crop" and rail.current() == "crop"
        assert studio.tool_sidebar.current_variant() == "grid_crop"
        QTest.mouseClick(rail.button("color"), Qt.MouseButton.LeftButton)
        assert studio.active_tool_id == "color_adjust" and rail.current() == "color"
        QTest.mouseClick(rail.button("crop"), Qt.MouseButton.LeftButton)
        assert studio.active_tool_id == "grid_crop"
        QTest.mouseClick(rail.button("color"), Qt.MouseButton.LeftButton)

        # 改了参数的入口带圆点
        studio._on_panel_params_changed("color_adjust", {"exposure_amt": 20})
        assert rail.modified() == frozenset({"color"}) and "color_adjust" in studio.modified_tools()
        assert window.window_actions.action("undo").isEnabled()

        # 照片信息展开时占用右侧栏的位置，收起后参数栏回来
        info = studio.info_sidebar
        assert info.isHidden() and window.photo_info_panel is studio.photo_info_panel
        window.top_bar._info_toggle.click()
        QApplication.processEvents()
        assert not info.isHidden() and right.isHidden() and window.top_bar._info_toggle.isChecked()
        assert info.mapTo(studio, QPoint(0, 0)).x() + INFO_SIDEBAR_WIDTH == studio.width()
        window.top_bar._info_toggle.click()
        QApplication.processEvents()
        assert info.isHidden() and not right.isHidden() and not window.top_bar._info_toggle.isChecked()

        # 收起参数栏后看完照片信息，参数栏仍是收起的；点工具栏的入口重新展开
        studio.tool_sidebar._close_btn.click()
        assert right.isHidden()
        window.top_bar._info_toggle.click()
        window.top_bar._info_toggle.click()
        QApplication.processEvents()
        assert info.isHidden() and right.isHidden()
        window.top_bar._info_toggle.click()
        QTest.mouseClick(rail.button("color"), Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        assert info.isHidden() and not right.isHidden() and not window.top_bar._info_toggle.isChecked()

        # 图片加载失败时右侧栏顶部出现提示条，重新装载时收起
        studio.show_image_load_error(None)
        assert not studio._notices.isHidden() and not studio._notice_load.isHidden()
        assert studio._notice_load.text() == language.translate("studio.load_failed")
        studio.prepare_image_load(None)
        assert studio._notice_load.isHidden() and studio._notices.isHidden()
    finally:
        window.shutdown_previews()
        window.deleteLater()


class _FooterPanel(_FakePanel):
    """带底部操作区的面板：操作区默认在自己的布局末尾，宿主可以取走。"""

    def __init__(self, content_height=40):
        super().__init__(120, content_height)
        self.footer = QWidget(self)
        self.taken = 0

    def take_footer(self):
        self.taken += 1
        return self.footer


def test_tool_sidebar_pins_the_current_panels_footer_below_the_scroll_area(theme, language):
    sidebar = ToolSidebar()
    tall, plain = _FooterPanel(3000), _FakePanel(120, 200)
    plain.take_footer = lambda: None
    tall.footer.setFixedHeight(64)
    sidebar.register_panel("ai_enhance", tall)
    sidebar.register_panel("color_adjust", plain)
    sidebar.register_panel("ai_enhance", tall)
    sidebar.resize(360, 500)
    sidebar.show()
    assert tall.taken == 1 and tall.footer.parentWidget() is sidebar._footer_host

    sidebar.switch_to_panel("ai_enhance")
    QApplication.processEvents()
    host = sidebar._footer_host
    assert not host.isHidden() and tall.footer.isVisible()
    # 固定在滚动区下方、侧栏底部；内容滚动时它不动
    assert host.geometry().top() >= sidebar._scroll_area.geometry().bottom()
    assert host.geometry().bottom() == sidebar.height() - 1
    bar = sidebar._scroll_area.verticalScrollBar()
    assert bar.maximum() > 0
    before = tall.footer.mapTo(sidebar, QPoint(0, 0))
    bar.setValue(bar.maximum())
    QApplication.processEvents()
    assert tall.footer.mapTo(sidebar, QPoint(0, 0)) == before

    # 换到没有操作区的面板时整块收起，换回来再出现
    sidebar.switch_to_panel("color_adjust")
    QApplication.processEvents()
    assert host.isHidden() and not tall.footer.isVisible()
    sidebar.switch_to_panel("ai_enhance")
    QApplication.processEvents()
    assert not host.isHidden() and tall.footer.isVisible()

    # 面板移除时操作区一并撤掉
    sidebar.remove_panel("ai_enhance")
    QApplication.processEvents()
    assert host.isHidden() and sidebar._footer_layout.count() == 0
    sidebar.close()
