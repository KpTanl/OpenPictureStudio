"""面板底部固定的操作区：一句说明加一个主按钮，由面板经 ``take_footer()`` 交给侧栏固定在底部。

需要用户点一下才开始的工具共用这一块，位置与样子都一样。两种用法：
``show_preview()`` 用于手动预览（画质增强、移除背景）：将要做什么、做不了的原因、“渲染预览”与取消，
预览任务归宿主，是否在跑、有没有完成由面板转告；
``show_action()`` 用于没有可渲染内容的工具（形状裁切）：一句说明和一个由面板命名的按钮。
它只把面板给出的状态投影成文字与按钮，不保存业务状态。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_SM, px
from ...i18n import tr
from .apple_button import AppleGhostButton, ApplePrimaryButton

# 预览状态：尚未预览、渲染中、正在取消、已是最新、失败、用户取消
PREVIEW_STATES = ("not_previewed", "processing", "cancelling", "updated", "failed", "cancelled")


class ActionFooter(QWidget):
    primary_clicked = pyqtSignal()
    cancel_clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        self.title = QLabel(self)
        self.title.setObjectName("stepTitle")
        self.title.hide()
        layout.addWidget(self.title)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.setSpacing(px(SPACING_SM))
        self.primary_button = ApplePrimaryButton(parent=self)
        self.cancel_button = AppleGhostButton(parent=self)
        self.primary_button.clicked.connect(self.primary_clicked)
        self.cancel_button.clicked.connect(self.cancel_clicked)
        buttons.addWidget(self.primary_button, 1)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.show_preview(steps="", preview_state="not_previewed", ready=False)

    def show_action(self, *, summary: str, button: str, enabled: bool, reason: str = "") -> None:
        """一句说明加一个由面板命名的按钮，没有取消；reason 是按钮现在点不了的原因。"""
        self.summary.setText(summary)
        self._set_status(reason, "statusWarning")
        self.primary_button.setText(button)
        self.primary_button.setEnabled(enabled)
        self.cancel_button.hide()

    def _set_status(self, text: str, kind: str) -> None:
        if self.status.objectName() != kind:
            self.status.setObjectName(kind)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def set_title(self, text: str) -> None:
        """这一步的标题（如“2  渲染预览”）；空字符串表示不显示。"""
        self.title.setText(text)
        self.title.setVisible(bool(text))

    def show_preview(self, *, steps: str, preview_state: str, idle: str = "", blocked: str = "",
                   ready: bool = True) -> None:
        """按面板给出的状态显示。

        steps 是将要做的处理；idle 是现在没有可做的处理时的说明，给出时不能渲染；
        blocked 是做不了的原因；ready 为假表示图片还没准备好。文字取当前语言，切换语言后再调一次即可。
        """
        if preview_state not in PREVIEW_STATES:
            raise ValueError(f"未知的预览状态: {preview_state!r}")
        summary, status, kind, button, enabled = "preview.summary", "", "statusWarning", "preview.render", False
        if idle:
            summary = ""
        elif blocked:
            status = blocked
        elif not ready:
            pass
        elif preview_state in ("processing", "cancelling"):
            summary, button = "preview.summary." + preview_state, "preview.rendering"
        elif preview_state == "updated":
            summary, button = "preview.summary.updated", "preview.rendered"
        else:
            enabled = True
            if preview_state == "failed":
                status, kind = tr("preview.failed"), "statusError"
            elif preview_state == "cancelled":
                status, kind = tr("preview.cancelled"), "mutedText"
        self.summary.setText(tr(summary, steps=steps) if summary else idle)
        self._set_status(status, kind)
        self.primary_button.setText(tr(button))
        self.primary_button.setEnabled(enabled)
        # 取消只在计算进行时出现；已经点过、任务还没停下时不能再点
        self.cancel_button.setText(tr("button.cancel"))
        self.cancel_button.setVisible(button == "preview.rendering")
        self.cancel_button.setEnabled(preview_state == "processing")
