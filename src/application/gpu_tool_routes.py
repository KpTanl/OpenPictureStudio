from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import RLock, Condition
from concurrent.futures import CancelledError
import hashlib
import uuid

from ..core.contracts.gpu import DeviceModelSpec, GpuExecutionError
from ..core.contracts.execution import current_execution, ExecutionContext, ExecutionTarget
from ..core.contracts.image_display import PreviewDisplaySpec
from ..core.contracts.gpu import display_request
from ..core.beauty_detection import DETECTION_SPECS
from ..core.contracts.beauty import detail_beauty_algorithm, DETAIL_BEAUTY_PROFILES
from .shared_stages import SharedStageStore


class _Route:
    def __init__(self, owner, identity, reference, definition, inference, resources, models, dependencies, neutral, layouts):
        self.owner, self.identity, self.reference, self.definition = owner, identity, reference, definition
        self.inference, self.resources, self.models = inference, resources, models
        self.dependencies, self.neutral = dependencies, neutral
        self.layouts = layouts
        self.runtime = inference.device_runtime_identity() if resources and not neutral and reference.tool_id != "beauty" else ()
        self.sessions, self.face, self.users = {}, None, 0
        self.retiring = False
        self.lock = RLock()
        self.passed = set()

    @property
    def required(self):
        if self.reference.tool_id == "beauty":
            from .gpu_beauty import COLOR_KEYS, WARP_KEYS
            return frozenset((*COLOR_KEYS, *WARP_KEYS, "combined", "multiple", "alpha", "crop", "no_face"))
        if self.reference.tool_id == "denoise":
            methods = ("ai", "tiles", "small", "tail") if self.resources else ("bilateral", "gaussian", "median")
            return frozenset((*methods, "luma", "chroma", "sharpen", "combined", "alpha", "edge"))
        if self.reference.tool_id == "bg_remove":
            return frozenset(("ai" if self.resources else "color", "edge", "transparent", "white", "black", "alpha"))
        if self.reference.tool_id == "id_photo":
            return frozenset(("crop", "single", "sheet", "alpha", "background", "ai" if self.resources else "preserve"))
        extra = ("contain", "cover", "pad", "2x", "4x", "4k") if "super_resolution" in self.models else ()
        return frozenset(("tiles", "alpha", "transparent", "all_transparent", "layout", "chain", *self.models, *extra))

    @property
    def capable(self):
        return self.neutral or self.reference.tool_id == "beauty" or self.required <= self.passed

    def face_session(self, cancelled):
        with self.lock:
            if self.face is None:
                algorithm = detail_beauty_algorithm(self.reference.implementation_version)
                version = DETAIL_BEAUTY_PROFILES[algorithm][2]
                spec = DETECTION_SPECS[version if "face_locator" in self.resources else min(version, 2)]
                self.face = self.inference.create_face_session(self.resources["face_model"], detection_spec=spec,
                    locator_path=self.resources.get("face_locator"), cancelled=cancelled)
            return self.face

    def model(self, role, cancelled, spec=None):
        with self.lock:
            key = role, spec
            if key not in self.sessions:
                if spec is None:
                    model = self.models[role]
                    size = model.tile, model.tile
                    spec = DeviceModelSpec(size, (model.tile*model.scale,)*2, source_colorspace="srgb", destination_colorspace="srgb")
                self.sessions[key] = self.inference.create_device_session(self.resources[role], spec, cancelled=cancelled)
            return self.sessions[key]

    def execute(self, source, params, context, cancelled, snapshot=None):
        if self.neutral:
            return source
        if self.reference.tool_id == "ai_enhance":
            from .gpu_enhancement import GpuEnhancement
            return GpuEnhancement(self.owner.backend, self.models, lambda role: self.model(role, cancelled), cancelled).execute(source, params)
        if self.reference.tool_id == "denoise":
            from .gpu_denoise import GpuDenoise
            version = int(self.reference.implementation_version) if self.reference.implementation_id == "denoise.basic" else 2
            return GpuDenoise(self.owner.backend, lambda role, spec: self.model(role, cancelled, spec),
                self.layouts.get("denoise_model"), cancelled).execute(source, params, version)
        if self.reference.tool_id in ("bg_remove", "id_photo"):
            from .gpu_segmentation import GpuSegmentation
            processor = GpuSegmentation(self.owner.backend, lambda role, spec: self.model(role, cancelled, spec),
                                        self.layouts.get("model"), cancelled)
            return (processor.background if self.reference.tool_id == "bg_remove" else processor.photo)(source, params)
        from .gpu_beauty import GpuBeauty
        from .gpu_faces import upload_face_snapshot
        prepared = snapshot if snapshot is not None else context.get("prepared_input")
        snapshot = upload_face_snapshot(self.owner.backend, source, prepared, cancelled)
        return GpuBeauty(self.owner.backend, cancelled).execute(source, params, snapshot, node_id=context.get("node_id", ""))

    def close(self):
        with self.lock:
            sessions, self.sessions = self.sessions, {}
            face, self.face = self.face, None
        for session in sessions.values():
            session.close()
        if face is not None:
            face.close()


