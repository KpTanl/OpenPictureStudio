import logging
import os
from contextlib import nullcontext
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

from .constants import RAW_EXTENSIONS


logger = logging.getLogger(__name__)


try:
    import rawpy  # type: ignore
except ImportError:
    rawpy = None


class RawSupportUnavailableError(RuntimeError):
    """Raised when attempting to load RAW images without rawpy installed."""


def is_raw_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in RAW_EXTENSIONS


def load_image(path: str, *, for_preview: bool = False, convert_mode: str | None = None) -> Image.Image:
    """
    统一打开图像：普通格式走 Pillow，RAW 格式走 rawpy。

    返回值始终是与文件句柄解耦的 PIL.Image，便于后续编辑流水线复用。
    """
    normalized_path = os.path.normpath(path)

    if is_raw_file(normalized_path):
        image = _load_raw_image(normalized_path, for_preview=for_preview)
    else:
        with Image.open(normalized_path) as source:
            image = source.copy()
        image.filename = normalized_path

    if convert_mode and image.mode != convert_mode:
        image = image.convert(convert_mode)

    return image


def _load_raw_image(path: str, *, for_preview: bool = False) -> Image.Image:
    if rawpy is None:
        raise RawSupportUnavailableError(
            "RAW support is unavailable because rawpy is not installed. "
            "Install dependencies and try again."
        )

    try:
        with rawpy.imread(path) as raw:
            rgb = raw.postprocess(
                output_bps=8,
                use_camera_wb=True,
                no_auto_bright=False,
                half_size=for_preview,
            )
    except Exception:
        logger.exception("Failed to decode RAW image: %s", path)
        raise

    image = Image.fromarray(rgb, mode="RGB")
    image.filename = path
    return image


PREVIEW_ASPECT_TOLERANCE = 0.02
QUICK_PREVIEW_MAX_SIDE = 2560
_EXIF_ORIENTATION = 0x0112
_FLIP_ORIENTATION = {0: 1, 3: 3, 5: 8, 6: 6}
_ORIENTATION_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


@dataclass(frozen=True)
class EmbeddedPreview:
    """RAW 内嵌 JPEG 预览；图像已转正，尺寸均为转正后的宽高。"""
    image: Image.Image
    reference_size: tuple[int, int]
    preview_size: tuple[int, int]


@dataclass(frozen=True)
class PreviewGeometry:
    reference_size: tuple[int, int]
    preview_size: tuple[int, int]
    raw_orientation: int
    preview_orientation: int
    crop: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class PreviewMapping:
    reference_size: tuple[int, int]
    content_rect: tuple[int, int, int, int]
    preview_orientation: int


@dataclass(frozen=True)
class EmbeddedPreviewData:
    jpeg: bytes
    mapping: PreviewMapping


def _oriented_size(size: tuple[int, int], orientation: int) -> tuple[int, int]:
    return size[::-1] if orientation in (5, 6, 7, 8) else size


def raw_sizes_match_output(sizes) -> bool:
    return (sizes.pixel_aspect == 1
            and (sizes.iwidth, sizes.iheight) == (sizes.width, sizes.height))


def _embedded_jpeg(raw) -> bytes | None:
    thumbnail = raw.extract_thumb()
    return thumbnail.data if thumbnail.format == rawpy.ThumbFormat.JPEG else None


def _embedded_mapping(sizes, source: Image.Image, min_short_side: int, *, crop=None):
    orientation = _FLIP_ORIENTATION.get(sizes.flip)
    if orientation is None:
        return None, "unknown_orientation"
    if min(source.size) < min_short_side:
        return None, "too_small"
    geometry = PreviewGeometry((sizes.width, sizes.height), source.size, orientation,
                               source.getexif().get(_EXIF_ORIENTATION, orientation), crop)
    mapping = map_preview_geometry(geometry)
    return (mapping, "") if mapping is not None else (None, "aspect_or_orientation_mismatch")


def map_preview_geometry(geometry: PreviewGeometry) -> PreviewMapping | None:
    """将预览映射到方向转正后的 RAW 像素边界；裁切坐标相对有效画幅。"""
    width, height = geometry.reference_size
    preview_width, preview_height = geometry.preview_size
    if min(width, height, preview_width, preview_height) <= 0:
        return None
    orientation = geometry.raw_orientation
    if orientation not in (1, 3, 6, 8) or geometry.preview_orientation not in range(1, 9):
        return None
    preview_width, preview_height = _oriented_size(geometry.preview_size, geometry.preview_orientation)
    reference_size = _oriented_size(geometry.reference_size, orientation)

    def oriented(rect):
        left, top, crop_width, crop_height = rect
        if orientation == 3:
            return width - left - crop_width, height - top - crop_height, crop_width, crop_height
        if orientation == 6:
            return height - top - crop_height, left, crop_height, crop_width
        if orientation == 8:
            return top, width - left - crop_width, crop_height, crop_width
        return rect

    candidates = []
    if geometry.crop is not None:
        left, top, crop_width, crop_height = geometry.crop
        if (left >= 0 and top >= 0 and crop_width > 0 and crop_height > 0
                and left + crop_width <= width and top + crop_height <= height):
            candidates.append(oriented(geometry.crop))
    candidates.append((0, 0, *reference_size))
    for rect in candidates:
        ratio = rect[2] / rect[3]
        if abs(preview_width / preview_height - ratio) <= PREVIEW_ASPECT_TOLERANCE * ratio:
            return PreviewMapping(reference_size, rect, geometry.preview_orientation)
    return None


