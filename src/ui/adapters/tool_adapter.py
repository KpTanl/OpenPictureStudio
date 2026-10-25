"""ToolAdapter: the parameter mapping contract between UI and Workflow.

Panels own transient widget state; workflows own engine-facing parameters. The
adapter is the only place allowed to convert between the two. It must never let
QWidget, QImage, callbacks or paths leak into workflow parameters.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable

from ...application.result_types import CoreError


@runtime_checkable
class ToolAdapter(Protocol):
    """Bidirectional UI/workflow parameter mapper for one tool."""

    tool_id: str

    def default_params(self) -> Dict[str, Any]:
        """Canonical default UI parameter set."""

    def ui_to_workflow(self, ui_state: Dict[str, Any]) -> Dict[str, Any]:
        """Translate UI values into engine/workflow parameters."""

    def workflow_to_ui(self, workflow_params: Dict[str, Any]) -> Dict[str, Any]:
        """Translate engine/workflow parameters back into UI values."""

    def validate(self, ui_state: Dict[str, Any]) -> List[CoreError]:
        """Return a list of structured errors; empty means valid."""


class ColorAdjustAdapter:
    """UI <-> workflow mapping for the color lab tool.

    Unit conversion examples:
        UI ``exposure_amt = 35``  -> workflow ``exposure = 0.35``
        UI ``contrast_amt = 28``  -> workflow ``contrast = 1.1``
    """

    tool_id = "color_adjust"

    def default_params(self):
        from ...core.tools.color import COLOR_UI_PARAMETERS
        return COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version)

    def ui_to_workflow(self, ui_state):
        from ...core.tools.color import ui_to_engine
        return ui_to_engine(ui_state)

    def workflow_to_ui(self, workflow_params: Dict[str, Any]) -> Dict[str, Any]:
        from ...core.tools.color import engine_to_ui
        return engine_to_ui(workflow_params)

    def validate(self, ui_state):
        from ...core.tools.color import color_ui_contract
        contract = color_ui_contract(ui_state)
        checked = contract.normalize(ui_state, contract.version)
        return [] if checked.success else [checked.error]


ADAPTERS: Dict[str, ToolAdapter] = {}


def register_adapter(adapter: ToolAdapter) -> None:
    ADAPTERS[adapter.tool_id] = adapter


def get_adapter(tool_id: str) -> ToolAdapter | None:
    return ADAPTERS.get(tool_id)


register_adapter(ColorAdjustAdapter())
