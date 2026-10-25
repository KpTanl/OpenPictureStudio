from __future__ import annotations

import ctypes
import os
from functools import lru_cache
from pathlib import Path

import numpy as np


@lru_cache(maxsize=2)
def _library(path):
    return ctypes.CDLL(path)


def effective_raw_matrix(path, meta):
    import rawpy
    from .dcp import xyz_d65_to_srgb_matrix

    if (meta is None or meta.raw_pattern_shape != (2, 2) or meta.color_description != "RGBG"
            or meta.num_colors != 3 or not meta.effective_whitebalance
            or meta.postprocess_kwargs.get("half_size") or not meta.postprocess_kwargs.get("use_camera_wb")):
        raise ValueError("calibration.raw_unsupported")
    gains = np.asarray(meta.effective_whitebalance, dtype=np.float64)
    camera = np.asarray(meta.camera_whitebalance, dtype=np.float64)
    if (gains.shape != (4,) or camera.shape != (4,) or not np.isfinite(gains).all()
            or not np.isfinite(camera).all() or np.any(gains <= 0) or np.any(camera[:3] <= 0)
            or abs(gains[3] / gains[1] - 1) > 1e-4
            or not np.allclose(gains[:3] / gains[1], camera[:3] / camera[1], rtol=1e-4, atol=1e-6)):
        raise ValueError("calibration.wb_fallback")
    directory = Path(rawpy.__file__).resolve().parent
    candidates = sorted(directory.glob("raw*.dll")) if os.name == "nt" else sorted(directory.glob("libraw*.dylib"))
    if os.name != "nt" and not candidates:
        candidates = sorted(directory.glob("libraw*.so*"))
    if len(candidates) != 1:
        raise ValueError("calibration.matrix_unavailable")
    try:
        library = _library(str(candidates[0]))
        version = library.libraw_versionNumber
        version.argtypes, version.restype = [], ctypes.c_int
        number = version()
        if ((number >> 16, (number >> 8) & 255, number & 255) != tuple(rawpy.libraw_version)
                or tuple(rawpy.libraw_version[:2]) not in ((0, 21), (0, 22))):
            raise ValueError("calibration.matrix_unavailable")
        init, close = library.libraw_init, library.libraw_close
        init.argtypes, init.restype = [ctypes.c_uint], ctypes.c_void_p
        close.argtypes, close.restype = [ctypes.c_void_p], None
        opened = library.libraw_open_wfile if os.name == "nt" else library.libraw_open_file
        opened.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p if os.name == "nt" else ctypes.c_char_p]
        opened.restype = ctypes.c_int
        wb, matrix = library.libraw_get_cam_mul, library.libraw_get_rgb_cam
        wb.argtypes, wb.restype = [ctypes.c_void_p, ctypes.c_int], ctypes.c_float
        matrix.argtypes, matrix.restype = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_float
        handle = init(0)
        if not handle:
            raise MemoryError()
        try:
            if opened(handle, str(path) if os.name == "nt" else os.fsencode(path)) != 0:
                raise ValueError("calibration.matrix_unavailable")
            if not np.allclose([wb(handle, i) for i in range(4)], camera, rtol=1e-5, atol=1e-6):
                raise ValueError("calibration.matrix_unavailable")
            rgb_cam = np.array([[matrix(handle, i, j) for j in range(4)] for i in range(3)], dtype=np.float64)
        finally:
            close(handle)
    except (AttributeError, OSError) as error:
        raise ValueError("calibration.matrix_unavailable") from error
    if not np.isfinite(rgb_cam).all() or np.any(np.abs(rgb_cam[:, 3]) > 1e-7) or abs(np.linalg.det(rgb_cam[:, :3])) < 1e-8:
        raise ValueError("calibration.matrix_unavailable")
    xyz_rgb = np.array([[.4124564, .3575761, .1804375], [.2126729, .7151522, .0721750], [.0193339, .1191920, .9503041]])
    return xyz_d65_to_srgb_matrix().astype(np.float64) @ xyz_rgb @ rgb_cam[:, :3], gains[:3]
