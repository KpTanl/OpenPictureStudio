"""顶栏：标志、工作区切换、当前工作区的操作、居中的标题、照片信息开关、导出与更多；兼作标题栏时最右侧是窗口按钮。

只接收动作与状态，通过信号表达用户意图，不持有主窗口。``set_workspace()``、``set_title()``、
``set_photo_info_state()`` 是程序回填，不发出信号。兼作标题栏时，``region_at()`` 回答一点是可以拖动窗口的空白、
最大化按钮还是普通控件。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Callable, Optional, Sequence

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QFont, QFontMetricsF, QIcon, QPainter, QPaintEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QSizePolicy, QWidget

from resources.styles.theme_variables import (CONTROL_HEIGHT_COMPACT, FONT_SIZE_BODY, SPACING_MD, SPACING_SM,
                                              SPACING_XS, TOP_BAR_HEIGHT, px)
from ...utils.resource_paths import resolve_app_icon_path
from ..icons import themed_icon
from ..theme import get_theme_tokens, to_qcolor
from ..window_chrome import CAPTION, CLIENT, MAXIMIZE
from .action_button import ActionIconButton
from .apple_button import AppleBrandButton, AppleGhostButton, AppleIconButton
from .apple_menu import AppleMenu
from .segmented_control import AppleSegmentedControl
from .toggle_button import ToggleButton
from .tooltip import set_tooltip
from .window_controls import WindowControls

VIEWER, EDITOR = 0, 1
_LOGO_SIZE = 28
_MENU_ICON_SIZE = 20
_CHEVRON_SIZE = 16


@dataclass(frozen=True)
class TopBarActions:
    """顶栏用到的动作；序列里的 None 表示分隔。"""

    open_menu: Sequence[Optional[QAction]]
    editor_buttons: Sequence[Optional[tuple[QAction, str]]]  # (动作, 图标名)
    export_default: tuple[QAction, QAction]  # 看图、编辑各自直接点击“导出”时的动作
    export_menus: tuple[Sequence[Optional[QAction]], Sequence[Optional[QAction]]]
    more_menu: Sequence[Optional[QAction]]


class _Logo(QWidget):
    """应用标志，取自随程序提供的图标文件，按屏幕缩放选用合适的尺寸。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedSize(_LOGO_SIZE, _LOGO_SIZE)
        self._icon = QIcon(str(resolve_app_icon_path()))

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._icon.paint(painter, self.rect(), Qt.AlignmentFlag.AlignCenter)
        painter.end()


