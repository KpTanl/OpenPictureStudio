"""参考图、拟合、双候选与风格保存放进真实主窗口：服务、Core 与渲染都是真的，只把模型换成按顺序给出工具调用的替身。"""

import asyncio
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.application import assistant_conversations  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from tests.support import add_assistant_model, wait_for_studio, wait_for_thumbnails, wait_until  # noqa: E402

_TAB_ADJUST, _TAB_HISTORY, _TAB_ASSISTANT = range(3)


def _pump(times=30):
    for _ in range(times):
        QApplication.processEvents()


def _call(index, tool, **arguments):
    return {"text": "", "summary": "", "usage": {}, "protocol_items": [],
            "tool_calls": [{"id": f"call-{index}", "name": tool, "arguments": json.dumps(arguments, ensure_ascii=False)}]}


def _history_texts(studio):
    history = studio.core.history_state(studio.session.core_id, include_revisions=True).data["revisions"]
    return [studio._revision_text(revision, history[index - 1] if index else None)
            for index, revision in enumerate(history)]


def _images(tmp_path):
    rng = np.random.default_rng(3)
    base = np.zeros((400, 600, 3), np.uint8)
    base[:, :200], base[:, 200:400], base[:, 400:] = (170, 120, 90), (60, 120, 70), (70, 110, 190)
    base = np.clip(base + rng.normal(0, 18, base.shape), 0, 255).astype(np.uint8)
    photo, reference = str(tmp_path / "photo.png"), str(tmp_path / "reference.png")
    Image.fromarray(base).save(photo)
    Image.fromarray(np.clip(base.astype(int) * [1.15, 1.0, 0.8] + 10, 0, 255).astype(np.uint8)).save(reference)
    return photo, reference


def _open(window_factory, monkeypatch, tmp_path, replies):
    sent, outputs = [], []

    class _Request:
        def __init__(self):
            self.slots = asyncio.Semaphore(2)

        async def request(self, connection, payload, emit, **_options):
            sent.append(payload)
            outputs[:] = [json.loads(item["content"]) for item in payload.get("messages", ()) if item.get("role") == "tool"]
            reply = replies.pop(0) if replies else "好的。"
            if isinstance(reply, dict):
                return reply
            emit("text", reply)
            return {"text": reply, "summary": "", "usage": {}, "protocol_items": [], "tool_calls": []}

        async def models(self, connection):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(assistant_conversations, "AssistantRequestService", _Request)
    monkeypatch.setattr(assistant_conversations, "builtin_reasoning",
                        lambda provider, model: {"tool_calling": True, "image_input": True})
    ThemeManager().apply("light")
    photo, reference = _images(tmp_path)
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
    return studio, panel, runner, events, sent, outputs, reference


def _say(panel, text):
    panel.input.setPlainText(text)
    panel._input_edited(text)
    panel.request_suggestion()


def _groups(runner):
    return [group for message in runner.state.get("messages", ()) for group in (message.get("variants") or [])]


def _run(runner):
    return next((message["run"] for message in reversed(runner.state.get("messages", ())) if message.get("run")), {})