def read_quick_preview(path: str):
    if rawpy is None:
        return None, "rawpy_unavailable"
    try:
        with rawpy.RawPy() as raw:
            raw.open_file(path)
            sizes = raw.sizes
            if not raw_sizes_match_output(sizes):
                return None, "output_geometry_mismatch"
            jpeg = _embedded_jpeg(raw)
            if jpeg is None:
                return None, "not_jpeg"
            crop = (sizes.crop_left_margin - sizes.left_margin,
                    sizes.crop_top_margin - sizes.top_margin,
                    sizes.crop_width, sizes.crop_height)
        with Image.open(BytesIO(jpeg)) as source:
            mapping, reason = _embedded_mapping(sizes, source, 720, crop=crop)
            if mapping is None:
                return None, reason
    except (rawpy.LibRawError, OSError, ValueError, SyntaxError) as error:
        return None, f"preview_failed:{type(error).__name__}"
    return EmbeddedPreviewData(jpeg, mapping), ""


def fit_mapped_preview(image: Image.Image, mapping: PreviewMapping, *, max_side=QUICK_PREVIEW_MAX_SIDE):
    width, height = mapping.reference_size
    scale = min(1, max_side / max(width, height))
    size = max(1, round(width * scale)), max(1, round(height * scale))
    left, top, crop_width, crop_height = mapping.content_rect
    x, y = round(left * scale), round(top * scale)
    right, bottom = round((left + crop_width) * scale), round((top + crop_height) * scale)
    transpose = _ORIENTATION_TRANSPOSE.get(mapping.preview_orientation)
    with (image.transpose(transpose) if transpose is not None else nullcontext(image)) as upright:
        with upright.resize((max(1, right - x), max(1, bottom - y)), Image.Resampling.LANCZOS) as fitted:
            canvas = Image.new("RGB", size)
            try:
                canvas.paste(fitted, (x, y))
                for source_box, fill_size, position in (
                    ((0, 0, 1, fitted.height), (x, fitted.height), (0, y)),
                    ((fitted.width - 1, 0, fitted.width, fitted.height),
                     (size[0] - right, fitted.height), (right, y)),
                ):
                    if min(fill_size) > 0:
                        with fitted.crop(source_box) as edge, edge.resize(fill_size, Image.Resampling.NEAREST) as fill:
                            canvas.paste(fill, position)
                for row, fill_height, position in ((y, y, (0, 0)),
                        (bottom - 1, size[1] - bottom, (0, bottom))):
                    if fill_height > 0:
                        with canvas.crop((0, row, size[0], row + 1)) as edge:
                            with edge.resize((size[0], fill_height), Image.Resampling.NEAREST) as fill:
                                canvas.paste(fill, position)
                return canvas.convert("RGBA")
            finally:
                canvas.close()


def raw_oriented_size(sizes) -> tuple[int, int]:
    return _oriented_size((sizes.width, sizes.height), _FLIP_ORIENTATION.get(sizes.flip, 1))


def raw_reference_size(path: str) -> tuple[int, int]:
    """方向转正后的 RAW 真实宽高。"""
    if rawpy is None:
        raise RawSupportUnavailableError("RAW support is unavailable")
    with rawpy.imread(path) as raw:
        return raw_oriented_size(raw.sizes)


def load_embedded_preview(path: str, *, min_short_side: int, decode_short_side: int | None = None):
    """读取 RAW 内嵌 JPEG 预览，返回 (EmbeddedPreview, "") 或 (None, 原因)。

    可用条件：格式为 JPEG、短边不小于 min_short_side、宽高比与 RAW 相差不超过
    PREVIEW_ASPECT_TOLERANCE、解码成功。方向取预览自带的 EXIF Orientation，缺失时取
    LibRaw flip。decode_short_side 让 JPEG 以短边不小于该值的最大缩小比解码。
    """
    if rawpy is None:
        return None, "rawpy_unavailable"
    try:
        with rawpy.imread(path) as raw:
            sizes = raw.sizes
            data = _embedded_jpeg(raw)
            if data is None:
                return None, "not_jpeg"
    except (rawpy.LibRawError, OSError) as error:
        return None, f"extract_failed:{type(error).__name__}"
    try:
        with Image.open(BytesIO(data)) as source:
            mapping, reason = _embedded_mapping(sizes, source, min_short_side)
            if mapping is None:
                return None, reason
            preview_size = _oriented_size(source.size, mapping.preview_orientation)
            transpose = _ORIENTATION_TRANSPOSE.get(mapping.preview_orientation)
            if decode_short_side is not None:
                source.draft("RGB", (decode_short_side, decode_short_side))
            image = (source.transpose(transpose)
                     if transpose is not None and source.getexif().get(_EXIF_ORIENTATION) is None
                     else ImageOps.exif_transpose(source))
            if image.mode != "RGB":
                converted = image.convert("RGB")
                image.close()
                image = converted
    except (OSError, ValueError, SyntaxError) as error:
        return None, f"decode_failed:{type(error).__name__}"
    return EmbeddedPreview(image, mapping.reference_size, preview_size), ""
