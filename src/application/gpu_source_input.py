from dataclasses import replace
from functools import lru_cache
from concurrent.futures import CancelledError
from contextlib import ExitStack
import numpy as np
import logging

from ..core.contracts.gpu import DevicePixels, GpuArtifact, GpuResident, GpuExecutionError, display_target
from ..core.photo_engine import RenderSource, srgb_to_linear, display_rgba8

logger = logging.getLogger(__name__)


@lru_cache(maxsize=2)
def transfer_lut(depth):
    values = np.arange(1 << depth, dtype=np.float32) / np.float32((1 << depth) - 1)
    table = srgb_to_linear(values)
    table.setflags(write=False)
    return table


def source_peak(spec, size):
    width, height = spec.size
    out_width, out_height = size
    input_bytes = (spec.nbytes + 3) // 4 * 4
    horizontal = out_width * height * 16
    output = out_width * out_height * 16
    return max(input_bytes * (2 if spec.transfer == "xyz-d65" else 3), input_bytes + horizontal + (1 << spec.depth) * 8,
               horizontal + output * 2) + 65536


def prepare(backend, source_input, metadata, context, cancelled):
    encoded = source_input.value
    spec = encoded.spec
    size = metadata.pixels.shape[1::-1]
    key = backend._stages.key("gpu.source_input", (metadata.content_identity, spec, size,
        metadata.orig_size, metadata.color_pipeline, context.backend_identity))
    cache_key = metadata.source_path, "source_input", key[-1], context.backend_identity
    family = backend.family(metadata.source_path, context.workflow_identity, display_target.get())
    def prepare_input(abandoned):
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
            hold = None
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
                if source_peak(spec, size) > budget:
                    raise GpuExecutionError("target_budget", "Integer source exceeds the device budget")
                table = transfer_lut(spec.depth)
                if backend._status.tier == "B":
                    hold = backend.resources.try_hold_memory(size[0] * size[1] * 16)
                host_bytes = incoming.data.nbytes + table.nbytes
                if backend._status.tier == "B":
                    host_bytes += source_peak(spec, size)
                with backend.host_memory(host_bytes, abandoned):
                    before = device.info()
                    options = {"blocks": incoming.blocks} if incoming.blocks is not None else {}
                    image = backend._native_call(lambda: device.source_image(incoming.data, *spec.size,
                        spec.channels, spec.depth, spec.layout == "png-filtered", spec.byte_order == "big",
                        table, *size, list(incoming.matrix) or None, abandoned, **options))
                    after = device.info()
                    logger.debug("GPU 源准备 source=%s layout=%s depth=%s channels=%s input=%s output=%s upload_bytes=%s readback_bytes=%s",
                        metadata.source_path, spec.layout, spec.depth, spec.channels, spec.size, size,
                        after["uploaded_bytes"] - before["uploaded_bytes"], after["readback_bytes"] - before["readback_bytes"])
                resident = GpuResident(image, context.backend_identity, hold, key, encoded.raw_meta)
                hold = None
                if retain:
                    backend._cache[cache_key] = resident
                    backend._trim(budget)
                return resident
            except (CancelledError, InterruptedError):
                raise CancelledError() from None
            except Exception as reason:
                backend.failed(reason, family)
            finally:
                if hold is not None:
                    hold.close()

    def load(abandoned):
        try:
            return prepare_input(abandoned)
        except GpuExecutionError as error:
            if error.code == "source_blocks":
                source_input.reject()
            raise

    lease = backend._stages.acquire(key, load, cancelled, context.priority, retain=False, optional=True)
    try:
        width, height = size
        artifact = GpuArtifact(lease.value, backend, family, metadata.source_path)
        return replace(metadata, pixels=DevicePixels((height, width, 3)),
            alpha=None if metadata.alpha is None else DevicePixels((height, width)), gpu_image=artifact,
            backend_identity=context.backend_identity, result_identity=key, raw_decode_meta=encoded.raw_meta,
            execution_ownership=None, input_spec=None)
    finally:
        lease.close()


def cached_source(backend, metadata, context, cancelled):
    key = backend._stages.key("gpu.source_input", (metadata.content_identity, metadata.input_spec,
        metadata.pixels.shape[1::-1], metadata.orig_size, metadata.color_pipeline, context.backend_identity))
    cache_key = metadata.source_path, "source_input", key[-1], context.backend_identity
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
            logger.debug("GPU 源驻留命中 source=%s size=%s", metadata.source_path, metadata.pixels.shape[1::-1])
            return replace(metadata, pixels=DevicePixels(metadata.pixels.shape),
                alpha=None if metadata.alpha is None else DevicePixels(metadata.alpha.shape),
                gpu_image=GpuArtifact(resident, backend, family, metadata.source_path), backend_identity=context.backend_identity,
                result_identity=key, raw_decode_meta=resident.raw_meta, execution_ownership=None, input_spec=None)
        finally:
            if lease is not None:
                lease.close()


