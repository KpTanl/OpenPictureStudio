"""进度对话框：标题、进度条、一句说明与“取消”。

点“取消”、按 Esc 或关窗都只发出 cancelled，不关闭自己；任务结束后由发起方调用 finish()。
进度无法量化时显示忙碌状态。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_LG, SPACING_MD, SPACING_SM, SPACING_XL, px
from ...utils.app_info import APP_NAME
from ...i18n.translation_service import tr
from ..base_window import BaseDialog
from .apple_button import AppleButton
from .message_dialog import dialog_heading, fit_dialog_height
from .usage_bar import UsageBar

_WIDTH = 380


class ProgressDialog(BaseDialog):
    cancelled = Signal()

    def __init__(self, parent: Optional[QWidget] = None, *, title: str, text: str = "",
                 cancel_text: str = "", modal: bool = True):
        super().__init__(title_default=APP_NAME, min_width=_WIDTH, min_height=0, modal=modal,
                         parent=parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint
                            | Qt.WindowType.WindowCloseButtonHint)
        if modal:
            self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedWidth(_WIDTH)
        self._finished = False
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.timeout.connect(self.show)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_LG))
        layout.setSpacing(px(SPACING_MD))
        heading, self._title = dialog_heading(self, title)
        layout.addLayout(heading)
        self._usage = UsageBar(self)
        self._usage.set_progress(0, 0)
        layout.addWidget(self._usage)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, px(SPACING_SM), 0, 0)
        footer.addStretch(1)
        self._cancel = AppleButton(cancel_text or tr("common.cancel", "Cancel"), self)
        self._cancel.clicked.connect(self.cancelled)
        footer.addWidget(self._cancel)
        layout.addLayout(footer)
        self.set_text(text)

    def title(self) -> str:
        return self._title.text()

    def text(self) -> str:
        return self._usage.text()

    def cancel_button(self) -> AppleButton:
        return self._cancel

    def set_title(self, title: str) -> None:
        self._title.setText(title)
        self._fit()

    def set_text(self, text: str) -> None:
        self._usage.set_text(text)
        self._fit()

    def set_cancel_text(self, text: str) -> None:
        self._cancel.setText(text)

    def set_progress(self, value: int, maximum: int) -> None:
        """maximum 为 0 表示忙碌状态。"""
        self._usage.set_progress(value, maximum)

    def set_cancelling(self, text: str) -> None:
        """已经请求取消：换一句说明，按钮不再可点，等发起方调用 finish()。"""
        self._cancel.setEnabled(False)
        self.set_text(text)

    def show_after(self, delay_ms: int) -> None:
        """过了这段时间任务还没结束才显示，避免一闪而过。"""
        self._finished = False
        self._cancel.setEnabled(True)
        self._delay.start(max(0, delay_ms))

    def finish(self) -> None:
        self._finished = True
        self._delay.stop()
        self.done(0)

    def _fit(self) -> None:
        self.ensurePolished()
        fit_dialog_height(self)

    def reject(self) -> None:
        if self._finished:
            super().reject()
        else:
            self.cancelled.emit()

    def closeEvent(self, event) -> None:
        if self._finished:
            super().closeEvent(event)
        else:
            event.ignore()
            self.cancelled.emit()
