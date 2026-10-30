"""真实编辑工作区里的编辑栈与历史：两者都是会话状态的投影，操作经工作区的同一套入口生效。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from tests.support import wait_for_studio, wait_for_thumbnails, wait_until  # noqa: E402


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


def _settle():
    for _ in range(12):
        QApplication.processEvents()


def _idle(studio):
    wait_until(lambda: studio._preview_queue.active is None and studio._preview_queue.pending is None, "预览渲染结束")
    _settle()


@pytest.fixture
def editor(theme, language, window_factory, tmp_path):
    path = str(tmp_path / "photo.png")
    Image.new("RGB", (640, 480), (120, 160, 200)).save(path)
    window = window_factory()
    window.resize(1440, 900)
    window.show()
    window.manager.open_file(path)
    window._refresh_all()
    wait_for_thumbnails(window.thumbnail_panel, [path], path, version=window.manager.list_version)

    def enter(tool_id="color_adjust"):
        studio = wait_for_studio(window, path, tool_id)
        _idle(studio)
        return studio, studio.tool_sidebar.get_current_panel()

    return enter


def _adjust(studio, panel, key, value):
    panel._parameter_rows[key].slider.edit_value(value)
    panel.end_edit_group()
    _idle(studio)


def _stack(studio):
    return [(row.key, row.summary, row.current) for row in studio.tool_sidebar.edit_stack.rows()]


def _history(studio):
    return [(step.text, step.state) for step in studio.history_panel.steps()]


def test_edit_stack_follows_the_steps_of_the_current_photo(editor):
    studio, color = editor()
    stack = studio.tool_sidebar.edit_stack
    # 刚打开、还没有调整：编辑栈是空的，写着一句说明
    assert _stack(studio) == [] and stack._empty.isVisible()

    _adjust(studio, color, "shadows", 32)
    assert _stack(studio) == [("color_adjust", "阴影 +32", True)]
    _adjust(studio, color, "exposure_amt", 35)
    assert _stack(studio) == [("color_adjust", "2 项调整", True)]

    # 形状裁切点“查看结果”才进入本次编辑；它排在调色之后，侧栏正显示它
    studio, shape = editor("shape_crop")
    assert _stack(studio) == [("color_adjust", "2 项调整", False)]
    shape.result_view.toggle()
    _idle(studio)
    assert _stack(studio) == [("color_adjust", "2 项调整", False), ("shape_crop", "", True)]
    assert [row.title for row in stack.rows()] == ["调色实验室", "形状裁切"]
    assert [row.icon for row in stack.rows()] == ["adjustments-horizontal", "crop"]

    # 点编辑栈里的一步回到它的工具
    stack.stage_requested.emit("color_adjust")
    _idle(studio)
    assert studio.active_tool_id == "color_adjust" and studio.tool_rail.current() == "color"
    assert _stack(studio) == [("color_adjust", "2 项调整", True), ("shape_crop", "", False)]

    # 重置另一步：那一步从栈里消失，当前工具不变，这一次重置可以撤销
    stack.reset_requested.emit("shape_crop")
    _idle(studio)
    assert _stack(studio) == [("color_adjust", "2 项调整", True)]
    assert studio.active_tool_id == "color_adjust" and "shape_crop" not in studio.session.tool_params
    studio.undo()
    _idle(studio)
    assert [key for key, _summary, _current in _stack(studio)] == ["color_adjust", "shape_crop"]

    # 不在“调整”页时没有哪一步是当前步
    studio.show_history()
    _settle()
    assert not any(current for _key, _summary, current in _stack(studio))


def test_history_lists_each_step_and_goes_back_and_forward(editor):
    studio, color = editor()
    _adjust(studio, color, "exposure_amt", 35)
    _adjust(studio, color, "shadows", 12)
    _adjust(studio, color, "highlights", -38)
    studio.show_history()
    _settle()
    assert studio.viewer._overlay is None
    assert _history(studio) == [("打开 photo.png", "past"), ("调色实验室 · 曝光度 +0.35", "past"),
                                ("调色实验室 · 阴影 +12", "past"), ("调色实验室 · 高光 -38", "current")]
    steps = studio.history_panel.steps()
    assert all(step.note for step in steps)

    # 回到第一步之后：后面两步列为已撤销，参数与撤销重做的状态随之回去
    studio.history_panel.step_requested.emit(steps[1].key)
    _idle(studio)
    assert [state for _text, state in _history(studio)] == ["past", "current", "undone", "undone"]
    assert studio.history_panel.steps()[2].note == "已撤销"
    assert studio.session.color_params["exposure_amt"] == 35 and studio.session.color_params["shadows"] == 0
    assert color._parameter_rows["shadows"].value() == 0
    assert studio.can_undo() and studio.can_redo() and not studio.session.read_only

    # 重做一步、再直接点最后一步
    studio.redo()
    _idle(studio)
    assert [state for _text, state in _history(studio)] == ["past", "past", "current", "undone"]
    studio.history_panel.step_requested.emit(steps[3].key)
    _idle(studio)
    assert [state for _text, state in _history(studio)] == ["past", "past", "past", "current"]
    assert studio.session.color_params["highlights"] == -38 and not studio.can_redo()

    # 回到打开时的状态再做新的一步：原来那几步不在这条线上了
    studio.history_panel.step_requested.emit(steps[0].key)
    _idle(studio)
    assert not studio.can_undo() and studio.tool_sidebar.edit_stack.rows() == ()
    studio.switch_tool("color_adjust")
    _idle(studio)
    _adjust(studio, color, "tint", 8)
    studio.show_history()
    _settle()
    assert _history(studio) == [("打开 photo.png", "past"), ("调色实验室 · 色调 +8", "current")]


def test_history_describes_resets_and_follows_the_language(editor, language):
    studio, color = editor()
    _adjust(studio, color, "shadows", 12)
    studio.reset_tool("color_adjust")
    _idle(studio)
    _adjust(studio, color, "shadows", 20)
    _adjust(studio, color, "tint", 4)
    studio.reset_all()
    _idle(studio)
    studio.show_history()
    _settle()
    assert [text for text, _state in _history(studio)] == [
        "打开 photo.png", "调色实验室 · 阴影 +12", "调色实验室 · 已重置", "调色实验室 · 阴影 +20",
        "调色实验室 · 色调 +4", "全部重置"]
    language.set_language("en")
    _settle()
    assert [text for text, _state in _history(studio)][:3] == [
        "Opened photo.png", "Color Lab · Shadows +12", "Color Lab · Reset"]
    assert studio.sidebar_tabs.currentText() == "History"


def test_history_and_edit_stack_are_empty_without_a_photo(editor):
    studio, color = editor()
    _adjust(studio, color, "shadows", 12)
    studio.show_history()
    _settle()
    assert len(studio.history_panel.steps()) == 2
    studio.load_image_path(None)
    _settle()
    assert studio.history_panel.is_empty() and studio.tool_sidebar.edit_stack.rows() == ()
