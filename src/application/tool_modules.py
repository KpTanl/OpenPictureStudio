from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from functools import partial

from ..core.contracts.tools import ImageCapabilities, ToolDescriptor
from ..core.contracts.tool_resources import ModelRequirement, RequiredResource, ResourceFeature
from ..core.tools.catalog import ToolDefinition
from ..core.tools.color import COLOR_PARAMETERS, LEGACY_COLOR_PARAMETERS
from ..core.tools.geometry import CROP_PARAMETERS, SHAPE_PARAMETERS, LASSO_PARAMETERS, execute_crop, execute_shape, execute_lasso
from ..core.tools.id_photo import (ID_PHOTO_RESOURCE_FEATURES, MANAGED_ID_PHOTO_PARAMETERS,
                                  id_photo_required_resources)
from ..core.nodes.color_adjust_node import execute as execute_color
from ..core.photo_engine import mask_working_memory, minimum_render_threads


@dataclass(frozen=True)
class ToolModule:
    parameters: object
    execute: object
    image: ImageCapabilities
    supports_proxy: bool = True
    needs_preparation: Callable | None = None
    minimum_threads: Callable | None = None
    uses_thread_limit: bool = False
    uses_cancellation: bool = False
    working_memory: Callable | None = None
    tool_version: str = "2"
    implementation_version: str = "2"
    resource_features: tuple[ResourceFeature, ...] = (ResourceFeature("basic"),)
    collect_resources: Callable | None = None
    sampling_behavior: str = "unsupported"
    uses_sampling: bool = False


MODULES = {
    "color_adjust": ToolModule(COLOR_PARAMETERS, execute_color, ImageCapabilities(),
                               minimum_threads=minimum_render_threads, uses_thread_limit=True,
                               uses_cancellation=True, working_memory=mask_working_memory,
                               tool_version="7", implementation_version="7",
                               sampling_behavior="capture", uses_sampling=True),
    "crop": ToolModule(CROP_PARAMETERS, execute_crop, ImageCapabilities(alpha="transform", geometry="resize"),
                       sampling_behavior="transport", uses_sampling=True),
    "shape_crop": ToolModule(SHAPE_PARAMETERS, execute_shape, ImageCapabilities(alpha="transform", geometry="resize"),
                             sampling_behavior="transport", uses_sampling=True),
    "lasso": ToolModule(LASSO_PARAMETERS, execute_lasso, ImageCapabilities(alpha="transform"),
                        sampling_behavior="transport", uses_sampling=True),
}

MODULE_VERSIONS = {
    "color_adjust": tuple(ToolModule(LEGACY_COLOR_PARAMETERS[version], execute_color, ImageCapabilities(),
        minimum_threads=minimum_render_threads, uses_thread_limit=True, uses_cancellation=True,
        working_memory=mask_working_memory, tool_version=str(version), implementation_version=str(version),
        sampling_behavior="capture", uses_sampling=True) for version in (6, 5, 4)),
}


def module_for(tool_id, settings):
    current = MODULES[tool_id]
    for module in (current, *MODULE_VERSIONS.get(tool_id, ())):
        if module.parameters.normalize(settings, module.parameters.version).success:
            return module
    return current


DENOISE_RESOURCE_FEATURES = (
    ResourceFeature("basic"),
    ResourceFeature("sharpen"),
    ResourceFeature("ai", (ModelRequirement("denoise_model", "legacy_resource", ("legacy",)),)),
)


def denoise_required_resources(params):
    if params["method"] != "ai" or not (params["strength"] or params["color_strength"]):
        return ()
    feature = DENOISE_RESOURCE_FEATURES[2]
    model = feature.models[0]
    return (RequiredResource(feature.feature_id, model.role, runtime="onnxruntime"),)


