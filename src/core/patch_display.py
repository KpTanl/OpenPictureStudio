from concurrent.futures import CancelledError
from dataclasses import replace

import numpy as np

from .image_patches import display_stages, native_patches


def display_patches(source, spec, threads, cancelled):
    from . import photo_engine as engine
    base = replace(source, patches=(), patch_base_identity=())
    size = spec.size_for(source.pixels.shape[1], source.pixels.shape[0])
    stages = display_stages.get()
    lease = None
    try:
        if stages is not None and source.patch_base_identity:
            def storage_id(array):
                while isinstance(getattr(array, "base", None), np.ndarray):
                    array = array.base
                return id(array)
            key = stages.key("display.base", (source.patch_base_identity, storage_id(source.pixels), storage_id(source.alpha),
                                              source.pixels.shape, size, spec))
            def load(abandoned):
                return engine.display_rgba8(base, parallelism=threads, max_dimension=spec.max_dimension, cancelled=abandoned)
            lease = stages.acquire(key, load, cancelled, optional=True)
            rgba = lease.value
        else:
            rgba = engine.display_rgba8(base, parallelism=threads, max_dimension=spec.max_dimension, cancelled=cancelled)
        boxes = [patch.bounds for patch in source.patches]
        bounds = (min(box[0] for box in boxes), min(box[1] for box in boxes),
                  max(box[2] for box in boxes), max(box[3] for box in boxes))
        update = getattr(engine.ops_accel, "update_display_patches", None) if engine._HAS_RUST_ACCEL else None
        if update is not None and source.pixels.flags.c_contiguous and (source.alpha is None or source.alpha.flags.c_contiguous):
            try:
                result = update(source.pixels, source.alpha, native_patches(source), rgba, bounds,
                                threads=threads, cancelled=cancelled)
            except InterruptedError:
                raise CancelledError() from None
        else:
            result = _update_numpy(source, rgba, bounds, threads, cancelled)
        result.setflags(write=False)
        return result
    finally:
        if lease is not None:
            lease.close()


def _update_numpy(source, base, bounds, threads, cancelled):
    from .photo_engine import display_rgba8
    height, width = source.pixels.shape[:2]
    oh, ow = base.shape[:2]
    sx, sy = width / ow, height / oh
    left, top, right, bottom = bounds
    x0, y0 = max(0, int(np.floor(left / sx)) - 1), max(0, int(np.floor(top / sy)) - 1)
    x1, y1 = min(ow, int(np.ceil(right / sx)) + 1), min(oh, int(np.ceil(bottom / sy)) + 1)
    result = base.copy()
    for y in range(y0, y1):
        if cancelled():
            raise CancelledError()
        yt, yb = y * sy, min(height, (y + 1) * sy)
        xl, xr = int(np.floor(x0 * sx)), min(width, int(np.ceil(x1 * sx)))
        total = np.zeros((x1 - x0, 4), np.float64)
        edges = np.arange(x0, x1 + 1, dtype=np.float64) * sx - xl
        indices = np.minimum(edges.astype(np.intp), xr - xl - 1)
        fractions = edges - indices
        for start in range(int(np.floor(yt)), min(height, int(np.ceil(yb))), 64):
            if cancelled():
                raise CancelledError()
            stop = min(height, int(np.ceil(yb)), start + 64)
            pixels, alpha = source.read_region((xl, start, xr, stop), cancelled)
            region = replace(source, pixels=pixels, alpha=alpha, patches=(), patch_base_identity=())
            rows = display_rgba8(region, parallelism=threads, cancelled=cancelled)
            if (width, height) == (ow, oh):
                result[y, x0:x1] = rows[0]
                continue
            for index, row in enumerate(rows):
                row = row.astype(np.float64)
                row[:, :3] *= row[:, 3:4]
                integral = np.empty((xr - xl + 1, 4), np.float64)
                integral[0] = 0
                np.cumsum(row, axis=0, out=integral[1:])
                samples = integral[indices] + row[indices] * fractions[:, None]
                yy = start + index
                total += np.diff(samples, axis=0) * (min(yb, yy + 1) - max(yt, yy))
        if (width, height) != (ow, oh):
            alpha = total[:, 3:4]
            np.divide(total[:, :3], alpha, out=total[:, :3], where=alpha > 0)
            total[alpha[:, 0] == 0, :3] = 0
            total[:, 3] /= sx * (yb - yt)
            result[y, x0:x1] = np.rint(np.clip(total, 0, 255)).astype(np.uint8)
    if cancelled():
        raise CancelledError()
    return result
