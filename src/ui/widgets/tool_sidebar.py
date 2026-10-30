from __future__ import annotations
from typing import TYPE_CHECKING, Optional, Sequence
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QStackedWidget, QScrollArea, QFrame
)

from resources.styles.theme_variables import CONTROL_HEIGHT_COMPACT, SPACING_MD, SPACING_SM, px
from ..themed_mixin import ThemedI18nMixin
from ..icons import themed_icon
from .apple_button import AppleIconButton
from .edit_stack import EditStackView, StackRow
from .notice_bar import NoticeBar
from .action_button import plain_action_text
from .segmented_control import AppleSegmentedControl
from .tooltip import set_tooltip

if TYPE_CHECKING:
    from ..panels.base_tool_panel import BaseToolPanel


class _PanelStack(QStackedWidget):
    """各工具面板的堆叠，尺寸只取当前面板。

    堆叠默认按全部页面里最宽、最高的一页计算尺寸：较宽的面板会把当前面板撑出视口，
    较高的面板会让滚动区在较矮的面板下面留出一大段空白。

    竖向放不下时由滚动区滚动，面板不会被压到理想高度以下：滚动区把内容的高度定为
    “视口高度与内容最小高度里较大的那个”，所以这里报出的最小高度就是面板的理想高度。
    高度随宽度变化的面板（含自动换行的文字）由滚动区另按 heightForWidth() 取高度，仍报布局的最小高度。
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def sizeHint(self) -> QSize:
        current = self.currentWidget()
        return current.sizeHint() if current is not None else QSize(0, 0)

    def minimumSizeHint(self) -> QSize:
        current = self.currentWidget()
        if current is None:
            return QSize(0, 0)
        minimum = current.minimumSizeHint()
        layout = current.layout()
        if layout is not None and layout.hasHeightForWidth():
            return minimum
        return QSize(minimum.width(), max(minimum.height(), current.sizeHint().height()))

    def heightForWidth(self, width: int) -> int:
        current = self.currentWidget()
        return current.heightForWidth(width) if current is not None else -1


class ToolSidebar(QWidget, ThemedI18nMixin):
    """编辑工作区右侧承载工具面板的容器。

    包含：
      1. 顶部：工具标题、本工具的重置、收起；同一入口下有多个工具时，标题下方是一行分段开关
      2. 暂存恢复提示条
      3. 各工具 BaseToolPanel 的堆叠与滚动视口
      4. 编辑栈：这张照片现在依次做了哪几步，固定在滚动视口下方，可以收起
      5. 当前面板交出的底部操作区（如“渲染预览”），固定在最下方

    用户在分段开关上选择工具、或点编辑栈里的一步时发出 ``tool_requested``；点编辑栈里一步的重置发出
    ``stage_reset_requested``。``set_variants()``、``set_edit_stack()`` 是程序回填，不发出信号。
    """

    close_requested = Signal()
    reset_requested = Signal()
    discard_stash_requested = Signal()
    tool_requested = Signal(str)
    stage_reset_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("toolSidebar")

        self._current_panel: Optional[BaseToolPanel] = None
        self._panels_by_id: dict[str, BaseToolPanel] = {}
        self._footers: dict[str, QWidget] = {}
        self._variant_ids: tuple[str, ...] = ()

        self._init_themed_i18n()
        self._init_ui()

    def _init_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ------------------------------------------------------------------
        # 1. 顶部标题栏
        # ------------------------------------------------------------------
        self._header = QWidget(self)
        self._header.setObjectName("toolSidebarHeader")
        self._header.setFixedHeight(44)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(14, 6, 10, 6)
        header_layout.setSpacing(8)

        self._title_label = QLabel(self._tr("tool_sidebar.default_title", "Tool Settings"), self._header)
        self._title_label.setObjectName("toolSidebarTitle")
        header_layout.addWidget(self._title_label)

        header_layout.addStretch()

        # 本工具的重置
        self._reset_btn = AppleIconButton(themed_icon("rotate"), CONTROL_HEIGHT_COMPACT, self._header)
        self._reset_btn.setIconSize(QSize(18, 18))
        self._reset_btn.clicked.connect(self.reset_requested.emit)
        header_layout.addWidget(self._reset_btn)

        # 关闭按钮（右侧栏镜像收起图标）
        self._close_btn = QPushButton(self._header)
        self._close_btn.setObjectName("toolSidebarCloseBtn")
        self._close_btn.setFixedSize(28, 28)
        self._close_btn.setIcon(themed_icon("layout-sidebar-right"))
        self._close_btn.setIconSize(QSize(18, 18))
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.clicked.connect(self.close_requested.emit)
        header_layout.addWidget(self._close_btn)

        root_layout.addWidget(self._header)

        # ------------------------------------------------------------------
        # 1.2 同一入口下的工具切换（只有一个工具时隐藏）
        # ------------------------------------------------------------------
        self._variants = AppleSegmentedControl(self)
        self._variants.currentIndexChanged.connect(self._on_variant_changed)
        self._variants_row = QWidget(self)
        variants_layout = QVBoxLayout(self._variants_row)
        variants_layout.setContentsMargins(px(SPACING_MD), 0, px(SPACING_MD), px(SPACING_SM))
        variants_layout.addWidget(self._variants)
        self._variants_row.setVisible(False)
        root_layout.addWidget(self._variants_row)

        # ------------------------------------------------------------------
        # 1.5 暂存恢复提示条幅（默认隐藏，存在未提交暂存时显示）
        # ------------------------------------------------------------------
        self._stash_banner = NoticeBar(
            "warning",
            self._tr("tool_sidebar.stash_recovered", "Restored unapplied changes"),
            self._tr("tool_sidebar.discard_stash", "Discard"),
            self,
        )
        self._stash_banner.action_triggered.connect(self.discard_stash_requested.emit)
        self._stash_banner.setVisible(False)
        banner_layout = QVBoxLayout()
        banner_layout.setContentsMargins(px(SPACING_MD), 0, px(SPACING_MD), px(SPACING_SM))
        banner_layout.addWidget(self._stash_banner)
        root_layout.addLayout(banner_layout)

        # 分割线
        sep_top = QFrame(self)
        sep_top.setFrameShape(QFrame.Shape.HLine)
        sep_top.setFrameShadow(QFrame.Shadow.Sunken)
        sep_top.setObjectName("toolSidebarSepTop")
        root_layout.addWidget(sep_top)

        # ------------------------------------------------------------------
        # 2. 滚动视口 + QStackedWidget
        # ------------------------------------------------------------------
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setObjectName("toolSidebarScrollArea")
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll_area.viewport().setAutoFillBackground(False)

        self._stack = _PanelStack(self._scroll_area)
        self._stack.setObjectName("toolSidebarStack")
        self._stack.setAutoFillBackground(False)
        self._scroll_area.setWidget(self._stack)

        root_layout.addWidget(self._scroll_area, 1)

        # ------------------------------------------------------------------
        # 3. 编辑栈：不随内容滚动，哪个工具的面板下面都看得到
        # ------------------------------------------------------------------
        sep_stack = QFrame(self)
        sep_stack.setFrameShape(QFrame.Shape.HLine)
        sep_stack.setFrameShadow(QFrame.Shadow.Sunken)
        sep_stack.setObjectName("toolSidebarSepTop")
        root_layout.addWidget(sep_stack)
        self.edit_stack = EditStackView(self)
        self.edit_stack.stage_requested.connect(self.tool_requested)
        self.edit_stack.reset_requested.connect(self.stage_reset_requested)
        root_layout.addWidget(self.edit_stack)

        # ------------------------------------------------------------------
        # 4. 当前面板的底部操作区：不随内容滚动
        # ------------------------------------------------------------------
        self._footer_host = QFrame(self)
        self._footer_host.setObjectName("toolSidebarFooter")
        self._footer_layout = QVBoxLayout(self._footer_host)
        self._footer_layout.setContentsMargins(px(SPACING_MD), px(SPACING_SM), px(SPACING_MD), px(SPACING_MD))
        self._footer_layout.setSpacing(0)
        self._footer_host.setVisible(False)
        root_layout.addWidget(self._footer_host)
        self._retranslate_chrome()

    def register_panel(self, tool_id: str, panel: BaseToolPanel) -> None:
        """向堆叠部件注册一个面板。"""
        if self._panels_by_id.get(tool_id) is panel:
            return
        if tool_id in self._panels_by_id:
            self.remove_panel(tool_id)
        self._panels_by_id[tool_id] = panel
        self._stack.addWidget(panel)
        footer = panel.take_footer()
        if footer is not None:
            self._footers[tool_id] = footer
            footer.setParent(self._footer_host)
            self._footer_layout.addWidget(footer)
            footer.hide()

    def remove_panel(self, tool_id):
        panel = self._panels_by_id.pop(tool_id, None)
        if panel is None:
            return
        self._stack.removeWidget(panel)
        footer = self._footers.pop(tool_id, None)
        if footer is not None:
            self._footer_layout.removeWidget(footer)
            footer.hide()
            footer.deleteLater()
        if self._current_panel is panel:
            self._current_panel = None
            self._footer_host.setVisible(False)

    def switch_to_panel(self, tool_id: str) -> Optional[BaseToolPanel]:
        """切换到特定工具面板。"""
        if tool_id not in self._panels_by_id:
            return None
        panel = self._panels_by_id[tool_id]
        self._current_panel = panel
        if self._stack.currentWidget() is not panel:
            self._stack.setCurrentWidget(panel)
            # 换了面板从顶部看起：上一个面板的滚动位置对新面板没有意义
            self._scroll_area.verticalScrollBar().setValue(0)
            self._scroll_area.horizontalScrollBar().setValue(0)

        for footer_id, footer in self._footers.items():
            footer.setVisible(footer_id == tool_id)
        self._footer_host.setVisible(tool_id in self._footers)

        # 刷新标题
        title = plain_action_text(self._tr(panel.TOOL_TITLE_KEY, panel.TOOL_TITLE_DEFAULT))
        self._title_label.setText(title)
        return panel

    show_panel = switch_to_panel

    def get_current_panel(self) -> Optional[BaseToolPanel]:
        return self._current_panel

    def content_min_width(self) -> int:
        """完整显示当前面板所需的宽度（含竖向滚动条）；宿主据此决定这一栏最窄能拖到多少。"""
        if self._current_panel is None:
            return 0
        return (self._current_panel.minimumSizeHint().width()
                + self._scroll_area.verticalScrollBar().sizeHint().width())

    def set_title(self, title: str) -> None:
        self._title_label.setText(plain_action_text(title))

    def set_stash_banner_visible(self, visible: bool) -> None:
        self._stash_banner.setVisible(visible)

    def set_edit_stack(self, rows: Sequence[StackRow]) -> None:
        """程序回填编辑栈里的各步，按处理顺序。"""
        self.edit_stack.set_rows(rows)

    def variants(self) -> tuple[str, ...]:
        return self._variant_ids

    def current_variant(self) -> str:
        index = self._variants.currentIndex()
        return self._variant_ids[index] if 0 <= index < len(self._variant_ids) else ""

    def set_variants(self, variants: Sequence[tuple[str, str]], current: str) -> None:
        """设置同一入口下可切换的工具，每项为 (tool_id, 文字)；不足两项时不显示分段开关。"""
        tool_ids = tuple(tool_id for tool_id, _ in variants)
        self._variants.blockSignals(True)
        try:
            if tool_ids != self._variant_ids:
                self._variants.clear()
                for tool_id, text in variants:
                    self._variants.add_segment(text, tool_id)
                self._variants.clear_selection()
                self._variant_ids = tool_ids
            else:
                for index, (_, text) in enumerate(variants):
                    self._variants.set_segment_text(index, text)
            if current in tool_ids:
                self._variants.setCurrentIndex(tool_ids.index(current))
            else:
                self._variants.clear_selection()
        finally:
            self._variants.blockSignals(False)
        self._variants_row.setVisible(len(tool_ids) > 1)

    def _on_variant_changed(self, index: int) -> None:
        if 0 <= index < len(self._variant_ids):
            self.tool_requested.emit(self._variant_ids[index])

    def _retranslate_chrome(self) -> None:
        reset = self._tr("tool_sidebar.reset_tool", "Reset this tool")
        self._reset_btn.setAccessibleName(reset)
        set_tooltip(self._reset_btn, reset)
        close = self._tr("tool_sidebar.close", "Close Sidebar (Esc)")
        self._close_btn.setAccessibleName(close)
        self._close_btn.setToolTip(close)
        self._stash_banner.set_text(self._tr("tool_sidebar.stash_recovered", "Restored unapplied changes"))
        self._stash_banner.set_action_text(self._tr("tool_sidebar.discard_stash", "Discard"))
        self._stash_banner.action_button.setToolTip(
            self._tr("tool_sidebar.discard_stash_tip", "Discard restored adjustments for this tool")
        )
        self.edit_stack.set_texts(self._tr("edit_stack.title", "Edit Stack"),
                                  self._tr("edit_stack.empty", "No adjustments yet"),
                                  self._tr("edit_stack.reset_stage", "Reset this step"))

    def retranslate_ui(self) -> None:
        self._retranslate_chrome()
        if self._current_panel:
            self._title_label.setText(
                plain_action_text(self._tr(self._current_panel.TOOL_TITLE_KEY, self._current_panel.TOOL_TITLE_DEFAULT))
            )
            self._current_panel.retranslate_ui()
