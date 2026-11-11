from concurrent.futures import CancelledError
import numpy as np

from .execution_memo import current_memo, cancellation_callback, cancelled as execution_cancelled


def render_incremental(pixels, params, native, threads, cancelled):
    scope = current_memo.get()
    if scope is None or not scope.enabled or not hasattr(native, "render_color_prefix"):
        return None
    tx, cl, dz = native.color_detail_parameters(params)
    if not (tx or cl or dz):
        return None
    prefix_params = {name: value for name, value in params.items()
                     if name not in ("texture", "clarity", "dehaze", "sharpness", "masks")}
    prefix_key = scope.input_identity, prefix_params
    scope.retain = scope.store.hot((scope.namespace, scope.input_identity[0][0],
                                   scope.input_identity[1:], "color.prefix"), prefix_key)
    def check():
        if execution_cancelled(cancelled):
            raise CancelledError()
    check()
    prefix = scope.memo("color.prefix", prefix_key,
                        lambda: native.render_color_prefix(pixels, prefix_params, threads=threads))
    check()
    luma = scope.memo("color.luma", prefix_key, lambda: native.detail_luma(prefix, threads=threads))
    height, width = luma.shape
    small = min(width, height)
    def filtered(name, radius, eps, subsample):
        check()
        return scope.memo("color." + name, (prefix_key, radius, float(eps), subsample),
            lambda: native.detail_filter(luma, radius, float(eps), subsample, threads=threads))
    fine = mid = coarse = None
    if tx or cl:
        fine = filtered("fine", max(2, small // 256), np.float32(0.10) * np.float32(0.10),
                        int(np.clip(np.float32(small) / np.float32(512), 1, 4)))
    if cl:
        mid = filtered("mid", max(4, small // 24), np.float32(0.14) * np.float32(0.14),
                       int(np.clip(np.float32(small) / np.float32(200), 2, 6)))
    if dz:
        coarse = filtered("coarse", max(8, small // 8), np.float32(0.20) * np.float32(0.20),
                          int(np.clip(np.float32(small) / np.float32(128), 4, 12)))
    check()
    output = native.finish_color_details(prefix, luma, fine, mid, coarse, tx, cl, dz, threads=threads)
    check()
    return output


def render_local_incremental(pixels, params, masks, encoded, reference, native, threads, cancelled, rasters):
    scope = current_memo.get()
    if scope is None or not scope.enabled or not hasattr(native, "render_local_prefix"):
        return None
    from .local_masks import LOCAL_KEYS
    names = ("texture", "clarity", "dehaze")
    active = tuple(bool(params.get(name, 0) or any(mask.delta[LOCAL_KEYS.index(name)] for mask in masks)) for name in names)
    if not any(active):
        return None
    prefix_params = {name: value for name, value in params.items() if name not in (*names, "sharpness", "masks")}
    prefix_masks = tuple((mask.identity, mask.kind, mask.geometry, getattr(mask, "components", ()),
        tuple(value for name, value in zip(LOCAL_KEYS, mask.delta) if name not in names)) for mask in masks)
    prefix_key = scope.input_identity, prefix_params, prefix_masks, reference
    scope.retain = scope.store.hot((scope.namespace, scope.input_identity[0][0], scope.input_identity[1:],
                                   "color.local.prefix"), prefix_key)
    aborted = lambda: execution_cancelled(cancelled)
    def check():
        if aborted():
            raise CancelledError()
    check()
    prefix = scope.memo("color.local.prefix", prefix_key,
        lambda: native.render_local_prefix(pixels, params, encoded, reference, threads, cancellation_callback(cancelled), rasters))
    check()
    luma = scope.memo("color.local.luma", prefix_key, lambda: native.detail_luma(prefix, threads=threads))
    small = min(luma.shape)
    fine = mid = coarse = None
    def filtered(name, radius, eps, subsample):
        check()
        return scope.memo("color.local." + name, (prefix_key, radius, float(eps), subsample),
            lambda: native.local_detail_filter(luma, radius, float(eps), subsample, threads, cancellation_callback(cancelled)))
    if active[0] or active[1]:
        fine = filtered("fine", max(2, small // 256), np.float32(0.10) * np.float32(0.10),
                        int(np.clip(np.float32(small) / np.float32(512), 1, 4)))
    if active[1]:
        mid = filtered("mid", max(4, small // 24), np.float32(0.14) * np.float32(0.14),
                       int(np.clip(np.float32(small) / np.float32(200), 2, 6)))
    if active[2]:
        coarse = filtered("coarse", max(8, small // 8), np.float32(0.20) * np.float32(0.20),
                          int(np.clip(np.float32(small) / np.float32(128), 4, 12)))
    check()
    output = native.finish_local_details(prefix, params, encoded, reference, threads, cancellation_callback(cancelled), rasters,
                                         luma, fine, mid, coarse)
    check()
    return output
