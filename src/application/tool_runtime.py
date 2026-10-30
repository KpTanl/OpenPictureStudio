from __future__ import annotations

import hashlib
from contextlib import contextmanager, ExitStack, nullcontext
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from threading import RLock
from concurrent.futures import CancelledError

from ..core.contracts.result import CoreOperationError, CoreResult, ErrorCode, memory_error_key
from ..core.contracts.tools import ImageCapabilities, ToolDescriptor, ToolReference
from ..core.contracts.tool_resources import (ModelRequirement, RequiredResource, ResourceFeature, ToolResourceSnapshot,
                                           ToolParameterRequest)
from ..core.denoiser import BASIC_DENOISE_VERSION
from ..core.project.assets import ProjectAssetError
from ..core.tools import ParameterContract, ToolCatalog, ToolDefinition
from ..core.tools.denoise import DenoiseBackendUnavailable
from ..core.workflow import NodeRegistry, NodeSpec, WorkflowExecutor
from ..core.workflow.validator import inspect_workflow, validate
from .tool_executor_pool import ToolExecutorPool
from .image_resources import ComputeBudget
from .runtime_libraries import environment_library

_NEUTRAL_BEAUTY_VERSIONS = ("5-neutral",)


class FunctionExecutor:
    def __init__(self, function, *, close=None, analyze=None, regions=None, uses_thread_limit=False,
                 uses_cancellation=False, uses_sampling=False, prepare=None, uses_prepared=False, uses_execution_input=False):
        self._function = function
        self._close = close
        self.analyze = analyze
        self.regions = regions
        self._uses_thread_limit = uses_thread_limit
        self._uses_cancellation = uses_cancellation
        self._uses_sampling = uses_sampling
        self.prepare = prepare
        self._uses_prepared = uses_prepared
        self._uses_execution_input = uses_execution_input

    def execute(self, source, params, context):
        if context["is_cancelled"]():
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        try:
            kwargs = {"threads": context["threads"]} if self._uses_thread_limit else {}
            if self._uses_cancellation:
                kwargs["cancelled"] = context["is_cancelled"]
            if self._uses_sampling and context.get("sampling") is not None:
                kwargs["sampling"] = context["sampling"]
            if self._uses_prepared:
                kwargs["snapshot"] = context["prepared_input"]
                kwargs["node_id"] = context.get("node_id", "")
            if self._uses_execution_input:
                kwargs["execution_input"] = context.get("execution_input")
            return CoreResult.ok(self._function(source, params, **kwargs))
        except CoreOperationError as error:
            return CoreResult.from_exception(error)
        except FileNotFoundError:
            return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing")
        except DenoiseBackendUnavailable:
            return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "denoise.backend_unavailable")

    def close(self):
        if self._close is not None:
            close, self._close = self._close, None
            close()


class LeasedExecutor:
    def __init__(self, executor, leases):
        self._executor = executor
        self._leases = leases
        self.analyze = getattr(executor, "analyze", None)
        self.regions = getattr(executor, "regions", None)
        self.prepare = getattr(executor, "prepare", None)

    def execute(self, source, params, context):
        return self._executor.execute(source, params, context)

    def close(self):
        try:
            self._executor.close()
        finally:
            self._leases.close()


class LeasedProcessor:
    def __init__(self, processor, hold):
        self._processor, self._hold = processor, hold

    def __getattr__(self, name):
        return getattr(self._processor, name)

    def __call__(self, *args, **kwargs):
        return self._processor(*args, **kwargs)

    def close(self):
        try:
            self._processor.close()
        finally:
            self._hold.close()


def builtin_catalog(engine=None, *, memory_threads=1):
    from ..core.photo_engine import mask_working_memory, minimum_render_threads
    from ..core.nodes.color_adjust_node import execute as color
    from ..core.tools.color import LEGACY_COLOR_PARAMETERS
    from ..core.nodes.crop_node import execute as crop, validate_params as validate_crop
    from ..core.tools.denoise import DENOISE_PARAMETERS, execute as execute_denoise
    from ..core.tools.geometry import GRID_PARAMETERS
    from .tool_modules import DENOISE_RESOURCE_FEATURES, denoise_required_resources

    definitions = []
    bindings = {}
    color_memory = partial(mask_working_memory, threads=memory_threads)
    entries = [
        ("color_adjust", "color_adjust", engine.render_source if engine else color, LEGACY_COLOR_PARAMETERS[1],
         ImageCapabilities(), minimum_render_threads, True, color_memory),
        ("crop", "crop", crop, ParameterContract(validate=validate_crop, allow_unknown=True),
         ImageCapabilities(alpha="transform", geometry="resize"), None, False, None),
    ]
    for kind, tool_id, function, parameters, image, minimum_threads, uses_cancellation, working_memory in entries:
        descriptor = ToolDescriptor(tool_id, "1", 1, 1, kind, "legacy-1", image)
        definitions.append(ToolDefinition(descriptor, parameters,
            lambda resources, fn=function, threaded=minimum_threads is not None,
                   cancellable=uses_cancellation: FunctionExecutor(
                fn, uses_thread_limit=threaded, uses_cancellation=cancellable, uses_sampling=True),
            can_disable=kind != "color_adjust", minimum_threads=minimum_threads, working_memory=working_memory,
            sampling_behavior="capture" if kind == "color_adjust" else "transport"))
        bindings[kind] = descriptor.reference
    for params_version in (2, 3, 4):
        descriptor = ToolDescriptor("color_adjust", str(params_version), 1, params_version,
                                    "color_adjust.float", str(params_version), ImageCapabilities())
        definitions.append(ToolDefinition(descriptor, LEGACY_COLOR_PARAMETERS[params_version],
            lambda resources: FunctionExecutor(engine.render_source if engine else color, uses_thread_limit=True,
                                               uses_cancellation=True, uses_sampling=True),
            can_disable=False, supports_proxy=True, minimum_threads=minimum_render_threads,
            working_memory=color_memory, sampling_behavior="capture"))
    descriptor = ToolDescriptor("denoise", str(BASIC_DENOISE_VERSION), 1, 2,
        "denoise.basic", str(BASIC_DENOISE_VERSION), ImageCapabilities(intermediate_precision="srgb8"))
    definitions.append(ToolDefinition(descriptor, DENOISE_PARAMETERS,
        lambda resources: FunctionExecutor(partial(execute_denoise, basic_version=BASIC_DENOISE_VERSION)),
        needs_preparation=lambda params: bool(denoise_required_resources(params)),
        resource_features=DENOISE_RESOURCE_FEATURES, collect_resources=denoise_required_resources,
        sampling_behavior="preserve"))
    descriptor = ToolDescriptor("grid_crop", "1", 1, GRID_PARAMETERS.version, "grid_crop.export", "1")
    definitions.append(ToolDefinition(descriptor, GRID_PARAMETERS, None))
    bindings["grid_crop"] = descriptor.reference
    return ToolCatalog(definitions), bindings


@dataclass(frozen=True)
class PreviewExecution:
    source: object
    input_size: tuple[int, int]
    input_is_proxy: bool


@dataclass(frozen=True)
class RuntimeSnapshot:
    runtime: ToolRuntime
    generation: int
    registry: NodeRegistry
    operator_threads: int = 1

    def is_cancelled(self):
        return not self.runtime.is_current(self.generation)

    @property
    def executor(self):
        return WorkflowExecutor(self.registry)

    def required_threads(self, workflow):
        requirements = [1]
        for node in workflow.nodes:
            spec = self.registry.get(node.node_type, node.version)
            requirements.append(spec.required_threads(node.params)
                                if spec is not None and spec.required_threads is not None
                                else self.operator_threads)
        return max(requirements)

    def sampling_capability(self, workflow, request):
        return self.executor.sampling_capability(workflow, request)


