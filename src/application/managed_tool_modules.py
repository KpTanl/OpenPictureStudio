from importlib.metadata import version
from functools import partial
from pathlib import Path

from ..core.contracts.result import CoreError, CoreOperationError, ErrorCode
from ..core.contracts.tools import ImageCapabilities, ToolDescriptor
from ..core.contracts.beauty import (BEAUTY_MODEL_FIELDS, DETAIL_BEAUTY_ALGORITHM, DETAIL_BEAUTY_PROFILES,
    detail_beauty_algorithm)
from ..core.tools.catalog import ToolDefinition
from ..core.tools.id_photo import (MANAGED_ID_PHOTO_PARAMETERS, ID_PHOTO_RESOURCE_FEATURES,
    id_photo_required_resources)
from ..core.tools.managed_models import (MANAGED_BG_PARAMETERS, MANAGED_BEAUTY_PARAMETERS, effect_settings,
    BACKGROUND_RESOURCE_FEATURES, BEAUTY_RESOURCE_FEATURES, background_required_resources, beauty_required_resources,
    beauty_effect_algorithm, beauty_uses_locator)


def bind_model_resources(requirements, service, *, tool_id, implementation_id):
    models, paths = {}, {}
    for requirement in requirements:
        details = {"tool_id": tool_id, "implementation_id": implementation_id,
                   "feature_id": requirement.feature_id, "resource_id": requirement.role,
                   "model_token": requirement.model_token}
        try:
            model, files = service.describe(requirement.model_token)
        except (ValueError, KeyError):
            raise CoreOperationError(CoreError(ErrorCode.TOOL_RESOURCE_MISSING,
                "error.tool.resource_missing", details)) from None
        details.update(resource_hash=model.files[0].digest, runtime=model.runtime)
        if model.capability != requirement.capability or model.adapter not in requirement.adapters:
            raise CoreOperationError(CoreError(ErrorCode.TOOL_INCOMPATIBLE, "models.error.capability", details))
        if not service.runtime_available(model):
            raise CoreOperationError(CoreError(ErrorCode.TOOL_INCOMPATIBLE, "models.runtime.missing", details))
        models[requirement.role] = model
        paths[requirement.role] = files[model.files[0].name]
    return models, paths


def background_binding(params, service, threads, inference=None):
    from .tool_runtime import FunctionExecutor
    from ..core.tools.effects import execute_background
    from ..core.tools.segmentation import U2NetMask
    models, paths = bind_model_resources(background_required_resources(params), service,
                                        tool_id="bg_remove", implementation_id="bg_remove.managed")
    identities = tuple(sorted((role, model.files[0].digest) for role, model in models.items()))
    descriptor = ToolDescriptor("bg_remove", "3", 1, 3, "bg_remove.managed", "1",
                                ImageCapabilities(alpha="transform"), identities)
    create_session = inference.create_session if inference is not None else None
    def factory(resources):
        processor = U2NetMask(resources["model"], execution_threads=threads,
                              create_session=create_session) if resources else None
        return FunctionExecutor(lambda source, settings, sampling=None: execute_background(source,
            effect_settings(settings, "segmentation_model"), mask_provider=processor, sampling=sampling),
            close=processor.close if processor else None, uses_sampling=True)
    return ToolDefinition(descriptor, MANAGED_BG_PARAMETERS, factory, reusable_executor=bool(paths),
        needs_preparation=lambda settings: bool(background_required_resources(settings)), supports_proxy=not paths,
        allow_resource_gaps_edit=True, resource_features=BACKGROUND_RESOURCE_FEATURES,
        collect_resources=background_required_resources, sampling_behavior="transport"), paths


