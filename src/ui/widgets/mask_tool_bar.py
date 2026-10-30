"""画布下沿的蒙版工具条：在照片上新建区域时说明怎么操作；涂抹时在这里调画笔，点“完成”结束。

只展示与发出请求：画笔设置由使用方经 ``show_tool()`` 回填，用户改动发出 ``settings_edited``，
点按钮发出 ``finish_requested``。外观在全局样式表里，阴影按主题取色。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Signal
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_MD, SPACING_SM, px
from ..icons import themed_icon
from ..theme import get_theme_tokens, to_qcolor
from .apple_button import AppleButton
from .parameter_row import ParameterRow, ParameterSpec

# 画笔设置在界面上的刻度：半径以照片短边的千分之一为单位，硬度与不透明度以百分之一为单位
_BRUSH_ROWS = (("radius", 1, 500, 30, "percent_tenths", 1000), ("hardness", 0, 100, 50, "percent", 100),
               ("opacity", 0, 100, 100, "percent", 100))
_ICONS = {"brush": "brush", "erase": "eraser"}


class MaskToolBar(QFrame):
    settings_edited = Signal(object)
    finish_requested = Signal()

    def __init__(self, tr, parent=None):
        super().__init__(parent)
        self.setObjectName("maskToolBar")
        self._tr, self._kind, self._operation, self._settings = tr, "brush", "add", {}
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(16)
        self._shadow.setOffset(0, 4)
        self.setGraphicsEffect(self._shadow)
        self._sync_shadow()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_MD), px(SPACING_SM), px(SPACING_SM), px(SPACING_SM))
        layout.setSpacing(px(SPACING_SM))
        head = QHBoxLayout()
        head.setSpacing(px(SPACING_SM))
        self._icon = QLabel(self)
        self._title = QLabel(self)
        self._title.setObjectName("maskToolTitle")
        self._hint = QLabel(self)
        self._hint.setObjectName("maskCaption")
        self._button = AppleButton(parent=self)
        self._button.clicked.connect(self.finish_requested.emit)
        head.addWidget(self._icon)
        head.addWidget(self._title)
        head.addWidget(self._hint, 1)
        head.addWidget(self._button)
        layout.addLayout(head)
        self._brush = QWidget(self)
        brush = QHBoxLayout(self._brush)
        brush.setContentsMargins(0, 0, 0, 0)
        brush.setSpacing(px(SPACING_MD))
        self._rows = {}
        for key, minimum, maximum, default, value_format, _scale in _BRUSH_ROWS:
            row = ParameterRow(ParameterSpec(key, "mask.brush." + key, "", minimum, maximum, default, value_format), "", self._brush)
            row.value_edited.connect(lambda value, k=key: self._edited(k, value))
            brush.addWidget(row, 1)
            self._rows[key] = row
        layout.addWidget(self._brush)
        self.retranslate_ui()

    def show_tool(self, kind, operation, settings):
        """新建区域开始：kind 为 brush、linear 或 radial；画笔时回填当前的画笔设置。"""
        self._kind, self._operation, self._settings = kind, operation, dict(settings)
        for key, *_rest, scale in _BRUSH_ROWS:
            self._rows[key].set_value(round(self._settings[key] * scale))
        self._brush.setVisible(kind == "brush")
        self.retranslate_ui()
        self.show()

    def _tool(self):
        return "erase" if self._kind == "brush" and self._operation == "subtract" else self._kind

    def _edited(self, key, value):
        scale = next(row[-1] for row in _BRUSH_ROWS if row[0] == key)
        self._settings[key] = value / scale
        self.settings_edited.emit(dict(self._settings))

    def retranslate_ui(self):
        tool = self._tool()
        icon = _ICONS.get(tool)
        self._icon.setVisible(icon is not None)
        if icon is not None:
            self._icon.setPixmap(themed_icon(icon).pixmap(QSize(16, 16)))
        self._title.setText(self._tr("mask.type." + tool))
        self._hint.setText(self._tr("mask.hint." + tool))
        self._button.setText(self._tr("mask.done" if self._kind == "brush" else "common.cancel"))
        for row in self._rows.values():
            row.set_label(self._tr(row.spec.label_key))

    def _sync_shadow(self):
        self._shadow.setColor(to_qcolor(get_theme_tokens()["shadow"]))

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.StyleChange:
            self._sync_shadow()
            if self._icon.isVisible():
                self.retranslate_ui()
        super().changeEvent(event)