class ToolRuntime:
    def __init__(self, engine=None, *, catalog=None, bindings=None, resources=None,
                 executor_capacity=3, execution_threads=1, compute_budget=None, model_service=None, resource_budget=None,
                 inference_backend=None, runtime_query=None):
        if type(execution_threads) is not int or execution_threads < 1:
            raise ValueError("算子线程数必须为正整数")
        compute = compute_budget or ComputeBudget(2, operator_threads=execution_threads)
        memory_threads = compute.snapshot().thread_capacity
        if inference_backend is None:
            from .inference_backend import InferenceBackend
            inference_backend = InferenceBackend()
        self.inference = inference_backend
        self._runtime_query = runtime_query or environment_library
        builtin = catalog is None
        if catalog is None:
            catalog, default_bindings = builtin_catalog(engine, memory_threads=memory_threads)
        else:
            default_bindings = {}
        self._catalog = catalog.copy()
        if builtin:
            from .tool_modules import current_resource_definitions
            self._resource_definitions = current_resource_definitions()
        else:
            latest = {}
            for descriptor in self._catalog.descriptors():
                if descriptor.tool_id not in latest or descriptor.params_version >= latest[descriptor.tool_id].descriptor.params_version:
                    latest[descriptor.tool_id] = self._catalog.resolve(descriptor.reference).data
            self._resource_definitions = tuple(latest.values())
        self._bindings = dict(default_bindings if bindings is None else bindings)
        self._resources = dict(resources or {})
        self._disabled = set()
        self._generation = 0
        self._snapshot_catalog = None
        self._lock = RLock()
        self._ai_model_prepare_lock = RLock()
        self._closed = False
        self._execution_threads = execution_threads
        self._compute = compute
        self._memory_threads = memory_threads
        self._resource_budget = resource_budget
        from .mask_assets import MaskAssetStore
        self.mask_assets = MaskAssetStore(resource_budget)
        from .beauty_preparation import BeautyPreparationService
        self._input_preparation = BeautyPreparationService(resource_budget)
        self._executors = ToolExecutorPool(executor_capacity)
        self.models = model_service
        self._owns_models = model_service is None and builtin
        if self._owns_models:
            from .model_service import ModelService
            from .bundled_model_resources import bundled_models
            from ..utils.model_paths import get_project_model_root, get_project_root, get_legacy_model_roots
            self.models = ModelService(get_project_model_root(create=False),
                get_project_root() / "resources" / "models" / "catalog.json",
                external_roots=get_legacy_model_roots(), bundled_provider=bundled_models,
                compute_budget=self._compute, resource_budget=self._resource_budget, runtime_query=self._runtime_query)
        if self.models is not None:
            self.models.bind_retirement(self.retire_model_resources)
        self._model_states = {}
        self._model_unsubscribe = self.models.subscribe(self._model_changed) if self.models is not None else None
        self._auto_ai_model = builtin
        from ..core.denoiser import get_recommended_model_path
        self._ai_model_path = get_recommended_model_path()
        self._ai_model_signature = None
        self._ai_model_reference = None
        if builtin:
            from .tool_modules import MODULES
            for tool_id, module in MODULES.items():
                self.effect_reference(tool_id, module.parameters.defaults(module.parameters.version))

    def _module_reference(self, tool_id, settings):
        from .tool_modules import module_for, create_module_executor
        module = module_for(tool_id, settings)
        checked = module.parameters.normalize(settings, module.parameters.version)
        if not checked.success:
            return checked
        descriptor = ToolDescriptor(tool_id, module.tool_version, 1, module.parameters.version,
                                    tool_id + ".float", module.implementation_version, module.image)
        with self._lock:
            if not self._catalog.resolve(descriptor.reference).success:
                self._catalog.register(ToolDefinition(descriptor, module.parameters,
                    lambda resources: create_module_executor(tool_id, resources, self._execution_threads),
                    supports_proxy=module.supports_proxy, needs_preparation=module.needs_preparation,
                    minimum_threads=module.minimum_threads,
                    working_memory=(partial(module.working_memory, threads=self._memory_threads)
                                    if module.working_memory is not None else None),
                    resource_features=module.resource_features, collect_resources=module.collect_resources,
                    sampling_behavior=module.sampling_behavior,
                    prepare_execution=self.mask_assets.prepare if tool_id == "color_adjust" and module.parameters.version in (6, 7) else None))
                self._generation += 1
        return CoreResult.ok(descriptor.reference)

    def _managed_reference(self, tool_id, settings, module):
        contract, bind = module
        checked = contract.normalize(settings, contract.version)
        if not checked.success:
            return checked
        try:
            definition, paths = bind(checked.data, self.models, self._execution_threads, self.inference)
        except CoreOperationError as error:
            return CoreResult.from_exception(error)
        except ImportError:
            return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "models.runtime.missing", details={"tool_id": tool_id})
        except (FileNotFoundError, KeyError, ValueError, OSError):
            return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing",
                                   details={"tool_id": tool_id})
        reference = definition.descriptor.reference
        with self._lock:
            if not self._catalog.resolve(reference).success:
                self._catalog.register(definition)
                self._generation += 1
            for role, digest in reference.resources:
                self._resources[(role, digest)] = str(paths[role])
        return CoreResult.ok(reference)

    def retire_model_resources(self, digests):
        self._executors.invalidate_resources(digests)

    def _model_changed(self, key):
        snapshot = self.models.snapshot(key)
        if snapshot.state not in ("installed", "missing", "corrupt", "waiting"):
            return
        signature = (snapshot.state, snapshot.paths)
        with self._lock:
            if self._closed or self._model_states.get(key) == signature:
                return
            self._model_states[key] = signature
            model_digests = {file.digest for file in snapshot.descriptor.files}
            implicit = {digest for descriptor in self._catalog.descriptors()
                        for definition in (self._catalog.resolve(descriptor.reference).data,)
                        if definition.implicit_resources
                        for _, digest in definition.resource_references(
                            definition.parameters.defaults(descriptor.params_version))}
            affected = bool(model_digests & implicit) or any(digest in model_digests for _, digest in self._resources)
            if not affected:
                return
            if snapshot.state == "installed":
                for file, (_, path) in zip(snapshot.descriptor.files, snapshot.paths):
                    for identity, digest in tuple(self._resources):
                        if digest == file.digest:
                            self._resources[(identity, digest)] = path
            self._generation += 1

    def ensure_denoise_ai_model(self):
        with self._ai_model_prepare_lock:
            return self._prepare_denoise_ai_model()

    def _prepare_denoise_ai_model(self):
        with self._lock:
            if self._closed:
                raise CancelledError()
        from ..core.denoiser import is_ai_model_available
        from ..core.tools.denoise import DENOISE_PARAMETERS, execute as execute_denoise
        from .tool_modules import DENOISE_RESOURCE_FEATURES, denoise_required_resources
        path = self._ai_model_path
        if not is_ai_model_available(str(path)):
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        signature = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if signature == self._ai_model_signature:
                return self._ai_model_reference
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    with self._lock:
                        if self._closed:
                            raise CancelledError()
                    digest.update(chunk)
        except OSError:
            return None
        identity = ("denoise_model", digest.hexdigest())
        descriptor = ToolDescriptor("denoise", "2", 1, 2, "denoise.ai", "2",
            ImageCapabilities(intermediate_precision="srgb8"), (identity,))
        with self._lock:
            if self._closed:
                raise CancelledError()
            if not self._catalog.resolve(descriptor.reference).success:
                self._catalog.register(ToolDefinition(descriptor, DENOISE_PARAMETERS,
                    lambda resources: self._denoise_executor(resources, execute_denoise),
                    needs_preparation=lambda params: bool(denoise_required_resources(params)), reusable_executor=True,
                    resource_features=DENOISE_RESOURCE_FEATURES, collect_resources=denoise_required_resources,
                    sampling_behavior="preserve"))
                self._generation += 1
            self._resources[identity] = str(path)
            self._ai_model_signature = signature
            self._ai_model_reference = descriptor.reference
        return descriptor.reference

    def descriptors(self):
        with self._lock:
            descriptors = self._catalog.descriptors()
            registered = {item.tool_id for item in descriptors}
            return (*descriptors, *(item.descriptor for item in self._resource_definitions
                                   if item.descriptor.tool_id not in registered))

    def resource_status(self, workflow=None, requests: tuple[ToolParameterRequest, ...] = ()):
        with self._lock:
            disabled = frozenset(self._disabled)
            definitions = self._resource_definitions
            catalog = self._catalog.copy()
            bindings = dict(self._bindings)
            paths = dict(self._resources)
            denoise_reference = self._ai_model_reference
            denoise_path = str(self._ai_model_path)
        model_snapshots = self.models.list() if self.models is not None else ()
        by_token = {snapshot.descriptor.key.token: snapshot for snapshot in model_snapshots}
        by_digest = {file.digest: (snapshot, file) for snapshot in model_snapshots
                     for file in snapshot.descriptor.files}
        current = {definition.descriptor.tool_id: definition for definition in definitions}

        def enrich(resource, reference=None):
            digest = dict(reference.resources).get(resource.role, resource.digest) if reference else resource.digest
            selected = by_token.get(resource.model_token)
            matched = by_digest.get(digest)
            if selected is not None:
                model = selected.descriptor
                if (resource.capability and model.capability != resource.capability
                        or resource.adapters and model.adapter not in resource.adapters):
                    return replace(resource, digest=digest, issue=ErrorCode.TOOL_INCOMPATIBLE.value)
                file = next((file for file in model.files if not digest or file.digest == digest), None)
                if file is None:
                    return replace(resource, digest=digest, issue=ErrorCode.TOOL_INCOMPATIBLE.value)
                matched = selected, file
                digest = file.digest
            path = paths.get((resource.role, digest), resource.path)
            if matched is None:
                return replace(resource, digest=digest, path=path,
                               issue=resource.issue or ("legacy_resource" if digest or path else ""))
            snapshot, file = matched
            model = snapshot.descriptor
            return replace(resource, digest=digest, path=path or dict(snapshot.paths).get(file.name, ""),
                           runtime=resource.runtime or model.runtime)

        result = []
        for definition in definitions:
            descriptor = definition.descriptor
            required = ()
            if descriptor.tool_id == "denoise":
                digest = dict(denoise_reference.resources).get("denoise_model", "") if denoise_reference else ""
                if not digest:
                    digest = next((file.digest for snapshot in model_snapshots
                        for file in snapshot.descriptor.files
                        if dict(snapshot.paths).get(file.name) == denoise_path), "")
                required = (enrich(RequiredResource("ai", "denoise_model",
                    digest=digest, path=denoise_path, runtime="onnxruntime")),)
            result.append(ToolResourceSnapshot(descriptor.tool_id, descriptor.tool_id not in disabled,
                definition.can_disable, descriptor.implementation_id, descriptor.implementation_version,
                definition.resource_features or (ResourceFeature("basic"),), required))
        if workflow is None and not requests:
            return tuple(result)

        contexts = []
        for node in getattr(workflow, "nodes", ()):
            if node.node_type in ("source", "export"):
                continue
            if node.node_type == "tool":
                try:
                    reference = ToolReference.from_dict(node.params)
                except (ValueError, TypeError, KeyError):
                    continue
                settings = node.params["settings"]
            else:
                reference = bindings.get(node.node_type)
                settings = node.params
            if reference is None:
                continue
            resolved = catalog.resolve(reference)
            definition = resolved.data if resolved.success else None
            base = current.get(reference.tool_id)
            if definition is None and base is not None and base.descriptor.params_version == reference.params_version:
                definition = base
            if definition is None:
                definition = next((catalog.resolve(descriptor.reference).data
                    for descriptor in catalog.descriptors() if descriptor.tool_id == reference.tool_id
                    and descriptor.params_version == reference.params_version), None)
            issue = ""
            resources = ()
            if definition is not None:
                checked = definition.parameters.normalize(settings, reference.params_version)
                if checked.success:
                    resources = definition.required_resources(checked.data)
                    if definition.legacy_resource_check and definition.legacy_resource_check(checked.data):
                        issue = ErrorCode.TOOL_LEGACY_UNPINNED.value
                    if (reference.tool_id == "denoise" and reference.params_version == 2
                            and bool(resources) != (reference.implementation_id == "denoise.ai")):
                        issue = ErrorCode.PARAMETER_INVALID.value
                    if (reference.implementation_id == "beauty.detail"
                            and resources and reference.implementation_version in _NEUTRAL_BEAUTY_VERSIONS):
                        issue = ErrorCode.PARAMETER_INVALID.value
                else:
                    issue = checked.error.code
            else:
                issue = ErrorCode.TOOL_INCOMPATIBLE.value
            if not resolved.success and not issue:
                known = base is not None and reference.implementation_id == base.descriptor.implementation_id
                known = known or (reference.tool_id == "denoise" and reference.implementation_id == "denoise.ai"
                                  and reference.implementation_version == "2")
                known = known or (reference.tool_id == "beauty" and reference.implementation_id == "beauty.detail")
                if not known:
                    issue = ErrorCode.TOOL_INCOMPATIBLE.value
            required = [enrich(resource, reference) for resource in resources]
            used_roles = {resource.role for resource in required}
            for role, digest in reference.resources:
                if role in used_roles:
                    continue
                runtime = ("onnxruntime" if "-ort" in reference.implementation_version
                           or reference.implementation_id == "denoise.ai" else "")
                required.append(enrich(RequiredResource("effect", role, digest=digest, runtime=runtime), reference))
            if reference.tool_id == "denoise":
                required = [replace(resource, path=resource.path or denoise_path) for resource in required]
            if base is not None and reference.implementation_id == base.descriptor.implementation_id and not issue:
                if reference.tool_id in ("ai_enhance", "bg_remove") and reference.implementation_version != base.descriptor.implementation_version:
                    issue = ErrorCode.TOOL_INCOMPATIBLE.value
            if (reference.implementation_id == "beauty.detail" and not issue
                    and reference.implementation_version not in _NEUTRAL_BEAUTY_VERSIONS):
                from .managed_tool_modules import detail_beauty_version
                from ..core.contracts.beauty import detail_beauty_algorithm
                try:
                    expected = detail_beauty_version(detail_beauty_algorithm(reference.implementation_version))
                except (ImportError, AttributeError, ValueError):
                    issue = ErrorCode.TOOL_INCOMPATIBLE.value
                else:
                    if not reference.implementation_version.startswith(expected):
                        issue = ErrorCode.TOOL_INCOMPATIBLE.value
            if self._runtime_query is not None and not issue:
                for marker, runtime in (("-mp", "mediapipe"), ("-ort", "onnxruntime")):
                    if marker in reference.implementation_version:
                        expected = reference.implementation_version.rsplit(marker, 1)[1]
                        actual = self._runtime_query(runtime)
                        if actual.version and actual.version != expected:
                            issue = ErrorCode.TOOL_INCOMPATIBLE.value
                            break
            if required:
                features = tuple(ResourceFeature(feature_id, tuple(ModelRequirement(resource.role,
                    resource.capability, resource.adapters) for resource in required if resource.feature_id == feature_id))
                    for feature_id in dict.fromkeys(resource.feature_id for resource in required))
            else:
                feature_id = "color" if reference.tool_id == "bg_remove" and settings.get("mode") == "color" else "basic"
                features = (ResourceFeature(feature_id),)
            result.append(ToolResourceSnapshot(reference.tool_id, reference.tool_id not in disabled,
                base.can_disable if base is not None else True, reference.implementation_id,
                reference.implementation_version, features, tuple(required), True, issue, False,
                tuple(enrich(RequiredResource(feature.feature_id, model.role, model.capability,
                    model.adapters, checked.data.get(model.selection_field, "")), reference)
                    for feature in definition.resource_features for model in feature.models
                    if model.selection_field) if definition is not None and checked.success else ()))
            contexts.append((reference, settings, result[-1]))
        for request in requests:
            if any(reference.tool_id == request.tool_id and settings == request.settings
                    and request.params_version in (0, reference.params_version) for reference, settings, _ in contexts):
                continue
            result = [snapshot for snapshot in result if not (snapshot.contextual and snapshot.tool_id == request.tool_id)]
            base = current.get(request.tool_id)
            version = request.params_version or (base.descriptor.params_version if base is not None else 0)
            origin = next(((reference, snapshot) for reference, _, snapshot in reversed(contexts)
                           if reference.tool_id == request.tool_id and reference.params_version == version), None)
            definition = base if base is not None and base.descriptor.params_version == version else None
            if definition is None and origin is not None:
                resolved = catalog.resolve(origin[0])
                definition = resolved.data if resolved.success else None
            if definition is None:
                definition = next((catalog.resolve(descriptor.reference).data for descriptor in catalog.descriptors()
                    if descriptor.tool_id == request.tool_id and descriptor.params_version == version), None)
            if definition is None and version == 2:
                from .tool_modules import MODULES
                module = MODULES.get(request.tool_id)
                if module is not None and module.parameters.version == version:
                    definition = ToolDefinition(ToolDescriptor(request.tool_id, module.tool_version, 1, version,
                        request.tool_id + ".float", module.implementation_version), module.parameters, None,
                        resource_features=module.resource_features, collect_resources=module.collect_resources)
            if definition is None:
                result.append(ToolResourceSnapshot(request.tool_id, request.tool_id not in disabled, True,
                    "", "", (), contextual=True, issue=ErrorCode.TOOL_INCOMPATIBLE.value if base else ErrorCode.TOOL_MISSING.value))
                continue
            descriptor = definition.descriptor
            checked = definition.parameters.normalize(request.settings, descriptor.params_version)
            original_reference = (origin[0] if origin is not None and (request.tool_id == "denoise"
                or base is None or version != base.descriptor.params_version) else None)
            required = tuple(enrich(resource, original_reference) for resource in definition.required_resources(checked.data)) if checked.success else ()
            issue = "" if checked.success else checked.error.code
            beauty_algorithm = None
            if (request.tool_id == "beauty" and version == 3 and checked.success
                    and (origin[0].implementation_id if origin is not None else descriptor.implementation_id) == "beauty.detail"):
                # 所需模型随效果版本与人脸定位模型的选择而定：版本按请求给出的精确版本，其次是工作流里的原节点，
                # 最后才是新建默认；选了人脸定位模型时按实际执行的版本
                from ..core.contracts.beauty import DETAIL_BEAUTY_ALGORITHM, detail_beauty_algorithm
                from ..core.tools.managed_models import beauty_required_resources
                exact = request.implementation_version or (origin[0].implementation_version if origin is not None else "")
                try:
                    beauty_algorithm = detail_beauty_algorithm(exact) if exact else DETAIL_BEAUTY_ALGORITHM
                except ValueError:
                    issue = ErrorCode.TOOL_INCOMPATIBLE.value
                else:
                    required = tuple(enrich(resource, original_reference)
                                     for resource in beauty_required_resources(checked.data, beauty_algorithm))
            if checked.success and definition.legacy_resource_check and definition.legacy_resource_check(checked.data):
                issue = ErrorCode.TOOL_LEGACY_UNPINNED.value
            if (checked.success and request.tool_id == "bg_remove" and version < 3
                    and required and (original_reference is None or not original_reference.resources)):
                issue = ErrorCode.TOOL_LEGACY_UNPINNED.value
            if (checked.success and not required and origin is not None and request.tool_id in ("beauty", "ai_enhance")
                    and (base is None or version != base.descriptor.params_version)
                    and not definition.parameters.is_neutral(checked.data, version)):
                required = origin[1].required_resources
                issue = issue or origin[1].issue
            if request.tool_id == "denoise" and required:
                original = next(snapshot.required_resources[0] for snapshot in result
                    if snapshot.tool_id == "denoise" and not snapshot.contextual)
                required = tuple(enrich(replace(resource, digest=resource.digest or original.digest,
                    path=resource.path or original.path), original_reference) for resource in required)
            features = tuple(ResourceFeature(feature_id, tuple(ModelRequirement(resource.role,
                resource.capability, resource.adapters) for resource in required if resource.feature_id == feature_id))
                for feature_id in dict.fromkeys(resource.feature_id for resource in required)) if required else (
                    ResourceFeature("color" if request.tool_id == "bg_remove" and request.settings.get("mode") == "color" else "basic"),)
            implementation_id, implementation_version = descriptor.implementation_id, descriptor.implementation_version
            if original_reference is not None and (base is None or version != base.descriptor.params_version):
                implementation_id, implementation_version = original_reference.implementation_id, original_reference.implementation_version
            if request.tool_id == "denoise" and required:
                implementation_id, implementation_version = "denoise.ai", "2"
            if request.tool_id == "beauty" and version == 3 and checked.success:
                if origin is not None:
                    implementation_id = origin[0].implementation_id
                if implementation_id == "beauty.detail":
                    from .managed_tool_modules import detail_beauty_version
                    try:
                        if beauty_algorithm is not None:
                            implementation_version = (detail_beauty_version(beauty_algorithm) if required
                                                      else f"{beauty_algorithm}-neutral")
                    except (ImportError, AttributeError, ValueError):
                        issue = ErrorCode.TOOL_INCOMPATIBLE.value
            result.append(ToolResourceSnapshot(request.tool_id, request.tool_id not in disabled, definition.can_disable,
                implementation_id, implementation_version, features, required, True,
                issue, True, tuple(enrich(RequiredResource(feature.feature_id, model.role,
                    model.capability, model.adapters, checked.data.get(model.selection_field, "")))
                    for feature in definition.resource_features for model in feature.models
                    if model.selection_field) if checked.success else ()))
        return tuple(result)

    def workflow_resource_digests(self, workflow):
        with self._lock:
            catalog = self._catalog.copy()
            bindings = dict(self._bindings)
        digests = set()
        for node in workflow.nodes:
            if node.node_type == "tool":
                try:
                    reference = ToolReference.from_dict(node.params)
                except (ValueError, TypeError, KeyError):
                    continue
                settings = node.params["settings"]
            else:
                reference = bindings.get(node.node_type)
                settings = node.params
            if reference is None:
                continue
            digests.update(digest for _, digest in reference.resources)
            resolved = catalog.resolve(reference)
            if resolved.success:
                checked = resolved.data.parameters.normalize(settings, reference.params_version)
                if checked.success:
                    digests.update(digest for _, digest in resolved.data.resource_references(checked.data))
        return frozenset(digests)

    def _denoise_executor(self, resources, execute):
        from ..core.denoiser import ImageDenoiser
        processor = ImageDenoiser(execution_threads=self._execution_threads)
        return FunctionExecutor(lambda source, params: execute(source, params,
            model_path=resources["denoise_model"], processor=processor), close=processor.close)

    def _resource_digest(self, path, is_cancelled):
        path = Path(path).resolve()
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                if is_cancelled():
                    raise CancelledError()
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError("资源在读取期间发生变化")
        return str(path), digest.hexdigest()

    def _verified_resources(self, reference, resource_paths, is_cancelled):
        resources = {}
        for identity, digest in reference.resources:
            path = resource_paths.get((identity, digest))
            try:
                resolved, actual = self._resource_digest(path, is_cancelled)
                if actual != digest:
                    raise ValueError("资源校验失败")
            except (OSError, ValueError, TypeError):
                self._executors.invalidate({reference.tool_id})
                raise CoreOperationError(CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING,
                    "error.tool.resource_missing", details={"tool_id": reference.tool_id,
                                                           "resource_id": identity}).error)
            resources[identity] = resolved
        return resources

    def _resource_paths(self, reference, resource_paths):
        resources = {}
        for identity, digest in reference.resources:
            try:
                resources[identity] = str(Path(resource_paths[(identity, digest)]).resolve())
            except (KeyError, OSError, ValueError, TypeError):
                self._executors.invalidate({reference.tool_id})
                raise CoreOperationError(CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING,
                    "error.tool.resource_missing", details={"tool_id": reference.tool_id,
                                                           "resource_id": identity}).error)
        return resources

    @contextmanager
    def _borrow_executor(self, reference, definition, resources, generation, is_cancelled, *, threads=None):
        cancelled = lambda: not self.is_current(generation) or is_cancelled()
        paths = {(identity, digest): resources[identity] for identity, digest in reference.resources}
        def create():
            stack = ExitStack()
            try:
                if definition.executor_memory is not None and self._resource_budget is not None:
                    hold = self._hold_processor_memory(definition.executor_memory(resources), cancelled)
                    stack.callback(hold.close)
                if self.models is not None:
                    keys = {found[0] for _, digest in reference.resources
                            if (found := self.models.resolve_digest(digest)) is not None}
                    for key in keys:
                        stack.enter_context(self.models.borrow(key))
                self._verified_resources(reference, paths, cancelled)
                executor = definition.create_executor(resources)
                return LeasedExecutor(executor, stack)
            except BaseException:
                stack.close()
                raise
        with ExitStack() as operations, self._compute.borrow(cancelled, threads=threads) as compute:
            if self.models is not None:
                keys = {found[0] for _, digest in reference.resources
                        if (found := self.models.resolve_digest(digest)) is not None}
                for key in keys:
                    operations.enter_context(self.models.borrow(key))
            if definition.reusable_executor:
                key = (reference, tuple(sorted(resources.items())), self._execution_threads, self.inference.target())
                with self._executors.borrow(key, reference.tool_id, create, cancelled,
                        digests=frozenset(digest for _, digest in reference.resources)) as executor:
                    self._verified_resources(reference, paths, cancelled)
                    yield executor, compute
                    self._verified_resources(reference, paths, cancelled)
            else:
                if cancelled():
                    raise CancelledError()
                executor = create()
                try:
                    yield executor, compute
                    self._verified_resources(reference, paths, cancelled)
                finally:
                    executor.close()

    @contextmanager
    def borrow_processor(self, tool_id, implementation_id, factory, *, resources=None,
                         is_cancelled=lambda: False, memory_bytes=0):
        with self._lock:
            generation = self._generation
        available = self.check_enabled(tool_id)
        if not available.success:
            raise CoreOperationError(available.error)
        cancelled = lambda: not self.is_current(generation) or is_cancelled()
        identities = []
        for identity, path in sorted((resources or {}).items()):
            try:
                resolved, digest = self._resource_digest(path, cancelled)
            except (OSError, ValueError, TypeError):
                self._executors.invalidate({tool_id})
                raise CoreOperationError(CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING,
                    "error.tool.resource_missing", details={"tool_id": tool_id, "resource_id": identity}).error)
            identities.append((identity, resolved, digest))
        key = (tool_id, implementation_id, tuple(identities), self._execution_threads, self.inference.target())
        def verify():
            for identity, path, digest in identities:
                self._verified_resources(
                    ToolReference(tool_id, 1, implementation_id, "legacy-1", ((identity, digest),)),
                    {(identity, digest): path}, cancelled)
        with self._compute.borrow(cancelled):
            def create():
                verify()
                hold = self._hold_processor_memory(memory_bytes, cancelled) if memory_bytes and self._resource_budget is not None else None
                try:
                    processor = factory(self._execution_threads)
                    return LeasedProcessor(processor, hold) if hold is not None else processor
                except BaseException:
                    if hold is not None:
                        hold.close()
                    raise
            with self._executors.borrow(key, tool_id, create, cancelled,
                    digests=frozenset(digest for _, _, digest in identities)) as processor:
                verify()
                yield processor
                verify()
                if cancelled():
                    raise CancelledError()

    def _hold_processor_memory(self, size, cancelled):
        try:
            return self._resource_budget.hold_memory(size, cancelled)
        except MemoryError:
            self._executors.trim_idle()
            return self._resource_budget.hold_memory(size, cancelled)

    def analyze(self, tool_id, image, settings, *, is_cancelled=lambda: False, purpose="parameters",
                reference=None, context=None, preset=False):
        if purpose not in ("parameters", "regions", "snapshot"):
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.tool.incompatible")
        available = self.check_enabled(tool_id)
        if not available.success:
            return available
        from .tool_modules import analysis_settings
        reference = self.effect_reference(tool_id, analysis_settings(tool_id, settings), reference=reference)
        if not reference.success:
            return reference
        with self._lock:
            generation = self._generation
            definition = self._catalog.resolve(reference.data).data
            paths = dict(self._resources)
        checked = definition.parameters.normalize(settings, reference.data.params_version)
        if not checked.success:
            checked.error.details["tool_id"] = tool_id
            return checked
        cancelled = lambda: not self.is_current(generation) or is_cancelled()
        try:
            resources = self._resource_paths(reference.data, paths)
            if definition.prepare_input is not None:
                from ..core.photo_engine import RenderSource, _src_from_pil
                with ExitStack() as preparation:
                    if not isinstance(image, RenderSource) and self._resource_budget is not None:
                        preparation.enter_context(self._resource_budget.working_memory(image.width * image.height * 64, cancelled))
                    source = image if isinstance(image, RenderSource) else _src_from_pil(image)
                    if source.is_proxy:
                        from .canonical_input import CanonicalInputUnavailable
                        raise CanonicalInputUnavailable()
                    execution_context = {**(context or {}), "is_cancelled": cancelled}
                    key = definition.prepare_input(source, execution_context, reference.data)
                    def load_preparation(abandoned):
                        memory = (self._resource_budget.working_memory(definition.preparation_memory(source), abandoned)
                                  if self._resource_budget is not None and definition.preparation_memory is not None else nullcontext())
                        with memory, self._borrow_executor(reference.data, definition, resources, generation, abandoned) as (executor, _):
                            return executor.prepare(source, key, abandoned)
                    with self._input_preparation.borrow(key, load_preparation, cancelled) as prepared:
                        if purpose == "snapshot":
                            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled") if cancelled() else CoreResult.ok(prepared)
                        if purpose == "parameters" and reference.data.implementation_id == "beauty.detail":
                            from ..core.contracts.beauty import FaceAnalysisSnapshot
                            if (isinstance(prepared, FaceAnalysisSnapshot) and not prepared.processed
                                    and prepared.status in ("no_face", "face_too_small", "insufficient_landmarks")):
                                return (CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled") if cancelled() else
                                    CoreResult.fail(ErrorCode.INVALID_INPUT, "beauty.detail." + prepared.status,
                                        details={"tool_id": "beauty", "processed": 0, "skipped": prepared.skipped}))
                        if purpose == "parameters" and self._resource_budget is not None and definition.working_memory is not None:
                            maximum = {key: max(1, value) if type(value) is int else value for key, value in checked.data.items()}
                            preparation.enter_context(self._resource_budget.working_memory(
                                definition.working_memory(source, maximum, prepared), cancelled))
                        with self._borrow_executor(reference.data, definition, resources, generation, is_cancelled) as (executor, _):
                            result = (executor.regions(source, prepared) if purpose == "regions"
                                      else executor.analyze(source, prepared, preset=preset))
                            if purpose == "parameters":
                                candidate = executor.execute(source, {**checked.data, **result},
                                    {**execution_context, "prepared_input": prepared, "threads": self._execution_threads})
                                if not candidate.success:
                                    return candidate
                    return (CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled") if cancelled() else CoreResult.ok(result))
            from ..core.photo_engine import RenderSource, _pil_from
            if isinstance(image, RenderSource):
                with _pil_from(image) as converted:
                    return self.analyze(tool_id, converted, settings, is_cancelled=is_cancelled,
                                        purpose=purpose, reference=reference.data, preset=preset)
            with self._borrow_executor(reference.data, definition, resources, generation, is_cancelled) as (executor, _):
                analyzer = executor.analyze if purpose == "parameters" else getattr(executor, "regions", None)
                if analyzer is None:
                    return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.incompatible")
                result = analyzer(image)
                return (CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled") if cancelled()
                        else CoreResult.ok(result))
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except CoreOperationError as error:
            return CoreResult.from_exception(error)
        except MemoryError as error:
            return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(error))

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            self._snapshot_catalog = None
            self._executors.close()
        self._input_preparation.close()
        if self._model_unsubscribe is not None:
            self._model_unsubscribe()
        if self._owns_models:
            self.models.close()

    def effect_reference(self, tool_id, settings, *, reference=None):
        from .tool_modules import MODULES, managed_module
        managed = managed_module(tool_id, settings, reference)
        if managed is not None:
            return self._managed_reference(tool_id, settings, managed)
        if tool_id in MODULES:
            return self._module_reference(tool_id, settings)
        if tool_id == "denoise":
            from ..core.tools.denoise import DENOISE_PARAMETERS
            from .tool_modules import denoise_required_resources
            checked = DENOISE_PARAMETERS.normalize(settings, 2)
            if not checked.success:
                return checked
            reference = (self.ensure_denoise_ai_model() if denoise_required_resources(checked.data) else
                next((item.reference for item in self.descriptors()
                      if item.tool_id == tool_id and item.params_version == 2
                      and item.implementation_id == "denoise.basic"
                      and item.implementation_version == str(BASIC_DENOISE_VERSION)), None))
        else:
            reference = self._bindings.get(tool_id)
        if reference is None:
            return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing",
                                   details={"tool_id": tool_id})
        return CoreResult.ok(reference)

    def check_enabled(self, tool_id):
        with self._lock:
            if self._closed:
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            if tool_id not in {item.tool_id for item in self.descriptors()}:
                return CoreResult.fail(ErrorCode.TOOL_MISSING, "error.tool.missing", details={"tool_id": tool_id})
            if tool_id in self._disabled:
                return CoreResult.fail(ErrorCode.TOOL_DISABLED, "error.tool.disabled", details={"tool_id": tool_id})
        return CoreResult.ok(None)

    def needs_preparation(self, tool_id, settings):
        from .tool_modules import MODULES, managed_module
        managed = managed_module(tool_id, settings)
        if managed is not None:
            checked = managed[0].normalize(settings, managed[0].version)
            definition = next(item for item in self._resource_definitions if item.descriptor.tool_id == tool_id)
            return checked.success and bool(definition.required_resources(checked.data))
        module = MODULES.get(tool_id)
        if module is not None and module.needs_preparation is not None:
            return bool(module.needs_preparation(settings))
        with self._lock:
            return self._catalog.needs_preparation(tool_id, settings)

    def validate_tool_params(self, tool_id, settings, *, reference=None):
        enabled = self.check_enabled(tool_id)
        if not enabled.success:
            return enabled
        reference = self.effect_reference(tool_id, settings, reference=reference)
        if not reference.success:
            return reference
        with self._lock:
            definition = self._catalog.resolve(reference.data)
        if not definition.success:
            return definition
        checked = definition.data.parameters.normalize(settings, reference.data.params_version)
        if not checked.success:
            checked.error.details["tool_id"] = tool_id
        return checked

    def supports_tool_proxy(self, tool_id, reference=None):
        if reference is not None:
            resolved = self._catalog.resolve(reference)
            return resolved.success and resolved.data.supports_proxy
        descriptors = tuple(item for item in self._catalog.descriptors() if item.tool_id == tool_id)
        if not descriptors:
            return False
        default = next((definition.descriptor for definition in self._resource_definitions
                        if definition.descriptor.tool_id == tool_id), None)
        matching = tuple(item for item in descriptors if default is None or item.implementation_id == default.implementation_id)
        if not matching:
            return False
        latest = max(matching, key=lambda item: item.params_version)
        return self._catalog.resolve(latest.reference).data.supports_proxy

    def supports_proxy(self, workflow, *, canonical_inputs=False):
        for node in workflow.nodes:
            if node.node_type in ("source", "export"):
                continue
            if node.node_type != "tool":
                return False
            try:
                resolved = self._catalog.resolve(ToolReference.from_dict(node.params))
            except (ValueError, TypeError, KeyError):
                return False
            if not resolved.success or not resolved.data.supports_proxy:
                return False
            if resolved.data.prepare_input is not None and not canonical_inputs:
                return False
        return True

    def uses_prepared_inputs(self, workflow):
        for node in workflow.nodes:
            if node.node_type == "tool":
                resolved = self._catalog.resolve(ToolReference.from_dict(node.params))
                if resolved.success and resolved.data.prepare_input is not None:
                    return True
        return False

    def execute_preview(self, snapshot, source, workflow, *, is_cancelled=lambda: False,
                        full_source=None, temporary_workflow=None, sampling=None, resources=None, describe=False):
        from .canonical_input import CanonicalInputPreparer
        cancelled = lambda: snapshot.is_cancelled() or is_cancelled()
        budget = self._resource_budget if resources is None else resources
        preparer = CanonicalInputPreparer(snapshot, workflow, full_source, budget,
                                         temporary_workflow=temporary_workflow)
        result = snapshot.executor.execute(source, workflow, is_cancelled=cancelled,
                                           sampling=sampling, input_preparer=preparer)
        actual_input = source
        if (not result.success and result.error.details.get("requires_full_input")
                and full_source is not None and not cancelled()):
            try:
                with full_source(cancelled) as lease:
                    memory = (budget.working_memory(lease.value.pixels.nbytes * 4, cancelled)
                              if budget is not None else nullcontext())
                    with memory:
                        result = snapshot.executor.execute(lease.value, workflow, is_cancelled=cancelled,
                                                           sampling=sampling, input_preparer=preparer)
                        actual_input = lease.value
            except CancelledError:
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            except MemoryError as error:
                return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(error))
            except Exception as error:
                return CoreResult.from_exception(error, message_key="error.workflow.execution_failed")
        if describe and result.success:
            return CoreResult.ok(PreviewExecution(result.data, tuple(reversed(actual_input.pixels.shape[:2])),
                                                 actual_input.is_proxy))
        return result

    def set_enabled(self, tool_id, enabled):
        if type(enabled) is not bool:
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.param.type")
        with self._lock:
            if tool_id not in {item.tool_id for item in self.descriptors()}:
                return CoreResult.fail(ErrorCode.TOOL_MISSING, "error.tool.missing", details={"tool_id": tool_id})
            if not enabled and not self._catalog.can_disable(tool_id):
                return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.required", details={"tool_id": tool_id})
            was_enabled = tool_id not in self._disabled
            if enabled:
                self._disabled.discard(tool_id)
            else:
                self._disabled.add(tool_id)
            if was_enabled != enabled:
                self._generation += 1
                self._executors.invalidate()
            return CoreResult.ok(None)

    def list_status(self):
        """Return the current tool availability without exposing runtime internals."""
        with self._lock:
            disabled = frozenset(self._disabled)
            descriptors = self.descriptors()
            switchable = {item.tool_id: self._catalog.can_disable(item.tool_id) for item in descriptors}
        return tuple({
            "tool_id": descriptor.tool_id,
            "implementation_id": descriptor.implementation_id,
            "implementation_version": descriptor.implementation_version,
            "enabled": descriptor.tool_id not in disabled,
            "can_disable": switchable[descriptor.tool_id],
        } for descriptor in descriptors)

    def restore_disabled(self, tool_ids):
        if isinstance(tool_ids, (str, bytes)):
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.param.type")
        requested = {str(tool_id) for tool_id in tool_ids}
        known = {item["tool_id"] for item in self.list_status()}
        unknown = sorted(requested - known)
        if unknown:
            return CoreResult.fail(ErrorCode.TOOL_MISSING, "error.tool.missing",
                                   details={"tool_ids": unknown})
        with self._lock:
            requested = {tool_id for tool_id in requested if self._catalog.can_disable(tool_id)}
            if requested != self._disabled:
                self._disabled = requested
                self._generation += 1
                self._executors.invalidate()
        return CoreResult.ok(None)

    @property
    def memory_limit(self):
        """工作内存的上限（字节），供工具在执行前判断做不做得了；没有资源预算时为 None。"""
        return self._resource_budget.policy.memory_bytes if self._resource_budget is not None else None

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def is_current(self, generation):
        with self._lock:
            return not self._closed and generation == self._generation

    @contextmanager
    def publication(self, snapshot):
        with self._lock:
            yield not self._closed and snapshot.generation == self._generation

    def snapshot(self, *, require_resource_versions=False, is_cancelled=lambda: False, allow_model_gaps=False):
        if is_cancelled():
            raise CancelledError()
        with self._lock:
            if self._closed:
                raise CancelledError()
        from ..core.nodes.source_node import execute as source
        from ..core.nodes.export_node import execute as export
        if self._auto_ai_model:
            self.ensure_denoise_ai_model()
        if is_cancelled():
            raise CancelledError()
        with self._lock:
            if self._closed:
                raise CancelledError()
            generation = self._generation
            disabled = frozenset(self._disabled)
            if self._snapshot_catalog is None or self._snapshot_catalog[0] != generation:
                self._snapshot_catalog = generation, self._catalog.copy()
            catalog = self._snapshot_catalog[1]
            resource_paths = dict(self._resources)
        model_paths = {file.digest: dict(snapshot.paths)[file.name]
                       for snapshot in self.models.list() if snapshot.usable
                       for file in snapshot.descriptor.files if file.name in dict(snapshot.paths)} if self.models else {}
        registry = NodeRegistry()

        def inspect_reference(reference, settings, *, check_legacy=False):
            if is_cancelled():
                raise CancelledError()
            resolved = catalog.resolve(reference)
            if not resolved.success:
                if (reference.tool_id == "denoise" and reference.params_version == 2
                        and reference.implementation_id == "denoise.ai"
                        and reference.implementation_version == "2" and reference.resources):
                    return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing",
                                           details={"tool_id": "denoise", "resource_id": "denoise_model"})
                return resolved
            if reference.tool_id in disabled:
                return CoreResult.fail(ErrorCode.TOOL_DISABLED, "error.tool.disabled", details={"tool_id": reference.tool_id})
            if resolved.data.create_executor is None:
                return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "error.tool.export_only", details={"tool_id": reference.tool_id})
            if (require_resource_versions or check_legacy) and resolved.data.legacy_resource_check:
                checked = resolved.data.parameters.normalize(settings, reference.params_version)
                if checked.success and resolved.data.legacy_resource_check(checked.data):
                    return CoreResult.fail(ErrorCode.TOOL_LEGACY_UNPINNED, "error.tool.legacy_unpinned",
                                           details={"tool_id": reference.tool_id})
            for identity, digest in reference.resources:
                if allow_model_gaps and resolved.data.allow_resource_gaps_edit:
                    continue
                path = resource_paths.get((identity, digest))
                managed = self.models.resolve_digest(digest) if self.models is not None else None
                unavailable = managed is not None and not self.models.snapshot(managed[0]).usable
                if unavailable or not path or not Path(path).is_file():
                    self._executors.invalidate({reference.tool_id})
                    return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing",
                                           details={"tool_id": reference.tool_id, "resource_id": identity})
                if require_resource_versions:
                    actual = hashlib.sha256()
                    try:
                        with open(path, "rb") as stream:
                            for chunk in iter(lambda: stream.read(1 << 20), b""):
                                if is_cancelled():
                                    raise CancelledError()
                                actual.update(chunk)
                    except OSError:
                        self._executors.invalidate({reference.tool_id})
                        return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing",
                                               details={"tool_id": reference.tool_id, "resource_id": identity})
                    if actual.hexdigest() != digest:
                        self._executors.invalidate({reference.tool_id})
                        return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing",
                                               details={"tool_id": reference.tool_id, "resource_id": identity})
            normalized = resolved.data.parameters.normalize(settings, reference.params_version)
            if not normalized.success:
                normalized.error.details["tool_id"] = reference.tool_id
            elif reference.tool_id == "denoise" and reference.params_version == 2:
                from .tool_modules import denoise_required_resources
                needs_model = bool(denoise_required_resources(normalized.data))
                if needs_model != (reference.implementation_id == "denoise.ai"):
                    return CoreResult.fail(ErrorCode.PARAMETER_INVALID, "error.workflow.params",
                                           details={"tool_id": "denoise", "field_path": "settings.method"})
            from .tool_modules import managed_module
            managed = managed_module(reference.tool_id, normalized.data, reference) if normalized.success else None
            if managed is not None:
                try:
                    definition, _ = managed[1](normalized.data, self.models, self._execution_threads, self.inference)
                except CoreOperationError as error:
                    return CoreResult.from_exception(error)
                except ImportError:
                    return CoreResult.fail(ErrorCode.TOOL_INCOMPATIBLE, "models.runtime.missing", details={"tool_id": reference.tool_id})
                except (ValueError, KeyError):
                    return CoreResult.fail(ErrorCode.PARAMETER_INVALID, "error.workflow.params")
                if definition.descriptor.reference != reference:
                    return CoreResult.fail(ErrorCode.PARAMETER_INVALID, "error.workflow.params")
            if normalized.success and resolved.data.implicit_resources:
                for identity, digest in resolved.data.implicit_resources(normalized.data):
                    path = model_paths.get(digest)
                    if not path or not Path(path).is_file():
                        return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing",
                                               details={"tool_id": reference.tool_id, "resource_id": identity})
            return normalized

        def run(reference, incoming, settings, context):
            checked = inspect_reference(reference, settings)
            if not checked.success:
                return checked
            if not self.is_current(generation):
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            definition = catalog.resolve(reference).data
            try:
                cancelled = lambda: not self.is_current(generation) or context["is_cancelled"]()
                execution_reference = replace(reference, resources=definition.resource_references(checked.data))
                paths = dict(resource_paths)
                for identity, digest in execution_reference.resources:
                    if (identity, digest) not in paths and digest in model_paths:
                        paths[(identity, digest)] = model_paths[digest]
                resources = self._resource_paths(execution_reference, paths)
                checked_image = validate_image(incoming, ImageCapabilities(), incoming)
                if not checked_image.success:
                    return checked_image
                pixels = incoming.pixels.view()
                pixels.setflags(write=False)
                alpha = None if incoming.alpha is None else incoming.alpha.view()
                if alpha is not None:
                    alpha.setflags(write=False)
                borrowed = replace(incoming, pixels=pixels, alpha=alpha)
                neutral = definition.parameters.is_neutral(checked.data, reference.params_version)
                if neutral and definition.prepare_input is not None:
                    return CoreResult.ok(incoming)
                if borrowed.is_proxy and definition.prepare_input is not None and not definition.supports_proxy:
                    from .canonical_input import CanonicalInputUnavailable
                    raise CanonicalInputUnavailable()
                input_preparer = context.get("input_preparer")
                budget = input_preparer.resources if input_preparer is not None else self._resource_budget
                memory = (budget.working_memory(definition.working_memory(borrowed, checked.data), cancelled)
                          if budget is not None and definition.working_memory is not None
                          and definition.prepare_input is None else nullcontext())
                threads = (definition.minimum_threads(checked.data, self._execution_threads)
                           if definition.minimum_threads is not None else self._execution_threads)
                with ExitStack() as preparation:
                    execution_context = {**context, "is_cancelled": cancelled}
                    if definition.prepare_execution is not None:
                        execution_context["execution_input"] = preparation.enter_context(
                            definition.prepare_execution(borrowed, checked.data, cancelled))
                    preparation.enter_context(memory)
                    if definition.prepare_input is not None:
                        key = definition.prepare_input(borrowed, execution_context, reference)
                        def prepare_full(full_input, identity, abandoned):
                            preparation_memory = (budget.working_memory(definition.preparation_memory(full_input), abandoned)
                                if budget is not None and definition.preparation_memory is not None else nullcontext())
                            with preparation_memory, self._borrow_executor(execution_reference, definition, resources, generation,
                                    abandoned, threads=threads) as (preparer, _):
                                return preparer.prepare(full_input, identity, abandoned)
                        def load_preparation(abandoned):
                            if not borrowed.is_proxy:
                                return prepare_full(borrowed, key, abandoned)
                            if input_preparer is None:
                                from .canonical_input import CanonicalInputUnavailable
                                raise CanonicalInputUnavailable()
                            return input_preparer.prepare(borrowed, execution_context, reference, key,
                                                          definition.prepare_input, prepare_full, abandoned)
                        prepared = None
                        if borrowed.is_proxy and input_preparer is not None:
                            temporary_key = input_preparer.temporary_key(borrowed, execution_context, reference,
                                                                        definition.prepare_input)
                            if temporary_key is not None:
                                prepared = preparation.enter_context(
                                    self._input_preparation.borrow_cached(temporary_key, cancelled))
                        if prepared is None:
                            prepared = preparation.enter_context(self._input_preparation.borrow(key, load_preparation, cancelled))
                        execution_context["prepared_input"] = prepared
                        if budget is not None and definition.working_memory is not None:
                            preparation.enter_context(budget.working_memory(
                                definition.working_memory(borrowed, checked.data, execution_context["prepared_input"]), cancelled))
                    with self._borrow_executor(execution_reference, definition, resources, generation,
                            context["is_cancelled"], threads=threads) as (executor, compute):
                        result = executor.execute(borrowed, checked.data,
                                                  {**execution_context, "threads": compute.threads})
                    if cancelled():
                        return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                    if not isinstance(result, CoreResult):
                        return CoreResult.fail(ErrorCode.INTERNAL_ERROR, "error.tool.image_contract")
                    if result.success:
                        checked_image = validate_image(result.data, definition.descriptor.image, incoming)
                        if not checked_image.success:
                            return checked_image
                    return result
            except CancelledError:
                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
            except ProjectAssetError as error:
                return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.mask.asset", details={"path": error.path})
            except MemoryError as memory_error:
                return CoreResult.fail(ErrorCode.DECODE_FAILED, memory_error_key(memory_error))
            except CoreOperationError as error:
                return CoreResult.from_exception(error)
            except FileNotFoundError:
                return CoreResult.fail(ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing", details={"tool_id": reference.tool_id})

        def inspect_tool(params):
            try:
                reference = ToolReference.from_dict(params)
            except (ValueError, TypeError, KeyError):
                return CoreResult.fail(ErrorCode.PARAMETER_INVALID, "error.workflow.params")
            return inspect_reference(reference, params["settings"], check_legacy=True)

        def required_threads(reference, settings):
            resolved = catalog.resolve(reference)
            if not resolved.success or resolved.data.minimum_threads is None:
                return self._execution_threads
            return resolved.data.minimum_threads(settings, self._execution_threads)

        def sampling_behavior(reference):
            resolved = catalog.resolve(reference)
            return resolved.data.sampling_behavior if resolved.success else "unsupported"


        registry.register(NodeSpec("tool", execute=lambda incoming, params, *, context: run(
            ToolReference.from_dict(params), incoming, params["settings"], context), inspect_params=inspect_tool,
            accepts_context=True, required_threads=lambda params: required_threads(
                ToolReference.from_dict(params), params["settings"]),
            sampling_behavior=lambda params: sampling_behavior(ToolReference.from_dict(params))))
        for kind, reference in self._bindings.items():
            if not isinstance(kind, str):
                continue
            registry.register(NodeSpec(kind, execute=lambda incoming, params, ref=reference, *, context: run(ref, incoming, params, context),
                inspect_params=lambda params, ref=reference: inspect_reference(ref, params), accepts_context=True,
                required_threads=lambda params, ref=reference: required_threads(ref, params),
                sampling_behavior=lambda params, ref=reference: sampling_behavior(ref)))

        def empty(params):
            if params:
                raise ValueError("结构节点不接受参数")
        registry.register(NodeSpec("source", execute=source, validate_params=empty, required_threads=lambda params: 1,
                                   sampling_behavior=lambda params: "preserve"))
        registry.register(NodeSpec("export", execute=export, validate_params=empty, required_threads=lambda params: 1,
                                   sampling_behavior=lambda params: "preserve"))
        return RuntimeSnapshot(self, generation, registry, self._execution_threads)

    def sampling_capability(self, workflow, request):
        self._prepare_modules(workflow)
        return self.snapshot().sampling_capability(workflow, request)

    def inspect(self, workflow, *, require_resource_versions=False, is_cancelled=lambda: False):
        _, result = self.inspect_with_generation(workflow, require_resource_versions=require_resource_versions,
                                                  is_cancelled=is_cancelled)
        return result

    def inspect_with_generation(self, workflow, *, require_resource_versions=False, is_cancelled=lambda: False):
        self._prepare_modules(workflow)
        snapshot = self.snapshot(require_resource_versions=require_resource_versions, is_cancelled=is_cancelled)
        result = inspect_workflow(workflow, snapshot.registry)
        return snapshot.generation, result

    def validate(self, workflow, *, allow_model_gaps=False):
        self._prepare_modules(workflow)
        return validate(workflow, self.snapshot(allow_model_gaps=allow_model_gaps).registry)

    def _prepare_modules(self, workflow):
        from .tool_modules import managed_module
        for node in getattr(workflow, "nodes", ()):
            if node.node_type != "tool":
                continue
            try:
                reference = ToolReference.from_dict(node.params)
            except (ValueError, TypeError, KeyError):
                continue
            with self._lock:
                available = self._catalog.resolve(reference).success
            # 受管模型的工具每次都重新绑定，模型的路径与可用状态以现在的为准
            if (not available or reference.params_version >= 3
                    or managed_module(reference.tool_id, node.params["settings"]) is not None):
                self.effect_reference(reference.tool_id, node.params["settings"], reference=reference)
            with self._lock:
                available = self._catalog.resolve(reference).success
            if not available:
                self._restore_denoise_resources(reference)

    def _restore_denoise_resources(self, reference):
        if (self.models is None or reference.tool_id != "denoise" or reference.params_version != 2
                or reference.implementation_id != "denoise.ai" or reference.implementation_version != "2"
                or not reference.resources):
            return
        resources = {}
        for role, digest in reference.resources:
            found = self.models.resolve_digest(digest)
            if found is None:
                return
            resources[role] = found[1]
        if set(resources) != {"denoise_model"}:
            return
        from ..core.tools.denoise import DENOISE_PARAMETERS, execute
        from .tool_modules import DENOISE_RESOURCE_FEATURES, denoise_required_resources
        descriptor = ToolDescriptor("denoise", "2", 1, 2, "denoise.ai", "2",
            ImageCapabilities(intermediate_precision="srgb8"), reference.resources)
        definition = ToolDefinition(descriptor, DENOISE_PARAMETERS,
            lambda paths: self._denoise_executor(paths, execute), reusable_executor=True,
            resource_features=DENOISE_RESOURCE_FEATURES, collect_resources=denoise_required_resources,
            sampling_behavior="preserve")
        with self._lock:
            if not self._catalog.resolve(reference).success:
                self._catalog.register(definition)
                self._generation += 1
            for role, digest in reference.resources:
                self._resources[(role, digest)] = resources[role]


def validate_image(source, capability, incoming):
    import numpy as np
    if (not hasattr(source, "pixels") or source.pixels.ndim != 3 or source.pixels.shape[2] != 3
            or min(source.pixels.shape[:2]) < 1 or source.pixels.dtype != np.float32
            or source.colorspace != capability.colorspace or source.white_point != "D65"
            or not np.all(np.isfinite(source.pixels))):
        return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.tool.image_contract")
    if source.alpha is not None and (source.alpha.shape != source.pixels.shape[:2]
            or source.alpha.dtype != np.float32 or not np.all(np.isfinite(source.alpha))
            or np.any(source.alpha < 0) or np.any(source.alpha > 1)):
        return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.tool.image_contract")
    if capability.geometry == "preserve" and source.pixels.shape != incoming.pixels.shape:
        return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.tool.image_contract")
    if capability.alpha == "preserve" and ((source.alpha is None) != (incoming.alpha is None)
            or source.alpha is not None and not np.array_equal(source.alpha, incoming.alpha)):
        return CoreResult.fail(ErrorCode.INVALID_INPUT, "error.tool.image_contract")
    return CoreResult.ok(None)