class GpuToolRoutes:
    def __init__(self, backend):
        self.backend, self.lock = backend, RLock()
        self.condition = Condition(self.lock)
        self.routes = OrderedDict()
        self.metadata = OrderedDict()

    @staticmethod
    def supported(reference):
        if reference.tool_id == "denoise":
            return reference.params_version == 2 and (reference.implementation_id == "denoise.basic" and reference.implementation_version in ("2", "3")
                or reference.implementation_id == "denoise.ai" and reference.implementation_version == "2")
        if reference.tool_id == "id_photo":
            return reference.params_version == 2 and reference.implementation_id == "id_photo.managed" and reference.implementation_version == "1"
        if reference.params_version != 3:
            return False
        if reference.tool_id == "bg_remove":
            return reference.implementation_id == "bg_remove.managed" and reference.implementation_version == "1"
        if reference.tool_id == "ai_enhance":
            return reference.implementation_id == "ai_enhance.neural" and reference.implementation_version == "1"
        if reference.tool_id == "beauty" and reference.implementation_id == "beauty.detail":
            try:
                return detail_beauty_algorithm(reference.implementation_version) == 5
            except ValueError:
                pass
        return False

    @staticmethod
    def contract(reference):
        from ..core.tools.managed_models import MANAGED_BEAUTY_PARAMETERS, MANAGED_BG_PARAMETERS
        from ..core.tools.enhancement import ENHANCE_PARAMETERS
        from ..core.tools.denoise import DENOISE_PARAMETERS
        from ..core.tools.id_photo import MANAGED_ID_PHOTO_PARAMETERS
        return {"beauty": MANAGED_BEAUTY_PARAMETERS, "ai_enhance": ENHANCE_PARAMETERS,
                "denoise": DENOISE_PARAMETERS, "bg_remove": MANAGED_BG_PARAMETERS,
                "id_photo": MANAGED_ID_PHOTO_PARAMETERS}[reference.tool_id]

    def bind(self, reference, params, definition, paths, models, inference, dependencies):
        if not self.supported(reference) or self.backend.snapshot().state != "ready":
            return None
        neutral = definition.parameters.is_neutral(params, reference.params_version)
        if paths and not neutral and reference.tool_id != "beauty" and not inference.device_models_enabled(planning=True):
            return None
        descriptors = {}
        if reference.tool_id == "ai_enhance":
            for role, digest in reference.resources:
                found = models.resolve_digest(digest) if models is not None else None
                if found is None:
                    return None
                descriptor = models.snapshot(found[0]).descriptor
                if descriptor.adapter != "rgb_restoration":
                    return None
                descriptors[role] = descriptor
        layouts = {}
        if reference.tool_id in ("denoise", "bg_remove", "id_photo") and paths:
            from .model_tensor_metadata import tensor_metadata
            try:
                for role, path in paths.items():
                    key = self.backend._path(path)
                    with self.lock:
                        layout = self.metadata.get(key)
                    if layout is None:
                        layout = tensor_metadata(path)
                    with self.lock:
                        self.metadata[key] = layout
                        self.metadata.move_to_end(key)
                        while len(self.metadata) > 32:
                            self.metadata.popitem(last=False)
                    layouts[role] = layout
            except (OSError, ValueError):
                return None
        runtime = inference.device_runtime_identity() if paths and not neutral and reference.tool_id != "beauty" else ()
        files = tuple(sorted((role, self.backend._path(str(Path(path).resolve()))) for role, path in paths.items()))
        identity = SharedStageStore.key("gpu.tool.route", (reference, files, tuple(sorted(descriptors.items())),
            tuple(sorted(layouts.items())), runtime, self.backend.snapshot().identity, neutral,
            "cpu-face-snapshot-v1" if reference.tool_id == "beauty" else "device-model-v1"))[-1]
        deps = (*dependencies, ("gpu_route", identity))
        with self.lock:
            if identity not in self.routes:
                self.routes[identity] = _Route(self, identity, reference, definition, inference, dict(paths),
                                               descriptors, deps, neutral, layouts)
            self.routes.move_to_end(identity)
            for key in tuple(self.routes):
                route = self.routes[key]
                if len(self.routes) <= 64:
                    break
                if key != identity and not route.users and not route.retiring and not route.sessions and route.face is None:
                    self.routes.pop(key)
        return identity

    def supports(self, implementation, dependencies):
        identity = next((item[1] for item in dependencies if item[0] == "gpu_route"), None)
        with self.lock:
            route = self.routes.get(identity)
            return bool(route is not None and route.capable and route.dependencies == dependencies
                and implementation == (route.reference.tool_id, route.reference.params_version,
                    route.reference.implementation_id, route.reference.implementation_version)
                and (route.reference.tool_id == "beauty" or not route.resources or route.neutral or route.inference.device_models_enabled(planning=True)))

    def pending(self):
        with self.lock:
            return tuple({"identity": identity, "reference": route.reference, "resources": dict(route.resources),
                "required": tuple(sorted(route.required)), "passed": tuple(sorted(route.passed)), "ready": route.capable}
                for identity, route in self.routes.items())

    def hint(self, reference, params):
        with self.lock:
            routes = tuple(self.routes.values())
        return any(route.reference == reference and route.capable
            and route.definition.parameters.is_neutral(params, reference.params_version) == route.neutral
            and (route.reference.tool_id == "beauty" or not route.resources or route.neutral or route.runtime == route.inference.device_runtime_identity()
                 and route.inference.device_models_enabled()) for route in routes)

    def estimate(self, requirements, graph, steps):
        peak, cost = 0, 0
        with self.lock:
            routes = {identity: route for identity, route in self.routes.items()}
        for item in requirements:
            identity = next((value[1] for value in item.dependencies if value[0] == "gpu_route"), None)
            route = routes.get(identity)
            if route is None or route.neutral:
                continue
            size, output_size = steps[item.node_id]
            incoming = size[0]*size[1]
            outgoing = output_size[0]*output_size[1]
            if route.reference.tool_id == "beauty":
                peak = max(peak, incoming*48 + min(incoming, 4_000_000)*768)
                cost += 80
            elif route.reference.tool_id == "denoise":
                peak = max(peak, incoming*192 + (512**2*2048 if route.resources else 0))
                cost += 48
            elif route.reference.tool_id in ("bg_remove", "id_photo"):
                model_bytes = sum(Path(path).stat().st_size*4 for path in route.resources.values())
                peak = max(peak, max(incoming, outgoing)*160+model_bytes+320**2*512)
                cost += 40 if route.resources else 8
            else:
                models = tuple(route.models.values())
                activations = max((model.tile**2*(1024+model.scale**2*64) for model in models), default=0)
                params = graph.node(item.node_id).params["settings"]
                from ..core.tools.enhancement import output_plan, _upscales, _final_plan, _banded
                model = route.models.get("super_resolution")
                working = max(incoming, outgoing)*160
                if model is not None and _upscales(output_plan(size, params)):
                    full_size = size[0]*model.scale, size[1]*model.scale
                    final = _final_plan(full_size, output_plan(size, params), params)
                    if _banded(full_size, final):
                        band = min(size[1], model.tile-2*model.overlap)*model.scale*full_size[0]
                        working = incoming*96+outgoing*48+band*128
                    else:
                        working = incoming*64+full_size[0]*full_size[1]*96+outgoing*48
                peak = max(peak, working+activations)
                cost += 40*len(models)
        return peak, cost

    def invalidate(self, face_identity=None):
        with self.lock:
            for route in self.routes.values():
                if face_identity is None or route.face is not None and route.face.identity == face_identity:
                    route.passed.clear()

    @contextmanager
    def borrow(self, identity, cancelled=lambda: False):
        with self.condition:
            route = self.routes.get(identity)
            if route is None:
                raise GpuExecutionError("device_changed", "GPU tool route belongs to a retired device")
            while route.retiring:
                if cancelled():
                    raise CancelledError()
                self.condition.wait(.05)
            if self.routes.get(identity) is not route:
                raise GpuExecutionError("device_changed", "GPU tool route belongs to a retired device")
            route.users += 1
            self.routes.move_to_end(identity)
            idle = [item for key, item in self.routes.items() if key != identity and not item.users
                    and (item.sessions or item.face is not None)]
        try:
            for item in idle[:-2]:
                self._close_idle(item)
            yield route
        finally:
            with self.condition:
                route.users -= 1
                self.condition.notify_all()
            if len(route.sessions) > 8:
                self._close_idle(route)

    def _close_idle(self, route):
        with self.condition:
            if route.users or route.retiring:
                return
            route.retiring = True
        try:
            route.close()
        finally:
            with self.condition:
                route.retiring = False
                self.condition.notify_all()

    def trim_models(self):
        with self.condition:
            idle = [route for route in self.routes.values() if not route.users and (route.sessions or route.face is not None)]
        for route in idle:
            self._close_idle(route)

    @contextmanager
    def face_session(self, identity, cancelled=lambda: False):
        with self.borrow(identity, cancelled) as route:
            yield route.face_session(cancelled)

    def qualification_input(self, source, cancelled=lambda: False):
        from .gpu_geometry import GpuGeometry
        if current_execution.get() is not None or source.is_proxy or not source.content_identity:
            raise ValueError("GPU 对照输入需要独立的完整请求")
        if source.gpu_image is not None:
            if source.backend_identity != self.backend.snapshot().identity:
                raise ValueError("GPU 对照输入设备无效")
            return source
        source = source.materialize(cancelled)
        identity = self.backend.snapshot().identity
        if identity[0] != "gpu" or self.backend.settings.engine == "cpu" or source.colorspace != "linear_srgb":
            raise GpuExecutionError("device_changed", "GPU qualification requires a ready device and linear input")
        digest = hashlib.sha256()
        for top in range(0, source.pixels.shape[0], 64):
            if cancelled():
                raise CancelledError()
            digest.update(source.pixels[top:top+64].tobytes())
            if source.alpha is not None:
                digest.update(source.alpha[top:top+64].tobytes())
        target = ExecutionTarget(source.pixels.shape[1::-1], source.orig_size, False, source.colorspace,
            str(source.pixels.dtype), source.alpha is not None, source.white_point, source.gamut, source.color_pipeline)
        content = source.content_identity, "qualification", digest.hexdigest()
        context = ExecutionContext(uuid.uuid4().hex, content, "qualification", target, (), 0, *identity,
            0, 0, "qualification", cancelled, "manual", source_path=source.source_path)
        with display_request(PreviewDisplaySpec()):
            token = current_execution.set(context)
            try:
                artifact = self.backend.upload(source, context)
                return replace(GpuGeometry.updated(source, artifact), content_identity=source.content_identity)
            finally:
                current_execution.reset(token)

    def execute(self, reference, source, params, context, cancelled):
        execution = current_execution.get()
        requirement = next((item for item in execution.requirements if item.node_id == context["node_id"]), None)
        if requirement is None or not self.supports(requirement.implementation, requirement.dependencies):
            raise GpuExecutionError("chain_capability_missing", "GPU tool route is unavailable")
        identity = next(item[1] for item in requirement.dependencies if item[0] == "gpu_route")
        with self.borrow(identity, cancelled) as route:
            if route.reference != reference:
                raise GpuExecutionError("model_identity", "GPU tool resources changed")
            return route.execute(source, params, context, cancelled)

    def qualify(self, identity, source, params, expected, case, cancelled=lambda: False, *, snapshot=None, node_id=""):
        from ..core.photo_engine import display_rgba8
        import numpy as np
        if current_execution.get() is not None:
            raise ValueError("GPU 工具验收需要独立调用")
        with self.borrow(identity, cancelled) as route:
            if case not in route.required:
                route.passed.clear()
                raise ValueError("GPU 工具验收场景无效")
            checked = route.definition.parameters.normalize(params, route.reference.params_version)
            if not checked.success or source.gpu_image is None or source.is_proxy:
                route.passed.clear()
                raise ValueError("GPU 工具验收输入无效")
            if not source.content_identity or source.content_identity != expected.content_identity or expected.backend_identity[0] != "cpu":
                route.passed.clear()
                raise ValueError("GPU 工具对照输入身份无效")
            try:
                context = {"node_id": node_id, "is_cancelled": cancelled,
                    "upstream_identity": (source.content_identity, source.gpu_image.resident.identity)}
                with self.backend.frame(source.gpu_image.backend_identity, cancelled):
                    result = route.execute(source, checked.data, context, cancelled, snapshot=snapshot)
                actual = result.materialize(cancelled)
                expected = expected.materialize(cancelled)
                passed = (actual.orig_size == expected.orig_size and actual.pixels.shape == expected.pixels.shape
                    and (actual.alpha is None) == (expected.alpha is None) and actual.effect_status == expected.effect_status)
                maximum = 0
                passed = passed and source.backend_identity == self.backend.snapshot().identity
                passed = passed and (route.reference.tool_id == "beauty" or not route.resources or route.runtime == route.inference.device_runtime_identity())
                if passed:
                    first, second = display_rgba8(actual), display_rgba8(expected)
                    maximum = int(np.max(np.abs(first.astype(np.int16)-second.astype(np.int16))))
                    passed = maximum <= 1 and np.isfinite(actual.pixels).all() and (actual.alpha is None or np.isfinite(actual.alpha).all())
                    passed = passed and (actual.colorspace, actual.white_point, actual.gamut, actual.color_pipeline) == (
                        expected.colorspace, expected.white_point, expected.gamut, expected.color_pipeline)
                if case == "multiple" and passed:
                    passed = bool(actual.effect_status and actual.effect_status[-1].processed >= 2)
                if case == "alpha" or route.reference.tool_id == "ai_enhance" and case in ("transparent", "all_transparent"):
                    alpha = source.materialize(cancelled).alpha
                    if alpha is None:
                        passed = False
                    elif case == "alpha":
                        passed = passed and bool(np.any((alpha > 0) & (alpha < 1)))
                    elif case == "transparent":
                        passed = passed and bool(np.any(alpha <= 1e-6) and np.any(alpha > 1e-6))
                    else:
                        passed = passed and bool(np.all(alpha <= 1e-6))
                if route.reference.tool_id == "beauty":
                    if case in checked.data and not checked.data[case]:
                        passed = False
                    if case == "combined":
                        from .gpu_beauty import COLOR_KEYS, WARP_KEYS
                        passed = passed and all(checked.data[key] for key in (*COLOR_KEYS, *WARP_KEYS))
                    if case == "no_face":
                        passed = passed and bool(actual.effect_status and actual.effect_status[-1].status == "no_face")
                    elif not actual.effect_status or actual.effect_status[-1].processed < 1:
                        passed = False
                elif route.reference.tool_id == "ai_enhance":
                    from ..core.tools.enhancement import enhancement_required_resources, output_plan, _upscales
                    settings = checked.data
                    plan = output_plan(source.orig_size, settings)
                    active = {item.role for item in enhancement_required_resources(settings)}
                    if case in route.models:
                        passed = passed and case in active and (case != "super_resolution" or _upscales(plan))
                    elif case == "chain":
                        passed = passed and set(route.models) <= active
                    elif case == "tiles":
                        step = min(model.tile-2*model.overlap for model in route.models.values())
                        passed = passed and max(source.orig_size) > step
                    elif case in ("2x", "4x", "4k"):
                        passed = passed and settings["target"] == case
                    elif case in ("contain", "cover", "pad"):
                        passed = passed and settings["fit"] == case
                        if case == "pad":
                            passed = passed and any(plan.padding)
                        elif case == "cover":
                            passed = passed and plan.crop_box is not None and plan.crop_box != (0, 0, *source.orig_size)
                elif route.reference.tool_id == "denoise":
                    settings = checked.data
                    filtering = bool(settings["strength"] or settings["color_strength"])
                    if case in ("bilateral", "gaussian", "median", "ai"):
                        passed = passed and settings["method"] == case and bool(settings["strength"] or settings["color_strength"])
                    elif case in ("luma", "chroma", "sharpen"):
                        passed = passed and bool(settings[{"luma": "strength", "chroma": "color_strength", "sharpen": "sharpen"}[case]])
                    elif case == "combined":
                        passed = passed and all(settings[key] for key in ("strength", "color_strength", "sharpen"))
                    elif case in ("tiles", "small", "tail"):
                        from .gpu_denoise import GpuDenoise
                        tile, _, stride = GpuDenoise.tile_spec(route.layouts["denoise_model"])
                        width, height = source.pixels.shape[1::-1]
                        tiled = max(width, height) > tile
                        tail = any(length-start < tile for length in (width, height) for start in range(0, length, stride))
                        passed = passed and settings["method"] == "ai" and filtering
                        passed = passed and (not tiled if case == "small" else tiled and (case != "tail" or tail))
                    elif case == "edge":
                        original = source.materialize(cancelled).pixels
                        border = np.concatenate((original[0], original[-1], original[:, 0], original[:, -1]))
                        passed = passed and bool(filtering or settings["sharpen"]) and bool(np.ptp(border, axis=0).max() > .01)
                elif route.reference.tool_id == "bg_remove":
                    settings = checked.data
                    if case in ("ai", "color"):
                        passed = passed and settings["mode"] == case
                    elif case in ("transparent", "white", "black"):
                        passed = passed and settings["bg_type"] == case
                    elif case == "edge":
                        passed = passed and settings["edge_radius"] > 0
                elif route.reference.tool_id == "id_photo":
                    settings = checked.data
                    if case in ("single", "sheet"):
                        passed = passed and settings["layout"] == ("single" if case == "single" else "sheet_6inch")
                    elif case == "crop":
                        passed = passed and settings["crop_rect"] is not None and tuple(settings["crop_rect"]) != (0, 0, 1, 1)
                    elif case == "background":
                        passed = passed and settings["background"] is not None
                    elif case == "ai":
                        passed = passed and settings["ai_matting"] and settings["background"] is not None
                    elif case == "preserve":
                        passed = passed and settings["background"] is None
                if passed:
                    route.passed.add(case)
                else:
                    route.passed.clear()
                return {"passed": bool(passed), "case": case, "max_lsb": maximum,
                        "identity": identity, "ready": route.capable, "actual": actual}
            except BaseException:
                route.passed.clear()
                raise

    def clear(self):
        with self.condition:
            routes, self.routes = tuple(self.routes.values()), OrderedDict()
            self.metadata.clear()
            self.condition.notify_all()
            while any(route.users or route.retiring for route in routes):
                self.condition.wait(.05)
        for route in routes:
            route.close()
