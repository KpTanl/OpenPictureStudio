from ..core.tools.enhancement import (ENHANCE_PARAMETERS, EnhancementExecutor, enhancement_required_resources,
                                     ENHANCE_RESOURCE_FEATURES, estimate_memory)
from ..core.contracts.tools import ToolDescriptor, ImageCapabilities
from ..core.tools.catalog import ToolDefinition
from .managed_tool_modules import bind_model_resources


def bind_enhancement(params, service):
    return bind_model_resources(enhancement_required_resources(params), service,
                                tool_id="ai_enhance", implementation_id="ai_enhance.neural")


def enhancement_binding(params, service, threads, inference=None):
    models, paths = bind_enhancement(params, service)
    identities = tuple(sorted((role, model.files[0].digest) for role, model in models.items()))
    descriptor = ToolDescriptor("ai_enhance", "3", 1, 3, "ai_enhance.neural", "1",
                               ImageCapabilities(alpha="transform", geometry="resize"), identities)
    create_session = inference.create_session if inference is not None else None
    definition = ToolDefinition(descriptor, ENHANCE_PARAMETERS,
        lambda resources: EnhancementExecutor(models, resources, threads, create_session), supports_proxy=False,
        needs_preparation=lambda settings: True, reusable_executor=True,
        working_memory=lambda source, settings: estimate_memory(source, settings, models), allow_resource_gaps_edit=True,
        resource_features=ENHANCE_RESOURCE_FEATURES, collect_resources=enhancement_required_resources,
        sampling_behavior="transport")
    return definition, paths