def id_photo_binding(params, service, threads, inference=None):
    from .tool_runtime import FunctionExecutor
    from ..core.tools.effects import make_id_photo, make_id_photo_memory
    from ..core.tools.segmentation import U2NetMask
    models, paths = bind_model_resources(id_photo_required_resources(params), service,
                                        tool_id="id_photo", implementation_id="id_photo.managed")
    identities = tuple(sorted((role, model.files[0].digest) for role, model in models.items()))
    descriptor = ToolDescriptor("id_photo", "3", 1, 2, "id_photo.managed", "1",
                                ImageCapabilities(alpha="transform", geometry="resize"), identities)
    create_session = inference.create_session if inference is not None else None
    def factory(resources):
        processor = U2NetMask(resources["model"], execution_threads=threads,
                              create_session=create_session) if resources else None
        return FunctionExecutor(lambda source, settings, sampling=None: make_id_photo(
            source, settings, mask_provider=processor, sampling=sampling),
            close=processor.close if processor else None, uses_sampling=True)
    return ToolDefinition(descriptor, MANAGED_ID_PHOTO_PARAMETERS, factory, reusable_executor=bool(paths),
        needs_preparation=lambda settings: bool(id_photo_required_resources(settings)), supports_proxy=not paths,
        working_memory=make_id_photo_memory, allow_resource_gaps_edit=True,
        resource_features=ID_PHOTO_RESOURCE_FEATURES, collect_resources=id_photo_required_resources,
        sampling_behavior="transport"), paths


def beauty_binding(params, service, threads, inference=None, *, implementation_id="beauty.detail",
                   implementation_version=None, retain_resources=False):
    if implementation_id == "beauty.detail":
        try:
            algorithm = DETAIL_BEAUTY_ALGORITHM if implementation_version is None else detail_beauty_algorithm(implementation_version)
        except ValueError as error:
            raise CoreOperationError(CoreError(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible",
                {"tool_id": "beauty", "implementation_id": implementation_id})) from error
        # 选了人脸定位模型：计算配方已是最新的记录按支持多人脸的版本执行，更早的配方不支持
        algorithm = beauty_effect_algorithm(algorithm, params)
        if params.get("face_locator_model") and not beauty_uses_locator(algorithm):
            raise CoreOperationError(CoreError(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible",
                {"tool_id": "beauty", "implementation_id": implementation_id}))
        return detail_beauty_binding(params, service, threads, algorithm=algorithm, retain_resources=retain_resources)
    if params.get("face_locator_model"):
        raise CoreOperationError(CoreError(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible",
            {"tool_id": "beauty", "implementation_id": implementation_id}))
    from .tool_runtime import FunctionExecutor
    from ..core.tools.beauty import execute
    from ..core.tools.beauty_backend import BeautyBackend, backend_version
    binding_params = {**params, "skin_smooth": max(1, params["skin_smooth"])} if retain_resources else params
    models, paths = bind_model_resources(beauty_required_resources(binding_params), service,
                                        tool_id="beauty", implementation_id="beauty.managed")
    identities = tuple(sorted((role, model.files[0].digest) for role, model in models.items()))
    backend = "managed-task-" + backend_version() + "-mp" + version("mediapipe") if paths else "4-neutral"
    descriptor = ToolDescriptor("beauty", "4", 1, 3, "beauty.managed", backend,
                                ImageCapabilities(intermediate_precision="srgb8"), identities)
    def factory(resources):
        if not resources:
            return FunctionExecutor(lambda source, settings, sampling=None: execute(
                source, effect_settings(settings, *BEAUTY_MODEL_FIELDS), sampling=sampling), uses_sampling=True)
        bound = BeautyBackend("mediapipe_tasks", backend, tuple((role, dict(identities)[role], path)
                                                                for role, path in resources.items()))
        processor = bound.create()
        return FunctionExecutor(lambda source, settings, sampling=None: execute(source,
            effect_settings(settings, *BEAUTY_MODEL_FIELDS), backend=bound, processor=processor, sampling=sampling),
            close=processor.close, analyze=processor.analyze_auto_beauty,
            regions=processor.face_regions, uses_sampling=True)
    return ToolDefinition(descriptor, MANAGED_BEAUTY_PARAMETERS, factory, reusable_executor=bool(paths),
        needs_preparation=lambda settings: bool(beauty_required_resources(settings)), allow_resource_gaps_edit=True,
        resource_features=BEAUTY_RESOURCE_FEATURES, collect_resources=beauty_required_resources,
        sampling_behavior="transport"), paths


BEAUTY_PROXY_ADMISSIONS = frozenset()


def default_face_locator(models):
    """新开美颜的人脸定位选择：模型已下载时选上（开启多人脸），否则不使用。"""
    from ..core.tools.managed_models import FACE_LOCATOR_MODEL
    return FACE_LOCATOR_MODEL if any(item.descriptor.key.token == FACE_LOCATOR_MODEL and item.usable
                                     for item in models.list(None)) else ""


