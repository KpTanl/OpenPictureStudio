import hashlib
from pathlib import Path

from ..core.contracts.models import ModelDescriptor, ModelFile, ModelKey, ModelSnapshot


def bundled_models():
    import cv2
    path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if path.is_file():
        content = path.read_bytes()
        file = ModelFile(path.name, len(content), hashlib.sha256(content).hexdigest(), "")
        descriptor = ModelDescriptor(ModelKey("opencv-haar", "cv" + cv2.__version__),
            "OpenCV Haar", "face_detection", "bundled", (file,), "OpenCV " + cv2.__version__, "BSD-3-Clause", runtime="")
        yield ModelSnapshot(descriptor, "bundled", installed_bytes=file.disk_size, paths=((file.name, str(path)),))