def test_reference_fit_and_two_results_adopted_as_one_step(qapp, window_factory, monkeypatch, tmp_path):
    previous = get_translation_service()
    set_translation_service(TranslationService(default_language="zh-CN", fallback_language="en"))
    replies = [
        _call(1, "compare_reference"),
        _call(2, "fit_reference", dimensions=["tone"], strength=60),
        _call(3, "propose_variants",
              A={"description": "暖一点", "color": {"temperature": 25, "curves": {"rgb": [[0, 6], [128, 140], [255, 252]]}}},
              B={"description": "冷一点", "color": {"temperature": -20, "color_mixer": {"blue": {"sat": 20}}}}),
    ]
    try:
        studio, panel, runner, events, sent, outputs, reference = _open(window_factory, monkeypatch, tmp_path, replies)
        runner.command("reference", identity=panel._identity, path=reference, photo=studio.assistant_context())
        wait_until(lambda: runner.state.get("reference"), "参考图载入", timeout=30)
        _say(panel, "参考这张图的色调，给我两个方向")
        wait_until(lambda: _groups(runner) and _run(runner).get("phase") == "waiting_user", "候选组生成", timeout=60)
        _pump(60)
        comparison, fit = outputs[0], outputs[1]
        assert comparison["ok"] and comparison["comparison"]["status"] == "available"
        # 默认强度下影调拟合给出建议，不再因截断折角跳过
        assert fit["ok"] and fit["status"] == "suggested" and "rgb" in fit["values"]["curves"], fit
        assert not any(item["code"] == "curve_precision_limit" for item in fit["skipped"])
        group = _groups(runner)[-1]
        assert group["status"] == "pending" and set(group["variants"]) == {"A", "B"}
        assert len(_history_texts(studio)) == 1     # 候选只是比较，不进历史
        # 等待选择时任务卡写明在等用户
        card = next(widget for widget in panel.transcript._widgets.values() if hasattr(widget, "badge"))
        assert card.badge.text() == panel._tr("assistant.task.state.waiting")

        panel._adopt_variant(group["id"], "A")
        wait_until(lambda: len(_history_texts(studio)) == 2, "采用 A", timeout=30)
        wait_until(lambda: studio.session.color_params.get("temperature") == 25, "界面同步", timeout=30)
        assert _history_texts(studio)[1].startswith("AI · 采用 A")
        wait_until(lambda: _groups(runner)[-1]["status"] == "adopted_A", "记为已采用 A", timeout=30)
        assert not [value for kind, value in events if kind == "error"]
    finally:
        set_translation_service(previous)


def test_older_pairs_do_not_block_new_tasks_and_saving_a_style_ends_the_wait(qapp, window_factory, monkeypatch, tmp_path):
    previous = get_translation_service()
    set_translation_service(TranslationService(default_language="zh-CN", fallback_language="en"))
    replies = [
        _call(1, "propose_variants", A={"description": "亮一点", "color": {"exposure_amt": 30}},
              B={"description": "暗一点", "color": {"exposure_amt": -30}}),
        "好的，先不选。",
        _call(2, "adjust_color", values={"exposure_amt": 20, "temperature": 10}, purpose="style"), "已提亮。",
        _call(3, "remember_style", kind="style", name="暖调", scene="室内"),
    ]
    try:
        studio, panel, runner, events, sent, outputs, _reference = _open(window_factory, monkeypatch, tmp_path, replies)
        _say(panel, "给我两个方向")
        wait_until(lambda: _groups(runner) and _run(runner).get("phase") == "waiting_user", "候选组生成", timeout=60)
        # 不选，任务结束；这一组仍可采用
        _say(panel, "先不选")
        wait_until(lambda: _run(runner).get("reason") == "completed" and not panel.is_busy(), "任务结束", timeout=30)
        assert _groups(runner)[-1]["status"] == "pending"
        # 新任务里的直接调整不被上一任务留下的这一组挡住；写入后编辑器变了，那一组随之失效
        _say(panel, "直接提亮一点")
        wait_until(lambda: len(_history_texts(studio)) == 2 and not panel.is_busy() and not panel._agent_active(),
                   "新任务写入", timeout=30)
        assert outputs[-1]["ok"] and outputs[-1]["changed"], outputs[-1]
        wait_until(lambda: _groups(runner)[-1]["status"] == "invalid", "旧候选组失效", timeout=30)

        # 保存风格：确认卡出现，确认后风格写入，等它的任务随之结束
        _say(panel, "记住这个风格")
        wait_until(lambda: (runner.state.get("style_pending") or {}).get("status") == "ready", "确认卡", timeout=30)
        _pump(30)
        assert _run(runner).get("phase") == "waiting_user"
        panel.style_confirmation._confirm()
        wait_until(lambda: any(style["name"] == "暖调" for style in runner.state.get("styles", ())), "风格写入", timeout=60)
        wait_until(lambda: _run(runner).get("reason") == "completed" and not panel._agent_active(), "任务收尾", timeout=30)
        style = next(style for style in runner.state["styles"] if style["name"] == "暖调")
        assert style["samples"][0]["delta"]["color"]["exposure_amt"] == 20
        assert not [value for kind, value in events if kind == "error"]
    finally:
        set_translation_service(previous)
