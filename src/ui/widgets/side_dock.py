"""把一个栏贴在宿主控件的右沿，自上而下，底部留出一段不遮挡；宿主大小变化时跟随。栏与宿主互不知道对方。

用于窄窗口：右侧栏不再占画布的宽度，展开时浮在画布上。显示与收起仍由栏的所属方决定。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QWidget


class SideDock(QObject):
    def __init__(self, widget: QWidget, bottom: int):
        super().__init__(widget)
        self._widget = widget
        self._bottom = bottom
        self._width = widget.minimumWidth()
        self._host: Optional[QWidget] = None

    def host(self) -> Optional[QWidget]:
        return self._host

    def set_width(self, width: int) -> None:
        self._width = width
        self.place()

    def attach(self, host: QWidget) -> None:
        """把栏移到宿主上；是否显示保持原样。"""
        if host is self._host:
            self.place()
            return
        self.detach()
        self._host = host
        visible = not self._widget.isHidden()
        self._widget.setParent(host)
        self._set_floating(True)
        self._widget.setVisible(visible)
        host.installEventFilter(self)
        self._widget.installEventFilter(self)
        self.place()

    def detach(self) -> None:
        """不再跟随宿主；栏留在原处，由调用方放回自己的布局。"""
        if self._host is None:
            return
        self._host.removeEventFilter(self)
        self._widget.removeEventFilter(self)
        self._host = None
        self._set_floating(False)

    def _set_floating(self, floating: bool) -> None:
        """浮起时栏的底边不再贴着窗口，样式表按这个属性给它补一条底边线。"""
        self._widget.setProperty("floating", floating)
        self._widget.style().unpolish(self._widget)
        self._widget.style().polish(self._widget)

    def place(self) -> None:
        host = self._host
        if host is None:
            return
        width = max(0, min(self._width, host.width()))
        self._widget.setGeometry(host.width() - width, 0, width, max(0, host.height() - self._bottom))
        self._widget.raise_()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._host and event.type() == QEvent.Type.Resize:
            self.place()
        elif watched is self._widget and event.type() == QEvent.Type.Show:
            self.place()
        return False
