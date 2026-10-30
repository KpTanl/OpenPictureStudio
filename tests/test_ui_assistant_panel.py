"""AI 助手面板：经宿主取上下文、经 AssistantRunner 把命令交给会话服务。模型经工具调用调整照片，每次有效写入
立即成为历史里的一步，落在与手动编辑相同的工具参数上，可以撤销、重做；停止或改了照片之后不再写入。

美颜只在宿主说它可用且画面里检测到人脸时开放。

服务、存储与后台循环都是真的（数据库在临时目录），只把网络请求换成替身：替身按顺序给出工具调用或文字回复。
"""

import ast
import asyncio
import copy
import inspect
import itertools
import json
from contextlib import nullcontext
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from src.application import assistant_conversations  # noqa: E402
from src.application.assistant_conversations import AssistantConversationService  # noqa: E402
from src.application.photo_library import LIBRARY_TOOLS  # noqa: E402
from src.application.result_types import CoreResult  # noqa: E402
from src.core.tools.color import COLOR_UI_PARAMETERS  # noqa: E402
from src.i18n import TranslationService, get_translation_service, set_translation_service  # noqa: E402
from src.ui.panels import ai_assistant_panel  # noqa: E402
from src.ui.panels.ai_assistant_panel import AIAssistantPanel, AssistantContext  # noqa: E402
from src.ui.services.assistant_runner import AssistantRunner  # noqa: E402
from src.ui.widgets.apple_menu import AppleMenu  # noqa: E402
from tests.support import add_assistant_model, wait_until  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OBJECT_KEY, PHOTO_NAME = "source:fixture", "sunset.jpg"


@pytest.fixture
def language(qapp):
    previous = get_translation_service()
    service = TranslationService(default_language="zh-CN", fallback_language="en")
    set_translation_service(service)
    yield service
    set_translation_service(previous)


class _Workflow:
    def __init__(self, name, color=None, beauty=None):
        self.name, self.nodes = name, ()
        self.color, self.beauty = dict(color or {}), dict(beauty or {})

    def workflow_hash(self):
        return self.name


class _Photo:
    """宿主、Core 与 Agent 替身共用的一份编辑状态：每个修订对应一个工作流，工作流里是调色与美颜参数。"""

    def __init__(self):
        self.loaded = True
        self.state_version = 7
        self.revisions = ["rev-1"]
        self.current = 0
        self.workflow = _Workflow("draft", {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version),
                                            "highlights": -38})
        self.workflows = {"rev-1": self.workflow}
        self.beauty_state = "disabled"   # 宿主报告的美颜可用状态：空字符串表示可用

    @property
    def color(self):
        return self.workflow.color

    @property
    def beauty(self):
        return self.workflow.beauty

    def move_to(self, revision):
        self.current, self.workflow = self.revisions.index(revision), self.workflows[revision]
        self.state_version += 1

    def edit(self, **color):
        """用户手动改参数：不进历史的草稿修改。"""
        self.workflow = _Workflow(self.workflow.name + "-edited", {**self.color, **color}, self.beauty)
        self.state_version += 1


class _Host:
    def __init__(self, photo):
        self.photo, self.stopped, self.candidates = photo, 0, []

    def assistant_binding(self):
        return (OBJECT_KEY, PHOTO_NAME) if self.photo.loaded else ("", "")

    def assistant_context(self):
        photo = self.photo
        if not photo.loaded:
            return None
        return AssistantContext("session", photo.state_version, dict(photo.color), photo.workflow, OBJECT_KEY,
                                PHOTO_NAME, "document", "hash", photo.revisions[photo.current], 6000, 4000,
                                beauty=dict(photo.beauty), tools={"beauty": photo.beauty_state})

    def assistant_candidate(self, context, adjustments):
        if context.state_version != self.photo.state_version:
            raise ValueError("STALE_CANDIDATE")
        self.candidates.append(dict(adjustments))
        return _Workflow(f"candidate-{len(self.candidates)}", {**self.photo.color, **adjustments.get("color", {})},
                         {**self.photo.beauty, **adjustments.get("beauty", {})})

    def stop_preview(self):
        self.stopped += 1

    def assistant_history_impact(self, affected):
        return ""


