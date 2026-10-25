"""AI 助手放进真实编辑工作区：模型每次调用调色工具都立即写进历史，落在与手动编辑相同的参数上（滑块随之变化）；
切换侧栏页不打断任务；任务进行中用户改了照片，AI 不再接着调；按住对比看原图。"""

import asyncio
import json
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.application import assistant_conversations  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from tests.support import add_assistant_model, wait_for_studio, wait_for_thumbnails, wait_until  # noqa: E402

_TAB_ADJUST, _TAB_HISTORY, _TAB_ASSISTANT = range(3)


def _call(index, name, **arguments):
    return {"text": "", "summary": "", "usage": {}, "protocol_items": [],
            "tool_calls": [{"id": f"call-{index}", "name": name, "arguments": json.dumps(arguments)}]}


def _pixel(studio):
    image = studio.viewer._pixmap.toImage()
    color = image.pixelColor(image.width() // 2, image.height() // 2)
    return color.red(), color.green(), color.blue()


def _pump(times=30):
    for _ in range(times):
        QApplication.processEvents()


def _history(studio):
    return studio.core.history_state(studio.session.core_id, include_revisions=True).data["revisions"]


def _history_texts(studio):
    history = _history(studio)
    return [studio._revision_text(revision, history[index - 1] if index else None)
            for index, revision in enumerate(history)]


def _settled(studio, panel):
    wait_until(lambda: not panel._committing and studio._preview_queue.pending is None, "写入与渲染结束", timeout=30)
    _pump(60)


def _setup(window_factory, monkeypatch, tmp_path, replies, gates=None):
    """gates：第几个请求（从 0 数）先停住，等测试放行，用来在两轮之间切页或改照片。"""
    gates = gates or {}
    sent = []

    class _Request:
        def __init__(self):
            self.slots = asyncio.Semaphore(2)

        async def request(self, connection, payload, emit, **_options):
            index = len(sent)
            sent.append(payload)
            if index in gates:
                await asyncio.get_running_loop().run_in_executor(None, gates[index].wait, 20)
            reply = replies.pop(0)
            if isinstance(reply, dict):
                return reply
            emit("text", reply)
            return {"text": reply, "usage": {}, "summary": "", "protocol_items": [], "tool_calls": []}

        async def models(self, connection):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(assistant_conversations, "AssistantRequestService", _Request)
    monkeypatch.setattr(assistant_conversations, "builtin_reasoning", lambda provider, model: {"tool_calling": True})
    ThemeManager().apply("light")
    photo = str(tmp_path / "photo.png")
    Image.new("RGB", (600, 400), (90, 90, 90)).save(photo)
    window = window_factory()
    window.resize(1440, 900)
    window.show()
    window.manager.open_file(photo)
    window._refresh_all()
    wait_for_thumbnails(window.thumbnail_panel, [photo], photo, version=window.manager.list_version)
    studio = wait_for_studio(window, photo, "color_adjust")
    wait_until(lambda: studio._original_qimage is not None, "原图就绪", timeout=20)
    studio.sidebar_tabs.setCurrentIndex(_TAB_ASSISTANT)
    panel, runner = studio.assistant_panel, studio.assistant_panel._runner
    wait_until(lambda: runner.state["ready"], "助手就绪")
    events = []
    runner.event_received.connect(lambda kind, value: events.append((kind, value)))
    add_assistant_model(runner, events)
    wait_until(lambda: (runner.state.get("conversation") or {}).get("preferences", {}).get("model_id"), "会话绑定")
    panel.input.setPlainText("提亮一点")
    panel.request_suggestion()
    return window, studio, panel, sent


def test_ai_writes_are_history_steps_on_the_same_sliders(qapp, window_factory, monkeypatch, tmp_path):
    previous = get_translation_service()
    set_translation_service(TranslationService(default_language="zh-CN", fallback_language="en"))
    try:
        gate = threading.Event()
        replies = [_call(1, "adjust_color", values={"exposure_amt": 60}),
                   _call(2, "adjust_color", values={"exposure_amt": 100}), "满意。"]
        window, studio, panel, sent = _setup(window_factory, monkeypatch, tmp_path, replies, gates={1: gate})
        # 第一次写入后，下一次请求停在半路：历史里已经有这一步，切到“调整”页看滑块，任务不停
        wait_until(lambda: len(sent) == 2 and not panel._committing, "第一次写入", timeout=30)
        _settled(studio, panel)
        texts = _history_texts(studio)
        assert len(texts) == 2 and texts[1].startswith("AI · 第 1 步")
        studio.sidebar_tabs.setCurrentIndex(_TAB_ADJUST)
        _pump()
        color = studio._tool_panels["color_adjust"]
        assert color.parameters["exposure_amt"] == 60 and color._parameter_rows["exposure_amt"].value() == 60
        assert panel._agent_active()
        studio.sidebar_tabs.setCurrentIndex(_TAB_HISTORY)
        _pump()
        assert len(studio.history_panel.steps()) == 2 and panel._agent_active()
        gate.set()
        wait_until(lambda: len(sent) == 3 and not panel._agent_active() and not panel.is_busy(), "任务结束", timeout=30)
        _settled(studio, panel)
        texts = _history_texts(studio)
        assert len(texts) == 3 and texts[1].startswith("AI · 第 1 步") and texts[2].startswith("AI · 第 2 步")
        assert len(studio.history_panel.steps()) == 3
        assert studio.session.color_params["exposure_amt"] == 100
        assert color.parameters["exposure_amt"] == 100 and color._parameter_rows["exposure_amt"].value() == 100
        # 画面是写入后的结果；按住对比看原图，松开回来
        original = (90, 90, 90)
        result = _pixel(studio)
        assert result != original and not hasattr(studio, "_candidate_badge")
        button = window.canvas_toolbar._compare_button
        assert button.isEnabled() and button.isVisible()
        QTest.mousePress(button, Qt.MouseButton.LeftButton)
        _pump()
        assert _pixel(studio) == original
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
        _pump()
        assert _pixel(studio) == result
        # 与手动编辑同一条历史：撤销一步退回第 1 步，滑块跟着回去；重做回到第 2 步
        studio.undo()
        _settled(studio, panel)
        assert studio.session.color_params["exposure_amt"] == 60
        assert color.parameters["exposure_amt"] == 60
        studio.redo()
        _settled(studio, panel)
        assert studio.session.color_params["exposure_amt"] == 100
    finally:
        gate.set()
        set_translation_service(previous)


def test_editing_the_photo_stops_the_task(qapp, window_factory, monkeypatch, tmp_path):
    gate = threading.Event()
    replies = [_call(1, "adjust_color", values={"exposure_amt": 60}),
               _call(2, "adjust_color", values={"exposure_amt": 100})]
    window, studio, panel, sent = _setup(window_factory, monkeypatch, tmp_path, replies, gates={1: gate})
    try:
        _edit_during_the_loop(studio, panel, sent, gate)
    finally:
        gate.set()


def _edit_during_the_loop(studio, panel, sent, gate):
    wait_until(lambda: len(sent) == 2 and not panel._committing, "第一次写入", timeout=30)
    _settled(studio, panel)
    assert panel._agent_active()
    studio.session.color_params = {**studio.session.color_params, "contrast_amt": 20}
    studio._schedule_pipeline_render()
    _settled(studio, panel)
    wait_until(lambda: not panel._agent_active(), "改了照片，任务停下", timeout=30)
    run = next(message["run"] for message in panel._state["messages"] if message.get("run"))
    assert run["reason"] == "photo_changed"
    gate.set()
    wait_until(lambda: not panel.is_busy(), "晚到的回复不写入", timeout=30)
    _settled(studio, panel)
    assert studio.session.color_params["exposure_amt"] == 60 and studio.session.color_params["contrast_amt"] == 20
    assert [revision.label for revision in _history(studio) if revision.label.startswith("ai.")] == ["ai.tool.1"]
