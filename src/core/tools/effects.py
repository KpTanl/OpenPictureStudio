from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageFilter

from .catalog import ParameterContract, ParameterField
from .geometry import checked
from .id_photo import (LAYOUT_SHEET, MANAGED_ID_PHOTO_PARAMETERS, SHEET_BORDER,
                       SHEET_SIZE, photo_bounds, sheet_positions)
from ..photo_engine import _pil_from, linear_to_srgb, srgb_to_linear


def _validate_color(params):
    color = params["target_color"]
    if len(color) != 3 or any(type(value) is not int or not 0 <= value <= 255 for value in color):
        raise ValueError("颜色无效")


BG_PARAMETERS = ParameterContract((
    ParameterField("mode", str, "ai", choices=("ai", "color")),
    ParameterField("tolerance", int, 30, 0, 100),
    ParameterField("edge_radius", (int, float), 2, 0, 20),
    ParameterField("target_color", (list, tuple), [255, 255, 255]),
    ParameterField("bg_type", str, "transparent", choices=("transparent", "white", "black")),
), version=2, validate=_validate_color)

def resize_arrays(pixels, alpha, size, *, box=None, parallelism=1):
    """缩放线性图像与直通透明通道，返回 (图像, 透明通道)。

    逐通道 Lanczos；有透明通道时颜色先乘透明度再缩放，缩放后除回，透明处置零。
    box 是取样范围（左、上、右、下，输入像素坐标，可以是小数），缺省为整幅。
    """
    width, height = size
    if width < 1 or height < 1:
        raise ValueError("输出尺寸无效")
    def resize(channel):
        return np.asarray(Image.fromarray(channel).resize(size, Image.Resampling.LANCZOS, box=box), dtype=np.float32)
    resized_alpha = None if alpha is None else np.clip(resize(alpha), 0, 1)

    def resize_channel(index):
        channel = pixels[..., index]
        return resize(channel if alpha is None else channel * alpha)

    if parallelism <= 1:
        channels = [resize_channel(index) for index in range(3)]
    else:
        with ThreadPoolExecutor(max_workers=min(3, parallelism), thread_name_prefix="ops-resize") as pool:
            channels = list(pool.map(resize_channel, range(3)))
    resized = np.stack(channels, axis=-1)
    if resized_alpha is not None:
        resized = np.divide(resized, resized_alpha[..., None], out=np.zeros_like(resized),
                            where=resized_alpha[..., None] > 1e-8)
    return resized, resized_alpha


def resize_source(source, size, *, box=None, parallelism=1, sampling=None):
    """缩放到指定尺寸。输出多大没有固定上限，内存由宿主按各工具声明的估算准入。"""
    pixels, alpha = resize_arrays(source.pixels, source.alpha, size, box=box, parallelism=parallelism)
    if sampling is not None and sampling.active:
        sampling.resize(size, box=box)
    return replace(source, pixels=pixels, alpha=alpha, orig_size=size)


def resize_working_memory(source, size):
    """resize_source() 的工作内存估算：三个通道各自的结果、合并后的图像与透明通道，加输入的一份转换。"""
    return size[0] * size[1] * 40 + source.pixels.nbytes


def _linear_color(color):
    return srgb_to_linear(np.asarray(color, dtype=np.float32) / np.float32(255))


def _matte_onto(pixels, alpha, color):
    """把直通透明的画面铺到纯色（8 位 sRGB）上，在 sRGB 编码值上按透明度混合，返回不透明的线性画面。

    抠图得到的蒙版是在编码值上估计的过渡，不是物理覆盖率；在线性光下按它混合，亮的背景色在边缘的观感占比
    会远大于它的权重。完全不透明的像素不经转换，原样保留。
    """
    result = np.array(pixels, dtype=np.float32)
    partial = alpha < 1
    if partial.any():
        weight = alpha[partial][:, None]
        encoded = linear_to_srgb(result[partial])
        background = np.asarray(color, dtype=np.float32) / np.float32(255)
        # 解码时两段公式都会算，缩放过冲留下的负值在不取用的幂函数那段里得到无效值，结果取的是线性段
        with np.errstate(invalid="ignore"):
            result[partial] = srgb_to_linear(encoded * weight + background * (np.float32(1) - weight))
    return result


def make_id_photo_memory(source, params, *, threads=1):
    """make_id_photo() 的工作内存估算：裁切范围按整幅计，铺背景的一份、抠图时的蒙版与 8 位副本，加缩放与相纸。"""
    area = source.pixels.shape[0] * source.pixels.shape[1]
    size = (params["custom_width"], params["custom_height"])
    required = resize_working_memory(source, size)
    if params["background"] is not None:
        required += area * (12 + (16 if params["ai_matting"] else 0))
    if params["layout"] == LAYOUT_SHEET:
        required += SHEET_SIZE[0] * SHEET_SIZE[1] * 12 + size[0] * size[1] * 12
    return required


