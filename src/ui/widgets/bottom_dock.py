"""把一个浮层贴在宿主控件的下沿居中，宿主大小变化时跟随。浮层与宿主互不知道对方。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QWidget

_SIDE_MARGIN = 8


class BottomDock(QObject):
    def __init__(self, widget: QWidget, bottom: int):
        super().__init__(widget)
        self._widget = widget
        self._bottom = bottom
        self._host: Optional[QWidget] = None
        widget.installEventFilter(self)

    def host(self) -> Optional[QWidget]:
        return self._host

    def attach(self, host: Optional[QWidget]) -> None:
        """换到另一个宿主；传入 None 时浮层留在原处，由调用方自行隐藏。"""
        if host is self._host:
            self.place()
            return
        if self._host is not None:
            self._host.removeEventFilter(self)
        self._host = host
        if host is None:
            return
        visible = not self._widget.isHidden()
        self._widget.setParent(host)
        self._widget.setVisible(visible)
        host.installEventFilter(self)
        self.place()

    def place(self) -> None:
        host = self._host
        if host is None:
            return
        size = self._widget.sizeHint().expandedTo(self._widget.minimumSize()).boundedTo(self._widget.maximumSize())
        width = max(0, min(size.width(), host.width() - 2 * _SIDE_MARGIN))
        self._widget.setGeometry((host.width() - width) // 2, host.height() - self._bottom - size.height(),
                                 width, size.height())
        self._widget.raise_()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._host and event.type() == QEvent.Type.Resize:
            self.place()
        elif watched is self._widget and event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Show):
            self.place()
        return False
