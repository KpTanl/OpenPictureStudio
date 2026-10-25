"""步进器：减、数值、加排成一行的整数输入，接口与 QSpinBox 一致。

只有用户操作（点加减、键入、方向键）才发出 valueEdited，程序用 setValue 回填不触发。
"""

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFocusEvent, QKeyEvent
from PyQt6.QtWidgets import QPushButton, QSpinBox

from resources.styles.theme_variables import CONTROL_HEIGHT
from ..icons import themed_icon
from .value_editing import ValueEditMixin

_BUTTON_WIDTH = 30
_ICON = 16


class AppleSpinBox(QSpinBox, ValueEditMixin):
    valueEdited = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("appleStepper")
        self.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(CONTROL_HEIGHT)
        self._text_edit_value = None
        self.lineEdit().textEdited.connect(self._begin_text_edit)
        self.lineEdit().textChanged.connect(self._finish_text_edit)

        self._minus = self._step_button("minus", -1)
        self._plus = self._step_button("plus", 1)
        self.valueChanged.connect(self._sync_buttons)
        self._sync_buttons()

    def _step_button(self, icon: str, steps: int) -> QPushButton:
        button = QPushButton(self)
        button.setObjectName("stepperButton")
        button.setIcon(themed_icon(icon))
        button.setIconSize(QSize(_ICON, _ICON))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 放在对话框里时不能成为默认按钮，否则按回车会触发加减
        button.setAutoDefault(False)
        button.setAutoRepeat(True)
        button.clicked.connect(lambda: self.stepBy(steps))
        return button

    def _sync_buttons(self) -> None:
        self._minus.setEnabled(self.isEnabled() and (self.wrapping() or self.value() > self.minimum()))
        self._plus.setEnabled(self.isEnabled() and (self.wrapping() or self.value() < self.maximum()))

    def setRange(self, minimum: int, maximum: int) -> None:
        super().setRange(minimum, maximum)
        self._sync_buttons()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == event.Type.EnabledChange:
            self._sync_buttons()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._minus.setGeometry(1, 1, _BUTTON_WIDTH, self.height() - 2)
        self._plus.setGeometry(self.width() - _BUTTON_WIDTH - 1, 1, _BUTTON_WIDTH, self.height() - 2)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(hint.width() + 2 * _BUTTON_WIDTH, max(hint.height(), CONTROL_HEIGHT))

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(hint.width() + 2 * _BUTTON_WIDTH, max(hint.height(), CONTROL_HEIGHT))

    def _begin_text_edit(self, _text: str) -> None:
        self._text_edit_value = self.value()

    def _finish_text_edit(self, _text: str) -> None:
        previous = self._text_edit_value
        self._text_edit_value = None
        if previous is not None and self.keyboardTracking():
            self._emit_value_edited(previous)

    def stepBy(self, steps: int) -> None:
        previous = self.value()
        super().stepBy(steps)
        self._emit_value_edited(previous)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        previous = self.value()
        super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._emit_value_edited(previous)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        previous = self.value()
        super().focusOutEvent(event)
        self._emit_value_edited(previous)
