from __future__ import annotations

from concurrent.futures import CancelledError
from math import ceil, floor
import hashlib
import logging

import cv2
import numpy as np

from .contracts.beauty import FaceAnalysisSnapshot, FaceLandmarks
from .face_regions import OVAL, eye_geometry
from .photo_engine import linear_to_srgb


DETECTION_SPEC = (1, 1600, 5, 0.5, 0.5, "area-linear-white")
DETECTION_SPEC_V2 = (2, *DETECTION_SPEC[1:], (0.0, 0.25, 0.5), 0.5, 0.5, 0.35, 0.02, 1.6, 5)
# 规格 3：整图关键点一次，再由 YuNet 在同一检测图上找脸，逐框居中裁切交给关键点模型。
# 第 6 项起依次为：定位器、定位得分门槛、去重框 IoU、去重五点均方根（倍眼距）、定位 NMS、
# 检测图尺度下框长边下限、裁切倍数、候选框上限、交付人脸上限。第 8、9 项与规格 2 同位同义。
DETECTION_SPEC_V3 = (3, *DETECTION_SPEC[1:], "yunet", 0.6, 0.5, 0.35, 0.3, 16, (1.5, 2.0, 3.0), 60, 50)
DETECTION_SPECS = {1: DETECTION_SPEC, 2: DETECTION_SPEC_V2, 3: DETECTION_SPEC_V3}
logger = logging.getLogger(__name__)


def preparation_memory(source, *, detection_spec=DETECTION_SPEC):
    pixels = source.pixels.shape[0] * source.pixels.shape[1]
    memory = (pixels * 20 if source.alpha is not None else 0) + min(pixels, 1600 ** 2) * 48 + 64 * 1024 * 1024
    if detection_spec[0] == 2:
        memory += 11 * 5 * 478 * 128
    elif detection_spec[0] == 3:
        memory += min(pixels, 1600 ** 2) * 24 + (1 + detection_spec[13]) * 5 * 478 * 128
    return memory


def preparation_identity(source, context, reference, *, detection_spec=DETECTION_SPEC):
    origin, prefix = context.get("upstream_identity", ((), ""))
    if not origin:
        digest = hashlib.sha256()
        for start in range(0, source.pixels.shape[0], 64):
            if context["is_cancelled"]():
                raise CancelledError()
            digest.update(np.ascontiguousarray(source.pixels[start:start + 64]).data)
            if source.alpha is not None:
                digest.update(np.ascontiguousarray(source.alpha[start:start + 64]).data)
        origin = (digest.hexdigest(), source.source_format, source.color_pipeline)
    return (origin, prefix, source.orig_size, source.colorspace,
            source.white_point, reference, detection_spec)


