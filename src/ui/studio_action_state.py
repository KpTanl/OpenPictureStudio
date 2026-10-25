"""编辑工作区对外公布的动作状态快照，供顶栏与动作表投影。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StudioActionState:
    busy: bool = False  # 提交等不可打断的操作进行中
    can_undo: bool = False
    can_redo: bool = False
    can_save: bool = False
    can_export: bool = False
    can_apply: bool = False
    can_reset_all: bool = False
    title: str = ""  # 项目或图片的文件名
    unsaved: bool = False
    can_compare: bool = False
    compare_hint: str = ""  # 不能对比时的原因；可对比时为空
