"""整幅放不下显存时，按行分条在 GPU 上计算。

工作流执行时各节点只登记成计划里的一步；到显示或读回时，按输出行逐条往回推出每一步要的行，
从常驻的整数源转出这几行，逐步算完，缩小写进显示帧的对应行，或读回拼成整幅。
坐标和边界都按整幅算，结果与整幅计算逐位相同。"""

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
from ..core.tools.geometry import crop_box
from .gpu_geometry import CONTRACTS, GpuGeometry, shape_primitives, _tool

logger = logging.getLogger(__name__)

# 分配器按 256 MiB 一块申请显存，更大的缓冲单独申请，用不上块里的空位
BLOCK_BYTES = 256 * 1024 ** 2
# 一条算到一半时占的显存，连分配器块里的空位，按这一条每个像素计
STRIP_PIXEL_BYTES = 256
MINIMUM_ROWS = 16


def capable(source, graph, order=None, spec=None):
    """工作流里的节点都能逐条计算时为 True。"""
    if source.is_proxy or isinstance(spec, ImageOutputSpec) and spec.resize is not None and (
            spec.resize.width is not None or spec.resize.height is not None):
        return False
    width, height = source.pixels.shape[1], source.pixels.shape[0]
    if max(width, height) > 32768:
        return False
    from .gpu_color import strip_capable, workflow_cost
    try:
        for node in (graph.node(identity) for identity in order) if order is not None else graph.nodes:
            item = _tool(node)
            if item is None:
                continue
            kind, params = item
            if kind == "color_adjust":
                if not strip_capable(params):
                    return False
                workflow_cost(WorkflowGraph(nodes=(node,), edges=()), BLOCK_BYTES // 16)
            elif kind == "crop":
                if params["rotation_angle"]:
                    return False
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

    def rows(self, top, bottom):
        return top + self.offset[1], bottom + self.offset[1]

    def run(self, backend, source, top, bottom, cancelled):
        width, count = self.size[0], bottom - top
        if self.kind == "color":
            from .gpu_color import GpuColor
            token = current_execution.set(self.execution)
            try:
                return GpuColor(backend, self.assets).execute(self.reference, source, self.settings, (), cancelled)
            finally:
                current_execution.reset(token)
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

    def artifact(self):
        return GpuArtifact(GpuResident(None, self.identity, None, ("gpu.strips", next(self.backend._serial))),
                           self, self.family, self.metadata.source_path)

    def extend(self, source, reference, settings, assets, cancelled):
        """登记一个节点，到显示或读回时才逐条计算。"""
        kind = reference.tool_id
        width, height = self.size
        orig = self.orig
        if kind == "color_adjust":
            step = _Step("color", (width, height), orig, settings=settings, reference=reference, assets=assets,
                         execution=current_execution.get())
        elif kind in CONTRACTS and CONTRACTS[kind].is_neutral(settings, 2):
            return source
        elif kind == "crop" and not settings["rotation_angle"]:
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
        alpha = source.alpha is not None or step.kind in ("shape", "lasso")
        return replace(source, pixels=DevicePixels((out_height, out_width, 3)),
            alpha=DevicePixels((out_height, out_width)) if alpha else None, orig_size=step.orig,
            gpu_image=plan.artifact(), patches=(), patch_base_identity=(), execution_ownership=None,
            backend_identity=self.identity)

    def rows(self, frame, reserved=0):
        """一条最多多少输出行：一份浮点缓冲放得进一块，一条的工作集放得进剩下的预算。"""
        widest = max([self.spec.size[0], *(step.size[0] for step in self.steps)])
        halo = max([0, *(step.halo for step in self.steps)])
        info = frame.device.info()
        free = self.backend._budget(info) - (info["allocated_bytes"] - info["idle_bytes"]) - reserved
        rows = min(BLOCK_BYTES // (widest * 16) - 2 * halo, free // (widest * STRIP_PIXEL_BYTES))
        if rows < MINIMUM_ROWS:
            raise GpuExecutionError("target_budget", "GPU strip does not fit the device budget", self.family)
        return rows

    @contextmanager
    def _recycling(self, frame):
        """各条大小相同，这期间放宽空闲缓冲的上限，让下一条直接接着用上一条的缓冲。"""
        backend = self.backend
        submission = 500 if backend._status.tier == "B" else 0
        frame.device.configure(frame.budget, frame.budget, submission)
        try:
            yield
        finally:
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
        spans = [(top, bottom)]
        for step in reversed(self.steps):
            spans.append(step.rows(*spans[-1]))
        spans.reverse()
        source = self._source(frame, *spans[0], cancelled)
        for step, (first, last) in zip(self.steps, spans[1:]):
            if cancelled():
                raise CancelledError()
            with self.backend.strip_rows(first, step.size):
                source = step.run(self.backend, source, first, last, cancelled)
        return source.gpu_image

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
        raise GpuExecutionError("strip_capability", "GPU strips do not provide face input", self.family)

    def resize(self, source, size, box, cancelled):
        raise GpuExecutionError("strip_capability", "GPU strips do not resize", self.family)