class _Core:
    image_sources = SimpleNamespace(resources=SimpleNamespace(working_memory=lambda *_args, **_kwargs: nullcontext()),
                                    compute=SimpleNamespace(borrow=lambda *_args, **_kwargs: nullcontext()))
    # 观察画面的几何身份按工具声明区分；替身没有工具，各节点都计入几何
    tools = SimpleNamespace(descriptors=lambda: ())

    def __init__(self, photo):
        self.photo = photo

    def preview(self, session_id, workflow, *, update_draft=False, max_dimension=1024, is_cancelled=None):
        return CoreResult.ok(SimpleNamespace(image=Image.new("RGB", (96, 64), (150, 120, 100)), full_size=None))

    def get_session(self, session_id):
        photo = self.photo
        return CoreResult.ok(SimpleNamespace(state_version=photo.state_version, draft_workflow=photo.workflow,
                                             current_revision=SimpleNamespace(revision_id=photo.revisions[photo.current])))

    def history_timeline(self, session_id):
        return CoreResult.ok({"revisions": [SimpleNamespace(revision_id=identity) for identity in self.photo.revisions],
                              "current": self.photo.current})

    def checkout(self, session_id, revision, *, expected_state=None, keep_redo_path=False):
        if expected_state != self.photo.state_version:
            return CoreResult.fail("STALE_CANDIDATE", "error.agent.stale_candidate")
        self.photo.move_to(revision)
        return CoreResult.ok(None)


class _Agent:
    def __init__(self, photo):
        self.photo = photo
        self.proposed, self.previewed, self.committed, self.discarded = [], [], [], []
        self.labels, self.workflows = [], {}

    def propose(self, session_id, workflow, *, model_version="", expected_state=None, audit_context=None):
        self.proposed.append((session_id, workflow.workflow_hash(), model_version, expected_state))
        candidate = f"candidate-{len(self.proposed)}"
        self.workflows[candidate] = workflow
        return CoreResult.ok({"candidate_id": candidate})

    def preview(self, candidate, *, is_cancelled=None, max_dimension=None):
        self.previewed.append(candidate)
        return CoreResult.ok(SimpleNamespace(image=Image.new("RGB", (96, 64), (160, 128, 108)), full_size=None))

    def commit(self, candidate, *, confirmed=False, is_cancelled=None, label=""):
        self.committed.append((candidate, confirmed))
        self.labels.append(label)
        photo = self.photo
        revision = f"rev-{len(photo.revisions) + 1}"
        del photo.revisions[photo.current + 1:]
        photo.revisions.append(revision)
        photo.workflows[revision] = self.workflows[candidate]
        photo.move_to(revision)
        return CoreResult.ok(SimpleNamespace(revision=SimpleNamespace(revision_id=revision)))

    def discard(self, candidate):
        self.discarded.append(candidate)


def _binding_error(name, keywords):
    """界面发出的命令能否经 AssistantRunner 原样调用服务端的 `_command_<name>`；能则返回 None。"""
    try:
        inspect.signature(AssistantRunner.command).bind(None, name, **dict.fromkeys(keywords))
    except TypeError as exc:
        return f"AssistantRunner.command 收不下 {name} {sorted(keywords)}：{exc}"
    if name == "send" and {"scope_kind", "scope_revision"} & set(keywords):
        # 运行器把批量范围换成 batch_context 再交给服务
        keywords = set(keywords) - {"scope_kind", "scope_revision"} | {"batch_context"}
    method = getattr(AssistantConversationService, "_command_" + name, None)
    if method is None:
        return f"服务没有 _command_{name}"
    try:
        inspect.signature(method).bind(None, **dict.fromkeys(keywords))
    except TypeError as exc:
        return f"_command_{name}{inspect.signature(method)} 不接受 {sorted(keywords)}：{exc}"
    return None


