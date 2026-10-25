from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from io import BytesIO
import math
import re

from PIL import ExifTags, Image, TiffImagePlugin


@dataclass(frozen=True)
class CaptureMetadata:
    make: str = ""
    model: str = ""
    serial: str = ""
    lens: str = ""
    captured: datetime | None = None
    shutter: Fraction | None = None
    aperture: Fraction | None = None
    iso: Fraction | None = None
    tv: Fraction | None = None
    av: Fraction | None = None
    flash: int | None = None
    mode: str = "calibration.mode_unknown"
    apex_raw: tuple = ()

    def exposure(self):
        if self.mode:
            raise ValueError(self.mode)
        if self.flash is None or self.flash & 1:
            raise ValueError("calibration.flash")
        values = (self.shutter, self.aperture, self.iso, self.tv, self.av)
        if any(value is None for value in values) or any(value <= 0 for value in values[:3]):
            raise ValueError("calibration.apex_missing")
        if len(self.apex_raw) != 2 or any(pair is None for pair in self.apex_raw):
            raise ValueError("calibration.apex_missing")
        steps = [1 / pair[1] for pair in self.apex_raw]
        resolution = sum(steps)
        if max(steps) > .01:
            raise ValueError("calibration.apex_precision")
        discrepancy = max(abs(float(self.tv) + math.log2(self.shutter)),
                          abs(float(self.av) - 2 * math.log2(self.aperture)))
        if discrepancy > .05:
            raise ValueError("calibration.apex_conflict")
        return math.log2(self.iso) - float(self.tv) - float(self.av), resolution, discrepancy


def _text(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value).strip("\x00 ") if value is not None else ""


def _raw_pair(value):
    try:
        if isinstance(value, tuple):
            value = value[0] if len(value) == 1 else None
        if value is None or isinstance(value, (bool, str, bytes)):
            return None
        if hasattr(value, "numerator"):
            numerator, denominator = int(value.numerator), int(value.denominator)
            return (numerator, denominator) if denominator > 0 else None
        return (value, 1) if isinstance(value, int) else None
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def _rational(value):
    pair = _raw_pair(value)
    return Fraction(*pair) if pair is not None else None


def _captured(tags):
    value = _text(tags.get(36867))
    try:
        captured = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
        sub = _text(tags.get(37521))
        if sub:
            if not sub.isdigit():
                return None
            captured = captured.replace(microsecond=int((sub + "000000")[:6]))
        offset = _text(tags.get(36881))
        if offset:
            if not re.fullmatch(r"[+-]\d{2}:\d{2}", offset):
                return None
            hours, minutes = int(offset[1:3]), int(offset[4:6])
            if hours > 14 or minutes > 59 or hours == 14 and minutes:
                return None
            delta = timedelta(hours=hours, minutes=minutes) * (-1 if offset[0] == "-" else 1)
            captured = captured.replace(tzinfo=timezone(delta)).astimezone(timezone.utc)
        return captured
    except ValueError:
        return None


def _nikon_mode(make, payload):
    if "nikon" not in make.casefold() or not isinstance(payload, bytes) or not payload.startswith(b"Nikon\x00\x02"):
        return "calibration.mode_unknown"
    try:
        stream = BytesIO(payload[10:])
        header = stream.read(8)
        directory = TiffImagePlugin.ImageFileDirectory_v2(header)
        stream.seek(directory.next)
        directory.load(stream)
        adl, info = directory.get(0x22), directory.get(0x25)
        if adl is None or not isinstance(info, bytes) or len(info) < 12:
            return "calibration.mode_unknown"
        if adl != 0 or any(info[4:6]) or any(info[10:12]):
            return "calibration.special_mode"
        return ""
    except (OSError, ValueError, TypeError, SyntaxError):
        return "calibration.mode_unknown"


def _read(exif):
    tags = exif.get_ifd(ExifTags.IFD.Exif)
    make = _text(exif.get(271))
    serial = _text(tags.get(42033))
    if not serial.strip("0 -") or serial.casefold() in ("unknown", "n/a", "none", "null"):
        serial = ""
    flash = tags.get(37385)
    return CaptureMetadata(make, _text(exif.get(272)), serial, _text(tags.get(42036)),
        _captured(tags), _rational(tags.get(33434)), _rational(tags.get(33437)),
        _rational(tags.get(34855)), _rational(tags.get(37377)), _rational(tags.get(37378)),
        flash if type(flash) is int else None, _nikon_mode(make, tags.get(37500)),
        (_raw_pair(tags.get(37377)), _raw_pair(tags.get(37378))))


def read_capture_metadata(path):
    with open(path, "rb") as stream:
        header = stream.read(16)
        if header[:4] in TiffImagePlugin.PREFIXES:
            exif = Image.Exif()
            if header[:4] in (b"II\x2b\x00", b"MM\x00\x2b"):
                exif.bigtiff = True
                exif.endian = "<" if header[:2] == b"II" else ">"
                stream.seek(0)
                exif.load_from_fp(stream, offset=int.from_bytes(header[8:16], "little" if exif.endian == "<" else "big"))
            else:
                stream.seek(0)
                exif.load_from_fp(stream)
            return _read(exif)
    with Image.open(path) as image:
        return _read(image.getexif())