def detail_beauty_version(algorithm=DETAIL_BEAUTY_ALGORITHM):
    if algorithm not in DETAIL_BEAUTY_PROFILES:
        raise ValueError("美颜算法版本不受支持")
    import cv2
    import numpy as np
    return f"{algorithm}-cv{cv2.__version__}-np{np.__version__}-mp{version('mediapipe')}"


def detail_beauty_binding(params, service, threads, *, algorithm=DETAIL_BEAUTY_ALGORITHM, retain_resources=False):
    from .tool_runtime import FunctionExecutor
    required_resources = partial(beauty_required_resources, algorithm=algorithm)
    binding_params = {**params, "skin_smooth": max(1, params["skin_smooth"])} if retain_resources else params
    models, paths = bind_model_resources(required_resources(binding_params), service,
                                        tool_id="beauty", implementation_id="beauty.detail")
    identities = tuple(sorted((role, model.files[0].digest) for role, model in models.items()))
    module_version, color_algorithm, detection_version = DETAIL_BEAUTY_PROFILES[algorithm]
    if not paths:
        descriptor = ToolDescriptor("beauty", module_version, 1, 3, "beauty.detail", f"{algorithm}-neutral",
                                    ImageCapabilities(alpha="transform"))
        return ToolDefinition(descriptor, MANAGED_BEAUTY_PARAMETERS,
            lambda resources: FunctionExecutor(lambda source, settings: source),
            needs_preparation=lambda settings: bool(required_resources(settings)), allow_resource_gaps_edit=True,
            resource_features=BEAUTY_RESOURCE_FEATURES, collect_resources=required_resources,
            sampling_behavior="transport"), paths
    from ..core.beauty_detection import BeautyDetector, DETECTION_SPECS, preparation_identity, preparation_memory
    from ..core.beauty_v2 import analyze_auto, face_rectangles
    from ..core.photo_engine import guided_filter_execution
    if color_algorithm == 3:
        from ..core.beauty_detail_v3 import execute, working_memory
    elif color_algorithm == 2:
        from ..core.beauty_detail_v2 import execute, working_memory
    else:
        from ..core.beauty_v2 import execute, working_memory
    implementation_version = detail_beauty_version(algorithm)
    # 支持多人脸的版本只在选了人脸定位模型时用检测规格 3，否则与上一版的检测相同
    detection_spec = DETECTION_SPECS[detection_version if "face_locator" in paths else min(detection_version, 2)]
    descriptor = ToolDescriptor("beauty", module_version, 1, 3, "beauty.detail", implementation_version,
                                ImageCapabilities(alpha="transform"), identities)

    def factory(resources):
        try:
            detector = BeautyDetector(resources["face_model"], detection_spec=detection_spec,
                                      locator_path=resources.get("face_locator"))
        except (ImportError, OSError) as error:
            raise CoreOperationError(CoreError(ErrorCode.TOOL_INCOMPATIBLE, "models.runtime.missing",
                {"tool_id": "beauty", "implementation_id": "beauty.detail"})) from error
        def render(source, settings, *, threads, cancelled, **kwargs):
            with guided_filter_execution(threads, cancelled):
                return execute(source, settings, cancelled=cancelled, **kwargs)
        return FunctionExecutor(render, close=detector.close, prepare=detector.prepare,
                                analyze=analyze_auto, regions=face_rectangles,
                                uses_thread_limit=True, uses_cancellation=True, uses_sampling=True, uses_prepared=True)

    return ToolDefinition(descriptor, MANAGED_BEAUTY_PARAMETERS, factory, reusable_executor=bool(paths),
        supports_proxy=descriptor.reference in BEAUTY_PROXY_ADMISSIONS,
        working_memory=working_memory, needs_preparation=lambda settings: bool(required_resources(settings)),
        allow_resource_gaps_edit=True, resource_features=BEAUTY_RESOURCE_FEATURES,
        collect_resources=required_resources, sampling_behavior="transport",
        prepare_input=partial(preparation_identity, detection_spec=detection_spec),
        preparation_memory=partial(preparation_memory, detection_spec=detection_spec),
        executor_memory=lambda resources: sum(Path(path).stat().st_size * 4 for path in resources.values()) + 16 * 1024 ** 2), paths
