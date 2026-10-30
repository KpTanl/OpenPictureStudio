"""设置里的文件关联页：格式列表、开关、筛选与搜索，以及随设置窗口的生命周期。"""

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QFrame, QLineEdit, QPushButton, QScrollArea

from src.ui.dialogs import file_association_page
from src.ui.dialogs.file_association_page import (
    FileAssociationPage,
    FormatItemWidget,
)
from src.ui.dialogs.settings_dialog import SettingsDialog
from src.ui.widgets.setting_row import SettingRow, SettingSection
from src.ui.widgets.toggle_switch import ToggleSwitch
from src.ui.theme import ThemeManager, set_theme_manager, get_theme_tokens
from src.core.constants import RAW_EXTENSIONS, SUPPORTED_EXTENSIONS


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def registry(monkeypatch):
    """用内存里的一份关联状态代替系统的：开关与批量操作不写真实的注册表。"""
    state = {ext: ext == ".png" for ext in SUPPORTED_EXTENSIONS}
    fa = file_association_page.fa
    monkeypatch.setattr(fa, "is_windows", lambda: True)
    monkeypatch.setattr(fa, "get_registration_status", lambda: dict(state))
    monkeypatch.setattr(fa, "register_extension", lambda ext: state.__setitem__(ext, True) or True)
    monkeypatch.setattr(fa, "unregister_extension", lambda ext: state.__setitem__(ext, False) or True)
    monkeypatch.setattr(fa, "register_file_associations",
                        lambda: {ext: state.__setitem__(ext, True) or True for ext in state})
    monkeypatch.setattr(fa, "unregister_file_associations",
                        lambda: {ext: state.__setitem__(ext, False) or True for ext in state})
    return state


def test_toggle_switch_widget(qapp):
    """测试 ToggleSwitch 滑块开关控件的基础属性、静音设置及状态切换。"""
    switch = ToggleSwitch()
    assert switch.isCheckable()
    assert not switch.isChecked()
    assert switch.handle_position == 0.0

    # 测试静音开启
    switch.set_checked_silent(True)
    assert switch.isChecked()
    assert switch.handle_position == 1.0

    # 测试静音关闭
    switch.set_checked_silent(False)
    assert not switch.isChecked()
    assert switch.handle_position == 0.0

    # 测试 toggle 切换
    switch.toggle()
    assert switch.isChecked()


def test_file_association_page_structure_and_themes(qapp, registry):
    """页面的结构：顶部是系统默认应用的引导，下面一种格式一行；整页随设置窗口滚动，自己不带滚动区。"""
    mgr = ThemeManager()
    set_theme_manager(mgr)

    mgr.apply("light")
    page = FileAssociationPage()
    page.show()

    assert not page.findChildren(QScrollArea) and not page.styleSheet()
    assert len(page._item_widgets) == len(SUPPORTED_EXTENSIONS)
    assert isinstance(page.default_section, SettingSection) and isinstance(page.formats_section, SettingSection)

    first_ext = SUPPORTED_EXTENSIONS[0]
    first_card = page._item_widgets[first_ext]
    assert isinstance(first_card, FormatItemWidget)
    assert isinstance(first_card.switch, ToggleSwitch)
    assert first_card.title() and first_ext in first_card.description()

    assert isinstance(page.search_input, QLineEdit)
    from src.ui.widgets.segmented_control import AppleSegmentedControl
    assert isinstance(page.filter_segment, AppleSegmentedControl)
    assert page.filter_segment.count() == 3
    assert page.btn_enable_all.text() and page.btn_disable_all.text()

    # 引导到系统默认应用设置的一行排在格式列表上面
    assert isinstance(page.tip_card, SettingRow) and isinstance(page.tip_card, QFrame)
    assert isinstance(page.btn_system, QPushButton) and page.tip_card.title()
    assert page.default_section.y() < page.formats_section.y()

    mgr.apply("dark")
    tokens_dark = get_theme_tokens()
    assert "accent" in tokens_dark

    mgr.apply("beige")
    mgr.apply("light")

    page.close()
    mgr.apply("dark")


def test_switches_show_the_current_state_and_apply_at_once(qapp, registry, monkeypatch):
    """开关显示系统里现在的关联状态；拨一下就生效，全部开启与全部关闭之后各行跟着变。"""
    page = FileAssociationPage()
    page.show()
    assert page._item_widgets[".png"].switch.isChecked() and not page._item_widgets[".jpg"].switch.isChecked()

    page._item_widgets[".jpg"].switch.toggle()
    assert registry[".jpg"]
    page._item_widgets[".png"].switch.toggle()
    assert not registry[".png"]

    shown = []
    monkeypatch.setattr(file_association_page, "show_info", lambda *args, **kwargs: shown.append("info"))
    monkeypatch.setattr(file_association_page, "show_error", lambda *args, **kwargs: shown.append("error"))
    page.btn_enable_all.click()
    assert all(registry.values()) and all(row.switch.isChecked() for row in page._item_widgets.values())
    assert shown == ["info"]

    # 全部关闭先确认：不同意就什么也不做
    monkeypatch.setattr(file_association_page, "show_confirm", lambda *args, **kwargs: False)
    page.btn_disable_all.click()
    assert all(registry.values())
    monkeypatch.setattr(file_association_page, "show_confirm", lambda *args, **kwargs: True)
    page.btn_disable_all.click()
    assert not any(registry.values()) and not any(row.switch.isChecked() for row in page._item_widgets.values())
    page.close()


