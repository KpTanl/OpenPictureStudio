"""证件照的人像对齐：找出画面里最主要的人脸，并按参考线算出裁切范围。

坐标都是输入画面的像素，矩形是（左、上、右、下）。检测较慢，由调用方放到后台执行。
"""

from __future__ import annotations

import os
from collections import deque

import numpy as np
from PIL import Image

from .id_photo import PORTRAIT_GUIDE

_SKIN_SCAN_SIDE = 360


def detect_primary_face(image: Image.Image) -> tuple[int, int, int, int] | None:
    """画面里最大的正面人脸；先用 OpenCV 的级联检测器，找不到时按肤色的连通区域估计。"""
    return _detect_face_cascade(image) or _detect_face_skin(image)


def _detect_face_cascade(image: Image.Image) -> tuple[int, int, int, int] | None:
    try:
        import cv2
    except ImportError:
        return None

    gray = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    cascade = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    if cascade.empty():
        return None
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                     minSize=(max(40, image.width // 12), max(40, image.height // 12)))
    if len(faces) == 0:
        return None
    x, y, width, height = max(faces, key=lambda rect: rect[2] * rect[3])
    return int(x), int(y), int(x + width), int(y + height)


def _is_skin_pixel(r: int, g: int, b: int) -> bool:
    if (r > 95 and g > 40 and b > 20 and max(r, g, b) - min(r, g, b) > 15
            and abs(r - g) > 15 and r > g and r > b):
        return True
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return y > 80 and 77 <= cb <= 127 and 133 <= cr <= 173


def _detect_face_skin(image: Image.Image) -> tuple[int, int, int, int] | None:
    """缩到小图后找肤色的连通区域，按面积、形状与上下位置打分，取得分最高的一块。"""
    image_width, image_height = image.size
    if image_width <= 0 or image_height <= 0:
        return None

    scale = min(_SKIN_SCAN_SIDE / image_width, _SKIN_SCAN_SIDE / image_height, 1.0)
    small_width = max(1, int(round(image_width * scale)))
    small_height = max(1, int(round(image_height * scale)))
    small = image.convert("RGB").resize((small_width, small_height), Image.Resampling.BILINEAR)
    skin = [_is_skin_pixel(r, g, b) for r, g, b in np.asarray(small).reshape(-1, 3).tolist()]
    visited = bytearray(len(skin))

    best_box = None
    best_score = 0.0
    for start, is_skin in enumerate(skin):
        if not is_skin or visited[start]:
            continue
        queue = deque([start])
        visited[start] = 1
        area = 0
        min_x, min_y, max_x, max_y = small_width, small_height, -1, -1
        while queue:
            current = queue.popleft()
            x, y = current % small_width, current // small_width
            area += 1
            min_x, min_y, max_x, max_y = min(min_x, x), min(min_y, y), max(max_x, x), max(max_y, y)
            for near_y in range(max(0, y - 1), min(small_height, y + 2)):
                base = near_y * small_width
                for near_x in range(max(0, x - 1), min(small_width, x + 2)):
                    index = base + near_x
                    if not visited[index] and skin[index]:
                        visited[index] = 1
                        queue.append(index)
        if area < 40:
            continue

        aspect = (max_x - min_x + 1) / (max_y - min_y + 1)
        shape_bonus = 1.2 if 0.45 <= aspect <= 1.9 else 1.0 if 0.35 <= aspect <= 2.3 else 0.7
        center = (min_y + max_y + 1) / 2 / small_height
        position_bonus = 1.2 if center < 0.55 else 1.0 if center < 0.75 else 0.85
        score = area * shape_bonus * position_bonus
        if score > best_score:
            best_score = score
            best_box = (min_x, min_y, max_x + 1, max_y + 1)

    if best_box is None:
        return None
    scale_x, scale_y = image_width / small_width, image_height / small_height
    pad_x = max(2, int(round(0.03 * image_width)))
    pad_y = max(2, int(round(0.03 * image_height)))
    left = max(0, int(round(best_box[0] * scale_x)) - pad_x)
    top = max(0, int(round(best_box[1] * scale_y)) - pad_y)
    right = min(image_width, int(round(best_box[2] * scale_x)) + pad_x)
    bottom = min(image_height, int(round(best_box[3] * scale_y)) + pad_y)
    if right - left < 10 or bottom - top < 10:
        return None
    return left, top, right, bottom


def portrait_crop_rect(image_size, ratio, face=None) -> tuple[int, int, int, int]:
    """符合宽高比 ratio 的裁切范围。

    有人脸时让头顶与下巴落在参考线上、左右以人脸居中，范围不小于人脸的两倍宽，并收在画面以内；
    没有人脸时取与画面等宽（或等高）的最大范围，左右居中、上下偏上。
    """
    image_width, image_height = image_size
    if face is not None:
        x1, y1, x2, y2 = face
        face_width, face_height = max(1, x2 - x1), max(1, y2 - y1)
        head_top = max(0, int(round(y1 - face_height * PORTRAIT_GUIDE["head_top_pad_ratio"])))
        chin = min(image_height, int(round(y2 + face_height * PORTRAIT_GUIDE["chin_bottom_pad_ratio"])))
        span = max(0.01, PORTRAIT_GUIDE["chin_ratio"] - PORTRAIT_GUIDE["head_top_ratio"])
        height = max((chin - head_top) / span, face_height * 1.9, face_width * 2.0 / ratio)
        height = min(float(image_height), height)
        width = height * ratio
        if width > image_width:
            width = float(image_width)
            height = width / ratio
        width, height = max(1, int(round(width))), max(1, int(round(height)))
        left = int(round(x1 + face_width / 2 - width / 2))
        top = int(round(head_top - height * PORTRAIT_GUIDE["head_top_ratio"]))
        left = max(0, min(image_width - width, left))
        top = max(0, min(image_height - height, top))
        return left, top, left + width, top + height

    width = image_width
    height = int(round(width / ratio))
    if height > image_height:
        height = image_height
        width = int(round(height * ratio))
    width, height = max(1, min(image_width, width)), max(1, min(image_height, height))
    left = (image_width - width) // 2
    top = max(0, min(image_height - height, int(round((image_height - height) * 0.32))))
    return left, top, left + width, top + height
