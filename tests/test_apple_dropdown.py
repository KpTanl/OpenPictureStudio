"""Apple HIG 风格 AppleMenu 与 AppleDropDown 专项单元测试。"""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QWidget, QToolButton

from src.ui.widgets.apple_menu import AppleMenu, AppleDropDown
from src.ui.dialogs.settings_dialog import SettingsDialog
from src.ui.theme import ThemeManager, set_theme_manager


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_apple_menu_properties(qapp):
    """验证 AppleMenu 具备无边框透明属性及主题应用。"""
    menu = AppleMenu("TestMenu")
    try:
        assert menu.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert bool(menu.windowFlags() & Qt.WindowType.FramelessWindowHint)
        assert not menu.styleSheet()
    finally:
        menu.close()


def test_apple_dropdown_items_and_selection(qapp):
    """验证 AppleDropDown 的选项操作、数据存取与单选状态。"""
    dropdown = AppleDropDown()
    try:
        assert dropdown.count() == 0
        assert dropdown.currentIndex() == -1
        assert dropdown.currentText() == ""
        assert dropdown.currentData() is None

        dropdown.addItem("Option A", "data_a")
        dropdown.addItem("Option B", "data_b")
        dropdown.addItem("Option C", "data_c")

        assert dropdown.count() == 3
        assert dropdown.currentIndex() == 0
        assert dropdown.currentText() == "Option A"
        assert dropdown.currentData() == "data_a"

        # findData & findText
        assert dropdown.findData("data_b") == 1
        assert dropdown.findData("non_existent") == -1
        assert dropdown.findText("Option C") == 2
        assert dropdown.findText("non_existent") == -1

        # itemText & itemData
        assert dropdown.itemText(1) == "Option B"
        assert dropdown.itemData(1) == "data_b"

        # setItemText & setItemData
        dropdown.setItemText(1, "Option B Renamed")
        dropdown.setItemData(1, "data_b_new")
        assert dropdown.itemText(1) == "Option B Renamed"
        assert dropdown.itemData(1) == "data_b_new"

        # setCurrentIndex
        dropdown.setCurrentIndex(1)
        assert dropdown.currentIndex() == 1
        assert dropdown.currentText() == "Option B Renamed"
        assert dropdown.currentData() == "data_b_new"

        # setCurrentText
        dropdown.setCurrentText("Option C")
        assert dropdown.currentIndex() == 2
        assert dropdown.currentData() == "data_c"

        # clear
        dropdown.clear()
        assert dropdown.count() == 0
        assert dropdown.currentIndex() == -1
        assert dropdown.currentText() == ""
    finally:
        dropdown.close()


def test_apple_dropdown_signals(qapp):
    """验证选项变更与激活信号发送机制。"""
    dropdown = AppleDropDown()
    try:
        index_changes = []
        text_changes = []
        activations = []

        dropdown.addItem("One", 1)
        dropdown.addItem("Two", 2)

        dropdown.currentIndexChanged.connect(lambda i: index_changes.append(i))
        dropdown.currentTextChanged.connect(lambda t: text_changes.append(t))
        dropdown.activated.connect(lambda i: activations.append(i))

        dropdown.setCurrentIndex(1)
        assert index_changes == [1]
        assert text_changes == ["Two"]

        # 模拟 action 触发
        dropdown._on_action_triggered(0)
        assert index_changes == [1, 0]
        assert text_changes == ["Two", "One"]
        assert activations == [0]
    finally:
        dropdown.close()


def test_apple_dropdown_popup_and_geometry(qapp):
    """验证 showPopup 与 hidePopup 的非阻塞行为与菜单对齐宽度。"""
    dropdown = AppleDropDown()
    dropdown.setFixedWidth(260)
    dropdown.addItems(["Apple", "Banana", "Cherry"])
    dropdown.show()
    qapp.processEvents()

    try:
        dropdown.showPopup()
        qapp.processEvents()

        assert dropdown._menu.isVisible()
        assert dropdown._menu.width() >= 260

        dropdown.hidePopup()
        qapp.processEvents()
        assert not dropdown._menu.isVisible()
    finally:
        dropdown.close()


def test_apple_dropdown_custom_icon_preservation(qapp):
    """验证自定义图标保留与默认对勾生成的对齐机制。"""
    dropdown = AppleDropDown()
    try:
        pm = QPixmap(16, 16)
        pm.fill(Qt.GlobalColor.red)
        custom_icon = QIcon(pm)

        dropdown.addItem("Standard Item", 1)
        dropdown.addItem("Icon Item", 2, icon=custom_icon)

        dropdown._refresh_action_icons()
        # Item 0 选中，使用 checkmark 图标
        act0 = dropdown._items[0]["action"]
        assert not act0.icon().isNull()

        # Item 1 未选中但带自定义图标，应维持自定义图标
        act1 = dropdown._items[1]["action"]
        assert not act1.icon().isNull()
    finally:
        dropdown.close()


