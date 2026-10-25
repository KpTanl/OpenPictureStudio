"""手动预览共用的底部操作区与凭据状态。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QPalette  # noqa: E402

from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.panels.manual_preview import ManualPreviewState  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from src.ui.widgets.action_footer import ActionFooter  # noqa: E402


@pytest.fixture
def theme(qapp):
    previous = qapp.styleSheet(), qapp.property("ops.theme"), QPalette(qapp.palette())
    manager = ThemeManager()
    manager.apply("dark")
    yield manager
    qapp.setStyleSheet(previous[0])
    qapp.setProperty("ops.theme", previous[1])
    qapp.setPalette(previous[2])


@pytest.fixture
def language(qapp):
    previous = get_translation_service()
    service = TranslationService(default_language="zh-CN", fallback_language="en")
    set_translation_service(service)
    yield service
    set_translation_service(previous)


def test_ticket_changes_with_the_parameters_and_stale_reports_are_dropped():
    preview = ManualPreviewState()
    first = preview.ticket
    assert preview.state == "not_previewed"
    assert preview.report("processing", first) and preview.state == "processing"
    preview.invalidate()
    assert preview.ticket != first and preview.state == "not_previewed"
    # 旧凭据的报告不处理
    assert not preview.report("updated", first) and preview.state == "not_previewed"
    second = preview.ticket
    assert preview.report("processing", second) and preview.report("updated", second)
    # 没有任务在跑时的 stopped 不改变状态
    assert not preview.report("stopped", second) and preview.state == "updated"
    # 没完成就停下：用户取消的记为已取消，否则回到尚未预览
    preview.report("processing", second)
    assert preview.report("stopped", second) and preview.state == "not_previewed"
    preview.report("processing", second)
    preview.report("cancelling", second)
    assert preview.report("stopped", second) and preview.state == "cancelled"
    # 不带凭据的报告（面板自己做宿主）照常处理
    assert preview.report("failed") and preview.state == "failed"


def test_footer_projects_each_state(theme, language):
    footer = ActionFooter()
    footer.resize(284, 120)
    footer.show()
    clicks, cancels = [], []
    footer.primary_clicked.connect(lambda: clicks.append(True))
    footer.cancel_clicked.connect(lambda: cancels.append(True))
    assert footer.title.isHidden()
    footer.set_title("2  渲染预览")
    assert footer.title.isVisible() and footer.title.text() == "2  渲染预览"

    footer.show_preview(steps="AI 智能抠图", preview_state="not_previewed")
    assert footer.summary.text() == "将执行：AI 智能抠图" and footer.status.isHidden()
    assert footer.primary_button.text() == "渲染预览" and footer.primary_button.isEnabled()
    assert footer.cancel_button.isHidden()
    footer.primary_button.click()
    assert clicks == [True]

    footer.show_preview(steps="AI 智能抠图", preview_state="processing")
    assert footer.summary.text() == "正在渲染：AI 智能抠图…"
    assert footer.primary_button.text() == "渲染中…" and not footer.primary_button.isEnabled()
    assert footer.cancel_button.isVisible() and footer.cancel_button.isEnabled()
    footer.cancel_button.click()
    assert cancels == [True]

    footer.show_preview(steps="AI 智能抠图", preview_state="cancelling")
    assert footer.summary.text() == "正在取消：AI 智能抠图…"
    assert footer.cancel_button.isVisible() and not footer.cancel_button.isEnabled()

    footer.show_preview(steps="AI 智能抠图", preview_state="updated")
    assert footer.summary.text() == "预览已是最新：AI 智能抠图"
    assert footer.primary_button.text() == "已渲染" and not footer.primary_button.isEnabled()
    assert footer.cancel_button.isHidden()

    footer.show_preview(steps="AI 智能抠图", preview_state="failed")
    assert footer.status.isVisible() and footer.status.objectName() == "statusError"
    assert footer.status.text() == language.translate("preview.failed") and footer.primary_button.isEnabled()
    footer.show_preview(steps="AI 智能抠图", preview_state="cancelled")
    assert footer.status.text() == "已取消" and footer.status.objectName() == "mutedText"
    assert footer.primary_button.isEnabled()

    # 做不了：写出原因，不能点
    reason = language.translate("preview.blocked", group="AI 智能抠图", reason="需要下载模型")
    footer.show_preview(steps="AI 智能抠图", preview_state="not_previewed", blocked=reason)
    assert footer.status.text() == reason and footer.status.objectName() == "statusWarning"
    assert not footer.primary_button.isEnabled() and footer.summary.text() == "将执行：AI 智能抠图"
    # 没有可做的处理：写出说明，不能点
    footer.show_preview(steps="", preview_state="updated", idle="先在上面打开至少一种处理")
    assert footer.summary.text() == "先在上面打开至少一种处理" and footer.status.isHidden()
    assert footer.primary_button.text() == "渲染预览" and not footer.primary_button.isEnabled()
    # 图片没准备好：不能点
    footer.show_preview(steps="颜色抠图", preview_state="not_previewed", ready=False)
    assert not footer.primary_button.isEnabled() and footer.status.isHidden()

    with pytest.raises(ValueError):
        footer.show_preview(steps="", preview_state="done")
    # 文字取当前语言
    language.set_language("en")
    footer.show_preview(steps="Color key", preview_state="not_previewed")
    assert footer.primary_button.text() == language.translate("preview.render")
    assert footer.summary.text() == language.translate("preview.summary", steps="Color key")
    footer.close()


def test_footer_can_show_a_plain_action_instead_of_a_preview(theme, language):
    """没有可渲染内容的工具用同一块：一句说明和一个由面板命名的按钮，没有原因一行与取消。"""
    footer = ActionFooter()
    footer.resize(284, 120)
    footer.show()
    clicks = []
    footer.primary_clicked.connect(lambda: clicks.append(True))
    footer.show_preview(steps="AI 智能抠图", preview_state="processing")
    footer.show_action(summary="将使用：圆形", button="使用这个形状", enabled=True)
    assert footer.summary.text() == "将使用：圆形" and footer.primary_button.text() == "使用这个形状"
    assert footer.primary_button.isEnabled() and footer.cancel_button.isHidden() and footer.status.isHidden()
    footer.primary_button.click()
    assert clicks == [True]
    footer.show_action(summary="已使用：圆形", button="已使用", enabled=False)
    assert not footer.primary_button.isEnabled()
    # 点不了的时候可以写明原因
    footer.show_action(summary="还没有选区", button="查看结果", enabled=False, reason="先在照片上圈出一个区域")
    assert not footer.status.isHidden() and footer.status.text() == "先在照片上圈出一个区域"
    assert footer.status.objectName() == "statusWarning"
    footer.show_action(summary="1 个区域", button="查看结果", enabled=True)
    assert footer.status.isHidden()
    footer.close()

