from __future__ import annotations

import logging
from dataclasses import dataclass

from .memory_settings import MIB, PREVIEW_CACHE_BYTES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StudioPreloadSettings:
    """预加载范围，以及编辑预览、编辑代理和缩略图各自的缓存容量。"""

    mode: str = "nearby"
    before: int = 1
    after: int = 3
    memory_mib: int = PREVIEW_CACHE_BYTES // MIB
    disk_mib: int = 2048
    thumbnail_mib: int = 512
    canvas_preview_mode: str = "auto"

    CANVAS_MODES = ("auto", "full", "preview1600")

    RANGES = {"before": (0, 10000), "after": (0, 10000),
              "memory_mib": (16, 1024), "disk_mib": (128, 32768), "thumbnail_mib": (16, 32768)}

    def __post_init__(self):
        if self.canvas_preview_mode not in self.CANVAS_MODES:
            raise ValueError("Invalid canvas preview mode")
        if self.mode not in ("off", "nearby", "all"):
            raise ValueError("Invalid OPS preload mode")
        for name, (low, high) in self.RANGES.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid OPS preload setting: {name}")

    @classmethod
    def load(cls, settings):
        defaults = cls()
        canvas_mode = settings.value("canvas_preview_mode", defaults.canvas_preview_mode)
        if canvas_mode not in cls.CANVAS_MODES:
            canvas_mode = defaults.canvas_preview_mode
        try:
            return cls(mode=str(settings.value("mode", defaults.mode)), canvas_preview_mode=canvas_mode, **{
                name: int(settings.value(name, getattr(defaults, name))) for name in cls.RANGES
            })
        except (TypeError, ValueError):
            logger.warning("Invalid OPS preload preferences; using defaults")
            return defaults

    def save(self, settings):
        for name in ("mode", "canvas_preview_mode", *self.RANGES):
            settings.setValue(name, getattr(self, name))
        settings.sync()

    def targets(self, paths, current):
        if self.mode == "off" or current not in paths:
            return []
        index = paths.index(current)
        if self.mode == "all":
            return paths[index + 1:] + list(reversed(paths[:index]))
        return (paths[index + 1:index + 1 + self.after]
                + list(reversed(paths[max(0, index - self.before):index])))
