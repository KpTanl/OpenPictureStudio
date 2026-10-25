"""表单行与分组：设置页里“说明在左、控件在右”的一行，行与行之间以细线分隔。

SettingRow 是一行：左侧是标题与说明，也可以换成一个自己的控件（如占用条）；右侧并排放控件；
下面可以有一句随状态出现的消息。左侧剩下的宽度不够时，控件改排到下一行。
SettingSection 是带标题的一组行。外观在全局样式表里。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QBoxLayout, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_MD, SPACING_SM, SPACING_XL, SPACING_XS, px

# 左侧至少留这么宽，否则控件换到下一行
_LEADING_MIN_WIDTH = 200


def plain_label(label: str) -> str:
    """表单行的标题不带末尾的冒号；用于沿用原先写给“标签：控件”版式的文案。"""
    return label.rstrip(":： ")


def _text_label(parent: QWidget, object_name: str) -> QLabel:
    label = QLabel(parent)
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setIndent(0)
    policy = label.sizePolicy()
    policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
    label.setSizePolicy(policy)
    return label


class SettingRow(QFrame):
    def __init__(self, parent: Optional[QWidget] = None, *, leading: Optional[QWidget] = None):
        """leading 给出时占据左侧，代替标题与说明。"""
        super().__init__(parent)
        self.setObjectName("settingRow")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, px(SPACING_MD), 0, px(SPACING_MD))
        outer.setSpacing(px(SPACING_XS))
        self._line = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._line.setContentsMargins(0, 0, 0, 0)
        self._line.setSpacing(px(SPACING_XL))
        self._title = _text_label(self, "settingRowTitle")
        self._description = _text_label(self, "settingRowDescription")
        self._description.hide()
        if leading is None:
            texts = QVBoxLayout()
            texts.setContentsMargins(0, 0, 0, 0)
            texts.setSpacing(px(SPACING_XS) // 2)
            texts.addWidget(self._title)
            texts.addWidget(self._description)
            self._line.addLayout(texts, 1)
        else:
            self._title.hide()
            leading.setParent(self)
            self._line.addWidget(leading, 1)
        self._controls = QHBoxLayout()
        self._controls.setContentsMargins(0, 0, 0, 0)
        self._controls.setSpacing(px(SPACING_SM))
        self._line.addLayout(self._controls)
        outer.addLayout(self._line)
        self._message = _text_label(self, "settingRowMessage")
        self._message.hide()
        outer.addWidget(self._message)

    def title(self) -> str:
        return self._title.text()

    def description(self) -> str:
        return self._description.text()

    def message(self) -> str:
        return self._message.text() if not self._message.isHidden() else ""

    def is_stacked(self) -> bool:
        """控件是否已经换到标题下面一行。"""
        return self._line.direction() == QBoxLayout.Direction.TopToBottom

    def set_text(self, title: str, description: str = "") -> None:
        self._title.setText(title)
        self._description.setText(description)
        self._description.setVisible(bool(description))

    def add_control(self, widget: QWidget) -> None:
        self._controls.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_message(self, text: str = "", kind: str = "info") -> None:
        """行下方的一句消息；kind 为 info、success、warning 或 error，空文字表示收起。"""
        self._message.setText(text)
        self._message.setVisible(bool(text))
        if self._message.property("kind") != kind:
            self._message.setProperty("kind", kind)
            self._message.style().unpolish(self._message)
            self._message.style().polish(self._message)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self) -> None:
        controls = self._controls.sizeHint().width()
        stacked = self._controls.count() > 0 and self.width() - controls - px(SPACING_XL) < _LEADING_MIN_WIDTH
        direction = QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
        if self._line.direction() == direction:
            return
        self._line.setDirection(direction)
        self._line.setSpacing(px(SPACING_SM if stacked else SPACING_XL))
        self._line.setAlignment(self._controls, Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag(0))


class SettingSection(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._title = QLabel(self)
        self._title.setObjectName("settingSectionTitle")
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        self._title.setIndent(0)
        self._title.hide()
        self._layout.addWidget(self._title)

    def title(self) -> str:
        return self._title.text()

    def set_title(self, title: str) -> None:
        self._title.setText(title)
        self._title.setVisible(bool(title))

    def add_row(self, row: SettingRow) -> SettingRow:
        self._layout.addWidget(row)
        return row

    def add_widget(self, widget: QWidget, stretch: int = 0) -> QWidget:
        """放一个不是表单行的控件，如列表。"""
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)