class _Title(QWidget):
    """文件名加一段说明（尺寸或保存状态）；空间不足时省略文件名。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._name = ""
        self._detail = ""
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def name(self) -> str:
        return self._name

    def detail(self) -> str:
        return self._detail

    def set_text(self, name: str, detail: str) -> None:
        if (name, detail) != (self._name, self._detail):
            self._name, self._detail = name, detail
            self.update()

    def _fonts(self) -> tuple[QFont, QFont]:
        regular = QFont(self.font())
        regular.setPixelSize(px(FONT_SIZE_BODY))
        bold = QFont(regular)
        bold.setWeight(QFont.Weight.DemiBold)
        return bold, regular

    def _detail_width(self) -> int:
        if not self._detail:
            return 0
        return ceil(QFontMetricsF(self._fonts()[1], self).horizontalAdvance(self._detail)) + px(SPACING_MD)

    def sizeHint(self) -> QSize:
        self.ensurePolished()
        return QSize(ceil(QFontMetricsF(self._fonts()[0], self).horizontalAdvance(self._name)) + self._detail_width(),
                     px(FONT_SIZE_BODY) * 2)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        tokens = get_theme_tokens()
        bold, regular = self._fonts()
        name_width = max(0, self.width() - self._detail_width())
        painter.setFont(bold)
        painter.setPen(to_qcolor(tokens["text_primary"]))
        metrics = QFontMetricsF(bold, self)
        name = self._name
        if metrics.horizontalAdvance(name) > name_width:
            name = metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, name_width)
        painter.drawText(QRect(0, 0, name_width, self.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        if self._detail:
            left = min(name_width, ceil(metrics.horizontalAdvance(name))) + px(SPACING_MD)
            painter.setFont(regular)
            painter.setPen(to_qcolor(tokens["text_secondary"]))
            painter.drawText(QRect(left, 0, max(0, self.width() - left), self.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._detail)
        painter.end()


def _menu(parent: QWidget, actions: Sequence[Optional[QAction]]) -> AppleMenu:
    menu = AppleMenu(parent)
    for action in actions:
        if action is None:
            menu.addSeparator()
        else:
            menu.addAction(action)
    return menu


def _separator(parent: QWidget) -> QFrame:
    line = QFrame(parent)
    line.setObjectName("topBarSeparator")
    line.setFixedSize(1, 20)
    return line


class TopBar(QWidget):
    workspace_requested = Signal(int)
    photo_info_toggled = Signal(bool)
    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    def __init__(self, actions: TopBarActions, translate: Callable[[str, str], str],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._actions = actions
        self._translate = translate
        self._workspace = VIEWER
        self.setObjectName("topBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(TOP_BAR_HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(SPACING_MD), 0, px(SPACING_MD), 0)
        layout.setSpacing(0)

        self._left = QWidget(self)
        left = QHBoxLayout(self._left)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(px(SPACING_XS))
        self._logo = _Logo(self._left)
        left.addWidget(self._logo)
        left.addSpacing(px(SPACING_SM))
        self._switch = AppleSegmentedControl(self._left)
        self._switch.add_segments(["", ""])
        self._switch.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._switch.activated.connect(self.workspace_requested.emit)
        left.addWidget(self._switch)
        left.addSpacing(px(SPACING_SM))
        left.addWidget(_separator(self._left))
        left.addSpacing(px(SPACING_SM))

        self._open_button = AppleIconButton(themed_icon("folder-open"), parent=self._left)
        self._open_button.setIconSize(QSize(_MENU_ICON_SIZE, _MENU_ICON_SIZE))
        self._open_button.setMenu(_menu(self._open_button, actions.open_menu))
        left.addWidget(self._open_button)

        self._editor_widgets: list[QWidget] = []
        for entry in actions.editor_buttons:
            widget = _separator(self._left) if entry is None else ActionIconButton(entry[0], entry[1], self._left)
            self._editor_widgets.append(widget)
            left.addWidget(widget)
        layout.addWidget(self._left)
        layout.addStretch(1)

        self._right = QWidget(self)
        right = QHBoxLayout(self._right)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(px(SPACING_SM))
        self._info_toggle = ToggleButton("", "layout-sidebar-right", self._right)
        self._info_toggle.clicked[bool].connect(self.photo_info_toggled.emit)
        right.addWidget(self._info_toggle)

        export = QHBoxLayout()
        export.setSpacing(2)
        self._export_button = AppleBrandButton("", self._right)
        self._export_button.clicked.connect(self._export)
        export.addWidget(self._export_button)
        self._export_menus = tuple(_menu(self._right, entries) for entries in actions.export_menus)
        self._export_options = AppleIconButton(themed_icon("chevron-down"), CONTROL_HEIGHT_COMPACT, self._right)
        self._export_options.setIconSize(QSize(_CHEVRON_SIZE, _CHEVRON_SIZE))
        export.addWidget(self._export_options)
        right.addLayout(export)
        for action in actions.export_default:
            action.changed.connect(self._sync_export)

        self._more_button = AppleGhostButton("", self._right)
        self._more_button.setMenu(_menu(self._more_button, actions.more_menu))
        right.addWidget(self._more_button)
        layout.addWidget(self._right)

        # 窗口按钮：顶栏兼作标题栏时才显示，贴着窗口的右上角
        self._window_controls = WindowControls(translate, self)
        self._window_controls.minimize_requested.connect(self.minimize_requested.emit)
        self._window_controls.maximize_requested.connect(self.maximize_requested.emit)
        self._window_controls.close_requested.connect(self.close_requested.emit)
        self._window_controls.hide()
        layout.addSpacing(px(SPACING_SM))
        layout.addWidget(self._window_controls)

        self._title = _Title(self)
        self.retranslate_ui()
        self.set_workspace(VIEWER)

    def workspace(self) -> int:
        return self._workspace

    def set_workspace(self, workspace: int) -> None:
        if workspace not in (VIEWER, EDITOR):
            raise ValueError(f"未知的工作区: {workspace!r}")
        self._workspace = workspace
        self._switch.setCurrentIndex(workspace)
        self._open_button.setVisible(workspace == VIEWER)
        for widget in self._editor_widgets:
            widget.setVisible(workspace == EDITOR)
        self._export_options.setMenu(self._export_menus[workspace])
        self._sync_export()
        self._place_title()

    def title(self) -> tuple[str, str]:
        return self._title.name(), self._title.detail()

    def set_title(self, name: str, detail: str = "") -> None:
        self._title.set_text(name, detail)
        self._place_title()

    def set_photo_info_state(self, checked: bool, enabled: bool) -> None:
        self._info_toggle.setChecked(checked)
        self._info_toggle.setEnabled(enabled)

    def set_actions_enabled(self, enabled: bool) -> None:
        """提交进行中暂停顶栏上的操作；窗口按钮与拖动窗口不受影响。"""
        self._left.setEnabled(enabled)
        self._right.setEnabled(enabled)

    def has_window_controls(self) -> bool:
        return not self._window_controls.isHidden()

    def set_window_controls_visible(self, visible: bool) -> None:
        """顶栏是否兼作标题栏：显示窗口按钮并让它贴到右边缘。"""
        self._window_controls.setVisible(visible)
        margin = px(SPACING_MD)
        self.layout().setContentsMargins(margin, 0, 0 if visible else margin, 0)
        self._place_title()

    def set_window_maximized(self, maximized: bool) -> None:
        self._window_controls.set_maximized(maximized)

    def set_maximize_state(self, hovered: bool, pressed: bool) -> None:
        self._window_controls.set_maximize_state(hovered, pressed)

    def region_at(self, position: QPoint) -> str:
        """顶栏里一点属于哪种区域：空白处可以拖动窗口，最大化按钮交给系统，其余是普通控件。"""
        if not self.has_window_controls() or not self.rect().contains(position):
            return CLIENT
        child = self.childAt(position)
        if child is self._window_controls.maximize_button():
            return MAXIMIZE
        if child is None or child in (self._left, self._right, self._logo) or child.objectName() == "topBarSeparator":
            return CAPTION
        return CLIENT

    def retranslate_ui(self) -> None:
        tr = self._translate
        self._switch.set_segment_text(VIEWER, tr("topbar.workspace.view", "View"))
        self._switch.set_segment_text(EDITOR, tr("topbar.workspace.edit", "Edit"))
        self._switch.updateGeometry()
        self._info_toggle.setText(tr("photo_info.title", "Photo Info"))
        self._info_toggle.updateGeometry()
        set_tooltip(self._info_toggle, tr("photo_info.toggle", "Show or hide photo information"))
        self._export_button.setText(tr("toolbar.export", "Export"))
        set_tooltip(self._open_button, tr("toolbar.open", "Open"))
        set_tooltip(self._export_options, tr("topbar.export_options", "More export options"))
        self._more_button.setText(tr("topbar.more", "More"))
        self._more_button.updateGeometry()
        self._window_controls.retranslate_ui()
        self._place_title()

    def _export(self) -> None:
        self._actions.export_default[self._workspace].trigger()

    def _sync_export(self) -> None:
        self._export_button.setEnabled(self._actions.export_default[self._workspace].isEnabled())

    def _place_title(self) -> None:
        """标题在整条顶栏上居中；两侧的操作占位时让开，放不下时省略文件名。"""
        self.layout().activate()
        gap = px(SPACING_MD)
        left_edge = self._left.geometry().right() + gap
        right_edge = self._right.geometry().left() - gap
        width = max(0, min(self._title.sizeHint().width(), right_edge - left_edge))
        x = max(left_edge, min((self.width() - width) // 2, right_edge - width))
        self._title.setGeometry(x, 0, width, self.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_title()
