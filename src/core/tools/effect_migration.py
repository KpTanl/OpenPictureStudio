from __future__ import annotations

import math

from .beauty import BEAUTY_PARAMETERS
from .denoise import DENOISE_PARAMETERS
from .effects import AI_PARAMETERS, BG_PARAMETERS
from .geometry import SHAPE_PARAMETERS, LASSO_PARAMETERS
from .id_photo import ID_PHOTO_PARAMETERS

MIGRATION_CONTRACTS = {"denoise": DENOISE_PARAMETERS, "beauty": BEAUTY_PARAMETERS,
                       "ai_enhance": AI_PARAMETERS, "bg_remove": BG_PARAMETERS,
                       "shape_crop": SHAPE_PARAMETERS, "lasso": LASSO_PARAMETERS,
                       "id_photo": ID_PHOTO_PARAMETERS}


def is_legacy_effect(node):
    if node.node_type == "studio_effect":
        return node.params.get("tool") in MIGRATION_CONTRACTS
    if node.node_type == "tool":
        return (node.params.get("tool_id") in MIGRATION_CONTRACTS
                and node.params.get("implementation_version") == "legacy-1"
                and node.params.get("params_version") == 1
                and node.params.get("implementation_id") == node.params.get("tool_id")
                or node.params.get("tool_id") == "beauty"
                and node.params.get("params_version") == 2
                and node.params.get("implementation_id") == "beauty"
                and node.params.get("implementation_version") == "2"
                or node.params.get("tool_id") == "denoise"
                and node.params.get("params_version") == 2
                and node.params.get("implementation_id") == "denoise.basic"
                and node.params.get("implementation_version") == "2")
    return False


def migrated_settings(tool_id, params, *, legacy_defaults=True):
    contract = MIGRATION_CONTRACTS[tool_id]
    if tool_id not in ("beauty", "denoise"):
        checked = contract.normalize(params, contract.version)
        if not checked.success:
            raise ValueError(checked.error.message_key)
        return checked.data
    displayed = contract.defaults(2)
    if tool_id == "denoise" and legacy_defaults:
        displayed.update({"strength": 40, "color_strength": 30})
    for field in contract.fields:
        value = params.get(field.name, displayed[field.name])
        if tool_id == "denoise" and field.name == "method" and value == "nlmeans":
            value = "bilateral"
        if field.kind is int and (type(value) is int or type(value) is float and math.isfinite(value)):
            value = int(value)
            if field.minimum is not None:
                value = max(field.minimum, value)
            if field.maximum is not None:
                value = min(field.maximum, value)
        if type(value) is field.kind and (not field.choices or value in field.choices):
            displayed[field.name] = value
    return displayed


def reference_params(reference, settings):
    return {
        "tool_id": reference.tool_id,
        "params_version": reference.params_version,
        "implementation_id": reference.implementation_id,
        "implementation_version": reference.implementation_version,
        "resources": [{"resource_id": key, "hash": digest} for key, digest in reference.resources],
        "settings": settings,
    }
