"""消息对话框与进度对话框：内容、按钮、返回值与取消。"""

from threading import Event
from time import sleep

import pytest
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QScrollArea

from src.ui.base_window import BaseDialog
from src.ui.dialogs.apply_target_dialog import ApplyTarget, ask_apply_target
from src.ui.services.background_operation import run_background
from src.ui.widgets import message_dialog
from src.ui.widgets.apple_button import (AppleBrandButton, AppleButton, AppleDestructiveButton,
                                         AppleGhostButton)
from src.ui.widgets.message_dialog import (DialogButton, MessageDialog, ask_choice, confirm_destructive,
                                           show_confirm, show_error, show_info, show_question)
from src.ui.widgets.progress_dialog import ProgressDialog


@pytest.fixture
def dialogs(monkeypatch):
    """记录每一次弹出的消息对话框，并按预先排好的答案返回，不进入模态循环。"""
    shown, answers = [], []

    def run(dialog):
        shown.append(dialog)
        return answers.pop(0) if answers else dialog.choice()

    monkeypatch.setattr(MessageDialog, "run", run)
    return shown, answers


def test_message_dialog_shows_content_and_returns_clicked_button(qapp):
    dialog = MessageDialog(
        title="应用修改", message="如何保存已应用的修改？", detail="覆盖后原文件的内容不能恢复。",
        buttons=(DialogButton("cancel", "取消", "text"), DialogButton("overwrite", "覆盖原图"),
                 DialogButton("save_as", "另存为…", "primary")),
        default="save_as", escape="cancel")
    dialog.show()
    assert (dialog.title(), dialog.message(), dialog.detail()) == (
        "应用修改", "如何保存已应用的修改？", "覆盖后原文件的内容不能恢复。")
    assert isinstance(dialog.button("cancel"), AppleGhostButton)
    assert isinstance(dialog.button("save_as"), AppleBrandButton)
    assert dialog.button("save_as").isDefault()
    assert dialog.choice() == "cancel"
    assert not dialog.styleSheet()

    dialog.button("overwrite").click()
    assert dialog.choice() == "overwrite" and not dialog.isVisible()


def test_message_dialog_allows_only_one_filled_button(qapp):
    with pytest.raises(ValueError):
        MessageDialog(title="t", message="m", buttons=(
            DialogButton("discard", "丢弃并退出", "danger"), DialogButton("save", "保存并退出", "primary")))
    dialog = MessageDialog(title="t", message="m", buttons=(
        DialogButton("keep", "继续编辑", "text"), DialogButton("discard", "丢弃并退出"),
        DialogButton("save", "保存并退出", "primary")))
    assert type(dialog.button("discard")) is AppleButton
    dialog.close()


def test_message_dialog_escape_and_close_return_escape_key(qapp):
    dialog = MessageDialog(title="t", message="m", buttons=(DialogButton("no", "取消"), DialogButton("yes", "确定")),
                           default="yes", escape="no")
    dialog.show()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert dialog.choice() == "no" and not dialog.isVisible()

    dialog = MessageDialog(title="t", message="m", buttons=(DialogButton("no", "取消"), DialogButton("yes", "确定")),
                           default="yes", escape="no")
    dialog.show()
    QTest.keyClick(dialog, Qt.Key.Key_Return)
    assert dialog.choice() == "yes"


def test_message_dialog_width_follows_text_and_long_text_scrolls(qapp):
    short = MessageDialog(title="t", message="短", buttons=(DialogButton("ok", "确定"),))
    lines = "\n".join(f"D:/Photos/2026/Trip/DSC_{index}.NEF: 图片保存失败，请检查目标目录、可用空间和图像编码支持。"
                      for index in range(60))
    long = MessageDialog(title="t", message=lines, buttons=(DialogButton("ok", "确定"),))
    short.show()
    long.show()
    assert short.width() < long.width() <= 560
    assert not short.findChildren(QScrollArea)
    assert len(long.findChildren(QScrollArea)) == 1
    screen = qapp.primaryScreen().availableGeometry()
    assert long.height() < screen.height()
    # 按钮一排始终在对话框里
    assert long.rect().contains(long.button("ok").geometry())
    short.close()
    long.close()