def current_resource_definitions():
    from ..core.contracts.beauty import DETAIL_BEAUTY_ALGORITHM, DETAIL_BEAUTY_PROFILES
    from ..core.tools.enhancement import ENHANCE_PARAMETERS, ENHANCE_RESOURCE_FEATURES, enhancement_required_resources
    from ..core.tools.managed_models import (MANAGED_BG_PARAMETERS, MANAGED_BEAUTY_PARAMETERS,
        BACKGROUND_RESOURCE_FEATURES, BEAUTY_RESOURCE_FEATURES, background_required_resources, beauty_required_resources)
    from ..core.tools.denoise import DENOISE_PARAMETERS
    from ..core.tools.geometry import GRID_PARAMETERS
    from ..core.denoiser import BASIC_DENOISE_VERSION

    definitions = [ToolDefinition(ToolDescriptor(tool_id, module.tool_version, 1, module.parameters.version,
        tool_id + ".float", module.implementation_version, module.image), module.parameters, None,
        can_disable=tool_id != "color_adjust", resource_features=(ResourceFeature("basic"),),
        sampling_behavior=module.sampling_behavior)
        for tool_id, module in MODULES.items()]
    for tool_id, tool_version, implementation, implementation_version, contract, features, collect, sampling_behavior in (
        ("bg_remove", "3", "bg_remove.managed", "1", MANAGED_BG_PARAMETERS,
         BACKGROUND_RESOURCE_FEATURES, background_required_resources, "transport"),
        ("beauty", DETAIL_BEAUTY_PROFILES[DETAIL_BEAUTY_ALGORITHM][0], "beauty.detail", str(DETAIL_BEAUTY_ALGORITHM), MANAGED_BEAUTY_PARAMETERS,
         BEAUTY_RESOURCE_FEATURES, partial(beauty_required_resources, algorithm=DETAIL_BEAUTY_ALGORITHM),
         "transport"),
        ("ai_enhance", "3", "ai_enhance.neural", "1", ENHANCE_PARAMETERS,
         ENHANCE_RESOURCE_FEATURES, enhancement_required_resources, "transport"),
        ("denoise", "2", "denoise.basic", str(BASIC_DENOISE_VERSION), DENOISE_PARAMETERS,
         DENOISE_RESOURCE_FEATURES, denoise_required_resources, "preserve"),
        ("grid_crop", "1", "grid_crop.export", "1", GRID_PARAMETERS, (ResourceFeature("basic"),), None, "preserve"),
        ("id_photo", "3", "id_photo.managed", "1", MANAGED_ID_PHOTO_PARAMETERS,
         ID_PHOTO_RESOURCE_FEATURES, id_photo_required_resources, "transport"),
    ):
        definitions.append(ToolDefinition(ToolDescriptor(tool_id, tool_version, 1, contract.version,
            implementation, implementation_version), contract, None, resource_features=features,
            collect_resources=collect, sampling_behavior=sampling_behavior))
    return tuple(definitions)


def managed_module(tool_id, settings, reference=None):
    from .enhancement_module import enhancement_binding
    from ..core.tools.enhancement import ENHANCE_PARAMETERS
    from .managed_tool_modules import background_binding, beauty_binding, id_photo_binding
    from ..core.tools.managed_models import MANAGED_BG_PARAMETERS, MANAGED_BEAUTY_PARAMETERS
    modules = {"ai_enhance": (ENHANCE_PARAMETERS, enhancement_binding),
               "bg_remove": (MANAGED_BG_PARAMETERS, background_binding),
               "beauty": (MANAGED_BEAUTY_PARAMETERS, beauty_binding),
               "id_photo": (MANAGED_ID_PHOTO_PARAMETERS, id_photo_binding)}
    entry = modules.get(tool_id)
    if entry is None:
        return None
    if tool_id == "beauty" and reference is not None:
        if reference.implementation_id != "beauty.detail":
            return None
        return entry[0], partial(entry[1], implementation_id=reference.implementation_id,
                                 implementation_version=reference.implementation_version,
                                 retain_resources=bool(reference.resources))
    return entry


def analysis_settings(tool_id, settings):
    if tool_id == "beauty" and "face_detection_model" in settings:
        return {**settings, "skin_smooth": max(1, settings["skin_smooth"])}
    return settings


def create_module_executor(tool_id, resources, execution_threads):
    from .tool_runtime import FunctionExecutor

    if tool_id == "color_adjust":
        from ..core.local_color import render
        def color(source, params, *, threads, cancelled, sampling=None, execution_input=None):
            if params.get("process_version") == 4:
                return render(source, params, backend="native", threads=threads, cancelled=cancelled,
                              sampling=sampling, prepared_masks=execution_input)
            return execute_color(source, params, threads=threads, cancelled=cancelled, sampling=sampling)
        return FunctionExecutor(color, uses_thread_limit=True, uses_cancellation=True, uses_sampling=True, uses_execution_input=True)
    module = MODULES[tool_id]
    return FunctionExecutor(module.execute, uses_thread_limit=module.uses_thread_limit,
                            uses_cancellation=module.uses_cancellation, uses_sampling=module.uses_sampling)
