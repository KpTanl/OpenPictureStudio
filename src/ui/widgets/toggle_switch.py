"""开关与带标签的开关。

ToggleSwitch 自绘：滑轨关闭时用滑轨色、开启时用强调色，圆钮平滑移动；可点击、拖动或用键盘切换。
LabeledToggle 把开关与标题、说明组合成一行，点击整行即可切换。
"""

from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSize, Qt, pyqtProperty, pyqtSignal
from PyQt6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_SM, SPACING_XS, px
from ..theme import get_theme_tokens, to_qcolor

_FOCUS_MARGIN = 2.0


class ToggleSwitch(QAbstractButton):
    stateChanged = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None, width: int = 36, height: int = 20):
        super().__init__(parent)
        self._switch_width = width
        self._switch_height = height
        self._thumb_margin = 2.0
        self._handle_position = 0.0
        self._is_pressed = False
        self._is_dragging = False
        self._press_pos = QPointF(0, 0)
        self._drag_start_pos = 0.0

        self.setCheckable(True)
        super().setChecked(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(self.sizeHint())

        self._animation = QPropertyAnimation(self, b"handle_position", self)
        self._animation.setDuration(50)
        self._animation.setEasingCurve(QEasingCurve.Type.OutQuad)
        self.toggled.connect(self._on_toggled)

    @pyqtProperty(float)
    def handle_position(self) -> float:
        return self._handle_position

    @handle_position.setter
    def handle_position(self, pos: float) -> None:
        self._handle_position = max(0.0, min(1.0, float(pos)))
        self.update()

    def is_checked(self) -> bool:
        return self.isChecked()

    def setChecked(self, checked: bool) -> None:
        checked = bool(checked)
        if self.isChecked() != checked:
            super().setChecked(checked)

    def set_checked_silent(self, checked: bool) -> None:
        """程序回填：不带动画，也不发出 toggled 与 stateChanged。"""
        checked = bool(checked)
        self._animation.stop()
        self.blockSignals(True)
        super().setChecked(checked)
        self._handle_position = 1.0 if checked else 0.0
        self.blockSignals(False)
        self.update()

    def sizeHint(self) -> QSize:
        """滑轨四周留出焦点描边的位置。"""
        return QSize(int(self._switch_width + 2 * _FOCUS_MARGIN), int(self._switch_height + 2 * _FOCUS_MARGIN))

    def _travel(self) -> float:
        return max(1.0, self._switch_width - self._switch_height)

    def _animate_to(self, target: float) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._handle_position)
        self._animation.setEndValue(target)
        self._animation.start()

    def _on_toggled(self, checked: bool) -> None:
        self._animate_to(1.0 if checked else 0.0)
        self.stateChanged.emit(checked)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._is_pressed = True
            self._is_dragging = False
            self._press_pos = event.position()
            self._drag_start_pos = self._handle_position
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.update()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_pressed and self.isEnabled():
            dx = event.position().x() - self._press_pos.x()
            if abs(dx) > 3.0:
                self._is_dragging = True
            if self._is_dragging:
                self.handle_position = self._drag_start_pos + dx / self._travel()
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._is_pressed and self.isEnabled():
            self._is_pressed = False
            if self._is_dragging:
                self._is_dragging = False
                target = self._handle_position >= 0.5
                if self.isChecked() != target:
                    self.setChecked(target)
                else:
                    self._animate_to(1.0 if target else 0.0)
            else:
                self.toggle()
            self.update()
            event.accept()
        else:
            self._is_pressed = False
            self._is_dragging = False
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.toggle()
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.4)
        tokens = get_theme_tokens()
        position = self._handle_position
        track = QRectF(_FOCUS_MARGIN, _FOCUS_MARGIN, self._switch_width, self._switch_height)
        radius = self._switch_height / 2.0

        off, on = to_qcolor(tokens["track"]), to_qcolor(tokens["accent"])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(*(round(a + (b - a) * position)
                                  for a, b in zip(off.getRgb()[:3], on.getRgb()[:3]))))
        painter.drawRoundedRect(track, radius, radius)
        if self.hasFocus():
            painter.setPen(QPen(to_qcolor(tokens["accent"]), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(track.adjusted(-1.25, -1.25, 1.25, 1.25), radius + 1.25, radius + 1.25)

        diameter = self._switch_height - 2 * self._thumb_margin
        left = track.left() + self._thumb_margin + position * self._travel()
        painter.setPen(QPen(to_qcolor(tokens["knob_border"]), 1.0))
        painter.setBrush(to_qcolor(tokens["knob_pressed" if self._is_pressed else "knob"]))
        painter.drawEllipse(QRectF(left, track.top() + self._thumb_margin, diameter, diameter))
        painter.end()


class LabeledToggle(QWidget):
    """开关加标题与说明；点击整行切换。"""

    toggled = pyqtSignal(bool)
    stateChanged = pyqtSignal(bool)

    def __init__(self, title: str = "", description: str = "", checked: bool = False,
                 switch_on_left: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._switch = ToggleSwitch(self)
        self._switch.set_checked_silent(checked)
        self._title_label = QLabel(title, self)
        self._desc_label = QLabel(description, self)
        self._desc_label.setObjectName("toggleDescription")
        self._desc_label.setVisible(bool(description))

        text_layout = QVBoxLayout()
        text_layout.setSpacing(0)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.addWidget(self._title_label)
        text_layout.addWidget(self._desc_label)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, px(SPACING_XS), 0, px(SPACING_XS))
        layout.setSpacing(px(SPACING_SM))
        if switch_on_left:
            layout.addWidget(self._switch)
            layout.addLayout(text_layout, 1)
        else:
            layout.addLayout(text_layout, 1)
            layout.addWidget(self._switch)

        self._switch.toggled.connect(self.toggled.emit)
        self._switch.stateChanged.connect(self.stateChanged.emit)

    @property
    def switch(self) -> ToggleSwitch:
        return self._switch

    def isChecked(self) -> bool:
        return self._switch.isChecked()

    def isCheckable(self) -> bool:
        return True

    def setChecked(self, checked: bool) -> None:
        self._switch.setChecked(checked)

    def set_checked_silent(self, checked: bool) -> None:
        self._switch.set_checked_silent(checked)

    def toggle(self) -> None:
        self._switch.toggle()

    def text(self) -> str:
        return self._title_label.text()

    def setText(self, text: str) -> None:
        self._title_label.setText(text)

    def description(self) -> str:
        return self._desc_label.text()

    def setDescriptionWordWrap(self, wrap: bool) -> None:
        """说明较长、所在的栏又较窄时允许换行，避免把整行撑宽。"""
        self._desc_label.setWordWrap(wrap)

    def setDescription(self, desc: str) -> None:
        self._desc_label.setText(desc)
        self._desc_label.setVisible(bool(desc))

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self._switch.setEnabled(enabled)
        self._title_label.setEnabled(enabled)
        self._desc_label.setEnabled(enabled)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled() and self._switch.isEnabled():
            self._switch.toggle()
            event.accept()
        else:
            super().mousePressEvent(event)