def test_file_association_filter_and_search(qapp, registry):
    """测试分类筛选（全部/常用/RAW）与即时搜索过滤功能。"""
    dlg = FileAssociationPage()
    dlg.show()

    # 1. 筛选 RAW 分类
    dlg.filter_segment.setCurrentIndex(2)
    for ext, card in dlg._item_widgets.items():
        if ext in RAW_EXTENSIONS:
            assert not card.isHidden()
        else:
            assert card.isHidden()

    # 2. 筛选 常用 分类
    dlg.filter_segment.setCurrentIndex(1)
    for ext, card in dlg._item_widgets.items():
        if ext not in RAW_EXTENSIONS:
            assert not card.isHidden()
        else:
            assert card.isHidden()

    # 3. 恢复 全部 分类
    dlg.filter_segment.setCurrentIndex(0)
    for card in dlg._item_widgets.values():
        assert not card.isHidden()

    # 4. 搜索框过滤
    dlg.search_input.setText(".png")
    assert not dlg._item_widgets[".png"].isHidden()
    assert dlg._item_widgets[".jpg"].isHidden()
    assert dlg.empty_label.isHidden()
    dlg.search_input.setText("no-such-format")
    assert not dlg.empty_label.isHidden()

    # 清空搜索框
    dlg.search_input.clear()
    assert not dlg._item_widgets[".jpg"].isHidden()
    assert not dlg._item_widgets[".png"].isHidden()

    dlg.close()


def test_format_names_follow_the_interface_language(qapp, registry, monkeypatch):
    """格式名称随界面语言显示；此前读错了属性，英文界面里也是中文。"""
    from src.i18n import TranslationService

    service = TranslationService(default_language="en", fallback_language="en")
    service.set_language("en")
    monkeypatch.setattr("src.i18n.translation_service._translation_service", service)
    dlg = FileAssociationPage()
    try:
        row = dlg._item_widgets[".jpg"]
        assert row.title() == "JPEG Standard Image" and ".jpg" in row.description()
        assert dlg.formats_section.title() == "File Association Management"
        service.set_language("zh-CN")
        assert row.title() == "JPEG 标准图像" and dlg.formats_section.title() == "文件格式关联管理"
    finally:
        dlg.close()


def test_page_stops_following_the_language_once_destroyed(qapp, registry, monkeypatch):
    """页面销毁后不再留在翻译服务的订阅里，之后切换语言不会去刷新已经不在的控件。"""
    from src.i18n import TranslationService

    service = TranslationService(default_language="en", fallback_language="en")
    monkeypatch.setattr("src.i18n.translation_service._translation_service", service)
    before = len(service._observers)
    page = FileAssociationPage()
    assert len(service._observers) == before + 1
    page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert len(service._observers) == before


def test_page_explains_itself_where_association_is_not_supported(qapp, monkeypatch):
    monkeypatch.setattr(file_association_page.fa, "is_windows", lambda: False)
    page = FileAssociationPage()
    assert page.notice.text() and not page._item_widgets
    page.close()


def test_settings_builds_the_page_when_it_is_first_entered(qapp, registry, monkeypatch):
    """打开设置不读系统里的关联状态，进到“文件关联”才建这一页；建好后没有默认按钮。"""
    reads = []
    monkeypatch.setattr(file_association_page.fa, "get_registration_status",
                        lambda: reads.append(True) or dict(registry))
    dialog = SettingsDialog()
    dialog.show()
    assert dialog.file_association_page is None and reads == []
    dialog.show_page("file_association")
    page = dialog.file_association_page
    assert isinstance(page, FileAssociationPage) and reads == [True]
    assert dialog._pages["file_association"].title.text() and page.isVisibleTo(dialog)
    assert not any(button.autoDefault() or button.isDefault() for button in dialog.findChildren(QPushButton))
    dialog.show_page("general")
    dialog.show_page("file_association")
    assert dialog.file_association_page is page and reads == [True]
    dialog.close()

    dialog = SettingsDialog(page="file_association")
    assert dialog.current_page() == "file_association" and dialog.file_association_page is not None
    dialog.close()
