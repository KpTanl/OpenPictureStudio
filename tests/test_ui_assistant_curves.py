"""AI 读写曲线与八色混合器，放进真实主窗口：读到手动设置的实际值，只改给出的通道与分量，
写成历史里的一步，曲线控件与混合器随之变化，改动卡与历史逐通道、逐分量写出改了什么。"""

import asyncio
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.application import assistant_conversations  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.theme import ThemeManager  # noqa: E402
from src.ui.widgets.assistant_transcript import change_rows  # noqa: E402
from tests.support import add_assistant_model, wait_for_studio, wait_for_thumbnails, wait_until  # noqa: E402

_TAB_ADJUST, _TAB_HISTORY, _TAB_ASSISTANT = range(3)


def _pump(times=30):
    for _ in range(times):
        QApplication.processEvents()


def _history_texts(studio):
    history = studio.core.history_state(studio.session.core_id, include_revisions=True).data["revisions"]
    return [studio._revision_text(revision, history[index - 1] if index else None)
            for index, revision in enumerate(history)]


def _call(name, **arguments):
    return {"text": "", "summary": "", "usage": {}, "protocol_items": [],
            "tool_calls": [{"id": "call-" + name, "name": name, "arguments": json.dumps(arguments)}]}


def test_ai_reads_and_writes_curves_and_mixer_in_the_real_window(qapp, window_factory, monkeypatch, tmp_path):
    previous = get_translation_service()
    set_translation_service(TranslationService(default_language="zh-CN", fallback_language="en"))
    sent = []
    replies = [
        _call("inspect_photo"),
        _call("adjust_color", values={"exposure_amt": 20,
                                      "curves": {"rgb": [[0, 8], [128, 140], [255, 250]]},
                                      "color_mixer": {"orange": {"sat": -12}}}),
        {"text": "完成。", "summary": "", "usage": {}, "protocol_items": [], "tool_calls": []},
    ]

    class _Request:
        def __init__(self):
            self.slots = asyncio.Semaphore(2)

        async def request(self, connection, payload, emit):
            sent.append(payload)
            return replies.pop(0)

        async def models(self, connection):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(assistant_conversations, "AssistantRequestService", _Request)
    monkeypatch.setattr(assistant_conversations, "builtin_reasoning",
                        lambda provider, model: {"tool_calling": True, "image_input": True})
    try:
        ThemeManager().apply("light")
        photo = str(tmp_path / "photo.png")
        Image.new("RGB", (600, 400), (150, 110, 80)).save(photo)
        window = window_factory()
        window.resize(1440, 900)
        window.show()
        window.manager.open_file(photo)
        window._refresh_all()
        wait_for_thumbnails(window.thumbnail_panel, [photo], photo, version=window.manager.list_version)
        studio = wait_for_studio(window, photo, "color_adjust")
        wait_until(lambda: studio._original_qimage is not None, "原图就绪", timeout=20)
        color = studio._tool_panels["color_adjust"]

        # 手动：红色曲线拉一条改黑白场的直线，橙色饱和度 +15
        color._curves_widget.set_curves({"r": [(0, 20), (255, 235)]})
        color._on_curve_changed()
        mixer = color._mixer_widget.get_values()
        mixer["orange"]["sat"] = 15
        color._mixer_widget.set_values(mixer)
        color._on_mixer_changed()
        wait_until(lambda: len(_history_texts(studio)) == 3, "两步手动编辑", timeout=20)
        texts = _history_texts(studio)
        assert texts[1] == "调色实验室 · 色调曲线 · 红" and texts[2] == "调色实验室 · 色彩混合 · 橙 · 饱和度 +15"

        studio.sidebar_tabs.setCurrentIndex(_TAB_ASSISTANT)
        panel, runner = studio.assistant_panel, studio.assistant_panel._runner
        wait_until(lambda: runner.state["ready"], "助手就绪")
        events = []
        runner.event_received.connect(lambda kind, value: events.append((kind, value)))
        add_assistant_model(runner, events)
        wait_until(lambda: (runner.state.get("conversation") or {}).get("preferences", {}).get("model_id"), "会话绑定")
        panel.input.setPlainText("暖一点、压一下橙色")
        panel.request_suggestion()
        wait_until(lambda: not replies and not panel.is_busy(), "任务结束", timeout=30)
        wait_until(lambda: len(_history_texts(studio)) == 4, "AI 写入一步", timeout=30)
        wait_until(lambda: studio.session.color_params.get("exposure_amt") == 20, "界面取回写入后的参数", timeout=30)
        wait_until(lambda: studio._preview_queue.pending is None, "渲染结束", timeout=30)
        _pump(60)

        # 模型读到的是手动设置的实际值，中性的通道与分量不列出
        inspected = next(json.loads(item["content"]) for item in sent[1]["messages"] if item.get("role") == "tool")
        assert inspected["adjustments"]["color"]["curves"] == {"r": [[0, 20], [255, 235]]}
        assert inspected["adjustments"]["color"]["color_mixer"] == {"orange": {"sat": 15}}

        # 只改了给出的通道与分量，写成历史里的一步，控件随之变化
        params = studio.session.color_params
        assert [list(point) for point in params["curves"]["rgb"]] == [[0, 8], [128, 140], [255, 250]]
        assert [list(point) for point in params["curves"]["r"]] == [[0, 20], [255, 235]]
        assert params["color_mixer"]["orange"] == {"hue": 0, "sat": -12, "lum": 0} and params["exposure_amt"] == 20
        texts = _history_texts(studio)
        assert len(texts) == 4 and texts[3] == "AI · 第 1 步 · 调色实验室 · 3 项调整"
        studio.sidebar_tabs.setCurrentIndex(_TAB_ADJUST)
        _pump()
        assert [list(point) for point in color._curves_widget.get_curves()["rgb"]] == [[0, 8], [128, 140], [255, 250]]
        assert color._mixer_widget.get_values()["orange"]["sat"] == -12

        # 改动卡逐通道、逐分量
        message = next(item for item in runner.state["conversation"]["messages"].values()
                       if (item.get("action") or {}).get("changes"))
        tr = get_translation_service().translate
        rows = [(row.name, row.before, row.after) for row in change_rows(message["action"]["changes"], tr)]
        assert rows[0][1:] == ("+0.00", "+0.20")
        assert rows[1:] == [("色调曲线 · RGB", "", ""), ("色彩混合 · 橙 · 饱和度", "+15", "-12")]

        # 撤销回到 AI 之前，重做回来
        assert studio.session.undo() and studio.session.color_params["color_mixer"]["orange"]["sat"] == 15
        assert studio.session.redo() and studio.session.color_params["color_mixer"]["orange"]["sat"] == -12
    finally:
        set_translation_service(previous)
