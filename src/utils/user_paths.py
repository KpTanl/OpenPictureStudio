from __future__ import annotations

import os
from pathlib import Path

from .app_info import APP_NAME


def home_root():
    return Path.home() / ".openpicturestudio"


def application_root(variable):
    base = os.environ.get(variable)
    if not base:
        return home_root()
    base = Path(base)
    return base / APP_NAME / APP_NAME


def environment_value(name, default=None):
    return os.environ.get(name, default)
