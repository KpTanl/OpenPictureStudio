"""复选框与单选按钮：自绘，取值在绘制时读取当前主题。

接口与 QCheckBox、QRadioButton 的常用部分一致；复选框支持三态。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PyQt6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from resources.styles.theme_variables import SPACING_SM, px
from ..theme import get_theme_tokens, to_qcolor

_MARK = 18
_HEIGHT = 24


class _ChoiceButton(QAbstractButton):
    """复选框与单选按钮共用的部分：悬停与按下状态、尺寸、文字和焦点描边。"""

    def __init__(self, text: str, parent: Optional[QWidget]):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._hovered = False
        self._pressed = False

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = False
            self.update()
        super().mouseReleaseEvent(event)

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        text_width = metrics.horizontalAdvance(self.text()) + px(SPACING_SM) if self.text() else 0
        return QSize(_MARK + text_width + 4, max(_HEIGHT, metrics.height() + 4))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _mark_rect(self) -> QRectF:
        return QRectF(1.0, (self.height() - _MARK) / 2.0, _MARK, _MARK)

    def _begin(self) -> tuple[QPainter, dict]:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.4)
        return painter, get_theme_tokens()

    def _outline(self, tokens: dict, selected: bool) -> QPen:
        """标记的描边：获得焦点或选中时用强调色，悬停时加深。"""
        if selected or self.hasFocus():
            return QPen(to_qcolor(tokens["accent_hover" if self._hovered or self._pressed else "accent"]), 1.5)
        return QPen(to_qcolor(tokens["text_secondary" if self._hovered else "text_caption"]), 1.5)

    def _draw_text(self, painter: QPainter, tokens: dict) -> None:
        if not self.text():
            return
        left = int(self._mark_rect().right() + px(SPACING_SM))
        painter.setPen(to_qcolor(tokens["text_primary"]))
        painter.setFont(self.font())
        painter.drawText(QRectF(left, 0, self.width() - left, self.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text())


class AppleCheckBox(_ChoiceButton):
    """复选框，支持三态。"""

    stateChanged = pyqtSignal(int)

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setObjectName("appleCheckBox")
        self._tristate = False
        self._check_state = Qt.CheckState.Unchecked

    def isTristate(self) -> bool:
        return self._tristate

    def setTristate(self, y: bool = True) -> None:
        self._tristate = y

    def checkState(self) -> Qt.CheckState:
        return self._check_state

    def setCheckState(self, state: Qt.CheckState) -> None:
        if self._check_state != state:
            self._check_state = state
            super().setChecked(state != Qt.CheckState.Unchecked)
            self.stateChanged.emit(state.value)
            self.update()

    def setChecked(self, checked: bool) -> None:
        self.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def nextCheckState(self) -> None:
        if not self._tristate:
            self.setChecked(not self.isChecked())
        elif self._check_state == Qt.CheckState.Unchecked:
            self.setCheckState(Qt.CheckState.PartiallyChecked)
        elif self._check_state == Qt.CheckState.PartiallyChecked:
            self.setCheckState(Qt.CheckState.Checked)
        else:
            self.setCheckState(Qt.CheckState.Unchecked)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.nextCheckState()
            self.clicked.emit(self.isChecked())
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter, tokens = self._begin()
        box = self._mark_rect().adjusted(0.75, 0.75, -0.75, -0.75)
        selected = self._check_state != Qt.CheckState.Unchecked
        painter.setPen(self._outline(tokens, selected))
        fill = "accent_hover" if self._hovered or self._pressed else "accent"
        painter.setBrush(to_qcolor(tokens[fill if selected else "page"]))
        painter.drawRoundedRect(box, 4.0, 4.0)

        if selected:
            mark = QPen(to_qcolor(tokens["on_accent"]), 1.9, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(mark)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            left, top = self._mark_rect().left(), self._mark_rect().top()
            if self._check_state == Qt.CheckState.Checked:
                path = QPainterPath()
                path.moveTo(left + 4.6, top + 9.3)
                path.lineTo(left + 7.6, top + 12.4)
                path.lineTo(left + 13.4, top + 5.8)
                painter.drawPath(path)
            else:
                painter.drawLine(QPointF(left + 5.0, top + _MARK / 2.0), QPointF(left + _MARK - 5.0, top + _MARK / 2.0))
        self._draw_text(painter, tokens)
        painter.end()


class AppleRadioButton(_ChoiceButton):
    """单选按钮；同一父控件内自动互斥。"""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setObjectName("appleRadioButton")
        self.setAutoExclusive(True)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.setChecked(True)
            self.clicked.emit(True)
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter, tokens = self._begin()
        circle = self._mark_rect().adjusted(0.75, 0.75, -0.75, -0.75)
        painter.setPen(self._outline(tokens, self.isChecked()))
        painter.setBrush(to_qcolor(tokens["page"]))
        painter.drawEllipse(circle)
        if self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(to_qcolor(tokens["accent_hover" if self._hovered or self._pressed else "accent"]))
            painter.drawEllipse(circle.center(), 4.0, 4.0)
        self._draw_text(painter, tokens)
        painter.end()
