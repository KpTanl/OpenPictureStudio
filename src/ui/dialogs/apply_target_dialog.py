"""Ask the user how an applied edit should be saved."""

from __future__ import annotations

from enum import Enum
from typing import Callable, Optional

from PyQt6.QtWidgets import QWidget

from ..widgets.message_dialog import DialogButton, ask_choice


class ApplyTarget(str, Enum):
    SAVE_AS = "save_as"
    OVERWRITE = "overwrite"
    CANCEL = "cancel"


def ask_apply_target(
    parent: Optional[QWidget],
    tr: Callable[..., str],
) -> ApplyTarget:
    """三选一：另存为、覆盖原图、取消；回车是另存为，Esc 是取消。"""
    choice = ask_choice(
        parent,
        tr("apply.target.title", "Apply changes"),
        tr("apply.target.message", "How do you want to save the applied changes?"),
        (DialogButton(ApplyTarget.CANCEL.value, tr("apply.target.cancel", "Cancel"), "text"),
         DialogButton(ApplyTarget.OVERWRITE.value, tr("apply.target.overwrite", "Overwrite original")),
         DialogButton(ApplyTarget.SAVE_AS.value, tr("apply.target.save_as", "Save as…"), "primary")),
        detail=tr("apply.target.detail", "Overwriting replaces the original file and cannot be undone."),
        default=ApplyTarget.SAVE_AS.value, escape=ApplyTarget.CANCEL.value)
    return ApplyTarget(choice)
