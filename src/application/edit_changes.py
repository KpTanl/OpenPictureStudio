"""两组编辑参数之间改了什么：编辑栈的摘要、历史里每一步的说明和 AI 候选的差异共用这一份比较。

参数按面板上的形式给出（各步骤一份字典）；这里只比较，不负责把结果写成文字。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..core.tools.color import curve_channels, mixer_values

# 嵌套的参数逐项比较：曲线按通道（curves.rgb），混合器按颜色与分量（color_mixer.orange.sat）。
# 展开方式与缺省时的中性取值由调色契约给出，两边写法不同（缺省、元组或列表）也按值比较。
_NESTED = {"curves": curve_channels, "color_mixer": mixer_values}


@dataclass(frozen=True)
class ParameterChange:
    key: str
    before: Any
    after: Any
    mask_name: str = ""


@dataclass(frozen=True)
class StageChange:
    """一个步骤的变化。kind：added 新加入、removed 被拿掉、changed 参数有改动。"""

    stage: str
    kind: str
    parameters: tuple[ParameterChange, ...]


def _unset(value) -> bool:
    """没有内容的取值：None 与空的容器等价。"""
    return value is None or (isinstance(value, (dict, list, tuple)) and not value)


def _expanded(key: str, value) -> dict:
    """嵌套参数的逐项取值，键为 “参数.通道” 或 “参数.颜色.分量”。"""
    items = {}
    for sub, item in _NESTED[key](value).items():
        if isinstance(item, dict):
            items.update({f"{key}.{sub}.{part}": part_value for part, part_value in item.items()})
        else:
            items[f"{key}.{sub}"] = item
    return items


def parameter_changes(before: Mapping[str, Any], after: Mapping[str, Any],
                      defaults: Mapping[str, Any]) -> tuple[ParameterChange, ...]:
    """按参数名比较；某一边没写的参数取默认值。顺序是默认值的声明顺序，其余参数排在后面。

    曲线与混合器逐通道、逐分量比较，每一项是一条变化。"""
    changes = []
    for key in dict.fromkeys((*defaults, *before, *after)):
        old = before.get(key, defaults.get(key))
        new = after.get(key, defaults.get(key))
        if old == new or (_unset(old) and _unset(new)):
            continue
        if key == "masks":
            changes.extend(mask_changes(old, new))
        elif key in _NESTED:
            olds, news = _expanded(key, old), _expanded(key, new)
            changes.extend(ParameterChange(item, olds[item], news[item]) for item in olds if olds[item] != news[item])
        else:
            changes.append(ParameterChange(key, old, new))
    return tuple(changes)


def mask_changes(before, after):
    def indexed(masks):
        result = {}
        for index, mask in enumerate(masks or ()):
            identity = mask.get("id")
            key = identity if isinstance(identity, str) and identity and identity not in result else "legacy:" + str(index)
            result[key] = mask
        return result
    olds, news = indexed(before), indexed(after)
    changes = []
    for identity in dict.fromkeys((*news, *olds)):
        old, new = olds.get(identity), news.get(identity)
        name = (new or old).get("name", "")
        prefix = "masks." + identity
        if old is None or new is None:
            changes.append(ParameterChange(prefix, old, new, name))
            continue
        for key in dict.fromkeys((*old, *new)):
            if key == "id":
                continue
            if key == "params":
                previous, current = old.get(key) or {}, new.get(key) or {}
                for part in dict.fromkeys((*previous, *current)):
                    a, b = previous.get(part, 0), current.get(part, 0)
                    if a != b:
                        changes.append(ParameterChange(prefix + ".params." + part, a, b, name))
            elif key == "components":
                previous, current = indexed(old.get(key)), indexed(new.get(key))
                component_prefix = prefix + ".components."
                for component_id in dict.fromkeys((*current, *previous)):
                    a, b = previous.get(component_id), current.get(component_id)
                    component_name = name + " · " + (b or a).get("name", "")
                    if a is None or b is None:
                        changes.append(ParameterChange(component_prefix + component_id, a, b, component_name))
                    else:
                        for part in dict.fromkeys((*a, *b)):
                            if part != "id" and a.get(part) != b.get(part):
                                changes.append(ParameterChange(component_prefix + component_id + "." + part,
                                                               a.get(part), b.get(part), component_name))
                if set(previous) == set(current) and tuple(previous) != tuple(current):
                    changes.append(ParameterChange(prefix + ".component_order", tuple(previous), tuple(current), name))
            else:
                a, b = old.get(key), new.get(key)
                if a != b:
                    changes.append(ParameterChange(prefix + "." + key, a, b, name))
    return changes


def stage_changes(before: Mapping[str, Mapping[str, Any]], after: Mapping[str, Mapping[str, Any]],
                  defaults: Callable[[str], Mapping[str, Any]]) -> tuple[StageChange, ...]:
    """逐步骤比较两组参数；``defaults(步骤)`` 给出该步骤没有改动时的参数。

    先按 ``after`` 的处理顺序列出新加入与有改动的步骤，再列出被拿掉的步骤；没有变化的步骤不出现。
    """
    changes = []
    for stage, params in after.items():
        neutral = defaults(stage)
        previous = before.get(stage)
        found = parameter_changes(neutral if previous is None else previous, params, neutral)
        if previous is None:
            changes.append(StageChange(stage, "added", found))
        elif found:
            changes.append(StageChange(stage, "changed", found))
    for stage, params in before.items():
        if stage not in after:
            neutral = defaults(stage)
            changes.append(StageChange(stage, "removed", parameter_changes(params, neutral, neutral)))
    return tuple(changes)
