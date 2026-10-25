"""Checks that the running interpreter can produce the full release build."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys

REQUIRED_MODULES = ("PyQt6", "rawpy", "httpx", "cv2", "onnxruntime", "rembg", "mediapipe", "PyInstaller")


def problems():
    found = []
    if sys.version_info < (3, 12):
        found.append(f"Python 3.12 or newer is required, found {sys.version.split()[0]}")
    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        found.append("Missing packages: " + ", ".join(missing))
    # opencv-* distributions share the cv2 package; two of them overwrite each other's files
    opencv = sorted({(dist.metadata["Name"] or "").lower() for dist in importlib.metadata.distributions()}
                    & {"opencv-python", "opencv-python-headless", "opencv-contrib-python",
                       "opencv-contrib-python-headless"})
    if len(opencv) != 1:
        found.append("Exactly one OpenCV distribution is required, found: " + (", ".join(opencv) or "none"))
    return found


if __name__ == "__main__":
    issues = problems()
    for issue in issues:
        print(issue, file=sys.stderr)
    sys.exit(1 if issues else 0)
