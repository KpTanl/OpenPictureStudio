from __future__ import annotations

from concurrent.futures import CancelledError
from pathlib import Path

from PIL import Image


class ProjectAssetError(ValueError):
    def __init__(self, path):
        super().__init__("error.mask.asset")
        self.path = str(path)


def validate_asset(path, digest, size, cancelled=lambda: False):
    from .project_store import hash_file
    try:
        if hash_file(Path(path), cancelled) != digest:
            raise ProjectAssetError(path)
        with Image.open(path) as image:
            if image.format != "PNG" or image.mode not in ("I;16", "I;16B", "I") or list(image.size) != list(size):
                raise ProjectAssetError(path)
            image.verify()
    except (OSError, ValueError) as error:
        if isinstance(error, ProjectAssetError):
            raise
        raise ProjectAssetError(path) from error
    if cancelled():
        raise CancelledError()
