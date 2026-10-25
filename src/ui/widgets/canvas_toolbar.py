"""底部工具条：浮在画布下沿的一条胶囊，放缩放、“适应 / 1:1”、旋转与翻转或按住对比、评分和胶片条开关。

只接收动作与状态，通过信号表达用户意图，不持有主窗口或画布。``set_*`` 都是程序回填，不发出信号。
位置由调用方安排。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from PyQt6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QKeyEvent
from PyQt6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QWidget

from resources.styles.theme_variables import CANVAS_TOOLBAR_HEIGHT, SPACING_MD, SPACING_SM, SPACING_XS, px
from ..icons import themed_icon
from ..theme import get_theme_tokens, to_qcolor
from .action_button import ActionIconButton
from .apple_button import AppleCompactButton, AppleIconButton
from ...application.studio_preview_cache import CanvasPreviewState
from .apple_menu import AppleMenu
from .segmented_control import AppleSegmentedControl
from .star_rating import StarRating
from .tooltip import set_tooltip

VIEWER, EDITOR = 0, 1
MIN_ZOOM_PERCENT, MAX_ZOOM_PERCENT = 5, 500
_FIT, _ACTUAL = 0, 1
_ICON_SIZE = 20
_DETAIL_DELAY_MS = 300


@dataclass(frozen=True)
class CanvasToolbarActions:
    zoom_out: QAction
    zoom_in: QAction
    zoom_fit: QAction
    zoom_actual: QAction
    rotations: Sequence[QAction]  # 第一个是按钮直接触发的，其余在右键菜单里
    flips: Sequence[QAction]


class _ZoomReadout(QLineEdit):
    """倍率读数：平时像标签，点击后键入百分比，回车提交，Esc 或移开焦点放弃。"""

    percent_edited = pyqtSignal(int)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("canvasZoomReadout")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedWidth(56)
        self._percent = 100
        self.returnPressed.connect(self._commit)
        self._show_percent()

    def percent(self) -> int:
        return self._percent

    def set_percent(self, percent: int) -> None:
        self._percent = int(percent)
        if not self.hasFocus():
            self._show_percent()

    def _show_percent(self) -> None:
        self.setText(f"{self._percent}%")

    def _commit(self) -> None:
        text = self.text().strip().rstrip("%").strip()
        self.clearFocus()
        if text.isdigit():
            self.percent_edited.emit(max(MIN_ZOOM_PERCENT, min(MAX_ZOOM_PERCENT, int(text))))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self._show_percent()


def _separator(parent: QWidget) -> QFrame:
    line = QFrame(parent)
    line.setObjectName("canvasToolbarSeparator")
    line.setFixedSize(1, 20)
    return line


class _MenuActionButton(ActionIconButton):
    """直接点击触发第一个动作；右键列出同组的全部动作。"""

    def __init__(self, actions: Sequence[QAction], icon_name: str, parent: QWidget):
        super().__init__(actions[0], icon_name, parent)
        self._menu = AppleMenu(self)
        for action in actions:
            self._menu.addAction(action)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda position: self._menu.popup(self.mapToGlobal(position)))

    def menu_actions(self) -> list[QAction]:
        return self._menu.actions()


class CanvasToolbar(QFrame):
    zoom_percent_edited = pyqtSignal(int)
    rating_edited = pyqtSignal(int)
    filmstrip_toggled = pyqtSignal(bool)
    compare_pressed = pyqtSignal()
    compare_released = pyqtSignal()
    preview_mode_selected = pyqtSignal(str)

    def __init__(self, actions: CanvasToolbarActions, translate: Callable[[str, str], str],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._actions = actions
        self._translate = translate
        self._mode = VIEWER
        self._compare_hint = ""
        self._detail_state = ""
        self._preview_state = CanvasPreviewState()
        self._preview_available_width = 1000
        self._detail_error_key = ""
        self._detail_shown = False
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(_DETAIL_DELAY_MS)
        self._detail_timer.timeout.connect(self._show_detail_status)
        self.setObjectName("canvasToolbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(CANVAS_TOOLBAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(16)
        self._shadow.setOffset(0, 4)
        self.setGraphicsEffect(self._shadow)
        self._sync_shadow()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(SPACING_MD), 0, px(SPACING_MD), 0)
        layout.setSpacing(px(SPACING_XS))

        layout.addWidget(ActionIconButton(actions.zoom_out, "minus", self))
        self._readout = _ZoomReadout(self)
        self._readout.percent_edited.connect(self.zoom_percent_edited.emit)
        layout.addWidget(self._readout)
        self._detail_status = QLabel(self)
        self._detail_status.setFixedSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self._detail_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail_status.hide()
        layout.addWidget(self._detail_status)
        self._preview_label = AppleCompactButton(parent=self)
        self._preview_label.setAutoDefault(False)
        self._preview_label.setFixedWidth(180)
        self._preview_label.clicked.connect(self._open_preview_menu)
        layout.addWidget(ActionIconButton(actions.zoom_in, "plus", self))
        layout.addWidget(self._preview_label)
        self._add_separator(layout)

        self._fit_switch = AppleSegmentedControl(self)
        self._fit_switch.add_segments(["", ""])
        self._fit_switch.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._fit_switch.activated.connect(self._on_fit_switch)
        layout.addWidget(self._fit_switch)
        self._add_separator(layout)

        self._rotate_button = _MenuActionButton(actions.rotations, "rotate-clockwise", self)
        self._flip_button = _MenuActionButton(actions.flips, "flip-horizontal", self)
        self._compare_button = AppleIconButton(themed_icon("eye"), parent=self)
        self._compare_button.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self._compare_button.pressed.connect(self.compare_pressed.emit)
        self._compare_button.released.connect(self.compare_released.emit)
        for button in (self._rotate_button, self._flip_button, self._compare_button):
            layout.addWidget(button)
        self._add_separator(layout)

        self._stars = StarRating(self)
        self._stars.rating_edited.connect(self.rating_edited.emit)
        layout.addWidget(self._stars)
        self._add_separator(layout)

        self._filmstrip_button = AppleIconButton(themed_icon("layout-bottombar", checked_role="accent"), parent=self)
        self._filmstrip_button.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self._filmstrip_button.setCheckable(True)
        self._filmstrip_button.setChecked(True)
        self._filmstrip_button.clicked.connect(self.filmstrip_toggled.emit)
        layout.addWidget(self._filmstrip_button)

        self.retranslate_ui()
        self.set_mode(VIEWER)

    def _add_separator(self, layout: QHBoxLayout) -> None:
        layout.addSpacing(px(SPACING_SM))
        layout.addWidget(_separator(self))
        layout.addSpacing(px(SPACING_SM))

    def mode(self) -> int:
        return self._mode

    def set_mode(self, mode: int) -> None:
        """看图显示旋转与翻转，编辑显示按住对比。"""
        if mode not in (VIEWER, EDITOR):
            raise ValueError(f"未知的工作区: {mode!r}")
        self._mode = mode
        self._rotate_button.setVisible(mode == VIEWER)
        self._flip_button.setVisible(mode == VIEWER)
        self._compare_button.setVisible(mode == EDITOR)
        policy = self._detail_status.sizePolicy()
        policy.setRetainSizeWhenHidden(mode == EDITOR)
        self._detail_status.setSizePolicy(policy)
        self._sync_detail_status()
        self._sync_preview_state()

    def zoom_percent(self) -> int:
        return self._readout.percent()

    def set_preview_state(self, state):
        self._preview_state = state
        self._sync_preview_state()

    def set_preview_available_width(self, width):
        self._preview_available_width = width
        self._sync_preview_state()

    def _sync_preview_state(self):
        self._preview_label.setVisible(self._mode == EDITOR)
        state = self._preview_state
        kind = self._translate("canvas.preview.state." + state.kind, state.kind)
        text = kind
        if state.size is not None:
            text += " · " + "×".join(map(str, state.size))
        narrow = self._preview_available_width < 600
        compact = self._preview_available_width < 800
        width = 28 if narrow else (100 if compact else 180)
        self._preview_label.setIcon(themed_icon("photo") if narrow else QIcon())
        self._preview_label.setFixedWidth(width)
        visible_text = "" if narrow else (kind if compact else text)
        self._preview_label.setText(self._preview_label.fontMetrics().elidedText(visible_text, Qt.TextElideMode.ElideRight, width - 20))
        self._preview_label.setAccessibleName(text)
        tip = text
        if state.input_size is not None and state.input_is_proxy is not None and not state.kind.startswith("original"):
            key = "canvas.preview.compute.proxy" if state.input_is_proxy else "canvas.preview.compute.full"
            tip += "\n" + self._translate(key, key).replace("{width}", str(state.input_size[0])).replace(
                "{height}", str(state.input_size[1]))
        set_tooltip(self._preview_label, tip)

    def _open_preview_menu(self):
        menu = AppleMenu(self)
        for mode in ("auto", "full", "preview1600"):
            action = menu.addAction(self._translate("canvas.preview.mode." + mode, mode))
            action.setCheckable(True)
            action.setChecked(mode == self._preview_state.mode)
            action.triggered.connect(lambda _checked=False, value=mode: self.preview_mode_selected.emit(value))
        menu.exec(self._preview_label.mapToGlobal(QPoint(0, self._preview_label.height())))
        menu.deleteLater()

    def set_zoom_state(self, percent: int, fit: bool) -> None:
        self._readout.set_percent(percent)
        if fit:
            self._fit_switch.setCurrentIndex(_FIT)
        elif percent == 100:
            self._fit_switch.setCurrentIndex(_ACTUAL)
        else:
            self._fit_switch.clear_selection()

    def rating(self) -> int:
        return self._stars.rating()

    def set_rating(self, rating: int) -> None:
        self._stars.set_rating(rating)
        self._sync_rating_tip()

    def set_filmstrip_visible(self, visible: bool) -> None:
        self._filmstrip_button.setChecked(bool(visible))
        self._sync_filmstrip_tip()

    def set_compare_state(self, enabled: bool, hint: str = "") -> None:
        """对比是否可用；hint 是不可用时的原因，为空时显示默认说明。"""
        self._compare_button.setEnabled(bool(enabled))
        self._compare_hint = hint
        self._sync_compare_tip()

    def set_detail_state(self, state: str, error_key: str = "") -> None:
        if (state, error_key) == (self._detail_state, self._detail_error_key):
            return
        previous = self._detail_state
        self._detail_state, self._detail_error_key = state, error_key
        if state == "preparing":
            if previous != "preparing":
                self._detail_shown = False
                self._detail_timer.start()
        else:
            self._detail_timer.stop()
            self._detail_shown = bool(state)
        self._sync_detail_status()

    def _show_detail_status(self) -> None:
        if self._detail_state == "preparing":
            self._detail_shown = True
            self._sync_detail_status()

    def _sync_detail_status(self) -> None:
        visible = self._mode == EDITOR and self._detail_shown
        self._detail_status.setVisible(visible)
        if not visible:
            return
        preparing = self._detail_state == "preparing"
        icon, role = ("refresh", "text_secondary") if preparing else ("alert-triangle", "warning")
        key = self._detail_error_key or ("studio.detail_preparing" if preparing else "studio.detail_failed")
        text = self._translate(key,
            "Preparing full-resolution image" if preparing else "Unable to prepare full-resolution image")
        self._detail_status.setPixmap(themed_icon(icon, role=role).pixmap(_ICON_SIZE, _ICON_SIZE))
        self._detail_status.setAccessibleName(text)
        set_tooltip(self._detail_status, text)

    def retranslate_ui(self) -> None:
        tr = self._translate
        self._fit_switch.set_segment_text(_FIT, tr("toolbar.zoom_fit", "Fit"))
        self._fit_switch.set_segment_text(_ACTUAL, tr("canvas_toolbar.actual", "1:1"))
        self._fit_switch.updateGeometry()
        set_tooltip(self._readout, tr("canvas_toolbar.zoom_tip", "Click to type a zoom level"))
        self._sync_rating_tip()
        self._sync_filmstrip_tip()
        self._sync_compare_tip()
        self._sync_detail_status()
        self._sync_preview_state()

    def _sync_rating_tip(self) -> None:
        rating = self._stars.rating()
        text = (self._translate("canvas_toolbar.rating", "Rating: {n} stars").replace("{n}", str(rating)) if rating
                else self._translate("canvas_toolbar.unrated", "Not rated"))
        set_tooltip(self._stars, text)

    def _sync_filmstrip_tip(self) -> None:
        key, default = (("thumbnail.collapse", "Collapse filmstrip") if self._filmstrip_button.isChecked()
                        else ("thumbnail.expand", "Expand filmstrip"))
        set_tooltip(self._filmstrip_button, self._translate(key, default))

    def _sync_compare_tip(self) -> None:
        set_tooltip(self._compare_button,
                    self._compare_hint or self._translate("studio.compare_tip", "Press and hold to view original image"))

    def _on_fit_switch(self, index: int) -> None:
        (self._actions.zoom_fit if index == _FIT else self._actions.zoom_actual).trigger()

    def _sync_shadow(self) -> None:
        self._shadow.setColor(to_qcolor(get_theme_tokens()["shadow"]))

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.StyleChange:
            self._sync_shadow()
            self._sync_detail_status()
        super().changeEvent(event)
