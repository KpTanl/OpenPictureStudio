"""分组标题：折叠箭头加标题，右侧可放操作，下面是可折叠的内容；分组之间以细线分隔。

外观由全局样式表按对象名决定，箭头取自图标资源。
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_MD, SPACING_SM, SPACING_XS, px
from ..icons import themed_icon

_ARROW_SIZE = 16


class CollapsibleCard(QFrame):
    toggled = pyqtSignal(bool)

    def __init__(self, title: str, parent=None, is_expanded: bool = True):
        super().__init__(parent)
        self._is_expanded = is_expanded

        self.setObjectName("collapsibleCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QWidget(self)
        # 标题一行不随所在布局被压矮：样式表给按钮写了 min-height，按钮自己的最小高度因此只剩描边，
        # 高度定在这一行的容器上，取标题文字的自然高度
        self._header.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._header_layout = QHBoxLayout(self._header)
        self._header_layout.setContentsMargins(0, 0, 0, 0)
        self._header_layout.setSpacing(px(SPACING_XS))

        self._header_btn = QPushButton(self._header)
        self.set_title(title)
        self._header_btn.setObjectName("collapsibleHeaderBtn")
        self._header_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._header_btn.setIconSize(QSize(_ARROW_SIZE, _ARROW_SIZE))
        self._header_btn.clicked.connect(self.toggle)
        self._header_layout.addWidget(self._header_btn, 1)
        layout.addWidget(self._header)

        self._content_widget = QWidget(self)
        self._content_widget.setObjectName("collapsibleContent")
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(0, px(SPACING_SM), 0, px(SPACING_MD))
        self._content_layout.setSpacing(px(SPACING_SM))
        layout.addWidget(self._content_widget)

        self._sync_expanded()

    def title(self) -> str:
        return self._title

    def set_title(self, title: str):
        self._title = title
        # 按钮文字里的 & 会被当作助记符，标题按原样显示
        self._header_btn.setText(title.replace("&", "&&"))

    def add_header_widget(self, widget: QWidget):
        """在标题右侧放一个操作控件，如“重置本组”的图标按钮。"""
        self._header_layout.addWidget(widget)

    def toggle(self):
        self.set_expanded(not self._is_expanded)

    def set_expanded(self, expanded: bool):
        self._is_expanded = expanded
        self._sync_expanded()
        self.toggled.emit(expanded)

    def is_expanded(self) -> bool:
        return self._is_expanded

    def _sync_expanded(self):
        self._content_widget.setVisible(self._is_expanded)
        self._header_btn.setIcon(
            themed_icon("chevron-down" if self._is_expanded else "chevron-right", role="text_caption"))

    def add_widget(self, widget: QWidget, stretch: int = 0):
        self._content_layout.addWidget(widget, stretch)

    def add_layout(self, layout, stretch: int = 0):
        self._content_layout.addLayout(layout, stretch)

    def setContentLayout(self, layout):
        self._content_layout.addLayout(layout)
