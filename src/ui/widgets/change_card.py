"""改动卡片：一组参数改动，标题右侧是它的去向，下面逐项是参数名、原值与新值；可按工具分段。

只显示交给它的文字；外观在全局样式表里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_MD, SPACING_SM, SPACING_XS, px
from .status_badge import StatusBadge


@dataclass(frozen=True)
class ChangeRow:
    name: str
    before: str = ""
    after: str = ""
    section: bool = False    # 分段标题（例如“调色 · 5 项”），独占一行


class ChangeCard(QFrame):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("changeCard")
        self._rows: tuple[ChangeRow, ...] = ()
        self._labels: list[QLabel] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_MD), px(SPACING_SM) + 2, px(SPACING_MD), px(SPACING_SM) + 2)
        layout.setSpacing(px(SPACING_SM))
        header = QHBoxLayout()
        header.setSpacing(px(SPACING_SM))
        self._title = QLabel(self)
        self._title.setObjectName("changeCardTitle")
        self._title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._badge = StatusBadge("pending", "", self)
        self._badge.hide()
        header.addWidget(self._title, 1)
        header.addWidget(self._badge)
        layout.addLayout(header)
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(px(SPACING_SM))
        self._grid.setVerticalSpacing(px(SPACING_XS))
        self._grid.setColumnStretch(0, 1)
        layout.addLayout(self._grid)

    def title(self) -> str:
        return self._title.text()

    def set_title(self, text: str) -> None:
        self._title.setText(text)
        self._title.setVisible(bool(text))

    def status(self) -> tuple[str, str]:
        """(状态标记的种类, 文字)；没有设置时文字为空。"""
        return self._badge.kind(), self._badge.text()

    def set_status(self, kind: str, text: str) -> None:
        self._badge.set_status(kind, text)
        self._badge.setVisible(bool(text))

    def rows(self) -> tuple[ChangeRow, ...]:
        return self._rows

    def set_rows(self, rows: Sequence[ChangeRow]) -> None:
        rows = tuple(rows)
        if rows == self._rows:
            return
        self._rows = rows
        for label in self._labels:
            self._grid.removeWidget(label)
            label.deleteLater()
        self._labels = []
        for index, row in enumerate(rows):
            if row.section:
                label = QLabel(row.name, self)
                label.setObjectName("changeCardSection")
                label.setTextFormat(Qt.TextFormat.PlainText)
                self._grid.addWidget(label, index, 0, 1, 4)
                self._labels.append(label)
                continue
            for column, (text, name, alignment) in enumerate((
                    (row.name, "changeCardName", Qt.AlignmentFlag.AlignLeft),
                    (row.before, "changeCardBefore", Qt.AlignmentFlag.AlignRight),
                    ("→" if row.before or row.after else "", "changeCardBefore", Qt.AlignmentFlag.AlignCenter),
                    (row.after, "changeCardAfter", Qt.AlignmentFlag.AlignRight))):
                label = QLabel(text, self)
                label.setObjectName(name)
                label.setTextFormat(Qt.TextFormat.PlainText)
                if column == 0:
                    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                self._grid.addWidget(label, index, column, alignment | Qt.AlignmentFlag.AlignVCenter)
                self._labels.append(label)
