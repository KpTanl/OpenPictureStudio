"""悬停提示：一句说明，后面可带淡一档的快捷键。

外框与底色由全局样式表的 ``QToolTip`` 决定；快捷键的颜色在提示弹出时按当前主题计算，切换主题后无需重设。
"""

from __future__ import annotations

import html

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QToolTip, QWidget

from resources.styles.semantic_colors import mix
from ..theme import get_theme_tokens

_SHORTCUT_OPACITY = 0.65


class _ShortcutTip(QObject):
    """挂在控件上，接管它的悬停提示事件。"""

    def __init__(self, widget: QWidget):
        super().__init__(widget)
        self.text = ""
        self.shortcut = ""
        widget.installEventFilter(self)

    def rich_text(self) -> str:
        tokens = get_theme_tokens()
        dim = mix(tokens["text_primary"], tokens["surface"], _SHORTCUT_OPACITY)
        return (f"<p style='white-space:pre; margin:0'>{html.escape(self.text)}"
                f"   <span style='color:{dim}'>{html.escape(self.shortcut)}</span></p>")

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.ToolTip or not self.shortcut:
            return False
        QToolTip.showText(event.globalPos(), self.rich_text(), watched)
        return True


def set_tooltip(widget: QWidget, text: str, shortcut: str = "") -> None:
    """给控件设置悬停提示；shortcut 是快捷键的显示文字（如 "R"、"Ctrl+Z"），显示在说明之后。"""
    tip = widget.findChild(_ShortcutTip, options=Qt.FindChildOption.FindDirectChildrenOnly)
    if tip is None and shortcut:
        tip = _ShortcutTip(widget)
    if tip is not None:
        tip.text, tip.shortcut = text, shortcut
    widget.setToolTip(f"{text}  {shortcut}" if shortcut else text)
