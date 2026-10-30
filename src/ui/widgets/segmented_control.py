"""分段开关：在凹槽里并排的互斥选项，选中项浮起。

用于工作区切换、“适应 / 1:1”、侧栏标签与模式选择；支持方向键切换，可与 QStackedWidget 联动。
外观在绘制时读取当前主题的语义取值。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence
from PySide6.QtCore import (
    QEvent,
    QPoint,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QStackedWidget, QWidget

from resources.styles.theme_variables import (CONTROL_HEIGHT, FONT_SIZE_BODY, RADIUS_CONTROL, RADIUS_SMALL,
                                              SPACING_LG, px)
from ..theme import get_theme_tokens

_INSET = 3.0


class AppleSegmentedControl(QWidget):
    """分段开关。"""

    currentIndexChanged = Signal(int)
    currentTextChanged = Signal(str)
    activated = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("appleSegmentedControl")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(CONTROL_HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self._segments: list[dict[str, Any]] = []
        self._current_index: int = 0
        self._hover_index: int = -1
        self._stacked_widget: Optional[QStackedWidget] = None

    def add_segment(self, text: str, data: Any = None, icon: Optional[QIcon] = None, tooltip: Optional[str] = None) -> None:
        self._segments.append({
            "text": text,
            "data": data,
            "icon": icon,
            "tooltip": tooltip,
        })
        self.update()

    def add_segments(self, texts: Sequence[str]) -> None:
        for t in texts:
            self.add_segment(t)

    def set_segment_text(self, index: int, text: str) -> None:
        if 0 <= index < len(self._segments):
            self._segments[index]["text"] = text
            self.update()

    def clear(self) -> None:
        self._segments.clear()
        self._current_index = 0
        self.update()

    def count(self) -> int:
        return len(self._segments)

    def currentIndex(self) -> int:
        return self._current_index

    def currentText(self) -> str:
        if 0 <= self._current_index < len(self._segments):
            return self._segments[self._current_index]["text"]
        return ""

    def currentData(self) -> Any:
        if 0 <= self._current_index < len(self._segments):
            return self._segments[self._current_index]["data"]
        return None

    def setCurrentIndex(self, index: int) -> None:
        if index < 0 or index >= len(self._segments):
            return
        if self._current_index != index:
            self._current_index = index
            self.update()
            self.currentIndexChanged.emit(index)
            self.currentTextChanged.emit(self._segments[index]["text"])
            if self._stacked_widget is not None and index < self._stacked_widget.count():
                self._stacked_widget.setCurrentIndex(index)

    def clear_selection(self) -> None:
        """取消选中，表示当前状态不属于任何一段；不发出信号。"""
        if self._current_index != -1:
            self._current_index = -1
            self.update()

    def setCurrentText(self, text: str) -> None:
        for i, seg in enumerate(self._segments):
            if seg["text"] == text:
                self.setCurrentIndex(i)
                break

    def bind_stacked_widget(self, stacked_widget: QStackedWidget) -> None:
        """绑定关联的 QStackedWidget，实现类似 macOS 首选项的分段导航。"""
        self._stacked_widget = stacked_widget
        self.currentIndexChanged.connect(stacked_widget.setCurrentIndex)
        if 0 <= self._current_index < stacked_widget.count():
            stacked_widget.setCurrentIndex(self._current_index)

    def _segment_rect(self, index: int) -> QRectF:
        n = len(self._segments)
        if n == 0:
            return QRectF()
        w = (self.width() - 2 * _INSET) / n
        return QRectF(_INSET + index * w, _INSET, w, self.height() - 2 * _INSET)

    def _index_at(self, pos: QPoint) -> int:
        n = len(self._segments)
        if n == 0 or not self.rect().contains(pos):
            return -1
        w = (self.width() - 2 * _INSET) / n
        idx = int((pos.x() - _INSET) / w)
        return max(0, min(n - 1, idx))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self._index_at(event.pos())
            if idx >= 0:
                self.setCurrentIndex(idx)
                self.activated.emit(idx)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        new_hover = self._index_at(event.pos())
        if new_hover != self._hover_index:
            self._hover_index = new_hover
            if 0 <= new_hover < len(self._segments):
                tip = self._segments[new_hover].get("tooltip")
                if tip:
                    self.setToolTip(tip)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if self._hover_index != -1:
            self._hover_index = -1
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            if self._current_index > 0:
                index = self._current_index - 1
                self.setCurrentIndex(index)
                self.activated.emit(index)
                event.accept()
                return
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            if self._current_index < len(self._segments) - 1:
                index = self._current_index + 1
                self.setCurrentIndex(index)
                self.activated.emit(index)
                event.accept()
                return
        super().keyPressEvent(event)

    def _text_font(self, selected: bool):
        font = self.font()
        font.setPixelSize(px(FONT_SIZE_BODY))
        font.setWeight(font.Weight.DemiBold if selected else font.Weight.Normal)
        return font

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self._text_font(True))
        widest = max((metrics.horizontalAdvance(seg["text"]) for seg in self._segments), default=0)
        segment = widest + 2 * px(SPACING_LG)
        return QSize(int(len(self._segments) * segment + 2 * _INSET), CONTROL_HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def focusInEvent(self, event) -> None:
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.update()
        super().focusOutEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = get_theme_tokens()
        enabled = self.isEnabled()

        track_rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(t["accent"] if self.hasFocus() else t["hairline_solid"]), 1.0))
        painter.setBrush(QColor(t["well"]))
        painter.drawRoundedRect(track_rect, px(RADIUS_CONTROL), px(RADIUS_CONTROL))

        for index, segment in enumerate(self._segments):
            rect = self._segment_rect(index)
            selected = index == self._current_index
            if selected or (index == self._hover_index and enabled):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(t["segment_selected" if selected else "control"]))
                painter.drawRoundedRect(rect, px(RADIUS_SMALL), px(RADIUS_SMALL))
            role = "text_disabled" if not enabled else "text_primary" if selected else "text_secondary"
            painter.setFont(self._text_font(selected))
            painter.setPen(QColor(t[role]))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, segment["text"])
        painter.end()
