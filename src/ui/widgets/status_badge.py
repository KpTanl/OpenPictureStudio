"""状态标记：描边胶囊里的一小段状态文字，成功与警告带图标。

四种状态：success 成功、warning 警告、pending 未就绪、active 进行中。自绘，取值在绘制时读取当前主题。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from resources.styles.theme_variables import FONT_SIZE_CAPTION, SPACING_SM, SPACING_XS, px
from ..icons import themed_icon
from ..theme import get_theme_tokens, to_qcolor

# 状态 -> (语义取值的键, 图标名)
_KINDS = {
    "success": ("success", "check"),
    "warning": ("warning", "alert-triangle"),
    "pending": ("text_caption", ""),
    "active": ("accent", ""),
}
_HEIGHT = 20
_ICON_SIZE = 12


class StatusBadge(QWidget):
    def __init__(self, kind: str = "pending", text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._kind = ""
        self._text = ""
        self.set_status(kind, text)

    def kind(self) -> str:
        return self._kind

    def text(self) -> str:
        return self._text

    def set_status(self, kind: str, text: str) -> None:
        if kind not in _KINDS:
            raise ValueError(f"未知的状态: {kind!r}")
        if (kind, text) != (self._kind, self._text):
            self._kind, self._text = kind, text
            self.setToolTip(text)
            self.setAccessibleName(text)
            self.updateGeometry()
            self.update()

    def _font(self) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(px(FONT_SIZE_CAPTION))
        return font

    def _icon_width(self) -> int:
        return _ICON_SIZE + px(SPACING_XS) if _KINDS[self._kind][1] else 0

    def sizeHint(self) -> QSize:
        text_width = QFontMetrics(self._font()).horizontalAdvance(self._text)
        return QSize(text_width + self._icon_width() + 2 * px(SPACING_SM), _HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return QSize(self._icon_width() + 2 * px(SPACING_SM) + QFontMetrics(self._font()).horizontalAdvance("…"), _HEIGHT)

    def paintEvent(self, event: QPaintEvent) -> None:
        role, icon_name = _KINDS[self._kind]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        color = to_qcolor(get_theme_tokens()[role])
        painter.setPen(QPen(color, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        outline = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.drawRoundedRect(outline, outline.height() / 2.0, outline.height() / 2.0)

        left = px(SPACING_SM)
        if icon_name:
            themed_icon(icon_name, role=role).paint(
                painter, QRect(left, (self.height() - _ICON_SIZE) // 2, _ICON_SIZE, _ICON_SIZE))
            left += self._icon_width()
        painter.setPen(color)
        painter.setFont(self._font())
        width = max(0, self.width() - left - px(SPACING_SM))
        text = QFontMetrics(self._font()).elidedText(self._text, Qt.TextElideMode.ElideRight, width)
        painter.drawText(QRect(left, 0, width, self.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        painter.end()