def qualify_blocks(device, compute, cancelled):
    import ops_accel
    if not hasattr(device, "source_blocks_probe") or not hasattr(ops_accel, "source_compress_blocks"):
        return False, "source_block_api"
    with compute.borrow(cancelled, priority=2, threads=1):
        data = np.arange(262147, dtype=np.uint32).astype(np.uint8)
        data[4096:16384] = 0
        data[65536:131072] = np.arange(65536, dtype=np.uint32).astype(np.uint8) % 3
        rng = np.random.default_rng(20261105)
        data[131072:196608] = rng.integers(0, 256, 65536, dtype=np.uint8)
        packed, index = ops_accel.source_compress_blocks(data, 65536, threads=1, cancelled=cancelled)
    actual = device.source_blocks_probe(packed, index, data.nbytes, cancelled)
    if not np.array_equal(actual, data):
        return False, "source_block_bytes"
    return True, ""


def _scanlines(values):
    height, width, channels = values.shape
    rows = np.ascontiguousarray(values).view(np.uint8).reshape(height, -1).astype(np.int32)
    step = channels * values.dtype.itemsize
    encoded = np.empty((height, rows.shape[1] + 1), np.uint8)
    for y, row in enumerate(rows):
        mode = y % 5
        left = np.zeros_like(row)
        left[step:] = row[:-step]
        up = rows[y - 1] if y else np.zeros_like(row)
        corner = np.zeros_like(row)
        corner[step:] = up[:-step]
        da, db, dc = np.abs(up - corner), np.abs(left - corner), np.abs(left + up - 2 * corner)
        paeth = np.where((da <= db) & (da <= dc), left, np.where(db <= dc, up, corner))
        predictor = (0, left, up, (left + up) // 2, paeth)[mode]
        encoded[y, 0], encoded[y, 1:] = mode, (row - predictor) & 255
    return encoded.reshape(-1)


def qualify_source(device, compute, cancelled):
    if not hasattr(device, "source_image"):
        return frozenset(), (("source_api", "unavailable"),)
    from ..core.tools.effects import resize_arrays
    from ..core.dcp import xyz_d65_to_srgb_matrix
    ready, reasons = set(), []
    width, height = 129, 35
    y, x = np.mgrid[:height, :width]
    cases = [(layout, channels, depth, "big" if layout == "png-filtered" else "little", "srgb")
             for layout in ("pixels", "png-filtered") for channels in (1, 2, 3, 4) for depth in (8, 16)]
    cases.append(("pixels", 3, 16, "little", "xyz-d65"))
    for identity in cases:
        if cancelled():
            raise CancelledError()
        layout, channels, depth, byte_order, transfer = identity
        try:
            with compute.borrow(cancelled, priority=2, threads=1):
                maximum = (1 << depth) - 1
                values = np.stack([((x * (31 + c) + y * (71 + c * 13)) * 257 + c * 17) % (maximum + 1)
                                   for c in range(channels)], axis=2)
                if channels in (2, 4):
                    values[..., -1] = (x + y) % 5 * (maximum // 4)
                    values[..., -1][(x + y) % 5 == 4] = maximum
                values = values.astype((">" if byte_order == "big" else "<") + ("u1" if depth == 8 else "u2"))
                floats = values.astype(np.float32) / np.float32(maximum)
                matrix = tuple(float(v) for row in xyz_d65_to_srgb_matrix() for v in row) if transfer == "xyz-d65" else ()
                rgb = floats[..., :3] if channels >= 3 else np.repeat(floats[..., :1], 3, axis=2)
                rgb = np.asarray(rgb.astype(np.float64) @ np.asarray(matrix).reshape(3, 3).T, np.float32) if matrix else srgb_to_linear(rgb)
                alpha = floats[..., -1] if channels in (2, 4) else None
                source = RenderSource(rgb, alpha, "srgb16" if depth == 16 else "srgb8", "qualification", False,
                    (width, height), "srgb-direct")
                data = _scanlines(values) if layout == "png-filtered" else values.view(np.uint8).reshape(-1)
                table = transfer_lut(depth)
            passed = True
            for size in ((width, height), (43, 11), (width, 11)):
                image = device.source_image(data, width, height, channels, depth, layout == "png-filtered",
                    byte_order == "big", table, *size, list(matrix) or None, cancelled)
                actual = device.read_region(image, 0, 0, *size, cancelled)
                with compute.borrow(cancelled, priority=2, threads=1):
                    if size == (width, height):
                        expected = source
                    else:
                        pixels, coverage = resize_arrays(source.pixels, source.alpha, size, parallelism=1)
                        expected = replace(source, pixels=pixels, alpha=coverage, orig_size=size)
                    if not np.isfinite(actual).all() or np.max(np.abs(actual[..., :3] - expected.pixels)) > 2e-5:
                        passed = False
                    if expected.alpha is not None and np.max(np.abs(actual[..., 3] - expected.alpha)) > 2e-5:
                        passed = False
                    reference = display_rgba8(expected)
                displayed = device.display(image, *size, cancelled)
                if np.max(np.abs(displayed.astype(np.int16) - reference.astype(np.int16))) > 1:
                    passed = False
                del image
            if passed:
                ready.add(identity)
            else:
                reasons.append((identity, "source_numeric"))
        except (CancelledError, InterruptedError):
            raise CancelledError() from None
        except Exception as error:
            if device.info()["lost"]:
                raise
            reasons.append((identity, str(error)))
        device.trim()
    return frozenset(ready), tuple(reasons)
