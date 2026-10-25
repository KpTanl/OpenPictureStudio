from __future__ import annotations

import sys
from pathlib import Path

from .user_paths import application_root


def get_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _frozen_model_root() -> Path:
    return application_root("APPDATA") / "models"


def get_project_model_root(*, create=True) -> Path:
    """Unified model directory shared by segmentation / denoise / AI-enhance.

    Source checkout -> <repo>/models (devs expect it there).
    Packaged build  -> %APPDATA%/OpenPictureStudio/OpenPictureStudio/models.
    """
    if getattr(sys, "frozen", False):
        model_root = _frozen_model_root()
    else:
        model_root = get_project_root() / "models"
    from ..application.storage_paths import storage_path
    model_root = storage_path("models", model_root)
    if create:
        model_root.mkdir(parents=True, exist_ok=True)
    return model_root


def get_runtime_root() -> Path:
    """用户自行安装的推理运行环境目录；启动维护与激活共用，不依赖 Qt 应用名。"""
    if getattr(sys, "frozen", False):
        root = application_root("APPDATA") / "runtimes"
    else:
        root = get_project_root() / "runtimes"
    from ..application.storage_paths import storage_path
    return storage_path("runtimes", root)


def get_legacy_model_roots():
    return tuple(dict.fromkeys((get_project_model_root(create=False), _frozen_model_root())))