@pytest.fixture
def assistant(tmp_path, monkeypatch, language, qapp):
    network = SimpleNamespace(replies=[], payloads=[], before_reply=None, faces=True)

    class _Request:
        def __init__(self):
            self.slots = asyncio.Semaphore(2)

        async def request(self, connection, payload, emit, **_options):
            network.payloads.append(payload)
            reply = network.replies.pop(0) if network.replies else "好的。"
            if network.before_reply:
                network.before_reply()
                await asyncio.sleep(0.05)      # 让替身在回复前发出的命令先在服务循环里执行
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
    runner = AssistantRunner(str(tmp_path / "assistant"))
    sent, events = [], []
    original = runner.command

    def command(name, /, **values):
        sent.append((name, copy.deepcopy(values)))
        original(name, **values)

    monkeypatch.setattr(runner, "command", command)
    runner.event_received.connect(lambda kind, value: events.append((kind, value)))
    wait_until(lambda: runner.state["ready"], "助手服务就绪")
    add_assistant_model(runner, events, "fixture-model")
    wait_until(lambda: runner.state["default_model"], "添加模型")

    photo = _Photo()
    host, core, agent = _Host(photo), _Core(photo), _Agent(photo)
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    runner.set_image_services(core, pool, face_detector=lambda image, params, cancelled: network.faces, agent=agent)
    panel = AIAssistantPanel(host, core, agent, pool, runner=runner)
    signals = SimpleNamespace(renders=[], commits=[], busy=[])
    panel.render_requested.connect(lambda: signals.renders.append(True))
    panel.committed.connect(signals.commits.append)
    panel.commit_busy.connect(signals.busy.append)
    panel.resize(420, 760)
    panel.show()
    wait_until(lambda: panel._identity and panel._requested_identity is None
               and (runner.state.get("conversation") or {}).get("preferences", {}).get("model_id"), "会话绑定到照片")
    env = SimpleNamespace(panel=panel, runner=runner, host=host, core=core, agent=agent, photo=photo,
                          network=network, sent=sent, events=events, signals=signals, tmp_path=tmp_path)
    yield env
    panel.cleanup()
    wait_until(lambda: pool.activeThreadCount() == 0, "助手任务结束")
    panel.close()
    runner.shutdown()
    problems = [problem for name, values in sent if (problem := _binding_error(name, values))]
    assert not problems, problems


def _state(env):
    return env.runner.state.get("conversation") or {}


def _errors(env):
    return [value for kind, value in env.events if kind == "error"]


def _ask(env, text="阴影太暗了"):
    before = len(env.runner.state.get("messages", ()))
    env.panel.input.setPlainText(text)
    env.panel.refresh()
    env.panel.request_suggestion()
    wait_until(lambda: not env.panel.is_busy() and len(env.runner.state.get("messages", ())) >= before + 2
               and env.runner.state["messages"][-1]["status"] in ("completed", "failed", "stopped")
               and not env.panel._agent_active(), "助手回复结束")
    QApplication.processEvents()


_CALLS = itertools.count(1)


