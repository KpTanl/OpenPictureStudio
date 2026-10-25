from enum import Enum


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
