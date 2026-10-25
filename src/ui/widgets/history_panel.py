"""右侧栏的“历史”页：标题、可以滚动的历史列表和一行说明；没有可以列的步骤时是空状态。

只接收要显示的步骤并转发 ``step_requested``，回到哪一步由宿主处理。
"""

from __future__ import annotations

from typing import Optional, Sequence

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QStackedWidget, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_MD, SPACING_SM, px
from .empty_state import EmptyState
from .history_list import HistoryList, HistoryStep

_HEADER_HEIGHT = 44


class HistoryPanel(QWidget):
    step_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("historyPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget(self)
        header.setObjectName("toolSidebarHeader")
        header.setFixedHeight(_HEADER_HEIGHT)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(px(SPACING_MD) + 2, 0, px(SPACING_MD), 0)
        self._title = QLabel(header)
        self._title.setObjectName("toolSidebarTitle")
        header_layout.addWidget(self._title)
        header_layout.addStretch(1)
        layout.addWidget(header)

        self._pages = QStackedWidget(self)
        self._scroll = QScrollArea(self._pages)
        self._scroll.setObjectName("toolSidebarScrollArea")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setAutoFillBackground(False)
        content = QWidget(self._scroll)
        content.setObjectName("historyContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(px(SPACING_SM), 0, px(SPACING_SM), px(SPACING_SM))
        content_layout.setSpacing(0)
        self.list = HistoryList(content)
        self.list.step_requested.connect(self.step_requested)
        content_layout.addWidget(self.list)
        content_layout.addStretch(1)
        self._scroll.setWidget(content)
        self._pages.addWidget(self._scroll)
        self._empty = EmptyState("history", self._pages)
        self._pages.addWidget(self._empty)
        layout.addWidget(self._pages, 1)

        self._hint = QLabel(self)
        self._hint.setObjectName("mutedText")
        self._hint.setWordWrap(True)
        self._hint.setContentsMargins(px(SPACING_MD) + 2, px(SPACING_SM), px(SPACING_MD), px(SPACING_MD))
        layout.addWidget(self._hint)
        self.set_steps(())

    def steps(self) -> tuple[HistoryStep, ...]:
        return self.list.steps()

    def is_empty(self) -> bool:
        return self._pages.currentWidget() is self._empty

    def set_steps(self, steps: Sequence[HistoryStep]) -> None:
        """程序回填，自旧到新；当前这一步滚进视口。"""
        self.list.set_steps(steps)
        self._pages.setCurrentWidget(self._scroll if steps else self._empty)
        self._hint.setVisible(bool(steps))
        if steps:
            QTimer.singleShot(0, self._reveal_current)

    def _reveal_current(self) -> None:
        current = self.list.current_rect()
        if not current.isNull():
            self._scroll.ensureVisible(0, current.center().y(), 0, current.height())

    def set_texts(self, title: str, hint: str, empty_title: str, empty_description: str = "") -> None:
        self._title.setText(title)
        self._hint.setText(hint)
        self._empty.set_text(empty_title, empty_description)
