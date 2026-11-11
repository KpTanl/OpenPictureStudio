from enum import Enum
from dataclasses import dataclass


DISPLAY_RESAMPLING = "area-rgba8-premultiplied-v1"


@dataclass(frozen=True)
class PreviewDisplaySpec:
    max_dimension: int | None = None
    resampling: str = DISPLAY_RESAMPLING

    def __post_init__(self):
        if (self.max_dimension is not None and (type(self.max_dimension) is not int or self.max_dimension < 1)
                or self.resampling != DISPLAY_RESAMPLING):
            raise ValueError("显示规格无效")

    def size_for(self, width, height):
        if self.max_dimension is None or max(width, height) <= self.max_dimension:
            return width, height
        scale = self.max_dimension / max(width, height)
        return max(1, round(width * scale)), max(1, round(height * scale))


class RawDisplayMode(str, Enum):
    CAMERA = "camera"
    ORIGINAL = "original"
    OPS = "ops"

    @classmethod
    def _missing_(cls, value):
        return cls.OPS if value == "opv" else None


DEFAULT_RAW_DISPLAY_MODE = RawDisplayMode.OPS


class NonRawDisplayMode(str, Enum):
    ORIGINAL = "original"
    OPS = "ops"

    @classmethod
    def _missing_(cls, value):
        return cls.OPS if value == "opv" else None


DEFAULT_NON_RAW_DISPLAY_MODE = NonRawDisplayMode.ORIGINAL


class CanvasBackground(str, Enum):
    """画布底色：跟随主题，或与主题无关的四档固定底色。"""

    THEME = "theme"
    DARK = "dark"
    MID = "mid"
    LIGHT = "light"
    BEIGE = "beige"


DEFAULT_CANVAS_BACKGROUND = CanvasBackground.THEME
