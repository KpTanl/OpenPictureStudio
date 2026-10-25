"""
图片基础处理引擎 — 旋转、翻转等操作。
"""

from __future__ import annotations

from PIL import Image


class ImageProcessor:
    """图片旋转与翻转处理器，所有方法均为纯函数（不修改原图，返回新图）。"""

    @staticmethod
    def rotate_90(image: Image.Image) -> Image.Image:
        """顺时针旋转 90 度。"""
        return image.rotate(-90, expand=True)

    @staticmethod
    def rotate_180(image: Image.Image) -> Image.Image:
        """旋转 180 度。"""
        return image.rotate(180, expand=True)

    @staticmethod
    def rotate_270(image: Image.Image) -> Image.Image:
        """顺时针旋转 270 度（逆时针 90 度）。"""
        return image.rotate(90, expand=True)

    @staticmethod
    def flip_horizontal(image: Image.Image) -> Image.Image:
        """水平翻转（左右镜像）。"""
        return image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

    @staticmethod
    def flip_vertical(image: Image.Image) -> Image.Image:
        """垂直翻转（上下镜像）。"""
        return image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
