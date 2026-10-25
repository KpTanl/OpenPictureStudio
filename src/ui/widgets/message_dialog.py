"""消息对话框：标题、正文、补充说明与右下角的一排按钮。

“选择”与“破坏性确认”两类对话框用它；提示、警告、错误是只有一个按钮的情形。
按钮用按钮组件，标题与文字的样式在全局样式表里，标题左侧的图标在绘制时读取当前主题。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QPainter, QPaintEvent
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_LG, SPACING_MD, SPACING_SM, SPACING_XL, SPACING_XS, px
from ...utils.app_info import APP_NAME
from ...i18n.translation_service import tr
from ..base_window import BaseDialog
from ..icons import themed_icon
from .apple_button import AppleBrandButton, AppleButton, AppleDestructiveButton, AppleGhostButton

_WIDTH = 380
_MAX_WIDTH = 560
_MESSAGE_MAX_HEIGHT = 280
_ICON_SIZE = 20

_BUTTON_TYPES = {
    "primary": AppleBrandButton,
    "secondary": AppleButton,
    "text": AppleGhostButton,
    "danger": AppleDestructiveButton,
}
# 标题左侧的图标：种类 -> 取色的语义键。提示与询问不带图标
_ICON_ROLES = {"warning": "warning", "error": "danger", "danger": "danger"}


@dataclass(frozen=True)
class DialogButton:
    """对话框里的一个按钮。key 是点它之后返回的值；role 为 primary、secondary、text 或 danger。"""

    key: str
    text: str
    role: str = "secondary"


class _KindIcon(QWidget):
    """标题左侧的图标；上方留出与标题文字相同的内边距，与标题的第一行对齐。"""

    def __init__(self, role: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._icon = themed_icon("alert-triangle", role=role)
        self.setFixedSize(_ICON_SIZE, _ICON_SIZE + px(SPACING_XS))

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        self._icon.paint(painter, QRect(0, px(SPACING_XS), _ICON_SIZE, _ICON_SIZE))
        painter.end()


def dialog_heading(parent: QWidget, title: str, kind: str = "info") -> tuple[QHBoxLayout, QLabel]:
    """对话框顶部的标题一行，警告、错误与破坏性确认在标题左侧带图标。"""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(px(SPACING_SM))
    if kind in _ICON_ROLES:
        row.addWidget(_KindIcon(_ICON_ROLES[kind], parent), 0, Qt.AlignmentFlag.AlignTop)
    label = QLabel(title, parent)
    label.setObjectName("dialogTitle")
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setIndent(0)
    row.addWidget(label, 1)
    return row, label


def fit_dialog_height(dialog: QWidget) -> None:
    """宽度固定的对话框按内容定高度；内容里有自动换行的文字，高度随宽度变化。"""
    layout = dialog.layout()
    layout.activate()
    height = layout.totalHeightForWidth(dialog.width())
    if height < 0:
        height = layout.totalSizeHint().height()
    dialog.setFixedHeight(height)


class MessageDialog(BaseDialog):
    """标题、正文、补充说明与按钮；``run()`` 返回用户所点按钮的 key。

    一个对话框里最多一个实底按钮（主操作或危险），其余用次要或文字按钮，否则两块颜色互相抢。
    """

    def __init__(self, parent: Optional[QWidget] = None, *, title: str, message: str,
                 buttons: Sequence[DialogButton], detail: str = "", kind: str = "info",
                 default: str = "", escape: str = ""):
        """default 是回车时触发的按钮；escape 是按 Esc 或关闭窗口时返回的 key。"""
        if sum(spec.role in ("primary", "danger") for spec in buttons) > 1:
            raise ValueError("一个对话框里最多一个主操作或危险按钮")
        super().__init__(title_default=APP_NAME, min_width=_WIDTH, min_height=0, parent=parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint
                            | Qt.WindowType.WindowCloseButtonHint)
        self._escape = escape
        self._choice = escape
        self._buttons: dict[str, AppleButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_LG))
        layout.setSpacing(px(SPACING_MD))
        heading, self._title = dialog_heading(self, title, kind)
        layout.addLayout(heading)

        self._message = QLabel(message, self)
        self._message.setObjectName("dialogMessage")
        self._message.setTextFormat(Qt.TextFormat.PlainText)
        self._message.setWordWrap(True)
        self._message.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._message)

        self._detail = QLabel(detail, self)
        self._detail.setObjectName("dialogDetail")
        self._detail.setTextFormat(Qt.TextFormat.PlainText)
        self._detail.setWordWrap(True)
        self._detail.setVisible(bool(detail))
        layout.addWidget(self._detail)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, px(SPACING_SM), 0, 0)
        footer.setSpacing(px(SPACING_SM))
        footer.addStretch(1)
        for spec in buttons:
            button = _BUTTON_TYPES[spec.role](spec.text, self)
            button.clicked.connect(lambda _checked=False, key=spec.key: self._choose(key))
            footer.addWidget(button)
            self._buttons[spec.key] = button
        layout.addLayout(footer)

        focused = self._buttons.get(default) or next(iter(self._buttons.values()), None)
        if focused is not None:
            focused.setDefault(True)
            focused.setFocus()
        self._fit(footer)

    def _fit(self, footer: QHBoxLayout) -> None:
        """宽度随正文最长的一行在上下限之间取，按钮一排始终放得下；正文太长时改在框内滚动。"""
        for widget in (self._title, self._message, self._detail, *self._buttons.values()):
            widget.ensurePolished()
        layout = self.layout()
        margins = layout.contentsMargins()
        horizontal = margins.left() + margins.right()
        metrics = self._message.fontMetrics()
        widest = max((metrics.horizontalAdvance(line) for line in self._message.text().splitlines()), default=0)
        widest += px(SPACING_SM)
        inner = max(min(max(widest, _WIDTH - horizontal), _MAX_WIDTH - horizontal), footer.sizeHint().width())
        self.setFixedWidth(inner + horizontal)
        if self._message.heightForWidth(inner) > _MESSAGE_MAX_HEIGHT:
            scroll = QScrollArea(self)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
            scroll.setFixedHeight(_MESSAGE_MAX_HEIGHT)
            layout.replaceWidget(self._message, scroll)
            scroll.setWidget(self._message)
        fit_dialog_height(self)

    def button(self, key: str) -> AppleButton:
        return self._buttons[key]

    def title(self) -> str:
        return self._title.text()

    def message(self) -> str:
        return self._message.text()

    def detail(self) -> str:
        return self._detail.text()

    def choice(self) -> str:
        return self._choice

    def _choose(self, key: str) -> None:
        self._choice = key
        self.accept()

    def reject(self) -> None:
        self._choice = self._escape
        super().reject()

    def run(self) -> str:
        """显示并等待用户选择，返回所点按钮的 key；按 Esc 或关闭窗口返回 escape。"""
        self.exec()
        self.deleteLater()
        return self._choice


def ask_choice(parent: Optional[QWidget], title: str, message: str, buttons: Sequence[DialogButton], *,
               detail: str = "", kind: str = "question", default: str = "", escape: str = "") -> str:
    """给出几个按钮，返回用户所选的 key。"""
    return MessageDialog(parent, title=title, message=message, buttons=buttons, detail=detail, kind=kind,
                         default=default, escape=escape).run()


def confirm_destructive(parent: Optional[QWidget], title: str, message: str, action_text: str, *,
                        cancel_text: str = "", detail: str = "") -> bool:
    """破坏性确认：危险按钮写明要做的事，默认停在“取消”上。"""
    buttons = (DialogButton("cancel", cancel_text or tr("common.cancel", "Cancel")),
               DialogButton("confirm", action_text, "danger"))
    return ask_choice(parent, title, message, buttons, detail=detail, kind="danger",
                      default="cancel", escape="cancel") == "confirm"


def _notify(parent: Optional[QWidget], kind: str, title: str, message: str, ok_text: str) -> None:
    buttons = (DialogButton("ok", ok_text or tr("common.ok", "OK"), "primary"),)
    ask_choice(parent, title, message, buttons, kind=kind, default="ok", escape="ok")


def show_info(parent: Optional[QWidget] = None, title: str = "", message: str = "", ok_text: str = "") -> None:
    _notify(parent, "info", title, message, ok_text)


def show_warning(parent: Optional[QWidget] = None, title: str = "", message: str = "", ok_text: str = "") -> None:
    _notify(parent, "warning", title, message, ok_text)


def show_error(parent: Optional[QWidget] = None, title: str = "", message: str = "", ok_text: str = "") -> None:
    _notify(parent, "error", title, message, ok_text)


def show_confirm(parent: Optional[QWidget] = None, title: str = "", message: str = "", yes_text: str = "",
                 no_text: str = "", default_yes: bool = True) -> bool:
    """确认：返回 True（确定）或 False（取消）。"""
    buttons = (DialogButton("no", no_text or tr("common.cancel", "Cancel")),
               DialogButton("yes", yes_text or tr("common.ok", "OK"), "primary"))
    return ask_choice(parent, title, message, buttons, default="yes" if default_yes else "no",
                      escape="no") == "yes"


def show_question(parent: Optional[QWidget] = None, title: str = "", message: str = "", yes_text: str = "",
                  no_text: str = "", default_yes: bool = True) -> bool:
    """询问：返回 True（是）或 False（否）。"""
    return show_confirm(parent, title, message, yes_text or tr("common.yes", "Yes"),
                        no_text or tr("common.no", "No"), default_yes)