class BeautyDetector:
    def __init__(self, model_path, *, detection_spec=DETECTION_SPEC, locator_path=None):
        if detection_spec not in DETECTION_SPECS.values():
            raise ValueError("人脸检测规格不受支持")
        if (detection_spec[0] == 3) != (locator_path is not None):
            raise ValueError("人脸定位模型与检测规格不匹配")
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
        self._mp = mp
        self._spec = detection_spec
        self._locator = (cv2.FaceDetectorYN.create(str(locator_path), "", (320, 320), detection_spec[7],
                                                   detection_spec[10], 5000) if locator_path is not None else None)
        self._detector = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.IMAGE, num_faces=self._spec[2],
            min_face_detection_confidence=self._spec[3], min_face_presence_confidence=self._spec[4]))

    def prepare(self, source, identity, cancelled):
        if source.is_proxy:
            raise ValueError("人脸快照需要完整上游输入")
        if self._spec[0] == 2:
            return self._prepare_multiscale(source, identity, cancelled)
        if self._spec[0] == 3:
            return self._prepare_located(source, identity, cancelled)
        if cancelled():
            raise CancelledError()
        height, width = source.pixels.shape[:2]
        scale = min(1.0, DETECTION_SPEC[1] / max(width, height))
        size = max(1, floor(width * scale + 0.5)), max(1, floor(height * scale + 0.5))
        # 先在线性空间合成白底，再缩小及编码；检测图不进入效果输出。
        rgb = source.pixels
        if source.alpha is not None:
            rgb = rgb * source.alpha[..., None] + (1 - source.alpha[..., None])
        small = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA) if size != (width, height) else rgb
        image = np.ascontiguousarray(np.rint(np.clip(linear_to_srgb(small), 0, 1) * 255).astype(np.uint8))
        if cancelled():
            raise CancelledError()
        result = self._detector.detect(self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=image))
        if cancelled():
            raise CancelledError()
        faces = []
        for landmarks in result.face_landmarks:
            points = tuple((float(point.x), float(point.y)) for point in landmarks)
            array = np.asarray(points, np.float32)
            status = "ready"
            if len(points) < 468 or not np.isfinite(array).all():
                status = "insufficient_landmarks"
            else:
                pixels = array * size
                _, distance = eye_geometry(pixels)
                area = abs(cv2.contourArea(pixels[list(OVAL)].astype(np.float32)))
                if distance < 8:
                    status = "face_too_small"
                elif (area < distance * distance * 0.1 or np.any(array.max(axis=0) <= 0)
                      or np.any(array.min(axis=0) >= 1)):
                    status = "insufficient_landmarks"
            faces.append(FaceLandmarks(points, status))
        faces.sort(key=lambda face: (sum(p[0] for p in face.points) / max(1, len(face.points)),
                                     sum(p[1] for p in face.points) / max(1, len(face.points)),
                                     float(np.ptp(np.asarray(face.points), axis=0).prod()) if face.points else 0))
        status = "ready" if any(face.status == "ready" for face in faces) else (
            faces[0].status if faces else "no_face")
        return FaceAnalysisSnapshot(identity, (width, height), size, tuple(faces), status)

    def _detection_input(self, source, bounds):
        """完整上游的一块区域：线性空间合成白底、面积缩小到长边不超过规格值，再编码为 sRGB 8 位。"""
        left, top, right, bottom = bounds
        width, height = right - left, bottom - top
        scale = min(1.0, self._spec[1] / max(width, height))
        size = max(1, floor(width * scale + 0.5)), max(1, floor(height * scale + 0.5))
        rgb = source.pixels[top:bottom, left:right]
        if source.alpha is not None:
            alpha = source.alpha[top:bottom, left:right, None]
            rgb = rgb * alpha + (1 - alpha)
        small = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA) if size != (width, height) else rgb
        return np.ascontiguousarray(np.rint(np.clip(linear_to_srgb(small), 0, 1) * 255).astype(np.uint8)), size

    def _landmarks(self, image, cancelled):
        if cancelled():
            raise CancelledError()
        result = self._detector.detect(self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=image))
        if cancelled():
            raise CancelledError()
        return result.face_landmarks

    def _detect_window(self, source, bounds, cancelled):
        if cancelled():
            raise CancelledError()
        image, size = self._detection_input(source, bounds)
        return self._landmarks(image, cancelled), size

    @staticmethod
    def _qualify(points, size):
        array = np.asarray(points, np.float32)
        status = "ready"
        if len(points) < 468 or not np.isfinite(array).all():
            status = "insufficient_landmarks"
        else:
            pixels = array * size
            _, distance = eye_geometry(pixels)
            area = abs(cv2.contourArea(pixels[list(OVAL)].astype(np.float32)))
            if distance < 8:
                status = "face_too_small"
            elif (area < distance * distance * 0.1 or np.any(array.max(axis=0) <= 0)
                  or np.any(array.min(axis=0) >= 1)):
                status = "insufficient_landmarks"
        return FaceLandmarks(points, status)

    def _same_face(self, first, second, size):
        a, b = np.asarray(first.points) * size, np.asarray(second.points) * size
        al, at = a[list(OVAL)].min(axis=0)
        ar, ab = a[list(OVAL)].max(axis=0)
        bl, bt = b[list(OVAL)].min(axis=0)
        br, bb = b[list(OVAL)].max(axis=0)
        intersection = max(0, min(ar, br) - max(al, bl)) * max(0, min(ab, bb) - max(at, bt))
        union = (ar - al) * (ab - at) + (br - bl) * (bb - bt) - intersection
        if union <= 0 or intersection / union < self._spec[8]:
            return False
        anchors = [33, 263, 1, 61, 291]
        distance = float(np.sqrt(np.mean(np.sum((a[anchors] - b[anchors]) ** 2, axis=1))))
        eye_distance = min(eye_geometry(a)[1], eye_geometry(b)[1])
        return distance <= self._spec[9] * eye_distance

    @staticmethod
    def _face_order(face):
        array = np.asarray(face.points)
        return (float(array[:, 0].mean()), float(array[:, 1].mean()), float(np.ptp(array, axis=0).prod()))

    def _prepare_multiscale(self, source, identity, cancelled):
        height, width = source.pixels.shape[:2]
        scale = min(1.0, self._spec[1] / max(width, height))
        size = max(1, floor(width * scale + 0.5)), max(1, floor(height * scale + 0.5))
        whole = (0, 0, width, height)
        windows = [whole]
        starts, fraction, _, _, border = self._spec[6:11]
        crop_width = max(1, floor(width * fraction + 0.5))
        crop_height = max(1, floor(height * fraction + 0.5))
        for y in starts:
            for x in starts:
                left = min(width - crop_width, floor(width * x + 0.5))
                top = min(height - crop_height, floor(height * y + 0.5))
                window = (left, top, left + crop_width, top + crop_height)
                if window not in windows:
                    windows.append(window)
        candidates = []
        returned = calls = 0
        for index, window in enumerate(windows):
            landmarks, detection_size = self._detect_window(source, window, cancelled)
            calls += 1
            returned += len(landmarks)
            left, top, right, bottom = window
            for face_index, values in enumerate(landmarks):
                raw = tuple((float(point.x), float(point.y)) for point in values)
                array = np.asarray(raw)
                if len(raw) < 468 or not np.isfinite(array).all():
                    continue
                low, high = array[list(OVAL)].min(axis=0), array[list(OVAL)].max(axis=0)
                margins = [float(low[0]) if left > 0 else 1.0, float(low[1]) if top > 0 else 1.0,
                           float(1 - high[0]) if right < width else 1.0,
                           float(1 - high[1]) if bottom < height else 1.0]
                margin = min(margins)
                if index and margin < border:
                    continue
                points = raw if not index else tuple(((x * (right - left) + left) / width,
                    (y * (bottom - top) + top) / height) for x, y in raw)
                face = self._qualify(points, size)
                priority = (face.status == "ready", index == 0, margin,
                            eye_geometry(array * detection_size)[1], -index, -face_index)
                matching = [item for item in candidates if self._same_face(face, item[0], size)]
                best = max([(face, priority), *matching], key=lambda item: item[1])
                candidates = [item for item in candidates if item not in matching]
                candidates.append(best)
            if sum(face.status == "ready" for face, _ in candidates) >= self._spec[2]:
                break
        if cancelled():
            raise CancelledError()
        candidates.sort(key=lambda item: (item[0].status == "ready", item[1][1],
            float(np.ptp(np.asarray(item[0].points), axis=0).prod()), tuple(-x for x in self._face_order(item[0]))),
            reverse=True)
        faces = []
        confirmations = rejected = 0
        for face, priority in candidates[:self._spec[2]]:
            if face.status == "ready" and not priority[1]:
                if confirmations >= self._spec[12]:
                    continue
                points = np.asarray(face.points) * (width, height)
                low, high = points.min(axis=0), points.max(axis=0)
                center, half = (low + high) / 2, (high - low) * (self._spec[11] / 2)
                left, top = map(int, np.maximum(0, np.floor(center - half)))
                right, bottom = map(int, np.minimum((width, height), np.ceil(center + half)))
                landmarks, _ = self._detect_window(source, (left, top, right, bottom), cancelled)
                calls += 1
                confirmations += 1
                matched = False
                for values in landmarks:
                    mapped = tuple(((float(point.x) * (right - left) + left) / width,
                        (float(point.y) * (bottom - top) + top) / height) for point in values)
                    confirmed = self._qualify(mapped, size)
                    if confirmed.status == "ready" and self._same_face(face, confirmed, size):
                        matched = True
                        break
                if not matched:
                    rejected += 1
                    continue
            faces.append(face)
        faces.sort(key=self._face_order)
        if not faces and returned:
            faces = [FaceLandmarks((), "insufficient_landmarks")]
        status = "ready" if any(face.status == "ready" for face in faces) else (faces[0].status if faces else "no_face")
        snapshot = FaceAnalysisSnapshot(identity, (width, height), size, tuple(faces), status)
        logger.debug("人脸检测 spec=%s input=%sx%s calls=%s confirmations=%s rejected=%s returned=%s processed=%s skipped=%s status=%s",
                     self._spec[0], width, height, calls, confirmations, rejected, returned,
                     snapshot.processed, snapshot.skipped, status)
        return snapshot

    @staticmethod
    def _oval_center(points, width, height):
        oval = np.asarray(points)[list(OVAL)] * (width, height)
        return (oval.min(axis=0) + oval.max(axis=0)) / 2

    def _prepare_located(self, source, identity, cancelled):
        height, width = source.pixels.shape[:2]
        score_floor, min_box, factors, max_boxes, max_faces = (self._spec[7], self._spec[11], self._spec[12],
                                                               self._spec[13], self._spec[14])
        if cancelled():
            raise CancelledError()
        image, size = self._detection_input(source, (0, 0, width, height))
        candidates = []
        whole = self._landmarks(image, cancelled)
        calls, returned, located = 1, len(whole), 0
        for values in whole:
            points = tuple((float(point.x), float(point.y)) for point in values)
            if len(points) < 468 or not np.isfinite(np.asarray(points)).all():
                continue
            face = self._qualify(points, size)
            if not any(self._same_face(face, other, size) for other in candidates):
                candidates.append(face)
        self._locator.setInputSize(size)
        _, found = self._locator.detect(cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        if cancelled():
            raise CancelledError()
        boxes = sorted((row for row in (() if found is None else found.tolist())
                        if row[14] >= score_floor and max(row[2], row[3]) >= min_box),
                       key=lambda row: -row[14])[:max_boxes]
        scale_x, scale_y = width / size[0], height / size[1]
        for row in boxes:
            x, y, w, h = row[0] * scale_x, row[1] * scale_y, row[2] * scale_x, row[3] * scale_y
            if any(x <= center[0] <= x + w and y <= center[1] <= y + h
                   for center in (self._oval_center(face.points, width, height) for face in candidates)):
                continue
            located += 1
            center_x, center_y = x + w / 2, y + h / 2
            for factor in factors:
                half = max(w, h) * factor / 2
                left, top = max(0, floor(center_x - half)), max(0, floor(center_y - half))
                right, bottom = min(width, ceil(center_x + half)), min(height, ceil(center_y + half))
                landmarks, _ = self._detect_window(source, (left, top, right, bottom), cancelled)
                calls += 1
                returned += len(landmarks)
                match = None
                for values in landmarks:
                    points = tuple(((float(point.x) * (right - left) + left) / width,
                                    (float(point.y) * (bottom - top) + top) / height) for point in values)
                    if len(points) < 468 or not np.isfinite(np.asarray(points)).all():
                        continue
                    face_x, face_y = self._oval_center(points, width, height)
                    if x <= face_x <= x + w and y <= face_y <= y + h:
                        match = self._qualify(points, size)
                        break
                if match is not None:
                    if not any(self._same_face(match, other, size) for other in candidates):
                        candidates.append(match)
                    break
        candidates.sort(key=lambda face: (face.status == "ready", float(np.ptp(np.asarray(face.points), axis=0).prod()),
                                          tuple(-value for value in self._face_order(face))), reverse=True)
        faces = sorted(candidates[:max_faces], key=self._face_order)
        if not faces and returned:
            faces = [FaceLandmarks((), "insufficient_landmarks")]
        status = "ready" if any(face.status == "ready" for face in faces) else (faces[0].status if faces else "no_face")
        snapshot = FaceAnalysisSnapshot(identity, (width, height), size, tuple(faces), status)
        logger.debug("人脸检测 spec=%s input=%sx%s calls=%s boxes=%s located=%s returned=%s processed=%s skipped=%s status=%s",
                     self._spec[0], width, height, calls, len(boxes), located, returned,
                     snapshot.processed, snapshot.skipped, status)
        return snapshot

    def close(self):
        self._locator = None
        self._detector.close()