def make_id_photo(source, params, *, mask_provider=None, sampling=None):
    """证件照（参数版本 2）：裁切 → 可选抠图 → 铺背景色 → 缩放到规格的像素尺寸 → 可选排满一张相纸。

    背景色在 sRGB 编码值上合成（见 _matte_onto），缩放与排版在线性光下。裁切范围取整到像素之后抠图与合成，
    缩放时再按小数位置取样，不另外重采样一次。
    """
    params = checked(MANAGED_ID_PHOTO_PARAMETERS, params)
    size = (params["custom_width"], params["custom_height"])
    height, width = source.pixels.shape[:2]
    bounds = photo_bounds(params["crop_rect"], (width, height), size[0] / size[1])
    left, top = max(0, int(np.floor(bounds[0]))), max(0, int(np.floor(bounds[1])))
    right, bottom = min(width, int(np.ceil(bounds[2]))), min(height, int(np.ceil(bounds[3])))
    right, bottom = max(right, left + 1), max(bottom, top + 1)
    pixels = source.pixels[top:bottom, left:right]
    alpha = None if source.alpha is None else source.alpha[top:bottom, left:right]
    if sampling is not None and sampling.active:
        sampling.crop((left, top, right, bottom))

    if params["background"] is not None:
        if params["ai_matting"]:
            if mask_provider is None:
                raise FileNotFoundError("抠图模型不可用")
            mask = mask_provider(_pil_from(replace(source, pixels=pixels, alpha=None)).convert("RGB"))
            if mask.size != (right - left, bottom - top):
                raise ValueError("抠图蒙版尺寸无效")
            coverage = np.asarray(mask.convert("L"), dtype=np.float32) / np.float32(255)
            if sampling is not None and sampling.active:
                sampling.apply_mask(coverage)
            alpha = coverage if alpha is None else alpha * coverage
        if alpha is not None:
            pixels, alpha = _matte_onto(pixels, alpha, params["background"]), None

    box = (max(0.0, bounds[0] - left), max(0.0, bounds[1] - top),
           min(float(right - left), bounds[2] - left), min(float(bottom - top), bounds[3] - top))
    pixels, alpha = resize_arrays(pixels, alpha, size, box=box)
    if sampling is not None and sampling.active:
        sampling.resize(size, box=box)
    if params["layout"] != LAYOUT_SHEET:
        return replace(source, pixels=pixels, alpha=alpha, orig_size=size)

    # 排版：白底的整张相纸上铺满同一张照片，透明处落在白底上；每张的左、上各压一条细边，右、下的细边贴在外侧
    if alpha is not None:
        pixels = _matte_onto(pixels, alpha, (255, 255, 255))
    positions = sheet_positions(size)
    sheet = np.ones((SHEET_SIZE[1], SHEET_SIZE[0], 3), dtype=np.float32)
    border = _linear_color(SHEET_BORDER)
    for x, y in positions:
        sheet[y:y + size[1], x:x + size[0]] = pixels
        sheet[y, x:x + size[0] + 1] = border
        sheet[y + size[1], x:x + size[0] + 1] = border
        sheet[y:y + size[1] + 1, x] = border
        sheet[y:y + size[1] + 1, x + size[0]] = border
    if sampling is not None and sampling.active:
        # 取样只对应第一张：其余各张是它的副本
        x, y = positions[0]
        sampling.pad(SHEET_SIZE, (x, y, SHEET_SIZE[0] - x - size[0], SHEET_SIZE[1] - y - size[1]))
    return replace(source, pixels=sheet, alpha=None, orig_size=SHEET_SIZE)


def execute_background(source, params, *, mask_provider=None, sampling=None):
    params = checked(BG_PARAMETERS, params)
    if params["mode"] == "ai":
        if mask_provider is None:
            raise FileNotFoundError("抠图模型不可用")
        mask = mask_provider(_pil_from(source).convert("RGB"))
        if mask.size != (source.pixels.shape[1], source.pixels.shape[0]):
            raise ValueError("抠图蒙版尺寸无效")
    else:
        rgb = np.clip(linear_to_srgb(source.pixels), 0, 1) * np.float32(255)
        distance = np.linalg.norm(rgb - np.asarray(params["target_color"], dtype=np.float32), axis=2)
        mask = Image.fromarray(np.where(distance <= params["tolerance"], 0, 255).astype(np.uint8))
    if params["edge_radius"]:
        ratio = min(source.pixels.shape[1] / source.orig_size[0], source.pixels.shape[0] / source.orig_size[1])
        mask = mask.filter(ImageFilter.GaussianBlur(params["edge_radius"] * ratio))
    alpha = np.asarray(mask.convert("L"), dtype=np.float32) / np.float32(255)
    if sampling is not None and sampling.active:
        sampling.apply_mask(alpha)
    if source.alpha is not None:
        alpha *= source.alpha
    if params["bg_type"] == "transparent":
        return replace(source, alpha=alpha)
    background = np.float32(1 if params["bg_type"] == "white" else 0)
    return replace(source, pixels=source.pixels * alpha[..., None] + background * (1 - alpha[..., None]), alpha=None)