def _call(name, **arguments):
    """模型的一次工具调用；调用编号在整个测试里不重复。"""
    return {"text": "", "summary": "", "usage": {}, "protocol_items": [], "tool_calls": [
        {"id": f"call-{next(_CALLS)}", "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}]}


def _tool_outputs(payload):
    return [json.loads(item["content"]) for item in payload.get("messages", ()) if item.get("role") == "tool"]


def _tools(payload):
    return {item["function"]["name"] for item in payload.get("tools", ())}


def _run(env):
    return next((message["run"] for message in env.runner.state.get("messages", ()) if message.get("run")), {})


def _finished(env):
    return bool(_run(env).get("reason")) and not env.panel.is_busy()


def _trigger(menu_owner, text):
    menu = menu_owner.findChildren(AppleMenu)[-1]
    action = next(action for action in menu.actions() if action.text() == text)
    action.trigger()
    menu.close()


def test_a_panel_with_a_photo_binds_its_conversation_to_that_photo(assistant):
    panel, sent = assistant.panel, assistant.sent
    assert ("photo", {"object_key": OBJECT_KEY, "name": PHOTO_NAME}) in sent
    record = _state(assistant)
    assert record["object_key"] == OBJECT_KEY and record["photo_name"] == PHOTO_NAME
    assert record["preferences"]["mode"] == "agent"
    # 第一行是照片名，第二行写明会发送什么
    assert panel.photo.text().splitlines()[0] == PHOTO_NAME
    assert ("history_position", {"object_key": OBJECT_KEY, "revisions": ["rev-1"], "current": 0}) in sent
    assert not _errors(assistant)


def test_every_command_the_interface_sends_binds_to_the_service():
    calls = []
    for path in sorted((ROOT / "src" / "ui").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "command"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                continue
            if node.args[0].value in LIBRARY_TOOLS:
                continue   # 图库窗口经自己的 command(操作, 取值, 回调) 交给图库运行器，是另一套契约
            # 运行器自己的方法里用 self.command 转交服务，或在服务线程里直接以 (命令, 取值字典) 调用服务，同样是这套契约
            owner = ast.unparse(node.func.value)
            internal = path.name == "assistant_runner.py" and owner in ("self", "self._service")
            assert any(name in owner for name in ("runner", "assistant")) or internal, ast.unparse(node)
            if owner == "self._service":
                assert len(node.args) == 2 and isinstance(node.args[1], ast.Dict), f"{path.name}:{node.lineno} 无法确定取值"
                calls.append((f"{path.relative_to(ROOT)}:{node.lineno}", node.args[0].value,
                              {key.value for key in node.args[1].keys}))
                continue
            assert len(node.args) == 1, f"{path.name}:{node.lineno} 命令的参数要用关键字传"
            variants = [set()]
            for keyword in node.keywords:
                if keyword.arg:
                    variants = [variant | {keyword.arg} for variant in variants]
                    continue
                value = keyword.value
                branches = (value.body, value.orelse) if isinstance(value, ast.IfExp) else (value,)
                assert all(isinstance(branch, ast.Dict) for branch in branches), f"{path.name}:{node.lineno} 无法确定关键字"
                variants = [variant | {key.value for key in branch.keys} for variant in variants for branch in branches]
            calls.extend((f"{path.relative_to(ROOT)}:{node.lineno}", node.args[0].value, variant) for variant in variants)
    assert len({name for _where, name, _keywords in calls}) >= 25
    problems = [f"{where} {problem}" for where, name, keywords in calls if (problem := _binding_error(name, keywords))]
    assert not problems, problems


def test_the_context_says_what_is_sent(assistant, language):
    panel = assistant.panel
    # 当前参数与照片分析默认发送，其余默认不带
    assert panel.composer.context.accessibleName() == language.translate("assistant.context_count", count=2)
    panel._preferences({"context": {"workflow": True}})
    wait_until(lambda: _state(assistant)["preferences"]["context"]["workflow"], "带上工作流")
    assert panel.composer.context.accessibleName() == language.translate("assistant.context_count", count=3)

    panel.composer.context.click()
    inspector = panel.context_side if not panel.context_side.isHidden() else panel.context_page
    assert inspector.photo.text() == language.translate("assistant.context_photo", name=PHOTO_NAME, version=7)
    assert inspector.manifest.isHidden()
    _ask(assistant)
    wait_until(lambda: not inspector.manifest.isHidden(), "列出上一轮发送的内容")
    assert language.translate("assistant.context.color") in inspector.manifest_text.text()
    payload = json.dumps(assistant.network.payloads[-1], ensure_ascii=False)
    # 模型不支持图片：不发图；照片分析按测量规格 1 发送摘要，完整计数不发
    assert "阴影太暗了" in payload and "image_url" not in payload and "input_image" not in payload
    assert "photo_analysis" in payload and "luma_percentiles" in payload and "histogram" not in payload
    # 有了对话之后历史也算一项
    assert panel.composer.context.accessibleName() == language.translate("assistant.context_count", count=4)
    language.set_language("en")
    assert panel.composer.context.accessibleName() == "Context · 4 items"


def test_an_adjustment_is_written_to_history_right_away(assistant):
    panel, agent, host, photo, signals = (assistant.panel, assistant.agent, assistant.host, assistant.photo,
                                          assistant.signals)
    network = assistant.network
    network.replies += [_call("adjust_color", values={"shadows": 32, "highlights": -52}), "提亮阴影并压低高光。"]
    _ask(assistant)
    wait_until(lambda: panel.candidate_outcomes() == ("applied",) and _finished(assistant), "写成历史里的一步")
    # 建候选、渲染校验后立即提交，没有等待用户应用的候选
    assert host.stopped == 1 and host.candidates == [{"color": {"shadows": 32, "highlights": -52}}]
    assert agent.proposed == [("session", "candidate-1", "fixture-model", 7)] and agent.previewed == ["candidate-1"]
    assert agent.committed == [("candidate-1", True)] and agent.labels == ["ai.tool.1"]
    assert signals.busy == [True, False] and len(signals.commits) == 1 and signals.commits[0] is not None
    # 落在与手动编辑相同的调色参数上；模型从工具结果读到实际改动与新修订
    assert (photo.color["shadows"], photo.color["highlights"]) == (32, -52) and photo.revisions == ["rev-1", "rev-2"]
    output = _tool_outputs(network.payloads[-1])[-1]
    assert output["ok"] and output["changed"] and output["revision"] == "rev-2"
    assert output["current_adjustments"]["color"]["shadows"] == 32
    message = assistant.runner.state["messages"][-1]
    action = message["action"]
    assert {(row["key"], row["before"], row["after"]) for row in action["changes"]} == {
        ("shadows", 0, 32), ("highlights", -38, -52)}
    assert (action["before_revision"], action["after_revision"]) == ("rev-1", "rev-2")
    assert panel.messages()[-1] == ("assistant", "提亮阴影并压低高光。") and _run(assistant)["reason"] == "completed"

    # 撤销这次调整回到 AI 之前，之后可以重做
    panel._message_action(message["id"], "undo_adjustment")
    wait_until(lambda: photo.current == 0 and not panel.is_busy(), "撤销这次调整")
    assert photo.color["shadows"] == 0 and signals.commits[-1] is None
    panel._message_action(message["id"], "redo_adjustment")
    wait_until(lambda: photo.current == 1 and not panel.is_busy(), "重做")
    assert photo.color["shadows"] == 32 and not agent.discarded


def test_an_edit_made_while_waiting_writes_nothing(assistant):
    agent, photo, network = assistant.agent, assistant.photo, assistant.network
    network.before_reply = lambda: photo.edit(contrast=10) if len(network.payloads) == 1 else None
    network.replies += [_call("adjust_color", values={"shadows": 32}), "好的。"]
    assistant.panel.input.setPlainText("阴影太暗了")
    assistant.panel.request_suggestion()
    wait_until(lambda: _finished(assistant), "照片变了，任务停下")
    assert _run(assistant)["reason"] == "photo_changed"
    assert not agent.proposed and not agent.committed and photo.revisions == ["rev-1"]
    assert photo.color.get("shadows", 0) == 0


def test_commands_from_the_panel_reach_the_service(assistant, monkeypatch):
    panel, runner = assistant.panel, assistant.runner
    tr = panel._tr
    first = panel._identity
    panel._preferences({"mode": "plan"})
    wait_until(lambda: _state(assistant)["preferences"]["mode"] == "plan", "切到方案模式")
    panel._input_edited("草稿")
    panel._flush_draft()
    wait_until(lambda: _state(assistant)["draft"] == "草稿", "保存草稿")
    panel._scroll_edited(0.5)
    panel.context_page.summary_edited.emit("摘要")

    panel.input.setPlainText("你好")
    panel.request_suggestion()
    wait_until(lambda: len(runner.state["messages"]) == 2 and runner.state["messages"][-1]["status"] == "completed",
               "对话回复")
    user, reply = runner.state["messages"]
    panel._summarize()
    panel._message_action(user["id"], "edit")
    wait_until(lambda: _state(assistant)["preferences"]["editing"] == user["id"], "编辑消息")
    panel.cancel_edit.click()
    wait_until(lambda: not _state(assistant)["preferences"]["editing"], "取消编辑")
    panel._message_menu(_state(assistant), reply)
    _trigger(panel, tr("assistant.branch_here"))
    panel._message_menu(_state(assistant), user)
    _trigger(panel, tr("assistant.withdraw_message"))
    wait_until(lambda: not runner.state["messages"], "撤回消息")

    panel._new_chat(True)
    wait_until(lambda: panel._identity != first and panel._requested_identity is None, "新建一般咨询")
    assert _state(assistant)["object_key"] == ""
    panel._select_conversation(first)
    wait_until(lambda: panel._identity == first and panel._requested_identity is None, "回到照片的会话")

    class _Rename:
        def __init__(self, *_args):
            self.name = SimpleNamespace(text=lambda: "新标题")

        def exec(self):
            return True

        def deleteLater(self):
            pass

    class _Export(_Rename):
        changes = images = SimpleNamespace(isChecked=lambda: True)

    target = assistant.tmp_path / "conversation.md"
    monkeypatch.setattr(ai_assistant_panel, "AssistantNameDialog", _Rename)
    monkeypatch.setattr(ai_assistant_panel, "AssistantExportDialog", _Export)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *_args, **_kwargs: (str(target), "")))
    monkeypatch.setattr(ai_assistant_panel, "confirm_destructive", lambda *_args: True)
    for key in ("assistant.rename_conversation", "assistant.pin", "assistant.export_conversation",
                "assistant.context_reset_start", "assistant.clear_attachments"):
        panel._conversation_menu()
        _trigger(panel, tr(key))
    wait_until(lambda: _state(assistant)["title"] == "新标题" and _state(assistant)["pinned"] and target.exists(),
               "改名、置顶与导出")
    panel._conversation_menu()
    _trigger(panel, tr("assistant.delete_conversation"))
    wait_until(lambda: first not in {item["id"] for item in runner.state["conversations"]}, "删除会话")
    wait_until(lambda: _state(assistant).get("object_key") == OBJECT_KEY, "照片换到新的会话")

    names = {name for name, _values in assistant.sent}
    assert names >= {"photo", "history_position", "preferences", "update", "summary_edit", "send", "summarize",
                     "edit", "cancel_edit", "branch", "withdraw", "new", "select", "export", "clear_attachments",
                     "delete"}
    # 只有“没有可压缩的对话”这一条是预期中的业务错误
    assert {error["code"] for error in _errors(assistant)} <= {"AI_SUMMARY_EMPTY"}


def test_beauty_is_offered_only_when_the_host_allows_it_and_a_face_is_found(assistant):
    panel, host, photo, network = assistant.panel, assistant.host, assistant.photo, assistant.network
    photo.beauty_state = ""
    network.replies += [_call("adjust_color", values={"shadows": 20}),
                        _call("adjust_beauty", values={"skin_smooth": 25, "face_slim": 10}), "完成。"]
    _ask(assistant, "自然一点地美颜")
    wait_until(lambda: panel.candidate_outcomes() == ("applied",) and _finished(assistant), "调色与美颜各写一步")
    assert "adjust_beauty" in _tools(network.payloads[0])
    assert host.candidates == [{"color": {"shadows": 20}}, {"beauty": {"skin_smooth": 25, "face_slim": 10}}]
    assert photo.color["shadows"] == 20 and photo.beauty == {"skin_smooth": 25, "face_slim": 10}
    assert assistant.agent.labels == ["ai.tool.1", "ai.tool.2"]
    # 任务卡汇总这次任务的全部改动
    assert {row["tool"] for row in _run(assistant)["changes"]} == {"color_adjust", "beauty"}

    # 没有检测到人脸：美颜工具不开放，模型硬要调用时得到明确的错误，照片不变
    network.faces = False
    network.replies += [_call("adjust_beauty", values={"skin_smooth": 40}), "那就不磨皮了。"]
    _ask(assistant, "再磨皮一点")
    assert "adjust_beauty" not in _tools(network.payloads[-2])
    assert _tool_outputs(network.payloads[-1])[-1]["error"]["code"] == "AI_TOOL_UNAVAILABLE"
    assert photo.beauty == {"skin_smooth": 25, "face_slim": 10}


def test_a_reply_without_tool_calls_is_kept_as_text(assistant):
    panel, photo = assistant.panel, assistant.photo
    assistant.network.replies.append("这张照片看起来已经不错了，我建议保持现状。")
    _ask(assistant)
    message = assistant.runner.state["messages"][-1]
    assert message["status"] == "completed" and not message.get("action")
    assert panel.messages()[-1] == ("assistant", "这张照片看起来已经不错了，我建议保持现状。")
    # 只有工具调用才会改照片：文字里夹着的参数照样只是文字
    assistant.network.replies.append('好的：{"adjustments": {"color": {"shadows": 15}}}')
    _ask(assistant)
    assert not assistant.runner.state["messages"][-1].get("action") and photo.revisions == ["rev-1"]
    assert not assistant.agent.proposed and not _errors(assistant)


def test_each_write_is_a_history_step_and_the_task_checks_the_result(assistant):
    panel, host, agent, network, photo = (assistant.panel, assistant.host, assistant.agent, assistant.network,
                                          assistant.photo)
    network.replies += [_call("adjust_color", values={"shadows": 30}), _call("analyze_photo"),
                        _call("adjust_color", values={"shadows": 40}), "已经符合要求。"]
    _ask(assistant)
    wait_until(lambda: _finished(assistant) and len(network.payloads) == 4, "任务结束")
    # 每次写入立即成为历史里的一步，下一步在写入后的状态上给出要改的参数
    assert agent.committed == [("candidate-1", True), ("candidate-2", True)]
    assert agent.labels == ["ai.tool.1", "ai.tool.2"] and not agent.discarded
    assert host.candidates == [{"color": {"shadows": 30}}, {"color": {"shadows": 40}}]
    assert agent.proposed[1][3] == 8 and photo.revisions == ["rev-1", "rev-2", "rev-3"] and photo.color["shadows"] == 40
    analysis = _tool_outputs(network.payloads[2])[-1]
    assert analysis["ok"] and analysis["revision"] == "rev-2" and "histogram" not in json.dumps(analysis)
    # 任务卡把几步汇总成一次调整；撤销这次调整一次回到 AI 开始前
    message = assistant.runner.state["messages"][-1]
    assert (message["action"]["before_revision"], message["action"]["after_revision"]) == ("rev-1", "rev-3")
    assert _run(assistant)["applied_rounds"] == 2
    panel._message_action(message["id"], "undo_adjustment")
    wait_until(lambda: photo.current == 0 and not panel.is_busy(), "回到 AI 开始前")
    assert photo.color["shadows"] == 0


def test_stopping_the_task_writes_nothing_more(assistant):
    panel, network, agent = assistant.panel, assistant.network, assistant.agent

    def stop():
        if len(network.payloads) == 2:
            assistant.runner.command("stop", identity=panel._identity)

    network.before_reply = stop
    network.replies += [_call("adjust_color", values={"shadows": 30}), _call("adjust_color", values={"shadows": 50})]
    panel.input.setPlainText("阴影太暗了")
    panel.request_suggestion()
    wait_until(lambda: _finished(assistant) and len(network.payloads) >= 2, "停止任务")
    QApplication.processEvents()
    # 停下之后到达的那一步不再写入；第一步仍在历史里，可以撤销
    assert _run(assistant)["reason"] == "stopped" and agent.labels == ["ai.tool.1"]
    assert assistant.photo.color["shadows"] == 30 and assistant.photo.revisions == ["rev-1", "rev-2"]


def test_editing_the_photo_stops_the_task(assistant):
    network, photo = assistant.network, assistant.photo
    network.before_reply = lambda: photo.edit(contrast=15) if len(network.payloads) == 2 else None
    network.replies += [_call("adjust_color", values={"shadows": 30}), _call("adjust_color", values={"shadows": 50})]
    assistant.panel.input.setPlainText("阴影太暗了")
    assistant.panel.request_suggestion()
    wait_until(lambda: _finished(assistant) and len(network.payloads) == 2, "改了照片，任务停下")
    assert _run(assistant)["reason"] == "photo_changed"
    assert assistant.agent.labels == ["ai.tool.1"] and photo.color["shadows"] == 30 and photo.color["contrast"] == 15


def test_a_failing_face_check_does_not_block_the_request(assistant):
    """人脸检测出错时只当作“不确定”，照片分析照常发送，请求不失败。"""
    def broken(image, params, cancelled):
        raise RuntimeError("detector crashed")

    assistant.photo.beauty_state = ""
    assistant.runner.set_image_services(assistant.core, assistant.panel._thread_pool, face_detector=broken,
                                        agent=assistant.agent)
    assistant.network.replies += [_call("adjust_color", values={"shadows": 12}), "好的。"]
    _ask(assistant)
    message = assistant.runner.state["messages"][-1]
    assert message["status"] == "completed" and message.get("action")
    assert "photo_analysis" in json.dumps(assistant.network.payloads[0], ensure_ascii=False)


def test_comparison_result_returns_from_worker_to_gui_thread(assistant, monkeypatch):
    panel = assistant.panel
    gui = threading.get_ident()
    workers, deliveries = [], []
    original = panel._comparison_ready

    def before_image(context, action, *, is_cancelled):
        workers.append(threading.get_ident())
        return {"image": Image.new("RGB", (8, 8), "red")}

    def receive(generation, context, result):
        deliveries.append(threading.get_ident())
        original(generation, context, result)

    monkeypatch.setattr(panel._actions, "before_image", before_image)
    monkeypatch.setattr(panel, "_comparison_ready", receive)
    wait_until(lambda: not panel.is_busy(), "助手空闲")
    panel._toggle_comparison({"id": "comparison", "action": {}})
    wait_until(lambda: bool(workers), "助手比较计算开始")
    wait_until(lambda: bool(deliveries), "助手比较结果送达")
    assert len(workers) == 1 and workers[0] != gui
    assert deliveries == [gui]
