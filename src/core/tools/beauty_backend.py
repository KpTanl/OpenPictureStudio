from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from .. import beauty_enhancer as engine
from ..contracts.result import CoreError, CoreOperationError, ErrorCode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BeautyBackend:
    name: str
    version: str
    resources: tuple[tuple[str, str, str], ...]

    def create(self):
        for _, digest, path in self.resources:
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
                raise CoreOperationError(CoreError(ErrorCode.TOOL_RESOURCE_MISSING,
                    "error.tool.resource_missing", {"tool_id": "beauty"}))
        paths = {key: path for key, _, path in self.resources}
        return engine.BeautyEnhancer(backend=self.name, model_path=paths.get("face_model"))


def backend_version():
    import PIL
    return f"3-cv{engine.cv2.__version__}-np{engine.np.__version__}-pil{PIL.__version__}"


def available_backends():
    if engine.cv2 is None or engine.np is None:
        return ()
    version = backend_version()
    candidates = []
    try:
        import mediapipe as mp
        from mediapipe.python.solutions import face_mesh
    except Exception as error:
        logger.debug("MediaPipe Face Mesh 不可用：%s", error)
    else:
        root = Path(mp.__file__).parent
        models = sorted(path for path in (root / "modules").rglob("*")
                        if path.suffix in (".tflite", ".binarypb") and "face" in str(path.relative_to(root)))
        if models:
            candidates.append(("mediapipe_legacy", version + f"-mp{mp.__version__}",
                               [(str(path.relative_to(root)).replace("\\", "/"), path) for path in models]))
    try:
        import mediapipe as mp
        from mediapipe.tasks.python import vision
    except Exception as error:
        logger.debug("MediaPipe Tasks 不可用：%s", error)
    else:
        model = engine.face_landmarker_model_path()
        try:
            model_available = model.is_file()
        except OSError as error:
            logger.debug("MediaPipe Tasks 模型不可访问：%s", error)
            model_available = False
        if model_available:
            candidates.append(("mediapipe_tasks", version + f"-mp{mp.__version__}", [("face_model", model)]))
    haar = Path(engine.cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if haar.is_file():
        candidates.append(("haar", version, [("face_model", haar)]))
    result = []
    for name, implementation_version, paths in candidates:
        try:
            resources = tuple(sorted((key, hashlib.sha256(path.read_bytes()).hexdigest(), str(path))
                                     for key, path in paths))
        except OSError as error:
            logger.warning("美颜检测资源读取失败：%s", error)
            continue
        result.append(BeautyBackend(name, implementation_version, resources))
    return tuple(result)


def preferred_backend():
    backends = available_backends()
    if not backends:
        raise CoreOperationError(CoreError(ErrorCode.TOOL_RESOURCE_MISSING,
            "beauty.backend.unavailable", {"tool_id": "beauty"}))
    return backends[0]
