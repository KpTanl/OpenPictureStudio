"""图片文件与拍摄信息的只读快照。"""
from __future__ import annotations

from concurrent.futures import CancelledError
from contextlib import ExitStack
from dataclasses import dataclass, fields, replace
from io import BytesIO
import logging
import math
from pathlib import Path
from zlib import crc32

from PIL import ExifTags, Image, ImageCms, TiffImagePlugin, UnidentifiedImageError

from ..core import raw_loader
from ..core.contracts.result import CoreError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhotoMetadata:
    shutter: str | None = None
    aperture: str | None = None
    iso: str | None = None
    focal_length: str | None = None
    focal_length_35mm: str | None = None
    exposure_bias: str | None = None
    captured_at: str | None = None
    exposure_program: int | None = None
    metering_mode: int | None = None
    white_balance: int | None = None
    flash: bool | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    lens_make: str | None = None
    lens_model: str | None = None
    unreadable: frozenset[str] = frozenset()

    @property
    def camera(self) -> str:
        make, model = self.camera_make or "", self.camera_model or ""
        if model.casefold().startswith(make.casefold()):
            return model or make
        return f"{make} {model}".strip()


_PHOTO_FIELDS = frozenset(field.name for field in fields(PhotoMetadata) if field.name != "unreadable")
_RAW_FIELDS = frozenset(("shutter", "aperture", "iso", "focal_length", "lens_make", "lens_model"))


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    kind: str
    camera: str
    path: str
    file_format: str
    size_bytes: int
    color_info: str
    photo: PhotoMetadata
    details: bool = False
    issue_key: str = ""


@dataclass(frozen=True)
class ImageInfoQuery:
    path: str | None
    details: bool = False
    force: bool = False


@dataclass(frozen=True)
class ImageInfoRequest:
    generation: int
    path: str
    file_stamp: tuple[int, int, int] | None
    details: bool


@dataclass(frozen=True)
class ImageInfoResult:
    request: ImageInfoRequest
    info: ImageInfo | None = None
    error: CoreError | None = None


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    return value.strip().strip("\0").strip() or None


def _number(value, *, positive=False) -> float | None:
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) and (not positive or result > 0) else None


def _shutter(value) -> str | None:
    seconds = _number(value, positive=True)
    if seconds is None:
        return None
    reciprocal = 1 / seconds if seconds < 1 else 0
    denominator = round(reciprocal) if math.isfinite(reciprocal) else 0
    if denominator > 1 and math.isclose(seconds, 1 / denominator, rel_tol=1e-5):
        return f"1/{denominator} s"
    return f"{seconds:.6g} s"


def _unit(value, suffix: str, *, prefix="", signed=False) -> str | None:
    number = _number(value, positive=not signed)
    if number is None:
        return None
    if signed and number != 0:
        text = f"{number:+.3g}"
    else:
        text = str(int(number)) if number.is_integer() else f"{number:.6g}"
    return f"{prefix}{text}{suffix}"


def _code(value) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _image_exif(image, is_cancelled) -> Image.Exif:
    if image.format != "PNG":
        return image.getexif()
    exif = Image.Image.getexif(image)
    if "exif" in image.info or "Raw profile type exif" in image.info:
        return exif
    stream = image.fp
    position = stream.tell()
    try:
        stream.seek(8)
        while True:
            if is_cancelled():
                raise CancelledError()
            header = stream.read(8)
            if len(header) != 8:
                raise OSError("PNG 元数据区块不完整")
            length, kind = int.from_bytes(header[:4], "big"), header[4:]
            if kind == b"eXIf":
                payload = stream.read(length)
                checksum = stream.read(4)
                if (len(payload) != length or len(checksum) != 4
                        or crc32(kind + payload) != int.from_bytes(checksum, "big")):
                    raise OSError("PNG EXIF 校验失败")
                exif = Image.Exif()
                exif.load(payload)
                return exif
            if kind == b"IEND":
                return exif
            stream.seek(length + 4, 1)
    finally:
        stream.seek(position)


