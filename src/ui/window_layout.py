"""随窗口宽度变化的版式规则，以及下次打开时要恢复的各栏状态。

规则只有三档：不足 ``NARROW_WINDOW_WIDTH`` 时右侧栏以浮层展开、胶片条用紧凑高度；达到
``WIDE_WINDOW_WIDTH`` 时右侧栏与缩略图放大一档；其余是标准版式。字号与控件高度不随窗口变化。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QSplitter, QWidget

from resources.styles.theme_variables import (EDIT_SIDEBAR_WIDTH, EDIT_SIDEBAR_WIDTH_WIDE, FILMSTRIP_HEIGHT,
                                              FILMSTRIP_HEIGHT_COMPACT, FILMSTRIP_HEIGHT_WIDE, INFO_SIDEBAR_WIDTH,
                                              INFO_SIDEBAR_WIDTH_WIDE, NARROW_WINDOW_WIDTH, SIDEBAR_MAX_WIDTH,
                                              SIDEBAR_MIN_WIDTH, WIDE_WINDOW_WIDTH)
from resources.styles.theme_variables import ASSISTANT_CONTEXT_BREAKPOINT, ASSISTANT_NAVIGATION_BREAKPOINT


@dataclass(frozen=True)
class WindowLayout:
    floating_sidebar: bool   # 右侧栏默认收起，展开时浮在画布上而不占画布的宽度
    info_width: int          # 照片信息栏的默认宽度
    edit_width: int          # 编辑右侧栏的默认宽度
    filmstrip_height: int    # 胶片条的默认高度


def window_layout(width: int) -> WindowLayout:
    """按窗口的逻辑像素宽度给出版式。"""
    narrow, wide = width < NARROW_WINDOW_WIDTH, width >= WIDE_WINDOW_WIDTH
    return WindowLayout(
        floating_sidebar=narrow,
        info_width=INFO_SIDEBAR_WIDTH_WIDE if wide else INFO_SIDEBAR_WIDTH,
        edit_width=EDIT_SIDEBAR_WIDTH_WIDE if wide else EDIT_SIDEBAR_WIDTH,
        filmstrip_height=FILMSTRIP_HEIGHT_COMPACT if narrow else FILMSTRIP_HEIGHT_WIDE if wide else FILMSTRIP_HEIGHT,
    )


def assistant_layout(width: int, expanded: bool) -> tuple[bool, bool]:
    return (expanded and width >= ASSISTANT_NAVIGATION_BREAKPOINT,
            expanded and width >= ASSISTANT_CONTEXT_BREAKPOINT)


def sidebar_width(preferred: int, default: int, needed: int = 0) -> int:
    """右侧栏的宽度：用户拖过就用拖出的宽度，否则用默认值；限制在可拖动的范围内，且不小于内容需要的宽度。"""
    width = preferred if preferred > 0 else default
    return max(SIDEBAR_MIN_WIDTH, needed, min(max(SIDEBAR_MAX_WIDTH, needed), width))


def set_splitter_width(splitter: QSplitter, widget: QWidget, width: int) -> None:
    """把分隔条里的一栏设为给定宽度，差额由第一格（画布）吸收。"""
    index = splitter.indexOf(widget)
    if index <= 0 or widget.isHidden():
        return
    sizes = splitter.sizes()
    delta = width - sizes[index]
    if delta and sizes[0] - delta > 0:
        sizes[index] = width
        sizes[0] -= delta
        splitter.setSizes(sizes)


@dataclass
class PanelPreferences:
    """各栏的展开状态与用户拖出的宽度；宽度为 0 表示没有拖过，跟随窗口尺寸的默认值。"""

    info_expanded: bool = False
    info_width: int = 0
    edit_collapsed: bool = False
    edit_width: int = 0

    @classmethod
    def load(cls, settings: QSettings) -> "PanelPreferences":
        return cls(
            info_expanded=settings.value("infoSidebarExpanded", False, type=bool),
            info_width=settings.value("infoSidebarWidth", 0, type=int),
            edit_collapsed=settings.value("editSidebarCollapsed", False, type=bool),
            edit_width=settings.value("editSidebarWidth", 0, type=int),
        )

    def save(self, settings: QSettings) -> None:
        settings.setValue("infoSidebarExpanded", self.info_expanded)
        settings.setValue("infoSidebarWidth", self.info_width)
        settings.setValue("editSidebarCollapsed", self.edit_collapsed)
        settings.setValue("editSidebarWidth", self.edit_width)
