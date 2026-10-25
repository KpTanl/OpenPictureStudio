"""轻提示：一句话消息，数秒后自动消失；不承载需要操作的内容，也不拦截鼠标。

外观由全局样式表按对象名决定，与界面反色。位置由调用方安排。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QLabel, QWidget

DEFAULT_DURATION_MS = 3000


class Toast(QLabel):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, text: str, duration_ms: int = DEFAULT_DURATION_MS) -> None:
        """显示一句消息；新消息替换尚未消失的旧消息。时长不大于零时一直显示到被替换或收起。"""
        if not text:
            self.dismiss()
            return
        self.setText(text)
        self.adjustSize()
        self.show()
        self.raise_()
        if duration_ms > 0:
            self._timer.start(int(duration_ms))
        else:
            self._timer.stop()

    def dismiss(self) -> None:
        self._timer.stop()
        self.hide()
