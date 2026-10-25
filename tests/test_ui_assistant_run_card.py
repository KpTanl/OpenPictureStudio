"""任务卡：最新一条有文字的回复作为正文只出现一次，其余消息按先后放进执行记录；Agent 任务的改动只在任务卡的汇总里显示。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.i18n import TranslationService  # noqa: E402
from src.ui.widgets.assistant_transcript import AssistantRunCard  # noqa: E402

_TRANSLATIONS = TranslationService(default_language="zh-CN", fallback_language="en")


def _tr(key, default=None, **values):
    return _TRANSLATIONS.translate(key, default=default, **values)


def _ids(layout):
    return [layout.itemAt(index).widget()._id for index in range(layout.count())]


def test_latest_reply_is_the_body_and_other_messages_stay_in_order_in_the_records(qapp):
    changes = [{"tool": "beauty", "key": "skin_smooth", "before": 0, "after": 20}]
    reply = {"id": "reply", "role": "assistant", "status": "completed", "mode": "agent", "run_id": "run",
             "text": "先轻度磨皮。", "action": {"outcome": "applied", "changes": changes},
             "run": {"id": "run", "runtime": "agent", "reason": "", "phase": "tool_running", "applied_rounds": 1,
                     "changes": changes, "tool_calls": [
                         {"id": "call", "name": "adjust_beauty", "status": "completed", "result": {"ok": True}}]}}
    card = AssistantRunCard([reply], _tr)
    try:
        assert _ids(card._summary) == ["reply"] and _ids(card._record_layout) == []
        assert not card._round_widgets["reply"].changes.isVisibleTo(card)
        assert card.rounds.isVisibleTo(card) and not card._record_messages.isVisibleTo(card)

        # 中途追加一句话：新回复还没有文字时，正文仍是上一条回复，追加的话和新回复按先后进执行记录
        steer = {"id": "steer", "role": "user", "status": "completed", "run_id": "run", "text": "再自然一点"}
        final = {"id": "final", "role": "assistant", "status": "streaming", "mode": "agent", "run_id": "run", "text": ""}
        card.set_messages([reply, steer, final], _tr)
        assert _ids(card._summary) == ["reply"] and _ids(card._record_layout) == ["steer", "final"]
        assert card._round_widgets["final"].status.text() == ""

        # 新回复有了文字就成为正文，上一条回复回到执行记录，仍排在追加的话前面
        card.update_message({**final, "text": "已收住。"}, _tr)
        assert _ids(card._summary) == ["final"] and _ids(card._record_layout) == ["reply", "steer"]
    finally:
        card.deleteLater()
