from dataclasses import dataclass
from concurrent.futures import CancelledError
import struct
import zlib

import numpy as np
from PIL import Image, ImageOps


SOURCE_INPUT_REVISION = "integer-source-1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class SourceInputSpec:
    size: tuple[int, int]
    channels: int
    depth: int
    layout: str = "pixels"
    byte_order: str = "little"
    transfer: str = "srgb"
    revision: str = SOURCE_INPUT_REVISION
    container: str = ""

    @property
    def qualification(self):
        return self.layout, self.channels, self.depth, self.byte_order, self.transfer

    @property
    def nbytes(self):
        width, height = self.size
        return (width * self.channels * (self.depth // 8) + int(self.layout == "png-filtered")) * height


@dataclass(frozen=True)
class IntegerSourceInput:
    spec: SourceInputSpec
    data: np.ndarray
    raw_meta: object = None
    matrix: tuple = ()
    blocks: object = None

    @property
    def nbytes(self):
        return self.data.nbytes + (self.blocks.nbytes if self.blocks is not None else 0)


def materialize_input(value, *, threads=1, cancelled=lambda: False):
    if value.blocks is None:
        return value
    import ops_accel
    data = ops_accel.source_decompress_blocks(value.data, value.blocks, value.spec.nbytes,
        threads=threads, cancelled=cancelled)
    data.setflags(write=False)
    return IntegerSourceInput(value.spec, data, value.raw_meta, value.matrix)


def render_input(value, path, *, half_size=False, threads=1, cancelled=lambda: False):
    import ops_accel
    from .photo_engine import RenderSource, srgb_integer_to_linear
    value = materialize_input(value, threads=threads, cancelled=cancelled)
    spec, data = value.spec, value.data
    width, height = spec.size
    if spec.layout == "png-filtered":
        data = ops_accel.source_unfilter_png(data, width, height, spec.channels * (spec.depth // 8), cancelled)
    values = data.view((">" if spec.byte_order == "big" else "<") + ("u1" if spec.depth == 8 else "u2"))
    values = values.reshape(height, width, spec.channels)
    if spec.transfer == "xyz-d65":
        pixels = ops_accel.raw_xyz_to_linear_srgb(np.ascontiguousarray(values, dtype=np.uint16),
            65535.0, np.asarray(value.matrix, np.float64).reshape(3, 3), threads=threads)
        alpha, fmt, pipeline = None, "raw", "libraw"
    else:
        maximum = np.float32((1 << spec.depth) - 1)
        alpha = values[..., -1].astype(np.float32) / maximum if spec.channels in (2, 4) else None
        rgb = values[..., :3] if spec.channels >= 3 else np.repeat(values[..., :1], 3, axis=2)
        pixels = srgb_integer_to_linear(rgb)
        fmt, pipeline = "srgb16" if spec.depth == 16 else "srgb8", "srgb-direct"
    if cancelled():
        raise CancelledError()
    reference = value.raw_meta.reference_size if half_size else (width, height)
    return RenderSource(pixels, alpha, fmt, path, half_size, reference, pipeline, raw_decode_meta=value.raw_meta)


def read_orientation(path, image):
    if image.format != "PNG":
        return image.getexif().get(274, 1)
    if "exif" in image.info or "Raw profile type exif" in image.info:
        return Image.Image.getexif(image).get(274, 1)
    with open(path, "rb") as stream:
        if stream.read(8) != PNG_SIGNATURE:
            raise ValueError("PNG signature is invalid")
        while True:
            chunk = stream.read(8)
            if len(chunk) != 8:
                return 1
            length, tag = struct.unpack(">I4s", chunk)
            if tag == b"eXIf":
                if length > 16 * 1024 * 1024:
                    raise ValueError("PNG EXIF exceeds the metadata limit")
                data = stream.read(length)
                crc = stream.read(4)
                if len(data) != length or len(crc) != 4 or struct.unpack(">I", crc)[0] != zlib.crc32(data, zlib.crc32(tag)):
                    raise ValueError("PNG EXIF is invalid")
                exif = Image.Exif()
                exif.load(data)
                return exif.get(274, 1)
            if tag == b"IEND":
                return 1
            stream.seek(length + 4, 1)


# sRGB 三原色在配置文件里的取值（XYZ，适配到 D50）
SRGB_COLORANTS = ((0.4361, 0.2225, 0.0139), (0.3851, 0.7169, 0.0971), (0.1431, 0.0606, 0.7141))


def srgb_profile(data):
    """内嵌的色彩配置文件是不是 sRGB：名称里写着 sRGB，三原色也对得上。"""
    from io import BytesIO
    from PIL import ImageCms
    try:
        profile = ImageCms.ImageCmsProfile(BytesIO(data))
        names = (ImageCms.getProfileDescription(profile) + ImageCms.getProfileName(profile)).lower()
        core = profile.profile
        if "srgb" not in names or core.xcolor_space.strip() != "RGB":
            return False
        colorants = (core.red_colorant, core.green_colorant, core.blue_colorant)
        return all(abs(actual - expected) <= 0.003 for colorant, reference in zip(colorants, SRGB_COLORANTS)
                   for actual, expected in zip(colorant[0], reference))
    except (ImageCms.PyCMSError, OSError, TypeError, ValueError, AttributeError):
        return False


def describe_input(path, *, half_size=False):
    from .photo_engine import _is_raw, _dcp_opt_in
    if _is_raw(path):
        if _dcp_opt_in():
            return None
        from .raw_loader import raw_reference_size
        width, height = raw_reference_size(path)
        size = ((width + 1) // 2, (height + 1) // 2) if half_size else (width, height)
        return SourceInputSpec(size, 3, 16, transfer="xyz-d65", container="raw")
    with Image.open(path) as image:
        profile = image.info.get("icc_profile")
        if image.format not in ("JPEG", "PNG") or profile and not srgb_profile(profile):
            return None
        size = image.size
        orientation = read_orientation(path, image)
        if orientation in (5, 6, 7, 8):
            size = size[::-1]
        if image.format == "JPEG":
            return SourceInputSpec(size, 3, 8, container="jpeg") if image.mode in ("RGB", "L") else None
        with open(path, "rb") as stream:
            header = stream.read(33)
        if header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR" or len(header) != 33:
            raise ValueError("PNG header is invalid")
        width, height, depth, kind, compression, filtering, interlace = struct.unpack(">IIBBBBB", header[16:29])
        channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(kind)
        direct = (channels is not None and depth in (8, 16) and not interlace and orientation == 1
                  and "transparency" not in image.info and compression == filtering == 0)
        if direct:
            return SourceInputSpec((width, height), channels, depth, "png-filtered", "big", container="png")
        alpha = kind in (4, 6) or "transparency" in image.info or (
            image.mode == "P" and image.palette is not None and image.palette.mode == "RGBA")
        return SourceInputSpec(size, 4 if alpha else 3, 16 if depth == 16 else 8, container="png")


def png_scanlines(path, spec, cancelled):
    width, height = spec.size
    expected = spec.nbytes
    inflater = zlib.decompressobj()
    result = bytearray()
    seen_header, seen_data, ended_data, finished = False, False, False, False
    with open(path, "rb") as stream:
        if stream.read(8) != PNG_SIGNATURE:
            raise ValueError("PNG signature is invalid")
        while not finished:
            if cancelled():
                raise CancelledError()
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError("PNG chunk is truncated")
            length, tag = struct.unpack(">I4s", header)
            if not seen_header and (tag != b"IHDR" or length != 13):
                raise ValueError("PNG IHDR is missing")
            if tag == b"IDAT" and ended_data:
                raise ValueError("PNG IDAT chunks are not consecutive")
            if tag[0] & 32 == 0 and tag not in (b"IHDR", b"PLTE", b"IDAT", b"IEND"):
                raise ValueError("PNG critical chunk is unsupported")
            checksum = zlib.crc32(tag)
            remaining = length
            ihdr = bytearray()
            while remaining:
                if cancelled():
                    raise CancelledError()
                data = stream.read(min(remaining, 65536))
                if not data:
                    raise ValueError("PNG chunk is truncated")
                remaining -= len(data)
                checksum = zlib.crc32(data, checksum)
                if tag == b"IDAT":
                    result.extend(inflater.decompress(data, expected + 1 - len(result)))
                    if len(result) > expected or inflater.unconsumed_tail or inflater.unused_data:
                        raise ValueError("PNG scanline length is invalid")
                elif tag == b"IHDR":
                    ihdr.extend(data)
            crc = stream.read(4)
            if len(crc) != 4 or struct.unpack(">I", crc)[0] != checksum:
                raise ValueError("PNG chunk CRC is invalid")
            if tag == b"IHDR":
                if seen_header or len(ihdr) != 13:
                    raise ValueError("PNG IHDR is invalid")
                w, h, depth, kind, compression, filtering, interlace = struct.unpack(">IIBBBBB", ihdr)
                if ((w, h) != (width, height) or depth != spec.depth
                        or {0: 1, 2: 3, 4: 2, 6: 4}.get(kind) != spec.channels
                        or compression or filtering or interlace):
                    raise ValueError("PNG input specification changed")
                seen_header = True
            elif tag == b"IDAT":
                seen_data = True
            elif tag == b"IEND":
                if length:
                    raise ValueError("PNG IEND is invalid")
                finished = True
            elif seen_data:
                ended_data = True
    if not seen_data or not inflater.eof or len(result) != expected:
        raise ValueError("PNG compressed data is incomplete")
    stride = width * spec.channels * (spec.depth // 8) + 1
    if any(result[row * stride] > 4 for row in range(height)):
        raise ValueError("PNG filter is invalid")
    return np.frombuffer(bytes(result), np.uint8)


def decode_integer_pixels(path, cancelled=lambda: False):
    if cancelled():
        raise CancelledError()
    with Image.open(path) as image:
        if image.format not in ("PNG", "JPEG"):
            return None
        info = dict(image.info)
        orientation = read_orientation(path, image)
        depth, kind = 8, None
        if image.format == "PNG":
            with open(path, "rb") as stream:
                header = stream.read(29)
            depth, kind = header[24:26]
        if depth == 16:
            import cv2
            with open(path, "rb") as stream:
                encoded = np.frombuffer(stream.read(), np.uint8)
            pixels = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
            if pixels is None or pixels.dtype != np.uint16:
                raise ValueError("PNG 16-bit decoding failed")
            if pixels.ndim == 2:
                pixels = pixels[..., None]
            elif pixels.shape[2] in (3, 4):
                pixels = pixels[..., [2, 1, 0, 3] if pixels.shape[2] == 4 else [2, 1, 0]]
            if "transparency" in info and kind in (0, 2) and pixels.shape[2] in (1, 3):
                transparent = np.asarray(info["transparency"], np.uint16)
                alpha = np.where(np.all(pixels == transparent, axis=2), 0, 65535).astype(np.uint16)
                pixels = np.concatenate((pixels, alpha[..., None]), axis=2)
            transforms = {2: lambda a: a[:, ::-1], 3: lambda a: a[::-1, ::-1], 4: lambda a: a[::-1],
                5: lambda a: a.swapaxes(0, 1), 6: lambda a: np.rot90(a, -1),
                7: lambda a: a.swapaxes(0, 1)[::-1, ::-1], 8: lambda a: np.rot90(a)}
            pixels = transforms.get(orientation, lambda a: a)(pixels)
            if pixels.shape[2] == 1:
                pixels = np.repeat(pixels, 3, axis=2)
            elif pixels.shape[2] == 2:
                pixels = np.concatenate((np.repeat(pixels[..., :1], 3, axis=2), pixels[..., 1:]), axis=2)
        else:
            has_alpha = image.mode in ("RGBA", "LA") or "transparency" in info or (
                image.mode == "P" and image.palette is not None and image.palette.mode == "RGBA")
            with ImageOps.exif_transpose(image) as oriented:
                with oriented.convert("RGBA" if has_alpha else "RGB") as converted:
                    pixels = np.array(converted)
    if cancelled():
        raise CancelledError()
    return np.ascontiguousarray(pixels), info


def load_input(path, spec, *, half_size=False, cancelled=lambda: False):
    if spec.transfer == "xyz-d65":
        from .photo_engine import _decode_raw_u16
        from .dcp import xyz_d65_to_srgb_matrix
        values, meta, maximum = _decode_raw_u16(path, for_preview=half_size)
        if maximum != 65535:
            raise ValueError("RAW input precision changed")
        matrix = tuple(float(value) for row in xyz_d65_to_srgb_matrix() for value in row)
    elif spec.layout == "png-filtered":
        values, meta, matrix = png_scanlines(path, spec, cancelled), None, ()
    else:
        decoded = decode_integer_pixels(path, cancelled)
        if decoded is None:
            raise ValueError("Integer source input is unavailable")
        values, _ = decoded
        meta, matrix = None, ()
        if values.shape[:2] != spec.size[::-1] or values.shape[2] != spec.channels or values.dtype.itemsize * 8 != spec.depth:
            raise ValueError("Integer source input specification changed")
    data = np.ascontiguousarray(values).view(np.uint8).reshape(-1)
    if data.nbytes != spec.nbytes:
        raise ValueError("Integer source input length changed")
    data.setflags(write=False)
    return IntegerSourceInput(spec, data, meta, matrix)
