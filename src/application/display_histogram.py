"""显示画面的直方图：按一帧 RGBA8 画面统计，供编辑预览与看图的照片信息栏使用。"""

from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass
from typing import Callable

from PIL import Image

from ..core.color_adjuster import HISTOGRAM_SAMPLE_PIXELS, ColorAdjuster
from ..core.contracts.image_display import DEFAULT_NON_RAW_DISPLAY_MODE, NonRawDisplayMode, RawDisplayMode

__all__ = ["HISTOGRAM_SAMPLE_PIXELS", "DisplayHistogramRequest", "DisplayHistogramService", "display_histogram"]


def display_histogram(size: tuple[int, int], rgba: bytes) -> dict:
    """一帧直通透明度的 RGBA8 画面的直方图；格式同 ColorAdjuster.compute_histogram()。"""
    with Image.frombytes("RGBA", size, rgba) as image:
        return ColorAdjuster.compute_histogram(image)


@dataclass(frozen=True)
class DisplayHistogramRequest:
    """看图终态画面的直方图请求，包含文件版本、显示方式和请求代次。"""

    generation: int
    path: str
    stamp: tuple
    raw_display_mode: RawDisplayMode
    non_raw_display_mode: NonRawDisplayMode = DEFAULT_NON_RAW_DISPLAY_MODE


class DisplayHistogramService:
    """看图画面的直方图。计入共享计算预算，按后台优先级领取一个线程，让位于当前图的加载。"""

    def __init__(self, *, compute_budget):
        self._compute = compute_budget

    def histogram(self, read_frame: Callable[[], tuple[tuple[int, int], bytes]], *,
                  is_cancelled: Callable[[], bool] = lambda: False) -> dict:
        """read_frame() 返回 ((宽, 高), RGBA8 字节)，领到计算额度后才调用；取消时抛出 CancelledError。"""
        with self._compute.borrow(is_cancelled, priority=1, threads=1):
            size, rgba = read_frame()
            if is_cancelled():
                raise CancelledError()
            return display_histogram(size, rgba)
