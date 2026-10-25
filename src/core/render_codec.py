from __future__ import annotations

from io import BytesIO

import numpy as np
import tifffile
from PIL import Image, ImageCms, ImageOps


def validate_source(source):
    pixels = source.pixels
    if (pixels.dtype != np.float32 or pixels.ndim != 3 or pixels.shape[2] != 3
            or min(pixels.shape[:2]) < 1 or not np.isfinite(pixels).all()
            or source.colorspace != "linear_srgb" or source.white_point != "D65"
            or source.is_proxy):
        raise ValueError("正式产物必须是全尺寸 float32 线性 sRGB 图像")
    alpha = source.alpha
    if alpha is not None and (alpha.dtype != np.float32 or alpha.shape != pixels.shape[:2]
                              or not np.isfinite(alpha).all()
                              or np.any(alpha < 0) or np.any(alpha > 1)):
        raise ValueError("Alpha 契约无效")


def _channels(pixels, alpha):
    return pixels if alpha is None else np.concatenate((pixels, alpha[..., None]), axis=2)


def write_linear(source, path):
    validate_source(source)
    tifffile.imwrite(path, _channels(source.pixels, source.alpha), photometric="rgb",
                     extrasamples=["unassalpha"] if source.alpha is not None else None,
                     metadata={"ops_render": 1, "colorspace": "linear_srgb", "white_point": "D65"})


def read_linear(path):
    import json
    from .photo_engine import RenderSource

    with tifffile.TiffFile(path) as file:
        page = file.pages[0]
        metadata = json.loads(page.description)
        if (len(file.pages) != 1 or metadata.get("ops_render", metadata.get("opv_render")) != 1
                or metadata.get("colorspace") != "linear_srgb"
                or metadata.get("white_point") != "D65" or page.photometric != 2):
            raise ValueError("不支持的浮点产物")
        data = page.asarray()
        if data.dtype != np.float32 or data.ndim != 3 or data.shape[2] not in (3, 4):
            raise ValueError("浮点产物通道无效")
        if data.shape[2] == 4 and tuple(page.extrasamples) != (2,):
            raise ValueError("浮点产物 Alpha 无效")
    height, width = data.shape[:2]
    source = RenderSource(data[..., :3], data[..., 3] if data.shape[2] == 4 else None,
                          "linear32", "", False, (width, height), "linear_srgb")
    validate_source(source)
    return source


def write_tiff(source, path):
    from .photo_engine import linear_to_srgb

    validate_source(source)
    rgb = np.rint(np.clip(linear_to_srgb(source.pixels), 0, 1) * np.float32(65535)).astype(np.uint16)
    alpha = None if source.alpha is None else np.rint(source.alpha * np.float32(65535)).astype(np.uint16)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    tifffile.imwrite(path, _channels(rgb, alpha), photometric="rgb", metadata=None,
                     extrasamples=["unassalpha"] if alpha is not None else None,
                     iccprofile=profile)


def validate_output(path, ext, *, size, has_alpha):
    if ext in (".tif", ".tiff"):
        with tifffile.TiffFile(path) as file:
            page = file.pages[0]
            if (len(file.pages) != 1 or page.dtype != np.dtype("uint16")
                    or (page.imagewidth, page.imagelength) != size or page.photometric != 2
                    or page.samplesperpixel != (4 if has_alpha else 3)
                    or (has_alpha and tuple(page.extrasamples) != (2,))):
                raise ValueError("TIFF 位深、尺寸或 Alpha 校验失败")
            page.asarray()
    else:
        formats = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".bmp": "BMP"}
        with Image.open(path) as image:
            if image.format != formats[ext] or image.size != size:
                raise ValueError("编码格式或尺寸校验失败")
            if has_alpha and ext in (".png", ".webp") and "A" not in image.getbands():
                raise ValueError("编码丢失 Alpha")
            image.load()


def load_tiff(path):
    from .photo_engine import RenderSource, srgb_to_linear, _src_from_pil

    with tifffile.TiffFile(path) as file:
        page = file.pages[0]
        if page.dtype in (np.dtype("uint8"), np.dtype("bool")):
            with Image.open(path) as image:
                return _src_from_pil(ImageOps.exif_transpose(image), source_path=str(path))
        if page.dtype not in (np.dtype("uint8"), np.dtype("uint16")) or page.photometric not in (0, 1, 2):
            raise ValueError("不支持的 TIFF 位深或色彩空间")
        profile = page.tags.get(34675)
        if profile is not None:
            name = ImageCms.getProfileName(ImageCms.ImageCmsProfile(BytesIO(profile.value)))
            if "srgb" not in name.lower():
                raise ValueError("TIFF 需要先转换为 sRGB")
        data = page.asarray()
        if page.planarconfig == 2:
            data = np.moveaxis(data, 0, -1)
        if data.ndim == 2:
            data = data[..., None]
        colors = 3 if page.photometric == 2 else 1
        if data.ndim != 3 or data.shape[2] not in (colors, colors + 1):
            raise ValueError("不支持的 TIFF 通道布局")
        values = data.astype(np.float32) / np.float32(np.iinfo(data.dtype).max)
        orientation = int(page.tags[274].value) if 274 in page.tags else 1
        if orientation == 2:
            values = values[:, ::-1]
        elif orientation == 3:
            values = values[::-1, ::-1]
        elif orientation == 4:
            values = values[::-1]
        elif orientation == 5:
            values = values.transpose(1, 0, 2)
        elif orientation == 6:
            values = np.rot90(values, 3)
        elif orientation == 7:
            values = values.transpose(1, 0, 2)[::-1, ::-1]
        elif orientation == 8:
            values = np.rot90(values)
        elif orientation != 1:
            raise ValueError("TIFF 方向无效")
        rgb = values[..., :colors]
        alpha = values[..., colors] if values.shape[2] > colors else None
        if alpha is not None:
            if tuple(page.extrasamples) == (1,):
                rgb = np.divide(rgb, alpha[..., None], out=np.zeros_like(rgb), where=alpha[..., None] > 0)
            elif tuple(page.extrasamples) != (2,):
                raise ValueError("TIFF Alpha 表示方式不明确")
        if page.photometric == 0:
            rgb = 1 - rgb
        if colors == 1:
            rgb = np.repeat(rgb, 3, axis=2)
    height, width = rgb.shape[:2]
    return RenderSource(np.ascontiguousarray(srgb_to_linear(rgb), dtype=np.float32),
                        None if alpha is None else np.ascontiguousarray(alpha),
                        "srgb16" if data.dtype == np.uint16 else "srgb8", str(path), False,
                        (width, height), "srgb-direct")
