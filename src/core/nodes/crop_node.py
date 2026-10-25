from dataclasses import replace

from PIL import Image


def validate_params(params):
    box = params.get("box")
    if set(params) != {"box"} or not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError("裁剪参数需要四个像素坐标")
    if any(type(value) is not int for value in box):
        raise ValueError("裁剪坐标必须为整数")
    left, top, right, bottom = box
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise ValueError("裁剪区域无效")


def execute(source, params, *, sampling=None):
    validate_params(params)
    left, top, right, bottom = params["box"]
    width, height = source.size if isinstance(source, Image.Image) else (source.pixels.shape[1], source.pixels.shape[0])
    if right > width or bottom > height:
        raise ValueError("裁剪区域超出图像范围")
    if sampling is not None and sampling.active:
        sampling.crop((left, top, right, bottom))
    if isinstance(source, Image.Image):
        return source.crop((left, top, right, bottom))
    return replace(source, pixels=source.pixels[top:bottom, left:right].copy(),
                   alpha=None if source.alpha is None else source.alpha[top:bottom, left:right].copy(),
                   orig_size=(right - left, bottom - top))
