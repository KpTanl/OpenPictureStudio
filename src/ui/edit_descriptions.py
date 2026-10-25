"""把编辑参数的变化写成界面上的文字：编辑栈的摘要、历史里每一步的说明与 AI 候选的差异共用。

改了什么由 ``application/edit_changes.py`` 比较；参数叫什么、读数怎么写取自各工具面板的 ``PARAMETER_LABELS``。
"""

from __future__ import annotations

from typing import Callable, Sequence

from ..application.edit_changes import ParameterChange, StageChange
from ..core.local_masks import LOCAL_UI_FIELDS, LOCAL_UI_SCALES
from .panels.registry import PANEL_TYPES
from .widgets.action_button import plain_action_text
from .widgets.value_editing import format_slider_value

Translate = Callable[..., str]


def stage_title(tool_id: str, tr: Translate) -> str:
    """步骤的名称，与该工具面板的标题一致；没有面板的步骤用它的标识。"""
    key = PANEL_TYPES.title_key(tool_id)
    return plain_action_text(tr(key, tool_id)) if key else tool_id


def _labels(tool_id: str):
    try:
        return PANEL_TYPES[tool_id].PARAMETER_LABELS
    except (KeyError, ImportError):
        return {}


def _label_keys(tool_id: str, key: str) -> list:
    """参数各级的名称键。嵌套参数逐级查：curves.rgb 查 curves 与 curves.rgb，
    color_mixer.orange.sat 查 color_mixer、color_mixer.orange 与 color_mixer.sat。"""
    labels = _labels(tool_id)
    head, *rest = key.split(".")
    return [labels.get(name, ("", ""))[0] for name in (head, *(head + "." + part for part in rest))]


def parameter_name(tool_id: str, key: str, tr: Translate) -> str:
    """参数在面板上的名称，嵌套参数逐级以 “·” 连接；面板没有声明的参数为空。"""
    if key.startswith("masks."):
        part = key.split(".")[-1]
        ui_key = LOCAL_UI_FIELDS.get(part)
        return tr("mask.title") + " · " + (parameter_name(tool_id, ui_key, tr) if ui_key else tr("mask.geometry"))
    label_keys = _label_keys(tool_id, key)
    names = [tr(label_key, "") for label_key in label_keys if label_key]
    return " · ".join(names) if len(names) == len(label_keys) and all(names) else ""


def value_text(tool_id: str, key: str, value) -> str:
    """参数取值的读数，写法与面板一致；不是数值或面板没有声明读数格式时为空。嵌套参数按最后一级的格式。"""
    if key.startswith("masks."):
        part = key.split(".")[-1]
        if part in LOCAL_UI_FIELDS and type(value) in (int, float):
            fmt = _labels(tool_id).get(LOCAL_UI_FIELDS[part], ("", "int_signed"))[1]
            return format_slider_value("int_signed" if part == "temperature" else fmt, value * LOCAL_UI_SCALES[part])
        return ""
    head, *rest = key.split(".")
    value_format = _labels(tool_id).get(head + "." + rest[-1] if rest else head, ("", ""))[1]
    if not value_format or isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return format_slider_value(value_format, int(round(value)))


def changes_summary(tool_id: str, changes: Sequence[ParameterChange], tr: Translate) -> str:
    """一句话说明一个步骤改了什么：只改一项时写出它的名称与读数，否则写改了几项；没有改动时为空。"""
    if not changes:
        return ""
    if any(change.key.startswith("masks.") for change in changes):
        descriptions = []
        for change in changes:
            if not change.key.startswith("masks."):
                descriptions.append(" ".join(text for text in (parameter_name(tool_id, change.key, tr),
                                                               value_text(tool_id, change.key, change.after)) if text))
                continue
            title = tr("mask.title") + " · " + (change.mask_name or tr("mask.name"))
            if change.before is None and change.key.count(".") in (1, 3):
                detail = tr("mask.added")
            elif change.after is None and change.key.count(".") in (1, 3):
                detail = tr("mask.deleted")
            elif ".params." in change.key:
                detail = parameter_name(tool_id, LOCAL_UI_FIELDS[change.key.split(".")[-1]], tr) + " " + value_text(tool_id, change.key, change.after)
            else:
                part = change.key.split(".")[-1]
                if part == "enabled":
                    detail = tr("mask.enabled" if change.after else "mask.disabled")
                elif part == "name":
                    detail = tr("mask.name") + " " + str(change.after)
                elif part == "op":
                    detail = tr("mask.component." + change.after)
                elif part == "component_order":
                    detail = tr("mask.component.order")
                else:
                    detail = tr("mask.invert" if part == "invert" else "mask.geometry")
            descriptions.append(title + "：" + detail)
        return tr("history.joiner", ", ").join(dict.fromkeys(descriptions))
    if len(changes) == 1:
        name = parameter_name(tool_id, changes[0].key, tr)
        if name:
            return " ".join(text for text in (name, value_text(tool_id, changes[0].key, changes[0].after)) if text)
    return tr("edit_stack.changes", "{count} adjustments", count=len(changes))


def step_text(changes: Sequence[StageChange], tr: Translate) -> str:
    """历史里一步的说明；这一步没有改动任何步骤的参数时为空。"""
    if not changes:
        return ""
    if len(changes) > 1:
        return tr("history.joiner", ", ").join(stage_title(change.stage, tr) for change in changes)
    change = changes[0]
    title = stage_title(change.stage, tr)
    if change.kind == "removed":
        detail = tr("history.stage_reset", "Reset")
    elif change.parameters:
        detail = changes_summary(change.stage, change.parameters, tr)
    else:
        return title
    return tr("history.step", "{stage} · {detail}", stage=title, detail=detail)
