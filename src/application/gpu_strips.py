"""整幅放不下显存时，按行分条在 GPU 上计算。

工作流执行时各节点只登记成计划里的一步；到显示或读回时，按输出行逐条往回推出每一步要的行，
从常驻的整数源转出这几行，逐步算完，写进显示帧或读回结果。坐标和边界都按整幅算，结果与整幅计算逐位相同：
- 传统降噪每条上下补核半径那么多行。
- 调色里的引导滤波先逐条算出输入、缩到整幅的低分辨率网格上，整幅算一次系数，再逐条应用。
- 美颜在整幅上算一次补丁，逐条叠加。
- 旋转裁剪按块从旋转前的图取样，旋转前的图按固定行块算，同一帧里各块共用。"""

from concurrent.futures import CancelledError
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from time import monotonic
import logging
import math

import numpy as np

from ..core.contracts.execution import current_execution
from ..core.contracts.gpu import DevicePixels, GpuArtifact, GpuResident, GpuExecutionError, ImageOutputSpec, display_target
from ..core.contracts.workflow import WorkflowGraph
from ..core.photo_engine import transfer_lut
from ..core.tools.geometry import crop_box, rotated_size
from .gpu_geometry import CONTRACTS, GpuGeometry, shape_primitives, _tool

logger = logging.getLogger(__name__)

# 一条里一份浮点缓冲最多这么大。分配器按 128 MiB 一块申请显存，比块大的缓冲单独申请，不留空位；
# 按 128 MiB 封顶时条数翻倍，旋转裁剪反而更慢
BLOCK_BYTES = 256 * 1024 ** 2
# 一条算到一半时占的显存，连分配器块里的空位，按这一条每个像素计
STRIP_PIXEL_BYTES = 256
MINIMUM_ROWS = 16
# 旋转裁剪的输出按这么大的块取样；旋转前的图最多按这么多行一块算
ROTATION_TILE = 256
ROTATION_ROWS = 512