def _photo_from(exif, *, details: bool) -> tuple[PhotoMetadata, bool]:
    tags = dict(exif)
    if details:
        tags.update(exif.get_ifd(ExifTags.IFD.Exif))
    base = ExifTags.Base
    photo = PhotoMetadata(camera_make=_text(tags.get(base.Make)), camera_model=_text(tags.get(base.Model)))
    if not details:
        return photo, bool(tags)
    iso_source = tags.get(base.ISOSpeed)
    iso = _number(iso_source, positive=True)
    if iso is None:
        iso_source = tags.get(base.ISOSpeedRatings)
        iso = _number(iso_source, positive=True)
        if iso == 65535:
            iso = None
    captured = _text(tags.get(base.DateTimeOriginal))
    if captured:
        date, _, time = captured.partition(" ")
        captured = f"{date.replace(':', '-')} {time}".strip()
        subsecond = _text(tags.get(base.SubsecTimeOriginal))
        if subsecond and subsecond.isdigit():
            captured += f".{subsecond}"
        offset = _text(tags.get(base.OffsetTimeOriginal))
        if offset:
            captured += f" {offset}"
    values = {
        "shutter": (tags.get(base.ExposureTime), _shutter(tags.get(base.ExposureTime))),
        "aperture": (tags.get(base.FNumber), _unit(tags.get(base.FNumber), "", prefix="f/")),
        "iso": (iso_source, _unit(iso, "")),
        "focal_length": (tags.get(base.FocalLength), _unit(tags.get(base.FocalLength), " mm")),
        "focal_length_35mm": (tags.get(base.FocalLengthIn35mmFilm), _unit(tags.get(base.FocalLengthIn35mmFilm), " mm")),
        "exposure_bias": (tags.get(base.ExposureBiasValue), _unit(tags.get(base.ExposureBiasValue), " EV", signed=True)),
        "captured_at": (tags.get(base.DateTimeOriginal), captured),
        "exposure_program": (tags.get(base.ExposureProgram), _code(tags.get(base.ExposureProgram))),
        "metering_mode": (tags.get(base.MeteringMode), _code(tags.get(base.MeteringMode))),
        "white_balance": (tags.get(base.WhiteBalance), _code(tags.get(base.WhiteBalance))),
        "flash": (tags.get(base.Flash), _code(tags.get(base.Flash))),
        "camera_make": (tags.get(base.Make), photo.camera_make),
        "camera_model": (tags.get(base.Model), photo.camera_model),
        "lens_make": (tags.get(base.LensMake), _text(tags.get(base.LensMake))),
        "lens_model": (tags.get(base.LensModel), _text(tags.get(base.LensModel))),
    }
    unreadable = frozenset(
        name for name, (source, value) in values.items()
        if value is None and source is not None
        and (not isinstance(source, (str, bytes)) or _text(source) is not None)
    )
    parsed = {name: value for name, (_, value) in values.items()}
    if parsed["flash"] is not None:
        parsed["flash"] = bool(parsed["flash"] & 1)
    return PhotoMetadata(**parsed, unreadable=unreadable), bool(tags)


def _color_info(image, exif) -> str:
    profile = image.info.get("icc_profile")
    if profile:
        return ImageCms.getProfileName(ImageCms.ImageCmsProfile(BytesIO(profile))).strip()
    color_space = exif.get_ifd(ExifTags.IFD.Exif).get(ExifTags.Base.ColorSpace) if exif is not None else None
    return "sRGB" if color_space == 1 else image.mode


def _raw_source_photo(path, details, is_cancelled) -> tuple[PhotoMetadata, bool]:
    with open(path, "rb") as stream:
        header = stream.read(16)
        if header[:4] in TiffImagePlugin.PREFIXES:
            exif = Image.Exif()
            if header[:4] in (b"II\x2b\x00", b"MM\x00\x2b"):
                exif.bigtiff = True
                exif.endian = "<" if header[:2] == b"II" else ">"
                endian = "little" if header[:2] == b"II" else "big"
                exif.load_from_fp(stream, offset=int.from_bytes(header[8:16], endian))
            else:
                stream.seek(0)
                exif.load_from_fp(stream)
            if is_cancelled():
                raise CancelledError()
            return _photo_from(exif, details=details)
    with Image.open(path) as image:
        return _photo_from(_image_exif(image, is_cancelled), details=details)


