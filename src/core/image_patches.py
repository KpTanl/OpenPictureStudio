from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from concurrent.futures import CancelledError

import numpy as np


display_stages = ContextVar("ops_display_stages", default=None)


@dataclass(frozen=True)
class ImagePatch:
    bounds: tuple[int, int, int, int]
    pixels: np.ndarray
    alpha: np.ndarray | None
    reference_size: tuple[int, int]

    def __post_init__(self):
        self.pixels.setflags(write=False)
        if self.alpha is not None:
            self.alpha.setflags(write=False)


def read_region(source, bounds, cancelled=lambda: False):
    left, top, right, bottom = bounds
    if source.gpu_image is not None:
        rgba = source.gpu_image.read_region(bounds, cancelled)
        pixels, alpha = rgba[..., :3], None if source.alpha is None else rgba[..., 3]
    else:
        pixels = source.pixels[top:bottom, left:right]
        alpha = None if source.alpha is None else source.alpha[top:bottom, left:right]
    copied = False
    for patch in source.patches:
        if cancelled():
            raise CancelledError()
        pl, pt, pr, pb = patch.bounds
        x0, y0, x1, y1 = max(left, pl), max(top, pt), min(right, pr), min(bottom, pb)
        if x1 <= x0 or y1 <= y0:
            continue
        if not copied:
            pixels = pixels.copy()
            alpha = None if alpha is None else alpha.copy()
            copied = True
        for start in range(y0, y1, 64):
            if cancelled():
                raise CancelledError()
            stop = min(start + 64, y1)
            pixels[start - top:stop - top, x0 - left:x1 - left] = patch.pixels[
                start - pt:stop - pt, x0 - pl:x1 - pl]
            if alpha is not None and patch.alpha is not None:
                alpha[start - top:stop - top, x0 - left:x1 - left] = patch.alpha[
                    start - pt:stop - pt, x0 - pl:x1 - pl]
    return pixels, alpha


def materialize(source, cancelled=lambda: False):
    if cancelled():
        raise CancelledError()
    if source.gpu_image is not None:
        source = source.gpu_image.materialize(source, cancelled)
    if not source.patches:
        return source
    height, width = source.pixels.shape[:2]
    pixels, alpha = read_region(source, (0, 0, width, height), cancelled)
    pixels.setflags(write=False)
    if alpha is not None:
        alpha.setflags(write=False)
    return replace(source, pixels=pixels, alpha=alpha, patches=(), patch_base_identity=())


def native_patches(source):
    return [(patch.bounds[0], patch.bounds[1], np.ascontiguousarray(patch.pixels),
             None if patch.alpha is None else np.ascontiguousarray(patch.alpha)) for patch in source.patches]


@contextmanager
def display_stage_scope(stages):
    token = display_stages.set(stages)
    try:
        yield
    finally:
        display_stages.reset(token)