def capable(source, graph, order=None, spec=None):
    """工作流里的节点都能逐条计算时为 True。"""
    if source.is_proxy or isinstance(spec, ImageOutputSpec) and spec.resize is not None and (
            spec.resize.width is not None or spec.resize.height is not None):
        return False
    width, height = source.pixels.shape[1], source.pixels.shape[0]
    if max(width, height) > 32768:
        return False
    from .gpu_color import strip_capable, strip_filters, workflow_cost
    try:
        for node in (graph.node(identity) for identity in order) if order is not None else graph.nodes:
            item = _tool(node)
            if item is None:
                continue
            kind, params = item
            if kind == "color_adjust":
                if not strip_capable(params):
                    return False
                # 引导滤波的系数在整幅的低分辨率网格上算，不降采样的那种分条算不了
                tone, details = strip_filters(params, (width, height))
                if any(spec[2] < 2 for spec in (tone, *details) if spec is not None):
                    return False
                workflow_cost(WorkflowGraph(nodes=(node,), edges=()), BLOCK_BYTES // 16)
            elif kind == "beauty":
                pass
            elif kind == "denoise":
                from ..core.contracts.tools import ToolReference
                from .gpu_denoise import GpuDenoise
                reference = ToolReference.from_dict(node.params)
                version = int(reference.implementation_version) if reference.implementation_id == "denoise.basic" else 2
                if GpuDenoise.strip_halo(params, version) is None:
                    return False
            elif kind == "crop":
                if params["rotation_angle"]:
                    width, height = rotated_size((width, height), params["rotation_angle"])
                left, top, right, bottom = crop_box(params["crop_rect"], (width, height))
                width, height = right - left, bottom - top
            elif kind == "shape_crop":
                x, y, w, h = params["rect"]
                left, top, right, bottom = crop_box((x, y, min(1, x + w), min(1, y + h)), (width, height))
                width, height = right - left, bottom - top
            elif kind != "lasso":
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def source_capable(backend, source):
    """整数源能整份留在显卡上、按行转成浮点时为 True。"""
    spec = source.input_spec
    if spec is None or source.is_proxy or tuple(source.pixels.shape[1::-1]) != tuple(spec.size):
        return False
    with backend._condition:
        if (backend._status.state != "ready" or backend.settings.engine == "cpu"
                or spec.qualification not in backend._source_ready):
            return False
        info = backend._device.info()
        return (not info["lost"] and max(spec.size) <= 32768
                and spec.nbytes <= min(info["max_binding_size"], info["max_buffer_size"])
                and spec.nbytes * 3 <= max(backend._budget(info), backend._reserved_budget()))


def is_strip(source):
    return source.gpu_image is not None and isinstance(source.gpu_image.service, StripPlan)


def _matrix(spec):
    if spec.transfer != "xyz-d65":
        return ()
    from ..core.dcp import xyz_d65_to_srgb_matrix
    return tuple(float(value) for row in xyz_d65_to_srgb_matrix() for value in row)


def _keys(backend, metadata, context):
    key = backend._stages.key("gpu.source_bytes", (metadata.content_identity, metadata.input_spec,
        metadata.pixels.shape[1::-1], metadata.orig_size, metadata.color_pipeline, context.backend_identity))
    return key, (metadata.source_path, "source_bytes", key[-1], context.backend_identity)


def prepare(backend, source_input, metadata, context, cancelled):
    encoded = source_input.value
    spec = encoded.spec
    key, cache_key = _keys(backend, replace(metadata, input_spec=spec), context)
    family = backend.family(metadata.source_path, context.workflow_identity, display_target.get())

    def load(abandoned):
        with ExitStack() as preparation:
            incoming = encoded
            if incoming.blocks is not None and not backend._blocks_ready:
                from ..core.source_input import materialize_input
                preparation.enter_context(backend.compute.suspend(abandoned))
                preparation.enter_context(backend.resources.working_memory(spec.nbytes, abandoned))
                with backend.compute.borrow(abandoned, priority=context.priority) as allocation:
                    try:
                        incoming = materialize_input(incoming, threads=allocation.threads, cancelled=abandoned)
                    except ValueError as error:
                        raise GpuExecutionError("source_blocks", str(error)) from error
            preparation.enter_context(backend.borrow(abandoned, priority=getattr(abandoned, "priority", context.priority)))
            try:
                if backend.snapshot().identity != context.backend_identity:
                    raise GpuExecutionError("device_changed", "Source input device changed")
                device = backend._device
                budget = backend._budget(device.info())
                retain = backend._trim(budget, metadata.source_path)
                device.configure(budget, backend._idle_limit(budget))
                cached = backend._cache.recent(cache_key)
                if cached is not None:
                    return cached
                options = {"blocks": incoming.blocks} if incoming.blocks is not None else {}
                with backend.host_memory(incoming.data.nbytes, abandoned):
                    started = monotonic()
                    image = backend._native_call(lambda: device.source_bytes(incoming.data, *spec.size, spec.channels,
                        spec.depth, spec.layout == "png-filtered", abandoned, **options))
                    logger.debug("GPU 整数源常驻 source=%s layout=%s depth=%s channels=%s size=%s bytes=%s duration_ms=%.1f",
                        metadata.source_path, spec.layout, spec.depth, spec.channels, spec.size, image.nbytes,
                        (monotonic() - started) * 1000)
                resident = GpuResident(image, context.backend_identity, None, key, encoded.raw_meta)
                if retain:
                    backend._cache[cache_key] = resident
                    backend._trim(budget)
                return resident
            except (CancelledError, InterruptedError):
                raise CancelledError() from None
            except Exception as reason:
                backend.failed(reason, family)

    def guarded(abandoned):
        try:
            return load(abandoned)
        except GpuExecutionError as error:
            if error.code == "source_blocks":
                source_input.reject()
            raise

    lease = backend._stages.acquire(key, guarded, cancelled, context.priority, retain=False, optional=True)
    try:
        return _root(backend, lease.value, spec, metadata, context, family, key)
    finally:
        lease.close()


def cached(backend, metadata, context, cancelled):
    key, cache_key = _keys(backend, metadata, context)
    with backend.borrow(cancelled, priority=context.priority):
        if backend.snapshot().identity != context.backend_identity:
            raise GpuExecutionError("device_changed", "Cached source device changed")
        lease = backend._stages.cached(key, cancelled)
        try:
            resident = lease.value if lease is not None else backend._cache.get(cache_key)
            if resident is None:
                return None
            if not resident.carrier.ready or resident.backend_identity != context.backend_identity:
                raise GpuExecutionError("device_changed", "Cached source is unavailable")
            backend._cache.recent(cache_key)
            family = backend.family(metadata.source_path, context.workflow_identity, display_target.get())
            return _root(backend, resident, metadata.input_spec, metadata, context, family, key)
        finally:
            if lease is not None:
                lease.close()


def _root(backend, resident, spec, metadata, context, family, key):
    width, height = spec.size
    plan = StripPlan(backend, resident, spec, _matrix(spec), replace(metadata, input_spec=None, execution_ownership=None),
                     family, context.backend_identity)
    return replace(metadata, pixels=DevicePixels((height, width, 3)),
        alpha=None if metadata.alpha is None else DevicePixels((height, width)), gpu_image=plan.artifact(),
        backend_identity=context.backend_identity, result_identity=key, raw_decode_meta=resident.raw_meta,
        execution_ownership=None, input_spec=None, patches=(), patch_base_identity=())


@dataclass(frozen=True)
class _Step:
    kind: str
    size: tuple
    orig: tuple
    offset: tuple = (0, 0)
    halo: int = 0
    settings: object = None
    reference: object = None
    assets: object = None
    execution: object = None
    shape: tuple = ()
    tool_context: object = None
    rotation: tuple = ()
    effects: tuple = ()

    def rows(self, top, bottom):
        if self.kind == "denoise":
            return max(0, top - self.halo), min(self.size[1], bottom + self.halo)
        if self.kind == "rotate_crop":
            return 0, self.rotation[-1]
        return top + self.offset[1], bottom + self.offset[1]

    def run(self, backend, source, top, bottom, cancelled, source_top=None, coefficients=None):
        width, count = self.size[0], bottom - top
        if self.kind == "color":
            from .gpu_color import FilterInput, GpuColor
            token = current_execution.set(self.execution)
            try:
                return GpuColor(backend, self.assets, coefficients).execute(self.reference, source, self.settings, (),
                                                                            cancelled)
            except FilterInput:
                raise GpuExecutionError("strip_capability", "GPU strip filter coefficients are missing") from None
            finally:
                current_execution.reset(token)
        if self.kind == "denoise":
            token = current_execution.set(self.execution)
            try:
                result = backend.routes.execute(self.reference, source, self.settings, self.tool_context, cancelled)
            finally:
                current_execution.reset(token)
            offset = top - source_top
            return GpuGeometry(backend).view(result, (0, offset, width, offset + count),
                ("strip.denoise", self.reference, top, bottom), cancelled, formal_size=self.orig)
        geometry = GpuGeometry(backend)
        if self.kind in ("crop", "shape"):
            left = self.offset[0]
            source = geometry.view(source, (left, 0, left + width, count), ("strip.view", self.offset, self.size),
                                   cancelled, formal_size=self.orig)
            if self.kind == "crop":
                return source
            primitives = self.shape
            mask = backend.operate(source.gpu_image, "shape.mask", ("strip", primitives), (width, count),
                lambda device, abort: device.shape_mask(width, count, primitives, abort, rows=(top, self.size[1])), cancelled)
            return geometry.updated(source, geometry.op(source, "shape.alpha", ("strip", primitives), 4, [],
                                                        (width, count), cancelled, mask), alpha=True)
        regions, invert, radius = self.shape
        rows = min(self.size[1], count + 2 * self.halo)
        first = max(0, min(top - self.halo, self.size[1] - rows))
        last = first + rows
        mask = backend.operate(source.gpu_image, "lasso.mask", ("strip", self.shape, first, last), (width, last - first),
            lambda device, abort: device.lasso_mask(width, last - first, regions, invert, radius, abort,
                                                    rows=(first, self.size[1])), cancelled)
        if (first, last) != (top, bottom):
            whole = mask
            mask = backend.operate(whole, "view", ("strip.mask", top - first, count), (width, count),
                lambda device, abort: device.view(whole.resident.carrier, 0, top - first, width, count), cancelled, view=True)
        return geometry.updated(source, geometry.op(source, "lasso.alpha", ("strip", self.shape), 4, [],
                                                    (width, count), cancelled, mask), alpha=True)


@dataclass(frozen=True)
class _Coefficients:
    """一个引导滤波在整幅低分辨率网格上的系数 a、b，和居中用的 1×1 图。"""
    a: GpuResident
    b: GpuResident
    center: GpuResident

    @property
    def carrier(self):
        return self.a.carrier

    @property
    def carriers(self):
        return self.a.carrier, self.b.carrier, self.center.carrier


@dataclass(frozen=True)
class _BeautyPatches:
    key: tuple
    patches: tuple
    has_alpha: bool
    effect: object

    @property
    def carrier(self):
        return self.patches[0].artifact.resident.carrier

    @property
    def carriers(self):
        return tuple(patch.artifact.resident.carrier for patch in self.patches)


class _RotationRows:
    """旋转前那张图按 rows 行一块、这一帧里各块共用。合计超过 limit 字节时，先放掉离要用的行最远的块。"""

    def __init__(self, compute, height, rows, row_bytes, limit):
        self.compute, self.height, self.rows = compute, height, rows
        self.row_bytes, self.limit = row_bytes, limit
        self.chunks, self.computed = {}, 0

    @property
    def unused(self):
        """预留了还没占上的字节。"""
        return max(0, self.limit - len(self.chunks) * self.rows * self.row_bytes)

    def cover(self, top, bottom):
        """盖住 [top, bottom) 行的各块：[(块的起始行, 块), ...]。"""
        needed = range(top // self.rows, (bottom - 1) // self.rows + 1)
        result = []
        for index in needed:
            chunk = self.chunks.get(index)
            if chunk is None:
                spare = [item for item in self.chunks if item not in needed]
                while spare and (len(self.chunks) + 1) * self.rows * self.row_bytes > self.limit:
                    farthest = max(spare, key=lambda item: min(abs(item - needed.start), abs(item - needed.stop)))
                    spare.remove(farthest)
                    del self.chunks[farthest]
                first = index * self.rows
                chunk = self.chunks[index] = self.compute(first, min(self.height, first + self.rows))
                self.computed += 1
            result.append((index * self.rows, chunk))
        return result


@dataclass(frozen=True, eq=False)
class StripPlan:
    backend: object
    resident: GpuResident
    spec: object
    matrix: tuple
    metadata: object
    family: tuple
    identity: tuple
    steps: tuple = ()

    @property
    def size(self):
        return self.steps[-1].size if self.steps else tuple(self.spec.size)

    @property
    def orig(self):
        return self.steps[-1].orig if self.steps else tuple(self.metadata.orig_size)

    @staticmethod
    def _step_key(step):
        prepared = step.tool_context.get("prepared_input") if step.kind == "beauty" and isinstance(step.tool_context, dict) else None
        face_key = None if prepared is None else (
            prepared.identity, prepared.reference_size, prepared.detection_size, prepared.status, len(prepared.faces))
        return (step.kind, step.size, step.orig, step.offset, step.halo, step.settings, step.reference,
                step.shape, step.rotation, face_key)

    def _plan_identity(self, count=None):
        steps = self.steps if count is None else self.steps[:count]
        return self.backend._stages.key("gpu.strips", (
            self.metadata.source_path, self.resident.identity, self.identity,
            tuple(self._step_key(step) for step in steps)))

    def artifact(self):
        return GpuArtifact(GpuResident(None, self.identity, None, self._plan_identity()),
                           self, self.family, self.metadata.source_path)

    def extend(self, source, reference, settings, assets, cancelled, tool_context=None):
        """登记一个节点，到显示或读回时才逐条计算。"""
        kind = reference.tool_id
        width, height = self.size
        orig = self.orig
        if kind == "color_adjust":
            step = _Step("color", (width, height), orig, settings=settings, reference=reference, assets=assets,
                         execution=current_execution.get(), effects=source.effect_status)
        elif kind == "denoise":
            from .gpu_denoise import GpuDenoise
            version = int(reference.implementation_version) if reference.implementation_id == "denoise.basic" else 2
            halo = GpuDenoise.strip_halo(settings, version)
            step = _Step("denoise", (width, height), orig, halo=halo, settings=settings,
                         reference=reference, execution=current_execution.get(), tool_context=tool_context,
                         effects=source.effect_status)
        elif kind == "beauty":
            step = _Step("beauty", (width, height), orig, settings=settings, reference=reference,
                         execution=current_execution.get(), tool_context=tool_context, effects=source.effect_status)
        elif kind in CONTRACTS and CONTRACTS[kind].is_neutral(settings, 2):
            return source
        elif kind == "crop":
            angle = settings["rotation_angle"]
            if angle:
                rotated = rotated_size((width, height), angle)
                formal = rotated_size(orig, angle)
                left, top, right, bottom = crop_box(settings["crop_rect"], rotated)
                fl, ft, fr, fb = crop_box(settings["crop_rect"], formal)
                radians = -math.radians(angle % 360)
                a, b = round(math.cos(radians), 15), round(math.sin(radians), 15)
                d, e = -b, a
                c = width / 2 - a * rotated[0] / 2 - b * rotated[1] / 2
                f = height / 2 - d * rotated[0] / 2 - e * rotated[1] / 2
                rotation = a, b, d, e, c, f, *rotated, width, height
                step = _Step("rotate_crop", (right - left, bottom - top), (fr - fl, fb - ft),
                             (left, top), rotation=rotation)
            else:
                left, top, right, bottom = crop_box(settings["crop_rect"], (width, height))
                fl, ft, fr, fb = crop_box(settings["crop_rect"], orig)
                step = _Step("crop", (right - left, bottom - top), (fr - fl, fb - ft), (left, top))
        elif kind == "shape_crop":
            ratio = min(width / orig[0], height / orig[1])
            x, y, w, h = settings["rect"]
            normalized = x, y, min(1, x + w), min(1, y + h)
            left, top, right, bottom = crop_box(normalized, (width, height))
            fl, ft, fr, fb = crop_box(normalized, orig)
            size = right - left, bottom - top
            primitives = tuple(tuple(item) for item in
                               shape_primitives(size, settings["shape"], settings["corner_radius"] * ratio))
            step = _Step("shape", size, (fr - fl, fb - ft), (left, top), shape=primitives)
        elif kind == "lasso":
            regions = list(settings["regions"])
            if len(settings["draft_points"]) >= 3:
                regions.append({"points": settings["draft_points"], "op": "add"})
            normalized = []
            for region in regions:
                points = []
                for x, y in region["points"]:
                    point = max(0, min(width - 1, round(x * width))), max(0, min(height - 1, round(y * height)))
                    if not points or points[-1] != point:
                        points.append(point)
                normalized.append((region["op"] == "subtract", tuple(points)))
            radius = settings["feather"] * min(width / orig[0], height / orig[1])
            # 羽化横竖各做三遍盒式模糊，每遍最多伸出半径加一行
            halo = 3 * (math.ceil(radius) + 2) if radius > 0 else 0
            step = _Step("lasso", (width, height), orig, halo=halo,
                         shape=(tuple(normalized), not settings["keep_inside"], radius))
        else:
            raise GpuExecutionError("strip_capability", "GPU strips do not support this tool", self.family)
        plan = replace(self, steps=self.steps + (step,))
        out_width, out_height = step.size
        alpha = source.alpha is not None or step.kind in ("shape", "lasso", "rotate_crop")
        return replace(source, pixels=DevicePixels((out_height, out_width, 3)),
            alpha=DevicePixels((out_height, out_width)) if alpha else None, orig_size=step.orig,
            gpu_image=plan.artifact(), patches=(), patch_base_identity=(), execution_ownership=None,
            backend_identity=self.identity)

    def _prefix_source(self, count, effects=()):
        prefix = replace(self, steps=self.steps[:count])
        width, height = prefix.size
        alpha = self.metadata.alpha is not None or any(
            step.kind in ("shape", "lasso", "rotate_crop") for step in prefix.steps)
        metadata = replace(self.metadata, pixels=DevicePixels((height, width, 3)),
            alpha=DevicePixels((height, width)) if alpha else None, orig_size=prefix.orig,
            effect_status=tuple(effects), gpu_image=None, patches=(), patch_base_identity=(), execution_ownership=None,
            backend_identity=self.identity)
        return replace(metadata, gpu_image=prefix.artifact())

    def rows(self, frame, reserved=0):
        """一条最多多少输出行：一份浮点缓冲放得进一块，一条的工作集放得进剩下的预算。"""
        widest = max([self.spec.size[0], *(step.size[0] for step in self.steps)])
        halo = sum(step.halo for step in self.steps)
        info = frame.device.info()
        held = sum(item.unused for item in frame.scratch.values() if isinstance(item, _RotationRows))
        free = self.backend._budget(info) - (info["allocated_bytes"] - info["idle_bytes"]) - reserved - held
        buffer_bytes = min(BLOCK_BYTES, info["max_binding_size"], info["max_buffer_size"])
        rows = min(buffer_bytes // (widest * 16) - 2 * halo, free // (widest * STRIP_PIXEL_BYTES))
        if rows < MINIMUM_ROWS:
            raise GpuExecutionError("target_budget", "GPU strip does not fit the device budget", self.family)
        return rows

    def _prepare(self, frame, cancelled):
        """各步调色要的引导滤波系数先算好、旋转共用的行块先留出显存，再按剩下的显存定每条多少行。"""
        for index, step in enumerate(self.steps):
            if step.kind == "color":
                self._coefficients(frame, index, step, cancelled)
            elif step.kind == "rotate_crop":
                self._window(frame, index, step, cancelled)

    def _window(self, frame, index, step, cancelled):
        """第 index 步旋转裁剪共用的旋转前行块。够用时留下一行输出块跨过的全部源图行，各块只算一次；
        显存不够时至少留两块，离得远的块用到时重算。"""
        slot = "strip.rotation", id(step)
        window = frame.scratch.get(slot)
        if window is None:
            a, b = step.rotation[:2]
            source_width, source_height = step.rotation[-2:]
            rows = max(MINIMUM_ROWS, min(ROTATION_ROWS, self.rows(frame, reserved=8 * 1024 * 1024)))
            row_bytes = source_width * 16
            info = frame.device.info()
            held = sum(item.unused for item in frame.scratch.values() if isinstance(item, _RotationRows))
            free = self.backend._budget(info) - (info["allocated_bytes"] - info["idle_bytes"]) - held
            span = ROTATION_TILE * abs(a) + step.size[0] * abs(b) + 6
            needed = (math.ceil(span / rows) + 2) * rows * row_bytes
            limit = min(needed, max(2 * rows * row_bytes, (free - rows * source_width * STRIP_PIXEL_BYTES) // 2))
            window = frame.scratch[slot] = _RotationRows(
                lambda first, last: self._run_steps(frame, index, first, last, cancelled), source_height, rows,
                row_bytes, limit)
        return window

    def _coefficients(self, frame, index, step, cancelled):
        """第 index 步调色要的引导滤波系数，按 (半径, eps, 降采样倍数) 取。同一帧只算一次；
        滤波的输入与上一帧相同时沿用上一帧的。"""
        from .gpu_color import filter_input_settings, strip_filters
        slot = "strip.coefficients", id(step)
        result = frame.scratch.get(slot)
        if result is not None:
            return result
        tone, details = strip_filters(step.settings, step.size)
        result = {}
        for stage, specs in (("tone", (tone,) if tone is not None else ()), ("detail", details)):
            if not specs:
                continue
            key = self._coefficient_key(index, step, filter_input_settings(step.settings, stage))
            with self.backend._hold(frame):
                found = {spec: entry for spec in specs
                         for entry in (self.backend._cache.recent(self._coefficient_slot(key, spec)),)
                         if entry is not None}
            missing = tuple(spec for spec in specs if spec not in found)
            if found:
                logger.debug("GPU 分条滤波系数沿用 stage=%s filters=%s", stage, sorted(found))
            if missing:
                found.update(self._prepare_filters(frame, index, step, result, key, missing, cancelled))
            result.update(found)
        frame.scratch[slot] = result
        return result

    def _coefficient_key(self, index, step, settings):
        prefix = tuple(self._step_key(item) for item in self.steps[:index])
        return self.backend._stages.key("gpu.strip.coefficients", (self.metadata.source_path, self.resident.identity,
            self.identity, prefix, step.reference, settings, step.size))[-1]

    def _coefficient_slot(self, key, spec):
        return self.metadata.source_path, "strip.coefficients", key, spec

    @staticmethod
    def _low_rows(height, low_height):
        """低分辨率网格每一行双线性取样时上面那一行落在整幅第几行；与着色器一样按 32 位浮点算。"""
        rows = np.arange(low_height, dtype=np.float32)
        return np.floor((rows + np.float32(.5)) * np.float32(height) / np.float32(low_height) - np.float32(.5))

    def _prepare_filters(self, frame, index, step, known, key, specs, cancelled):
        """逐条算到这几个引导滤波的输入，缩到各自的低分辨率网格上拼成整幅，整幅算一次系数。
        每条多算上一行、下两行，让每个低分辨率行的取样都落在这一条里。"""
        from .gpu_color import FilterInput, GpuColor
        backend = self.backend
        width, height = step.size
        grids = {}
        for spec in specs:
            subsample = spec[2]
            if subsample < 2:
                raise GpuExecutionError("target_budget", "GPU strip filter needs a reduced grid", self.family)
            low = max(1, width // subsample), max(1, height // subsample)
            grids[spec] = low, self._low_rows(height, low[1])
        info = frame.device.info()
        sizes = [low_width * low_height * 16 for (low_width, low_height), _ in grids.values()]
        if max(sizes) > min(info["max_binding_size"], info["max_buffer_size"]):
            raise GpuExecutionError("target_budget", "GPU strip filter grid exceeds a buffer", self.family)
        # 各条缩小的部分和拼好的整幅、留下的系数各约两份，算系数时临时约 16 份最大的那张
        rows = self.rows(frame, 4 * sum(sizes) + 16 * max(sizes)) - 3
        if rows < MINIMUM_ROWS:
            raise GpuExecutionError("target_budget", "GPU strip filter does not fit the device budget", self.family)
        started = monotonic()
        parts = {spec: [] for spec in specs}
        chunks = self.chunks(0, height, rows)
        for top, bottom in chunks:
            if cancelled():
                raise CancelledError()
            first, last = max(0, top - 1), min(height, bottom + 2)
            source = self._run_steps(frame, index, first, last, cancelled)
            token = current_execution.set(step.execution)
            try:
                with backend.strip_rows(first, step.size):
                    GpuColor(backend, step.assets, known).execute(step.reference, source, step.settings, (), cancelled)
            except FilterInput as reached:
                image = reached.source.gpu_image
            else:
                raise GpuExecutionError("strip_capability", "GPU strip filter input was not reached", self.family)
            finally:
                current_execution.reset(token)
            for spec in specs:
                (low_width, low_height), lows = grids[spec]
                start = int(np.searchsorted(lows, top))
                stop = low_height if bottom == height else int(np.searchsorted(lows, bottom))
                if stop <= start:
                    continue
                args = [0, first, width, height, 0, start, low_width, low_height]
                part = backend.operate(image, "strip.color.low", (spec, start, stop), (low_width, stop - start),
                    lambda device, abort, image=image, args=args, count=stop - start: device.image_op(
                        image.resident.carrier, image.resident.carrier, 7, args, low_width, count, abort), cancelled)
                parts[spec].append((part, start))
            source = image = None
        result = {}
        for spec in specs:
            (low_width, low_height), _ = grids[spec]
            pieces = parts[spec]
            low = backend.operate(pieces[0][0], "strip.color.low.full",
                (key, spec, tuple(part.resident.identity for part, _ in pieces)), (low_width, low_height),
                lambda device, abort, pieces=pieces: device.assemble([part.resident.carrier for part, _ in pieces],
                    [(0, start) for _, start in pieces], low_width, low_height, abort), cancelled, input_identity=())
            radius, eps, subsample = spec
            with backend._hold(frame):
                carriers = backend._run(frame, lambda: frame.device.guided_coefficients(
                    low.resident.carrier, radius, eps, subsample, cancelled))
            slot = self._coefficient_slot(key, spec)
            held = _Coefficients(*(GpuResident(carrier, self.identity, None, (*slot, name))
                                   for carrier, name in zip(carriers, "abc")))
            frame.stable.append((slot, held))
            result[spec] = held
            low = None
        logger.debug("GPU 分条滤波系数 filters=%s strips=%s rows=%s duration_ms=%.1f",
            sorted(specs), len(chunks), rows, (monotonic() - started) * 1000)
        return result

    @contextmanager
    def _recycling(self, frame):
        """各条大小相同，这期间放宽空闲缓冲的上限，让下一条直接接着用上一条的缓冲。"""
        backend = self.backend
        depth = frame.scratch.get("strip.recycling", 0)
        frame.scratch["strip.recycling"] = depth + 1
        submission = 500 if backend._status.tier == "B" else 0
        if not depth:
            frame.device.configure(frame.budget, frame.budget, submission)
        try:
            yield
        finally:
            if depth:
                frame.scratch["strip.recycling"] = depth
            else:
                frame.scratch.pop("strip.recycling", None)
                frame.device.configure(frame.budget, backend._idle_limit(frame.budget), submission)

    def _source(self, frame, top, bottom, cancelled):
        backend, spec = self.backend, self.spec
        width, height = spec.size
        count = bottom - top
        image = backend._run(frame, lambda: frame.device.source_rows(self.resident.carrier, width, height, spec.channels,
            spec.depth, spec.layout == "png-filtered", spec.byte_order == "big", transfer_lut(spec.depth), top, count,
            list(self.matrix) or None, cancelled))
        resident = GpuResident(image, self.identity, None, ("gpu.frame", next(backend._serial)))
        metadata = self.metadata
        return replace(metadata, pixels=DevicePixels((count, width, 3)),
            alpha=None if metadata.alpha is None else DevicePixels((count, width)),
            gpu_image=GpuArtifact(resident, backend, self.family, metadata.source_path),
            backend_identity=self.identity, patches=(), patch_base_identity=(), execution_ownership=None)

    def strip(self, frame, top, bottom, cancelled):
        """算出输出的 [top, bottom) 行。"""
        return self._run_steps(frame, len(self.steps), top, bottom, cancelled).gpu_image

    def _run_steps(self, frame, count, top, bottom, cancelled):
        if count == 0:
            return self._source(frame, top, bottom, cancelled)
        step = self.steps[count - 1]
        if step.kind == "rotate_crop":
            return self._rotate(frame, count - 1, step, top, bottom, cancelled)
        if step.kind == "beauty":
            return self._beauty(frame, count - 1, step, top, bottom, cancelled)
        coefficients = self._coefficients(frame, count - 1, step, cancelled) if step.kind == "color" else None
        source_top, source_bottom = step.rows(top, bottom)
        source = self._run_steps(frame, count - 1, source_top, source_bottom, cancelled)
        if cancelled():
            raise CancelledError()
        with self.backend.strip_rows(source_top, step.size):
            return step.run(self.backend, source, top, bottom, cancelled, source_top, coefficients)

    def _beauty(self, frame, prefix_count, step, top, bottom, cancelled):
        slot = "strip.beauty", id(step)
        result = frame.scratch.get(slot)
        if result is None:
            prefix_source = self._prefix_source(prefix_count, step.effects)
            node_id = step.tool_context.get("node_id", "") if isinstance(step.tool_context, dict) else ""
            cache_slot = (self.metadata.source_path, "strip.beauty", node_id, prefix_count, step.size)
            cache_key = self.backend._stages.key("gpu.strip.beauty", (
                self.identity, prefix_source.gpu_image.resident.identity, self._step_key(step)))
            with self.backend._hold(frame):
                cached = self.backend._cache.recent(cache_slot, cache_key)
            if cached is not None:
                artifact = replace(prefix_source.gpu_image, patches=cached.patches)
                result = replace(prefix_source,
                    alpha=DevicePixels((step.size[1], step.size[0])) if cached.has_alpha else None,
                    gpu_image=artifact,
                    effect_status=(*prefix_source.effect_status, cached.effect) if cached.effect is not None else prefix_source.effect_status)
            else:
                token = current_execution.set(step.execution)
                try:
                    result = self.backend.routes.execute(step.reference, prefix_source, step.settings, step.tool_context, cancelled)
                finally:
                    current_execution.reset(token)
                patches = result.gpu_image.patches
                if patches:
                    patch_bytes = sum(patch.artifact.resident.carrier.nbytes for patch in patches)
                    if patch_bytes <= self.backend.settings.cache_bytes(frame.budget) // 2:
                        effect = result.effect_status[-1] if len(result.effect_status) > len(prefix_source.effect_status) else None
                        frame.stable.append((cache_slot, _BeautyPatches(cache_key, patches, result.alpha is not None, effect)))
            frame.scratch[slot] = result
        width = step.size[0]
        region = self.backend.resolve(result.gpu_image, cancelled, (0, top, width, bottom))
        return replace(result, pixels=DevicePixels((bottom - top, width, 3)),
            alpha=DevicePixels((bottom - top, width)) if result.alpha is not None else None,
            gpu_image=region, patches=(), patch_base_identity=(), execution_ownership=None,
            backend_identity=self.identity)

    def _rotate(self, frame, prefix_count, step, top, bottom, cancelled):
        """旋转裁剪的 [top, bottom) 行：输出按块从旋转前的图取样。定点矩阵按整数平移到每块，与整幅逐位相同。"""
        a, b, d, e, c, f, rotated_width, rotated_height, source_width, source_height = step.rotation
        fixed_a, fixed_b, fixed_c, fixed_f = (round(value * (1 << 40)) for value in (a, b, c, f))
        output_width = step.size[0]
        window = self._window(frame, prefix_count, step, cancelled)
        computed = window.computed
        tiles, positions = [], []
        target_meta = None
        for output_top in range(top, bottom, ROTATION_TILE):
            output_bottom = min(bottom, output_top + ROTATION_TILE)
            for output_left in range(0, output_width, ROTATION_TILE):
                if cancelled():
                    raise CancelledError()
                output_right = min(output_width, output_left + ROTATION_TILE)
                tile_width, tile_height = output_right - output_left, output_bottom - output_top
                global_left, global_top = output_left + step.offset[0], output_top + step.offset[1]
                corners = [(a * (x + .5) + b * (y + .5) + c,
                            d * (x + .5) + e * (y + .5) + f)
                           for x in (global_left, global_left + tile_width - 1)
                           for y in (global_top, global_top + tile_height - 1)]
                source_left = max(0, math.floor(min(point[0] for point in corners)) - 2)
                source_top = max(0, math.floor(min(point[1] for point in corners)) - 2)
                source_right = min(source_width, math.ceil(max(point[0] for point in corners)) + 3)
                source_bottom = min(source_height, math.ceil(max(point[1] for point in corners)) + 3)
                if source_left >= source_right or source_top >= source_bottom:
                    continue
                parts, source_positions = [], []
                for chunk_top, chunk in window.cover(source_top, source_bottom):
                    first = max(source_top, chunk_top)
                    last = min(source_bottom, chunk_top + chunk.pixels.shape[0])
                    tile = GpuGeometry(self.backend).view(chunk,
                        (source_left, first - chunk_top, source_right, last - chunk_top),
                        ("strip.rotation.source", step.rotation, chunk_top, source_left, first, source_right, last),
                        cancelled)
                    parts.append(tile.gpu_image)
                    source_positions.append((0, first - source_top))
                    target_meta = chunk
                identity = (step.rotation, step.offset, source_left, source_top, source_right, source_bottom,
                            tuple(part.resident.identity for part in parts))
                assembled = self.backend.operate(parts[0], "strip.rotation.assemble", identity,
                    (source_right - source_left, source_bottom - source_top),
                    lambda device, abort: device.assemble([part.resident.carrier for part in parts], source_positions,
                        source_right - source_left, source_bottom - source_top, abort), cancelled, input_identity=())
                # 整幅那张的定点矩阵，按整数平移到这一块的输出起点和源图起点
                shifted = (fixed_a, fixed_b,
                           fixed_c + fixed_a * global_left + fixed_b * global_top - (source_left << 40),
                           fixed_f - fixed_b * global_left + fixed_a * global_top - (source_top << 40))
                matrix = [float((value >> shift) & 65535) for value in shifted for shift in (0, 16, 32, 48)]
                with self.backend.strip_rows(output_top, step.size):
                    rotated = self.backend.operate(assembled, "strip.rotation.tile", (identity, output_left, output_top),
                        (tile_width, tile_height), lambda device, abort: device.image_op(
                            assembled.resident.carrier, assembled.resident.carrier, 1, matrix,
                            tile_width, tile_height, abort), cancelled)
                    tiles.append(rotated)
                    positions.append((output_left, output_top - top))
                parts = assembled = None
        if not tiles:
            with self.backend._hold(frame):
                carrier = self.backend._run(frame, lambda: frame.device.empty(output_width, bottom - top, cancelled))
            target = GpuArtifact(GpuResident(carrier, self.identity, None, ("gpu.frame", next(self.backend._serial))),
                                 self.backend, self.family, self.metadata.source_path)
            target_meta = self.metadata
        else:
            logger.debug("GPU 分条旋转 rows=%s-%s source_chunks=%s chunk_rows=%s held=%s",
                         top, bottom, window.computed - computed, window.rows, len(window.chunks))
            identity = (step.rotation, step.offset, top, bottom,
                        tuple(tile.resident.identity for tile in tiles), tuple(positions))
            target = self.backend.operate(tiles[0], "strip.rotation.output", identity,
                (output_width, bottom - top), lambda device, abort: device.assemble(
                    [tile.resident.carrier for tile in tiles], positions, output_width, bottom - top, abort),
                cancelled, input_identity=())
        return replace(target_meta, pixels=DevicePixels((bottom - top, output_width, 3)),
            alpha=DevicePixels((bottom - top, output_width)), orig_size=step.orig, gpu_image=target,
            backend_identity=self.identity, patches=(), patch_base_identity=(), execution_ownership=None)

    @staticmethod
    def chunks(first, last, rows):
        count = -(-(last - first) // rows)
        step = -(-(last - first) // count)
        return [(top, min(last, top + step)) for top in range(first, last, step)]

    def spans(self, chunks, size):
        """每条要算的输出行，都补成一样高，让各条的缓冲大小相同、能接着用上一条的。"""
        needed = [self.needed(size, first, last) for first, last in chunks]
        rows = max(hi - lo for lo, hi in needed)
        height = self.size[1]
        return [(first, last, start, start + rows) for (first, last), (lo, hi) in zip(chunks, needed)
                for start in (max(0, min(lo, height - rows)),)]

    def needed(self, size, first, last):
        """显示帧的 [first, last) 行按面积缩小时要读的输出行，两头各多留一行。"""
        width, height = self.size
        if tuple(size) == (width, height):
            return first, last
        scale = np.float32(height) / np.float32(size[1])
        lo = np.float32(first) * scale
        hi = min(np.float32(last) * scale, np.float32(height))
        return max(0, int(math.floor(lo)) - 1), min(height, int(math.ceil(hi)) + 1)

    def display(self, artifact, size, cancelled, region=None):
        backend = self.backend
        context = current_execution.get()
        priority = context.priority if context is not None else backend.compute.scheduling()[0]
        key = backend._stages.key("gpu.readback", (artifact.backend_identity, artifact.resident.identity, size, region))
        lease = backend._stages.acquire(key, lambda abandoned: self._display(size, abandoned, region),
                                        cancelled, priority, retain=False, optional=True)
        try:
            return lease.value
        finally:
            lease.close()

    def _display(self, size, cancelled, region):
        backend = self.backend
        width, height = size
        left, top, right, bottom = region or (0, 0, width, height)
        started = monotonic()
        with backend.frame(self.identity, cancelled, self.family) as frame, backend._hold(frame):
            try:
                self._prepare(frame, cancelled)
                count = (right - left) * (bottom - top) * 4
                rows = self.rows(frame, count * 2)
                same = tuple(size) == self.size
                chunks = self.chunks(top, bottom, rows if same else max(1, int((rows - 4) * height / self.size[1])))
                with self._recycling(frame):
                    target = backend._native_call(lambda: frame.device.display_buffer(right - left, bottom - top))
                    for first, last, lo, hi in self.spans(chunks, size):
                        if cancelled():
                            raise CancelledError()
                        image = self.strip(frame, lo, hi, cancelled)
                        backend._native_call(lambda: frame.device.display_into(target, image.resident.carrier, width,
                            height, (left, top, right, bottom), (first, last), lo, self.size, cancelled))
                        image = None
                with backend.host_memory(count if backend._status.tier == "B" else 0, cancelled):
                    rgba = backend._native_call(lambda: frame.device.read_display(target, cancelled))
                rgba.setflags(write=False)
                logger.debug("GPU 分条显示完成 size=%s region=%s strips=%s rows=%s duration_ms=%.1f",
                    size, region, len(chunks), rows, (monotonic() - started) * 1000)
                return rgba
            except (CancelledError, GpuExecutionError):
                raise
            except InterruptedError:
                raise CancelledError() from None
            except Exception as reason:
                backend.failed(reason, self.family)
            finally:
                backend._finish_readback()

    def _read(self, bounds, cancelled, channels=4, alpha=False):
        left, top, right, bottom = bounds
        width, height = self.size
        if left < 0 or top < 0 or right <= left or bottom <= top or right > width or bottom > height:
            raise ValueError("GPU 读取范围无效")
        backend = self.backend
        context = current_execution.get()
        if context is not None and context.engine == "gpu":
            raise GpuExecutionError("host_boundary", "GPU chain requested host pixels", self.family)
        started = monotonic()
        result = np.empty((bottom - top, right - left, channels), np.float32)
        coverage = np.empty((bottom - top, right - left), np.float32) if alpha else None
        with backend.frame(self.identity, cancelled, self.family) as frame, backend._hold(frame):
            try:
                self._prepare(frame, cancelled)
                rows = self.rows(frame, (right - left) * min(bottom - top, BLOCK_BYTES // (width * 16)) * 16)
                chunks = self.chunks(top, bottom, rows)
                with self._recycling(frame):
                    for first, last, lo, hi in self.spans(chunks, self.size):
                        if cancelled():
                            raise CancelledError()
                        image = self.strip(frame, lo, hi, cancelled)
                        backend._native_call(lambda: frame.device.read_region_into(image.resident.carrier, left, first - lo,
                            right - left, last - first, result[first - top:last - top],
                            None if coverage is None else coverage[first - top:last - top], cancelled))
                        image = None
            except (CancelledError, GpuExecutionError):
                raise
            except InterruptedError:
                raise CancelledError() from None
            except Exception as reason:
                backend.failed(reason, self.family)
            finally:
                backend._finish_readback()
        logger.debug("GPU 分条读回完成 bounds=%s strips=%s rows=%s duration_ms=%.1f",
            bounds, len(chunks), rows, (monotonic() - started) * 1000)
        result.setflags(write=False)
        if coverage is not None:
            coverage.setflags(write=False)
        return result, coverage

    def read_region(self, artifact, bounds, cancelled):
        left, top, right, bottom = bounds
        with self.backend.host_memory((right - left) * (bottom - top) * 16, cancelled):
            return self._read(bounds, cancelled)[0]

    def gpu_region(self, artifact, bounds, cancelled):
        left, top, right, bottom = bounds
        width, height = self.size
        if left < 0 or top < 0 or right <= left or bottom <= top or right > width or bottom > height:
            raise ValueError("GPU 区域无效")
        parts, positions = [], []
        with self.backend.frame(self.identity, cancelled, self.family) as frame, self.backend._hold(frame):
            self._prepare(frame, cancelled)
            rows = self.rows(frame, reserved=(right - left) * min(bottom - top, 256) * 16)
            with self._recycling(frame):
                for first, last in self.chunks(top, bottom, rows):
                    source = self._run_steps(frame, len(self.steps), first, last, cancelled)
                    part = GpuGeometry(self.backend).view(source, (left, 0, right, last - first),
                        ("strip.region", artifact.resident.identity, bounds, first, last), cancelled)
                    parts.append(part.gpu_image)
                    positions.append((0, first - top))
            identity = (artifact.resident.identity, bounds, tuple(part.resident.identity for part in parts))
            return self.backend.operate(parts[0], "strip.region.assemble", identity, (right - left, bottom - top),
                lambda device, abort: device.assemble([part.resident.carrier for part in parts], positions,
                    right - left, bottom - top, abort), cancelled, input_identity=())

    def resolve_region(self, artifact, bounds, cancelled):
        width, height = self.size
        bounds = bounds or (0, 0, width, height)
        left, top, right, bottom = bounds
        base = self.gpu_region(artifact, bounds, cancelled)
        for patch in artifact.patches:
            pl, pt, pr, pb = patch.bounds
            x0, y0, x1, y1 = max(left, pl), max(top, pt), min(right, pr), min(bottom, pb)
            if x0 >= x1 or y0 >= y1:
                continue
            patch_source = self.backend.operate(patch.artifact, "strip.patch.view",
                (patch.identity, x0 - pl, y0 - pt, x1 - pl, y1 - pt), (x1 - x0, y1 - y0),
                lambda device, abort: device.view(patch.artifact.resident.carrier, x0 - pl, y0 - pt,
                    x1 - x0, y1 - y0), cancelled, view=True)
            previous = base
            base = self.backend.operate(previous, "strip.patch.compose",
                (artifact.resident.identity, patch.identity, bounds, x0, y0, x1, y1), (right - left, bottom - top),
                lambda device, abort: device.image_op(previous.resident.carrier, patch_source.resident.carrier, 5,
                    [x0 - left, y0 - top, float(not patch.has_alpha)], right - left, bottom - top, abort), cancelled)
        return base

    def materialize(self, artifact, source, cancelled):
        from ..core.execution_memo import ResourceOwner
        width, height = self.size
        hold = self.backend.resources.hold_memory(width * height * 16, cancelled)
        try:
            pixels, alpha = self._read((0, 0, width, height), cancelled, 3, source.alpha is not None)
            result = replace(source, pixels=pixels, alpha=alpha,
                gpu_image=None, backend_identity=self.identity, execution_ownership=ResourceOwner((hold,)))
            hold = None
            return result
        finally:
            if hold is not None:
                hold.close()

    def face_input(self, artifact, bounds, size, cancelled):
        import numpy as np
        from .gpu_faces import FaceGeometry
        left, top, right, bottom = bounds
        width, height = self.size
        if (left < 0 or top < 0 or right <= left or bottom <= top or right > width or bottom > height
                or size[0] <= 0 or size[1] <= 0):
            raise GpuExecutionError("face_input", "Face analysis bounds are invalid", self.family)
        output = np.empty((size[1], size[0], 3), np.uint8)
        scale_y = (bottom - top) / size[1]
        with self.backend.frame(self.identity, cancelled, self.family) as frame, self.backend._hold(frame):
            self._prepare(frame, cancelled)
            rows = self.rows(frame, reserved=8 * 1024 * 1024)
            output_rows = max(1, min(128, max(1, rows - 2) * size[1] // (bottom - top)))
            with self._recycling(frame):
                for first_output in range(0, size[1], output_rows):
                    last_output = min(size[1], first_output + output_rows)
                    source_first = max(top, math.floor(top + first_output * scale_y))
                    source_last = min(bottom, math.ceil(top + last_output * scale_y))
                    source = self._run_steps(frame, len(self.steps), source_first, source_last, cancelled)
                    local_bounds = (left, top + first_output * scale_y - source_first,
                                    right, top + last_output * scale_y - source_first)
                    image = FaceGeometry(self.backend, ("face.detection_input", bounds, size, first_output)).op(
                        source, 0, (size[0], last_output - first_output), cancelled, args=local_bounds)
                    values = self.backend.face_metadata(image, cancelled, final="region")
                    output[first_output:last_output] = np.rint(values[..., :3] * 255).astype(np.uint8)
        return output

    def resize(self, source, size, box, cancelled):
        raise GpuExecutionError("strip_capability", "GPU strips do not resize", self.family)
