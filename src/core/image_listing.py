from concurrent.futures import CancelledError
import os

from .constants import SUPPORTED_EXTENSIONS


def check_cancelled(is_cancelled):
    if is_cancelled():
        raise CancelledError()


def directory_images(directory, *, is_cancelled=lambda: False):
    extensions = frozenset(SUPPORTED_EXTENSIONS)
    paths = []
    check_cancelled(is_cancelled)
    with os.scandir(directory) as entries:
        for entry in entries:
            check_cancelled(is_cancelled)
            if os.path.splitext(entry.name)[1].lower() in extensions and entry.is_file():
                paths.append(os.path.normpath(entry.path))
    check_cancelled(is_cancelled)
    paths.sort()
    check_cancelled(is_cancelled)
    return paths


def existing_images(paths, *, is_cancelled=lambda: False):
    valid = []
    for path in paths:
        check_cancelled(is_cancelled)
        path = os.path.normpath(path)
        if os.path.isfile(path):
            valid.append(path)
    check_cancelled(is_cancelled)
    return list(dict.fromkeys(valid))
