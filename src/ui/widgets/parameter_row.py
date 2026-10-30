"""参数行：标签、读数、滑块与重置合一的数值参数控件。

上一行是标签和可键入的读数，下一行是滑块。数值偏离默认值时，标签与读数加重显示。
只有用户操作（拖动、键盘、键入、双击重置）才发出 value_edited；程序用 set_value 回填不触发。
编辑组跟踪使用 slider 的 sliderPressed、sliderReleased 与 isSliderDown。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QWidget

from resources.styles.theme_variables import SPACING_XS, px
from .apple_slider import AppleSlider
from .value_editing import format_slider_value, parse_slider_value
from .value_labels import ClickableLabel, EditableValueLabel


@dataclass(frozen=True)
class ParameterSpec:
    """一个数值参数在界面上的展示声明；范围与默认值应取自所属的参数契约。"""

    key: str
    label_key: str
    label_fallback: str
    minimum: int
    maximum: int
    default: int
    value_format: str = "int"
    track: tuple[str, ...] = ()


class ParameterRow(QWidget):
    value_edited = Signal(int)
    typing_finished = Signal()

    def __init__(self, spec: ParameterSpec, label: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.spec = spec
        self._label = ClickableLabel(label, self)
        self._label.setObjectName("parameterLabel")
        self.slider = AppleSlider(Qt.Orientation.Horizontal, self, default_value=spec.default)
        self.slider.setRange(spec.minimum, spec.maximum)
        self.slider.setValue(spec.default)
        if spec.minimum < spec.default < spec.maximum:
            self.slider.set_fill_origin(spec.default)
        self.slider.set_track_colors(spec.track)
        self.value_edit = EditableValueLabel(format_slider_value(spec.value_format, spec.default), self)

        layout = QGridLayout(self)
        layout.setContentsMargins(0, px(SPACING_XS), 0, 0)
        layout.setHorizontalSpacing(px(SPACING_XS))
        layout.setVerticalSpacing(0)
        layout.setColumnStretch(0, 1)
        layout.addWidget(self._label, 0, 0)
        layout.addWidget(self.value_edit, 0, 1)
        layout.addWidget(self.slider, 1, 0, 1, 2)

        self.slider.valueChanged.connect(self._project)
        self.slider.valueEdited.connect(self.value_edited)
        self.slider.resetRequested.connect(self._on_slider_reset)
        self.value_edit.editingFinished.connect(self._on_typed)
        self._label.double_clicked.connect(self.reset)
        self._project(spec.default)

    def value(self) -> int | float:
        return self._projected_value

    def set_value(self, value: int | float) -> None:
        """程序回填：只更新显示，不发出 value_edited。"""
        if value == self._projected_value and self.value_edit.hasFocus():
            return
        self.slider.setValue(int(round(value)))
        self._project(value)

    def reset(self) -> None:
        """用户重置为默认值。"""
        if self.slider.value() == self.spec.default and self._projected_value != self.spec.default:
            self._project(self.spec.default)
            self.value_edited.emit(self.spec.default)
        else:
            self.slider.edit_value(self.spec.default)

    def _on_slider_reset(self):
        if self.slider.value() == self.spec.default and self._projected_value != self.spec.default:
            self.reset()

    def set_label(self, text: str) -> None:
        self._label.setText(text)

    def label(self) -> str:
        return self._label.text()

    def is_modified(self) -> bool:
        return self._projected_value != self.spec.default

    def _project(self, value: int | float) -> None:
        self._projected_value = value
        self.value_edit.setText(format_slider_value(self.spec.value_format, value))
        modified = value != self.spec.default
        for widget in (self._label, self.value_edit):
            if widget.property("modified") != modified:
                widget.setProperty("modified", modified)
                widget.style().unpolish(widget)
                widget.style().polish(widget)

    def _on_typed(self) -> None:
        self.typing_finished.emit()
        if self.value_edit.text() == format_slider_value(self.spec.value_format, self._projected_value):
            return
        value = parse_slider_value(self.spec.value_format, self.value_edit.text(),
                                   self.slider.minimum(), self.slider.maximum())
        if value is None:
            self._project(self._projected_value)
        elif value != self.slider.value():
            self.slider.edit_value(value)
        elif value != self._projected_value:
            self._project(value)
            self.value_edited.emit(value)
        else:
            self._project(value)
