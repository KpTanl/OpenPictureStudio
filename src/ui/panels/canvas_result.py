"""画布工具（自由裁切、形状裁切、套索、证件照）底部的“查看结果 / 继续调整”。

这类工具有两种状态：调整时画布上是这一步之前的画面，加上框或轮廓；看结果时是全部各步之后的画面，
框与轮廓收起。画布上现在是哪一种由宿主决定，并经 ``set_viewing()`` 告知；结果还在渲染时由宿主经
``set_busy()`` 告知，这时按钮是“取消”，点了回到调整。这里只发出请求，并把面板给出的说明投影到底部操作区。
它是界面状态，不是参数。
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QWidget

from ...i18n import tr
from ..widgets.action_footer import ActionFooter
from ..widgets.tooltip import set_tooltip

_SHORTCUT = "Enter"
# 结果很快就出来时不闪一下“正在生成”：渲染超过这么久才显示
_BUSY_DELAY_MS = 300


class ResultViewControl(QObject):
    # 请求看结果（True）或回到调整（False）
    requested = Signal(bool)
    # 宿主告知的状态变了：现在是否在看结果
    viewing_changed = Signal(bool)

    def __init__(self, panel: QWidget, *, prepare: Optional[Callable[[], None]] = None):
        """prepare 在请求看结果之前调用，面板在里面把还没记入的内容记入（如闭合正在画的轮廓）。"""
        super().__init__(panel)
        self.footer = ActionFooter(panel)
        self.footer.primary_clicked.connect(self.toggle)
        self._prepare = prepare
        self._viewing = False
        self._busy = False
        self._busy_shown = False
        self._busy_timer = QTimer(self)
        self._busy_timer.setSingleShot(True)
        self._busy_timer.setInterval(_BUSY_DELAY_MS)
        self._busy_timer.timeout.connect(self._show_busy)
        self._summary = ""
        self._reason = ""
        self._project()

    @property
    def viewing(self) -> bool:
        return self._viewing

    @property
    def busy(self) -> bool:
        """结果是否还在渲染（不论“正在生成”是否已经显示出来）。"""
        return self._busy

    def set_viewing(self, viewing: bool) -> None:
        """宿主告知画布上现在是结果还是调整。回到调整时不再是忙碌状态。"""
        if not viewing:
            self._set_busy(False)
        if viewing != self._viewing:
            self._viewing = viewing
            self._project()
            self.viewing_changed.emit(viewing)

    def set_busy(self, busy: bool) -> None:
        """宿主告知结果是否还在渲染；只在看结果时有意义。"""
        self._set_busy(busy and self._viewing)

    def _set_busy(self, busy: bool) -> None:
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._busy_timer.start()
            return
        self._busy_timer.stop()
        if self._busy_shown:
            self._busy_shown = False
            self._project()

    def _show_busy(self) -> None:
        if self._busy and self._viewing:
            self._busy_shown = True
            self._project()

    def describe(self, summary: str, reason: str = "") -> None:
        """调整时显示的说明；reason 非空表示现在没有结果可看，按钮不可点。切换语言后再调一次。"""
        self._summary, self._reason = summary, reason
        self._project()

    def can_toggle(self) -> bool:
        return self._viewing or not self._reason

    def toggle(self) -> bool:
        """请求切到另一种状态；现在没有结果可看时不请求。返回是否发出了请求。"""
        if not self.can_toggle():
            return False
        if not self._viewing and self._prepare is not None:
            self._prepare()
        self.requested.emit(not self._viewing)
        return True

    def _project(self) -> None:
        if self._viewing and self._busy_shown:
            self.footer.show_action(summary=tr("canvas_tool.busy"), button=tr("button.cancel"), enabled=True)
            tip = tr("canvas_tool.adjust_tip")
        elif self._viewing:
            self.footer.show_action(summary=tr("canvas_tool.viewing"), button=tr("canvas_tool.adjust"), enabled=True)
            tip = tr("canvas_tool.adjust_tip")
        else:
            self.footer.show_action(summary=self._summary, reason=self._reason,
                                    button=tr("canvas_tool.view_result"), enabled=not self._reason)
            tip = tr("canvas_tool.view_result_tip")
        set_tooltip(self.footer.primary_button, tip, _SHORTCUT)
