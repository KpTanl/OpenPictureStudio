from dataclasses import dataclass

from .constants import IMAGE_QUALITY_RANGE


@dataclass(frozen=True)
class OutputFormat:
    extension: str
    label: str
    media_type: str
    supports_quality: bool = False
    supports_target_size: bool = False
    supports_alpha: bool = False
    bit_depth: int = 8


OUTPUT_FORMATS = (
    OutputFormat("jpg", "JPG", "image/jpeg", supports_quality=True, supports_target_size=True),
    OutputFormat("png", "PNG", "image/png", supports_alpha=True),
    OutputFormat("webp", "WebP", "image/webp", supports_quality=True, supports_alpha=True),
    OutputFormat("bmp", "BMP", "image/bmp"),
    OutputFormat("tiff", "TIFF", "image/tiff", supports_alpha=True, bit_depth=16),
)


def output_format(extension: str) -> OutputFormat:
    extension = extension.lower().lstrip(".")
    extension = {"jpeg": "jpg", "tif": "tiff"}.get(extension, extension)
    for item in OUTPUT_FORMATS:
        if item.extension == extension:
            return item
    raise ValueError("error.export.format")


def validate_quality(quality: int) -> int:
    if type(quality) is not int or not IMAGE_QUALITY_RANGE[0] <= quality <= IMAGE_QUALITY_RANGE[1]:
        raise ValueError("error.param.range")
    return quality
