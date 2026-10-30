"""工具面板放进真实编辑工作区后的版式：不被滚动区压扁、不被裁掉；各面板的数值参数与契约一致。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.panels.registry import PANEL_TYPES, TOOL_ENTRIES  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from src.ui.widgets.collapsible_card import CollapsibleCard  # noqa: E402
from src.ui.widgets.parameter_row import ParameterRow  # noqa: E402
from src.ui.widgets.tool_sidebar import ToolSidebar  # noqa: E402
from tests.support import wait_for_studio, wait_for_thumbnails  # noqa: E402

TOOLS = tuple(tool_id for entry in TOOL_ENTRIES for tool_id in entry.tool_ids)


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
def photo(tmp_path):
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (640, 480), (120, 160, 200)).save(path)
    return path


class _Block(QWidget):
    """理想高度 60、最少可以压到 10 的一块内容。"""

    def sizeHint(self):
        return QSize(100, 60)

    def minimumSizeHint(self):
        return QSize(100, 10)


class _Panel(QWidget):
    """高度不随宽度变化、最小高度小于理想高度的面板。"""

    TOOL_TITLE_KEY = "menu.edit.beauty"
    TOOL_TITLE_DEFAULT = "Beauty"

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        for _ in range(6):
            layout.addWidget(_Block(self))
        layout.addStretch()

    def take_footer(self):
        return None

    def retranslate_ui(self):
        pass


def test_scroll_area_scrolls_instead_of_squeezing_a_panel(theme, language):
    """视口高度落在面板的最小高度与理想高度之间时，面板保持理想高度并出现滚动条。"""
    sidebar = ToolSidebar()
    panel = _Panel()
    sidebar.register_panel("beauty", panel)
    sidebar.switch_to_panel("beauty")
    sidebar.resize(308, 900)
    sidebar.show()
    QApplication.processEvents()
    minimum, ideal = panel.minimumSizeHint().height(), panel.sizeHint().height()
    assert not panel.layout().hasHeightForWidth() and minimum < ideal
    chrome = sidebar.height() - sidebar._scroll_area.viewport().height()

    sidebar.resize(308, chrome + (minimum + ideal) // 2)
    QApplication.processEvents()
    viewport = sidebar._scroll_area.viewport().height()
    assert minimum < viewport < ideal
    assert panel.height() == ideal and sidebar._scroll_area.verticalScrollBar().maximum() == ideal - viewport
    # 放得下时面板占满视口，没有滚动条
    sidebar.resize(308, chrome + ideal + 60)
    QApplication.processEvents()
    assert panel.height() == ideal + 60 and sidebar._scroll_area.verticalScrollBar().maximum() == 0
    sidebar.close()


def test_group_title_keeps_its_height_when_space_runs_out(theme, language):
    """分组标题一行不随所在布局被压矮：空间不够时让给内容去压。"""
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    card = CollapsibleCard("皮肤", host)
    body = QLabel("内容", card)
    body.setMinimumSize(QSize(10, 0))
    card.add_widget(body)
    layout.addWidget(card)
    host.resize(300, 200)
    host.show()
    QApplication.processEvents()
    button = card._header_btn
    natural = button.height()
    assert natural >= button.fontMetrics().height()
    host.resize(300, natural + 4)
    QApplication.processEvents()
    assert button.height() == natural and card._header.height() == natural
    host.close()


def _layout_problems(panel):
    """面板里被压到最小尺寸以下、或伸出父控件之外的可见控件。"""
    found = []
    need = (panel.heightForWidth(panel.width()) if panel.layout().hasHeightForWidth()
            else panel.sizeHint().height())
    if panel.height() < need:
        found.append(("panel", panel.height(), need))
    for child in panel.findChildren(QWidget):
        if not child.isVisible():
            continue
        if child.height() < child.minimumSizeHint().height():
            found.append((type(child).__name__, child.objectName(), child.height(), child.minimumSizeHint().height()))
        parent = child.parentWidget()
        if (child.x() < 0 or child.y() < 0 or child.x() + child.width() > parent.width() + 1
                or child.y() + child.height() > parent.height() + 1):
            found.append(("clipped", type(child).__name__, child.objectName(), child.geometry().getRect()))
    return found


@pytest.mark.parametrize("size", [(1024, 640), (1280, 720), (1440, 900)])
@pytest.mark.parametrize("code,ready", [("zh-CN", True), ("en", False), ("fr", False)])
def test_no_tool_panel_is_squeezed_or_clipped_in_the_editor(theme, qapp, window_factory, photo, size, code, ready):
    """逐个工具进入真实的编辑工作区：模型就绪与缺模型两种状态、三种窗口尺寸下都没有被压扁或裁掉的控件。"""
    translations = TranslationService(default_language=code, fallback_language="en")
    previous = get_translation_service()
    set_translation_service(translations)
    try:
        window = window_factory()
        window.resize(*size)
        window.show()
        window.manager.open_file(photo)
        window._refresh_all()
        wait_for_thumbnails(window.thumbnail_panel, [photo], photo, version=window.manager.list_version)
        problems = {}
        for tool_id in TOOLS:
            studio = wait_for_studio(window, photo, tool_id)
            panel = studio.tool_sidebar.get_current_panel()
            if ready and hasattr(panel, "_readiness"):
                states = {feature.feature_id: "ready" for feature in panel.RESOURCE_FEATURES}
                panel._readiness.states = lambda params, states=states: states
                panel._model_status_changed()
            for card in panel.findChildren(CollapsibleCard):
                card.set_expanded(True)
            for _ in range(10):
                QApplication.processEvents()
            found = _layout_problems(panel)
            if found:
                problems[tool_id] = found[:6]
        assert not problems
    finally:
        set_translation_service(previous)


@pytest.mark.parametrize("tool_id", sorted(PANEL_TYPES))
def test_parameter_rows_take_their_range_and_default_from_the_contract(theme, language, panel_factory, photo, tool_id):
    """各面板的数值参数行：范围取自所属参数契约；重置后回到契约的默认值。"""
    panel = panel_factory(tool_id, photo)
    contract = panel.PARAMETER_CONTRACT
    fields = {field.name: field for field in contract.fields}
    defaults = contract.defaults(contract.version)
    # 只看面板当前一页上的参数行：调色的蒙版页里也有同名的行，调的是蒙版自己的局部增量
    rows = [row for row in panel.findChildren(ParameterRow) if row.spec.key in fields and row.isVisibleTo(panel)]
    for row in rows:
        field = fields[row.spec.key]
        assert (row.slider.minimum(), row.slider.maximum()) == (field.minimum, field.maximum), row.spec.key
    if not rows:
        return
    # 回填一组非默认值再重置
    changed = {row.spec.key: (row.slider.maximum() if defaults[row.spec.key] != row.slider.maximum()
                              else row.slider.minimum()) for row in rows}
    panel.restore_params({**panel.parameters, **changed})
    assert all(row.value() == changed[row.spec.key] for row in rows)
    panel.reset()
    assert {row.spec.key: row.value() for row in rows} == {row.spec.key: defaults[row.spec.key] for row in rows}
