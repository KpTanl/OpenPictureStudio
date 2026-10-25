from dataclasses import dataclass
import logging

from ..core.contracts.image_display import (DEFAULT_CANVAS_BACKGROUND, DEFAULT_NON_RAW_DISPLAY_MODE,
                                            DEFAULT_RAW_DISPLAY_MODE, CanvasBackground, NonRawDisplayMode,
                                            RawDisplayMode)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageDisplaySettings:
    raw_mode: RawDisplayMode = DEFAULT_RAW_DISPLAY_MODE
    canvas_background: CanvasBackground = DEFAULT_CANVAS_BACKGROUND
    non_raw_mode: NonRawDisplayMode = DEFAULT_NON_RAW_DISPLAY_MODE

    @classmethod
    def load(cls, settings):
        defaults = cls()
        try:
            raw_mode = RawDisplayMode(settings.value("raw_mode", defaults.raw_mode.value))
        except (TypeError, ValueError):
            logger.warning("Invalid RAW display preference; using default")
            raw_mode = defaults.raw_mode
        try:
            non_raw_mode = NonRawDisplayMode(settings.value("non_raw_mode", defaults.non_raw_mode.value))
        except (TypeError, ValueError):
            logger.warning("Invalid non-RAW display preference; using default")
            non_raw_mode = defaults.non_raw_mode
        try:
            canvas_background = CanvasBackground(
                settings.value("canvas_background", defaults.canvas_background.value))
        except (TypeError, ValueError):
            logger.warning("Invalid canvas background preference; using default")
            canvas_background = defaults.canvas_background
        return cls(raw_mode=raw_mode, canvas_background=canvas_background, non_raw_mode=non_raw_mode)

    def save(self, settings):
        settings.setValue("raw_mode", self.raw_mode.value)
        settings.setValue("non_raw_mode", self.non_raw_mode.value)
        settings.setValue("canvas_background", self.canvas_background.value)
        settings.sync()
