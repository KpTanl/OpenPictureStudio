from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


OVAL = (10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378,
        400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109)
EYES = ((33, 246, 161, 160, 159, 158, 157, 173, 133, 155, 154, 153, 145, 144, 163, 7),
        (362, 398, 384, 385, 386, 387, 388, 466, 263, 249, 390, 373, 374, 380, 381, 382))
BROWS = ((70, 63, 105, 66, 107, 55, 65, 52, 53, 46),
         (336, 296, 334, 293, 300, 276, 283, 282, 295, 285))
LIPS = (61, 185, 40, 39, 37, 0, 267, 269, 270, 409, 291, 375, 321, 405, 314, 17, 84, 181, 91, 146)
MOUTH = (78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95)
NOSTRILS = ((49, 48, 64, 98, 97), (279, 278, 294, 327, 326))
JAW = ((234, 93, 132, 58, 172, 136, 150), (454, 323, 361, 288, 397, 365, 379))


@dataclass(frozen=True)
class FaceRegions:
    bounds: tuple[int, int, int, int]
    points: np.ndarray
    distance: float
    skin: np.ndarray
    features: np.ndarray
    under_eyes: np.ndarray
    mouth: np.ndarray
    support: np.ndarray
    eyes: np.ndarray
    lips: np.ndarray


def eye_geometry(points):
    centers = np.array([points[list(eye)].mean(axis=0) for eye in EYES], dtype=np.float32)
    return centers, float(np.linalg.norm(centers[1] - centers[0]))


def face_bounds(points, size, margin=0.3):
    width, height = size
    pixels = np.asarray(points, np.float32) * (width, height)
    _, distance = eye_geometry(pixels)
    pad = distance * margin
    left, top = np.floor(pixels.min(axis=0) - pad).astype(int)
    right, bottom = np.ceil(pixels.max(axis=0) + pad + 1).astype(int)
    return max(0, left), max(0, top), min(width, right), min(height, bottom)


def face_regions(points, size):
    bounds = face_bounds(points, size)
    left, top, right, bottom = bounds
    pixels = np.asarray(points, np.float32) * size - (left, top)
    centers, distance = eye_geometry(pixels)
    shape = (bottom - top, right - left)
    feather = max(1.0, 0.03 * distance)
    expansion = max(1, int(round(0.02 * distance)))

    def polygon(indices):
        mask = np.zeros(shape, np.uint8)
        cv2.fillPoly(mask, [np.rint(pixels[list(indices)]).astype(np.int32)], 1)
        return mask

    def inward(mask):
        mask = mask.copy()
        mask[[0, -1], :] = 0
        mask[:, [0, -1]] = 0
        value = np.clip(cv2.distanceTransform(mask, cv2.DIST_L2, 5) / feather, 0, 1)
        return value * value * (3 - 2 * value)

    oval = polygon(OVAL)
    eyes = [polygon(eye) for eye in EYES]
    protected = np.maximum.reduce([*eyes, *(polygon(brow) for brow in BROWS), polygon(LIPS),
                                   *(polygon(nose) for nose in NOSTRILS)])
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * expansion + 1,) * 2)
    protected = cv2.dilate(protected, kernel)
    skin = inward(oval) * inward(1 - protected)
    mouth = polygon(MOUTH)
    lips = polygon(LIPS) * (1 - mouth)
    features = inward(np.maximum.reduce([*eyes, *(polygon(brow) for brow in BROWS), lips]))
    under = np.zeros(shape, np.uint8)
    direction = (centers[1] - centers[0]) / max(distance, 1e-6)
    normal = np.array([-direction[1], direction[0]], np.float32)
    if np.dot(normal, pixels[152] - centers.mean(axis=0)) < 0:
        normal = -normal
    angle = float(np.degrees(np.arctan2(direction[1], direction[0])))
    for eye in EYES:
        contour = pixels[list(eye)]
        center = contour.mean(axis=0)
        width = np.linalg.norm(contour[0] - contour[8])
        cv2.ellipse(under, tuple(np.rint(center + normal * (0.13 * distance)).astype(int)),
                    (max(1, round(width * 0.5)), max(1, round(distance * 0.11))), angle, 0, 360, 1, -1)
    under = inward(under) * skin
    support = inward(cv2.dilate(oval, cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * max(1, round(distance * 0.20)) + 1,) * 2)))
    return FaceRegions(bounds, pixels, distance, skin, features, under, inward(mouth), support,
                       inward(np.maximum.reduce(eyes)), inward(lips))
