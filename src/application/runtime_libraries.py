from importlib import metadata, util
import sys

from ..core.contracts.runtime_libraries import RuntimeLibrarySnapshot


_DISTRIBUTIONS = {
    "onnxruntime": ("onnxruntime", "onnxruntime-directml", "onnxruntime-gpu"),
    "mediapipe": ("mediapipe",),
    "cv2": ("opencv-contrib-python",),
}


def environment_library(name):
    source = "bundled" if getattr(sys, "frozen", False) else "environment"
    if not name:
        return RuntimeLibrarySnapshot("", "available", "bundled")
    module = sys.modules.get(name)
    try:
        available = module is not None or util.find_spec(name) is not None
    except (ImportError, ValueError):
        available = False
    if not available:
        return RuntimeLibrarySnapshot(name, "missing", source, error="models.runtime.missing")
    version = str(getattr(module, "__version__", "")) if module is not None else ""
    if not version:
        for distribution in _DISTRIBUTIONS.get(name, (name,)):
            try:
                version = metadata.version(distribution)
                break
            except metadata.PackageNotFoundError:
                continue
    return RuntimeLibrarySnapshot(name, "available", source, version=version)