class ImageInfoService:
    def __init__(self, *, compute_budget, resource_pool):
        self._compute = compute_budget
        self._resources = resource_pool

    @staticmethod
    def _check(is_cancelled):
        if is_cancelled():
            raise CancelledError()

    def read(self, path: str, *, details=False, is_cancelled=lambda: False) -> ImageInfo:
        self._check(is_cancelled)
        size = Path(path).stat().st_size
        if raw_loader.is_raw_file(path):
            return self._read_raw(path, size, details, is_cancelled)
        with Image.open(path) as image:
            issue = ""
            color = image.mode
            exif = None
            try:
                exif = _image_exif(image, is_cancelled)
                photo, _ = _photo_from(exif, details=details)
            except (OSError, ValueError, SyntaxError, TypeError, KeyError) as error:
                logger.warning("图片元数据读取失败：%s (%s)", path, error)
                photo = PhotoMetadata(unreadable=_PHOTO_FIELDS)
                issue = "photo_info.metadata_failed"
            if details:
                try:
                    color = _color_info(image, exif)
                except (OSError, ValueError, TypeError, KeyError, ImageCms.PyCMSError) as error:
                    logger.warning("图片颜色信息读取失败：%s (%s)", path, error)
                    issue = "photo_info.metadata_failed"
            self._check(is_cancelled)
            return ImageInfo(image.width, image.height, image.mode, photo.camera, path,
                             image.format or Path(path).suffix[1:].upper(), size, color,
                             photo, details, issue)

    def _read_raw(self, path, size, details, is_cancelled):
        photo, readable, issue = PhotoMetadata(), False, ""
        try:
            photo, readable = _raw_source_photo(path, details, is_cancelled)
        except UnidentifiedImageError:
            pass
        except (OSError, ValueError, SyntaxError, TypeError, KeyError) as error:
            logger.warning("RAW 源元数据读取失败：%s (%s)", path, error)
            issue = "photo_info.metadata_failed"
        if not details:
            width, height = raw_loader.raw_reference_size(path)
        else:
            import rawpy

            with ExitStack() as native_lifetime:
                raw = rawpy.imread(path)
                native_lifetime.callback(raw.close)
                sizes = raw.sizes
                width, height = raw_loader.raw_oriented_size(sizes)
                needs_values = any(getattr(photo, name) is None for name in _RAW_FIELDS)
                if not readable or needs_values:
                    estimate = sizes.raw_width * sizes.raw_height * 16 + 32 * 1024 ** 2
                    with self._resources.working_memory(estimate, is_cancelled):
                        with native_lifetime.pop_all():
                            with self._compute.borrow(is_cancelled, priority=1):
                                photo, readable, issue = self._raw_photo(path, raw, photo, readable, issue, is_cancelled)
                if not readable:
                    photo = replace(photo, unreadable=_PHOTO_FIELDS - _RAW_FIELDS)
                    issue = issue or "photo_info.partial"
        self._check(is_cancelled)
        return ImageInfo(width, height, "RAW", photo.camera, path, Path(path).suffix[1:].upper(),
                         size, "RAW", photo, details, issue)

    def _raw_photo(self, path, raw, photo, readable, issue, is_cancelled):
        import rawpy

        self._check(is_cancelled)
        if not readable:
            try:
                thumbnail = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                thumbnail = None
            if thumbnail is not None and thumbnail.format == rawpy.ThumbFormat.JPEG:
                try:
                    with Image.open(BytesIO(thumbnail.data)) as image:
                        photo, readable = _photo_from(_image_exif(image, is_cancelled), details=True)
                except (OSError, ValueError, SyntaxError, TypeError, KeyError) as error:
                    logger.warning("RAW 内嵌元数据读取失败：%s (%s)", path, error)
                    issue = "photo_info.metadata_failed"
            thumbnail = None
            self._check(is_cancelled)
        if any(getattr(photo, name) is None for name in ("shutter", "aperture", "iso", "focal_length")):
            other = raw.other
            photo = replace(photo, shutter=photo.shutter or _shutter(other.shutter_speed),
                            aperture=photo.aperture or _unit(other.aperture, "", prefix="f/"),
                            iso=photo.iso or _unit(other.iso_speed, ""),
                            focal_length=photo.focal_length or _unit(other.focal_length, " mm"))
        self._check(is_cancelled)
        if photo.lens_make is None or photo.lens_model is None:
            lens = raw.lens
            photo = replace(photo, lens_make=photo.lens_make or _text(lens.make),
                            lens_model=photo.lens_model or _text(lens.model))
        return photo, readable, issue
