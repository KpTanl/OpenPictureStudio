"""历史列表：一条时间线上的各步，新的在上；当前所在的一步突出显示，其后已撤销的各步减淡。

自绘，取值在绘制时读取当前主题。点一行、或在键盘光标所在的行按回车或空格，只发出 ``step_requested``；
程序用 ``set_steps()`` 回填不发出。当前这一步点了不发出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from PyQt6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from resources.styles.theme_variables import (CONTROL_HEIGHT, FONT_SIZE_BODY, FONT_SIZE_CAPTION, RADIUS_CONTROL,
                                              SPACING_MD, SPACING_SM, SPACING_XS, px)
from ..theme import get_theme_tokens, to_qcolor

_ROW_HEIGHT = CONTROL_HEIGHT + px(SPACING_XS)
_DOT_RADIUS = 3.0
_CURRENT_DOT_RADIUS = 4.5
_STATES = ("past", "current", "undone")


@dataclass(frozen=True)
class HistoryStep:
    """列表里的一步。state：past 已经做过、current 当前所在、undone 已撤销（可以重做）。"""

    key: str
    text: str
    note: str = ""
    state: str = "past"

    def __post_init__(self):
        if self.state not in _STATES:
            raise ValueError(f"未知的历史状态: {self.state!r}")


class HistoryList(QWidget):
    step_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        # 自旧到新；显示时最新的一步在最上面
        self._steps: tuple[HistoryStep, ...] = ()
        self._hovered = -1
        self._pressed = -1
        self._cursor = -1
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def steps(self) -> tuple[HistoryStep, ...]:
        return self._steps

    def set_steps(self, steps: Sequence[HistoryStep]) -> None:
        """程序回填，自旧到新。键盘光标回到当前这一步。"""
        steps = tuple(steps)
        if steps == self._steps:
            return
        self._steps = steps
        self._hovered = self._pressed = -1
        self._cursor = self._row_of_current()
        self.updateGeometry()
        self.update()

    def current_rect(self) -> QRect:
        """当前这一步所在的那一行；没有时为空矩形。宿主据此把它滚进视口。"""
        row = self._row_of_current()
        return self._row_rect(row) if row >= 0 else QRect()

    def sizeHint(self) -> QSize:
        return QSize(200, _ROW_HEIGHT * len(self._steps))

    def minimumSizeHint(self) -> QSize:
        return QSize(120, _ROW_HEIGHT * len(self._steps))

    # 行号自上而下，第 0 行是最新的一步
    def _step_at(self, row: int) -> HistoryStep:
        return self._steps[len(self._steps) - 1 - row]

    def _row_of_current(self) -> int:
        return next((row for row in range(len(self._steps)) if self._step_at(row).state == "current"), -1)

    def _row_rect(self, row: int) -> QRect:
        return QRect(0, row * _ROW_HEIGHT, self.width(), _ROW_HEIGHT)

    def _row_at(self, position) -> int:
        row = int(position.y()) // _ROW_HEIGHT
        return row if 0 <= row < len(self._steps) and 0 <= position.x() < self.width() else -1

    def _activate(self, row: int) -> None:
        if 0 <= row < len(self._steps) and self._step_at(row).state != "current":
            self.step_requested.emit(self._step_at(row).key)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        row = self._row_at(event.position())
        if row != self._hovered:
            self._hovered = row
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hovered = self._pressed = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = self._row_at(event.position())
            if self._pressed >= 0:
                self._cursor = self._pressed
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pressed, self._pressed = self._pressed, -1
            self.update()
            if pressed >= 0 and pressed == self._row_at(event.position()):
                self._activate(pressed)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and self._steps:
            step = -1 if key == Qt.Key.Key_Up else 1
            self._cursor = max(0, min(len(self._steps) - 1, self._cursor + step))
            self.update()
            event.accept()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and self._cursor >= 0:
            self._activate(self._cursor)
            event.accept()
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.update()
        super().focusOutEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        tokens = get_theme_tokens()
        radius = px(RADIUS_CONTROL)
        axis = px(SPACING_MD) + _CURRENT_DOT_RADIUS
        text_left = int(axis + _CURRENT_DOT_RADIUS) + px(SPACING_SM)
        enabled = self.isEnabled()

        # 时间线：从最上面一步的圆点连到最下面一步的圆点
        if len(self._steps) > 1:
            painter.setPen(QPen(to_qcolor(tokens["hairline_solid"]), 1.0))
            painter.drawLine(QPointF(axis, _ROW_HEIGHT / 2.0),
                             QPointF(axis, (len(self._steps) - 0.5) * _ROW_HEIGHT))

        for row in range(len(self._steps)):
            bounds = self._row_rect(row)
            if not bounds.intersects(event.rect()):
                continue
            step = self._step_at(row)
            current = step.state == "current"
            undone = step.state == "undone"
            frame = QRectF(bounds).adjusted(0, 1, 0, -1)

            background = ("well" if row == self._pressed and not current
                          else "control" if current or row == self._hovered else "")
            if background and enabled:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(to_qcolor(tokens[background]))
                painter.drawRoundedRect(frame, radius, radius)
            if row == self._cursor and self.hasFocus():
                painter.setPen(QPen(to_qcolor(tokens["accent"]), 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(frame.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)

            center = QPointF(axis, bounds.center().y() + 0.5)
            if current:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(to_qcolor(tokens["accent" if enabled else "text_disabled"]))
                painter.drawEllipse(center, _CURRENT_DOT_RADIUS, _CURRENT_DOT_RADIUS)
            elif undone:
                # 空心：这一步现在不在画面里
                painter.setPen(QPen(to_qcolor(tokens["text_disabled"]), 1.0))
                painter.setBrush(to_qcolor(tokens["surface"]))
                painter.drawEllipse(center, _DOT_RADIUS, _DOT_RADIUS)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(to_qcolor(tokens["text_caption" if enabled else "text_disabled"]))
                painter.drawEllipse(center, _DOT_RADIUS, _DOT_RADIUS)

            right = bounds.right() - px(SPACING_MD)
            if step.note:
                caption = QFont(self.font())
                caption.setPixelSize(px(FONT_SIZE_CAPTION))
                painter.setFont(caption)
                painter.setPen(to_qcolor(tokens["text_caption" if enabled and not undone else "text_disabled"]))
                width = painter.fontMetrics().horizontalAdvance(step.note)
                painter.drawText(QRect(right - width, bounds.top(), width, bounds.height()),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, step.note)
                right -= width + px(SPACING_SM)

            font = QFont(self.font())
            font.setPixelSize(px(FONT_SIZE_BODY))
            font.setWeight(QFont.Weight.DemiBold if current else QFont.Weight.Normal)
            painter.setFont(font)
            role = ("text_disabled" if not enabled else "text_primary" if current
                    else "text_caption" if undone else "text_secondary")
            painter.setPen(to_qcolor(tokens[role]))
            available = max(0, right - text_left)
            text = painter.fontMetrics().elidedText(step.text, Qt.TextElideMode.ElideRight, available)
            painter.drawText(QRect(text_left, bounds.top(), available, bounds.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        painter.end()
