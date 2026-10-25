from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from .tools.effects import resize_source
from .contracts.result import SourceChangedError


RESIZE_MODES = ("contain", "cover", "pad", "stretch")


@dataclass(frozen=True)
class ResizeOptions:
    """输出多大没有固定上限；能不能做由调用方按 resize_memory() 的估算与内存预算把关。"""

    width: int | None = None
    height: int | None = None
    mode: str = "contain"

    def __post_init__(self):
        if self.mode not in RESIZE_MODES:
            raise ValueError("compress.status.invalid_dimensions")
        for value in (self.width, self.height):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError("compress.status.invalid_dimensions")

    @classmethod
    def from_text(cls, width, height, mode):
        def dimension(text):
            text = text.strip()
            if not text:
                return None
            if not text.isascii() or not text.isdigit() or len(text) > 9:
                raise ValueError("compress.status.invalid_dimensions")
            return int(text)
        return cls(dimension(width), dimension(height), mode)


@dataclass(frozen=True)
class ResizePlan:
    """裁切使用原图浮点坐标；补边顺序为左、上、右、下。"""

    source_size: tuple[int, int]
    output_size: tuple[int, int]
    content_size: tuple[int, int]
    crop_box: tuple[float, float, float, float] | None = None
    padding: tuple[int, int, int, int] = (0, 0, 0, 0)


def plan_resize(source_size, options):
    width, height = source_size
    if type(width) is not int or type(height) is not int or min(width, height) < 1:
        raise ValueError("error.export.size")
    target_w, target_h = options.width, options.height
    crop_box = None
    padding = (0, 0, 0, 0)
    if target_w is None and target_h is None:
        output_size = content_size = source_size
    elif target_h is None:
        output_size = content_size = (target_w, max(1, round(height * target_w / width)))
    elif target_w is None:
        output_size = content_size = (max(1, round(width * target_h / height)), target_h)
    elif options.mode == "stretch":
        output_size = content_size = (target_w, target_h)
    elif options.mode == "cover":
        if width * target_h > height * target_w:
            crop_w = height * target_w / target_h
            left = (width - crop_w) / 2
            crop_box = (left, 0.0, left + crop_w, float(height))
        else:
            crop_h = width * target_h / target_w
            top = (height - crop_h) / 2
            crop_box = (0.0, top, float(width), top + crop_h)
        output_size = content_size = (target_w, target_h)
    else:
        scale = min(target_w / width, target_h / height)
        content_size = (max(1, min(target_w, round(width * scale))),
                        max(1, min(target_h, round(height * scale))))
        output_size = content_size
        if options.mode == "pad":
            output_size = (target_w, target_h)
            spare_w, spare_h = target_w - content_size[0], target_h - content_size[1]
            left, top = spare_w // 2, spare_h // 2
            padding = (left, top, spare_w - left, spare_h - top)
    return ResizePlan(source_size, output_size, content_size, crop_box, padding)


def resize_memory(plan):
    """按规划缩放一张图片所需工作内存的估算（字节），含解码源图、缩放与编码前的缓冲。

    源图按完整解码的口径每像素 64 字节；输出每像素 48 字节：三个通道各自的结果、合并后的图像、
    透明通道、补边时的另一份，以及编码前按目标位深转换的一份。
    """
    width, height = plan.source_size
    out_width, out_height = plan.output_size
    return width * height * 64 + out_width * out_height * 48


def apply_resize(source, plan, *, sampling=None):
    size = (source.pixels.shape[1], source.pixels.shape[0])
    if size != plan.source_size or source.is_proxy:
        raise SourceChangedError()
    result = source
    if plan.crop_box is not None or plan.content_size != size:
        result = resize_source(source, plan.content_size, box=plan.crop_box, sampling=sampling)
    if any(plan.padding):
        width, height = plan.output_size
        content_w, content_h = plan.content_size
        left, top, _, _ = plan.padding
        pixels = np.zeros((height, width, 3), dtype=np.float32)
        alpha = np.zeros((height, width), dtype=np.float32)
        pixels[top:top + content_h, left:left + content_w] = result.pixels
        alpha[top:top + content_h, left:left + content_w] = 1 if result.alpha is None else result.alpha
        result = replace(result, pixels=pixels, alpha=alpha, orig_size=plan.output_size)
        if sampling is not None and sampling.active:
            sampling.pad(plan.output_size, plan.padding)
    return result