def test_settings_dialog_integration_with_apple_dropdown(qapp):
    """验证 SettingsDialog 已成功集成 AppleDropDown，且生命周期正常。"""
    mgr = ThemeManager()
    set_theme_manager(mgr)

    dlg = SettingsDialog()
    try:
        assert isinstance(dlg.language_combo, AppleDropDown)
        assert isinstance(dlg.theme_combo, AppleDropDown)

        # 验证语言选项与主题选项数量
        assert dlg.language_combo.count() == len(SettingsDialog.LANGUAGE_OPTIONS) == 3
        assert dlg.theme_combo.count() == 4

        # 验证主题切换与刷新
        for theme in ["beige", "dark", "light"]:
            mgr.apply(theme)
            dlg.apply_theme(theme)
            qapp.processEvents()
    finally:
        dlg.close()


def test_apple_dropdown_flat_mode(qapp):
    """验证 AppleDropDown 扁平模式 (flat=True) 下按钮文字就是所选项的文字，箭头由图标绘制。"""
    dropdown = AppleDropDown(flat=True)
    try:
        dropdown.addItem("全部图片", "all")
        dropdown.addItem("5 星 (★★★★★)", "5")

        # 验证默认选中第 0 项，按钮文字与 currentText() 一致
        assert dropdown.text() == "全部图片"
        assert dropdown.currentText() == "全部图片"
        assert dropdown.currentData() == "all"

        # 切换到第 1 项
        dropdown.setCurrentIndex(1)
        assert dropdown.text() == "5 星 (★★★★★)"
        assert dropdown.currentText() == "5 星 (★★★★★)"
        assert dropdown.currentData() == "5"

        # 外观由全局样式表按 flat 属性决定
        assert dropdown.property("flat") is True
        assert not dropdown.styleSheet()
    finally:
        dropdown.close()


def test_apple_menu_polymorphic_signatures(qapp):
    """验证 AppleMenu 对齐原生 QMenu 的所有多态构造签名（parent、title+parent、kwargs）。"""
    parent_btn = QToolButton()
    parent_widget = QWidget()

    menus = []
    try:
        # 1. 无参构造
        m1 = AppleMenu()
        assert m1.title() == ""
        assert m1.parent() is None
        menus.append(m1)

        # 2. 单位置参数为 QWidget/QToolButton（最关键场景，原 Bug 点）
        m2 = AppleMenu(parent_btn)
        assert m2.parent() is parent_btn
        assert m2.title() == ""
        menus.append(m2)

        # 3. 单位置参数为 str 标题
        m3 = AppleMenu("FileMenu")
        assert m3.title() == "FileMenu"
        assert m3.parent() is None
        menus.append(m3)

        # 4. 两个位置参数：(str, QWidget)
        m4 = AppleMenu("EditMenu", parent_widget)
        assert m4.title() == "EditMenu"
        assert m4.parent() is parent_widget
        menus.append(m4)

        # 5. 关键字参数：title
        m5 = AppleMenu(title="ViewMenu")
        assert m5.title() == "ViewMenu"
        assert m5.parent() is None
        menus.append(m5)

        # 6. 关键字参数：parent
        m6 = AppleMenu(parent=parent_btn)
        assert m6.parent() is parent_btn
        assert m6.title() == ""
        menus.append(m6)

        # 7. 关键字参数：title 与 parent
        m7 = AppleMenu(title="HelpMenu", parent=parent_widget)
        assert m7.title() == "HelpMenu"
        assert m7.parent() is parent_widget
        menus.append(m7)

        # 8. 关键字参数逆序：parent 与 title
        m8 = AppleMenu(parent=parent_btn, title="ToolsMenu")
        assert m8.title() == "ToolsMenu"
        assert m8.parent() is parent_btn
        menus.append(m8)

        # 9. 混合传参：位置 title + 关键字 parent
        m9 = AppleMenu("MixedMenu", parent=parent_widget)
        assert m9.title() == "MixedMenu"
        assert m9.parent() is parent_widget
        menus.append(m9)

        # 验证所有实例的基础属性与主题适配
        for m in menus:
            assert m.objectName() == "appleMenu"
            assert m.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            assert bool(m.windowFlags() & Qt.WindowType.FramelessWindowHint)
            assert not m.styleSheet()
    finally:
        for m in menus:
            m.close()
        parent_btn.close()
        parent_widget.close()
