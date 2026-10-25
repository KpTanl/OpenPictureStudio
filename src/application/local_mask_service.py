from __future__ import annotations

import copy
from dataclasses import dataclass
from uuid import uuid4

from ..core.contracts.result import CoreResult, ErrorCode
from ..core.local_masks import prepare_masks, upgrade_masks
from ..core.mask_components import prepare_components, upgrade_components
from ..core.tools.color import color_identity, migrate_legacy_color
from ..core.tools.effect_migration import reference_params


@dataclass
class MaskGesture:
    session_id: str
    state_version: int
    tool_generation: int
    source_identity: object
    params: dict
    identity: str
    generation: int = 0

    @classmethod
    def begin(cls, session_id, state_version, tool_generation, source_identity, params):
        return cls(session_id, state_version, tool_generation, source_identity, copy.deepcopy(params), uuid4().hex)

    def update(self, mask):
        candidate = copy.deepcopy(self.params)
        masks = candidate.get("masks") or []
        found = next((i for i, item in enumerate(masks) if item["id"] == mask["id"]), None)
        if found is None:
            masks.append(copy.deepcopy(mask))
        else:
            masks[found] = copy.deepcopy(mask)
        candidate["masks"] = masks
        (prepare_components if candidate.get("process_version") == 4 else prepare_masks)(masks, active_only=False)
        self.generation += 1
        return candidate

    def matches(self, session_id, state_version, tool_generation, source_identity):
        return (self.session_id, self.state_version, self.tool_generation, self.source_identity) == (
            session_id, state_version, tool_generation, source_identity)


def upgrade_color_masks(core, session_id, node_id, reference_size, *, expected_version, cancellation):
    from dataclasses import replace
    current = core.get_session(session_id)
    if not current.success:
        return current
    state = current.data
    if state.state_version != expected_version or cancellation.is_cancelled():
        return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
    node = next((item for item in state.draft_workflow.nodes if item.node_id == node_id), None)
    if node is None or not (node.node_type == "color_adjust" or node.node_type == "tool" and node.params["tool_id"] == "color_adjust"):
        return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.workflow.params")
    settings = copy.deepcopy(node.params if node.node_type == "color_adjust" else node.params["settings"])
    settings = migrate_legacy_color(settings)
    version = color_identity(settings)[1]
    if version == 4:
        return CoreResult.ok(state)
    try:
        gradients = settings.get("masks") if version == 3 else upgrade_masks(settings.get("masks"), reference_size)
        settings.update(masks=upgrade_components(gradients), process_version=4)
    except (ValueError, KeyError, TypeError) as error:
        return CoreResult.from_exception(error, code=ErrorCode.PARAMETER_INVALID, message_key=str(error))
    reference = core.tools.effect_reference("color_adjust", settings)
    if not reference.success:
        return reference
    workflow = replace(state.draft_workflow, nodes=tuple(
        replace(item, node_type="tool", version=1, params=reference_params(reference.data, settings))
        if item.node_id == node_id else item for item in state.draft_workflow.nodes))
    generation = core.tools.generation
    source_identity = core.source_identity(session_id)
    preview = core.preview(session_id, workflow, update_draft=False, max_dimension=1600,
                           expected_state=expected_version, is_cancelled=cancellation.is_cancelled)
    if not preview.success:
        return preview
    preview.data.close()
    return core.edit_workflow(session_id, workflow, label="mask.upgrade", expected_version=expected_version,
                              expected_tools=generation, expected_source=source_identity, cancellation=cancellation)