def test_notify_confirm_and_destructive_helpers(dialogs):
    shown, answers = dialogs
    show_info(None, "提示", "完成")
    show_error(None, "错误", "失败")
    assert [dialog.title() for dialog in shown] == ["提示", "错误"]
    assert shown[0].button("ok").text() == message_dialog.tr("common.ok", "OK")

    answers.extend(["yes", "no", "no", "confirm", "cancel"])
    assert show_confirm(None, "确认", "继续？") is True
    assert show_confirm(None, "确认", "继续？") is False
    assert show_question(None, "询问", "是否？", default_yes=False) is False
    assert shown[-1].button("no").isDefault() and shown[-1].button("yes").text() == message_dialog.tr("common.yes", "Yes")
    assert confirm_destructive(None, "清除评分", "这将永久删除。", "清除评分") is True
    assert confirm_destructive(None, "清除评分", "这将永久删除。", "清除评分") is False
    danger = shown[-1]
    assert isinstance(danger.button("confirm"), AppleDestructiveButton)
    assert danger.button("cancel").isDefault() and danger.choice() == "cancel"

    answers.append("b")
    assert ask_choice(None, "t", "m", (DialogButton("a", "A"), DialogButton("b", "B"))) == "b"


def test_base_dialog_wrappers_and_apply_target_use_message_dialog(dialogs, qapp):
    shown, answers = dialogs
    host = BaseDialog()
    host.show_warning("警告", "内容")
    answers.append("yes")
    assert host.show_confirm("确认", "内容") is True
    assert [dialog.title() for dialog in shown] == ["警告", "确认"]
    assert shown[0].parent() is host
    host.close()

    for answer, expected in (("save_as", ApplyTarget.SAVE_AS), ("overwrite", ApplyTarget.OVERWRITE),
                             ("cancel", ApplyTarget.CANCEL)):
        answers.append(answer)
        assert ask_apply_target(None, lambda key, default="": default) is expected
    target = shown[-1]
    assert target.button("save_as").isDefault() and target.detail()


def test_message_dialog_runs_modally(qapp):
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().button("b").click())
    assert ask_choice(None, "t", "m", (DialogButton("a", "A"), DialogButton("b", "B")), escape="a") == "b"


def test_progress_dialog_cancel_does_not_close_until_finished(qapp):
    cancelled = []
    dialog = ProgressDialog(title="导出图片", text="正在保存图片…", modal=False)
    dialog.cancelled.connect(lambda: cancelled.append(True))
    dialog.show()
    assert (dialog.title(), dialog.text()) == ("导出图片", "正在保存图片…")

    dialog.cancel_button().click()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    dialog.close()
    assert len(cancelled) == 3 and dialog.isVisible()

    dialog.set_cancelling("正在取消…")
    assert dialog.text() == "正在取消…" and not dialog.cancel_button().isEnabled()
    dialog.finish()
    assert not dialog.isVisible() and len(cancelled) == 3


def test_progress_dialog_delayed_show_is_skipped_when_finished_first(qapp, qtbot):
    dialog = ProgressDialog(title="打开图片", modal=False)
    dialog.show_after(10_000)
    dialog.finish()
    assert not dialog.isVisible()
    dialog.show_after(0)
    qtbot.waitUntil(dialog.isVisible)
    assert dialog.cancel_button().isEnabled()
    dialog.finish()


def test_run_background_returns_result_raises_error_and_cancels(qapp):
    assert run_background(None, lambda cancellation: 42) == 42

    def fail(cancellation):
        raise ValueError("error.export.failed")

    with pytest.raises(ValueError, match="error.export.failed"):
        run_background(None, fail)

    started, seen = Event(), []

    def wait_for_cancel(cancellation):
        started.set()
        while not cancellation.is_cancelled():
            sleep(0.005)
        return "cancelled"

    def cancel():
        if not started.is_set() or not isinstance(QApplication.activeModalWidget(), ProgressDialog):
            QTimer.singleShot(5, cancel)
            return
        dialog = QApplication.activeModalWidget()
        dialog.cancel_button().click()
        seen.append((dialog.text(), dialog.cancel_button().isEnabled()))

    QTimer.singleShot(0, cancel)
    assert run_background(None, wait_for_cancel) == "cancelled"
    assert seen == [(message_dialog.tr("export.cancelling", "Cancelling…"), False)]
