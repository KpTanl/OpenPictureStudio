"""进度与占用条：一根细条加下面的一行说明。

用于进度对话框和设置里的缓存占用。maximum 为 0 时表示正在进行但无法量化，细条显示忙碌状态。
外观来自全局样式表里不显示文字的进度条。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QProgressBar, QSizePolicy, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_XS, px


class UsageBar(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_XS))
        self._bar = QProgressBar(self)
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 1)
        self._bar.setValue(0)
        layout.addWidget(self._bar)
        self._label = QLabel(self)
        self._label.setObjectName("usageBarText")
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        self._label.setWordWrap(True)
        policy = self._label.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self._label.setSizePolicy(policy)
        layout.addWidget(self._label)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def text(self) -> str:
        return self._label.text()

    def value(self) -> int:
        return self._bar.value()

    def maximum(self) -> int:
        return self._bar.maximum()

    def set_text(self, text: str) -> None:
        self._label.setText(text)
        self._label.setVisible(bool(text))

    def set_progress(self, value: int, maximum: int) -> None:
        """maximum 为 0 表示忙碌状态；否则显示 value / maximum。"""
        maximum = max(0, int(maximum))
        self._bar.setRange(0, maximum)
        self._bar.setValue(min(max(0, int(value)), maximum) if maximum else 0)
