from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass

from ..core.contracts.tools import ToolReference
from ..core.contracts.result import MEMORY_AVAILABLE_KEY, MEMORY_BUDGET_KEY
from .assistant_images import (MIN_REGION_SIDE, OBSERVATION_IMAGE_LIMIT, REQUEST_IMAGE_BYTES,
                               ImageMemory, ImageObservationError, jpeg_information)
from .assistant_models import IMAGE_SOURCES, AssistantContext, AssistantModel, context_matches
from .photo_analysis import encode_jpeg


logger = logging.getLogger(__name__)


def geometry_identity(workflow, descriptors):
    geometry = []
    for node in workflow.nodes:
        if node.node_type in ("source", "export"):
            continue
        if node.node_type == "tool":
            reference = ToolReference.from_dict(node.params)
            matched = [item for item in descriptors if item.reference == reference]
        else:
            matched = [item for item in descriptors if item.implementation_id == node.node_type
                       or node.node_type == "studio_effect" and item.tool_id == node.params.get("tool")]
        if matched and all(item.image.geometry == "preserve" for item in matched):
            continue
        geometry.append({"type": node.node_type, "version": node.version, "parameters": node.params})
    return hashlib.sha256(json.dumps(geometry, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def frame_descriptor(photo, workflow, source, size, geometry, *, run_id="", goal_version=0, regions=()):
    data = {"source": source, "session_id": photo.session_id, "object_key": photo.object_key,
            "source_hash": photo.source_hash, "revision": "original" if source == "original" else photo.revision_id,
            "workflow_hash": workflow.workflow_hash(), "full_size": list(size), "geometry": geometry,
            "run_id": run_id, "goal_version": goal_version}
    identity = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {**data, "frame_id": identity, "face_regions": [list(box) for box in regions]}


@dataclass(frozen=True)
class ObservationSource:
    name: str
    photo: AssistantContext
    workflow: object
    size: tuple[int, int]


@dataclass(frozen=True)
class ObservationRegion:
    name: str
    box: tuple[float, float, float, float]


@dataclass(frozen=True)
class ObservationRequest:
    photo: AssistantContext
    sources: tuple[ObservationSource, ...]
    regions: tuple[ObservationRegion, ...]
    quality: str
    model: AssistantModel
    run_id: str
    goal_version: int


class PhotoObservationService:
    def __init__(self, core, face_detector=None):
        self.core, self.face_detector = core, face_detector

    def observe(self, request, *, is_cancelled, progress=lambda _stage: None):
        memory = ImageMemory(self.core.image_sources.resources)
        try:
            return self._observe(request, is_cancelled=is_cancelled, progress=progress, memory=memory)
        finally:
            memory.close()

    def _observe(self, request, *, is_cancelled, progress, memory):
        def valid():
            if is_cancelled():
                raise ImageObservationError("AI_CANCELLED")
            state = self.core.get_session(request.photo.session_id)
            if not state.success or not context_matches(request.photo, state.data):
                raise ImageObservationError("AI_PHOTO_CHANGED")

        valid()
        count = max(1, len(request.regions)) * len(request.sources)
        if count > OBSERVATION_IMAGE_LIMIT:
            raise ImageObservationError("AI_OBSERVATION_COUNT", max_images=OBSERVATION_IMAGE_LIMIT,
                                        max_regions=OBSERVATION_IMAGE_LIMIT // len(request.sources), requested=count)
        effective_long_edge = request.model.effective_image_long_edge
        if request.quality == "native" and (not request.regions or not effective_long_edge):
            raise ImageObservationError("AI_OBSERVATION_CAPABILITY", effective_long_edge=effective_long_edge,
                                        regions_required=True)
        progress("observing_prepare")
        descriptors = self.core.tools.descriptors()
        geometries = [geometry_identity(source.workflow, descriptors) for source in request.sources]
        if request.regions and len(set(geometries)) != 1:
            raise ImageObservationError("AI_OBSERVATION_GEOMETRY", sources=[source.name for source in request.sources])
        mixed_preview = (request.quality == "preview" and bool(request.regions) and len(request.sources) > 1
                         and len({self.core.tools.supports_proxy(source.workflow, canonical_inputs=True)
                                  for source in request.sources}) > 1)
        render_full = request.quality == "native" or mixed_preview
        frames, generated = [], []
        total_bytes, aligned_size, aligned_pixels = 0, None, None
        for source, geometry in zip(request.sources, geometries):
            valid()
            edge, quality = IMAGE_SOURCES["original" if source.name == "original" else "current"]
            if request.regions and len(request.sources) > 1:
                edge = min(value[0] for value in IMAGE_SOURCES.values())
            progress("observing_render")
            scale = 1 if render_full else min(1, edge / max(source.size))
            pixels = math.ceil(source.size[0] * scale) * math.ceil(source.size[1] * scale)
            frame_memory = self.core.image_sources.resources.hold_memory(pixels * 4, is_cancelled)
            extra_memory = None
            try:
                rendered = self.core.preview(request.photo.session_id, source.workflow, update_draft=False,
                                             max_dimension=None if render_full else edge,
                                             expected_state=request.photo.state_version, is_cancelled=is_cancelled)
            except BaseException:
                frame_memory.close()
                raise
            if not rendered.success:
                frame_memory.close()
                code = rendered.error.code
                if code == "CANCELLED":
                    code = "AI_CANCELLED"
                elif rendered.error.message_key in (MEMORY_AVAILABLE_KEY, MEMORY_BUDGET_KEY):
                    code = "AI_OBSERVATION_MEMORY"
                raise ImageObservationError(code)
            try:
                image = rendered.data.image
                if image.width * image.height > pixels:
                    extra_memory = self.core.image_sources.resources.hold_memory((image.width * image.height - pixels) * 4,
                                                                                 is_cancelled)
                full_size = rendered.data.full_size or image.size
                if request.quality == "native" and tuple(full_size) != image.size:
                    raise ImageObservationError("AI_OBSERVATION_RESOLUTION", source=source.name,
                                                full_size=list(full_size), rendered_size=list(image.size))
                if mixed_preview and max(image.size) > edge:
                    with self.core.image_sources.resources.working_memory(image.width * image.height * 4, is_cancelled):
                        with self.core.image_sources.compute.borrow(is_cancelled, threads=1):
                            image.thumbnail((edge, edge))
                if request.regions and aligned_size is not None and tuple(full_size) != aligned_size:
                    raise ImageObservationError("AI_OBSERVATION_GEOMETRY", source=source.name,
                                                full_size=list(full_size), expected_size=list(aligned_size))
                if request.regions and aligned_pixels is not None and image.size != aligned_pixels:
                    raise ImageObservationError("AI_OBSERVATION_GEOMETRY", source=source.name,
                                                rendered_size=list(image.size), expected_size=list(aligned_pixels))
                aligned_size = tuple(full_size)
                aligned_pixels = image.size
                valid()
                boxes = request.regions or (ObservationRegion("whole", (0, 0, 1, 1)),)
                pixel_boxes = []
                for region in boxes:
                    left, top, right, bottom = region.box
                    box = (math.floor(left * image.width), math.floor(top * image.height),
                           math.ceil(right * image.width), math.ceil(bottom * image.height))
                    size = (box[2] - box[0], box[3] - box[1])
                    if request.regions and (min(size) < MIN_REGION_SIDE or request.quality == "native"
                                           and max(size) > effective_long_edge):
                        raise ImageObservationError("AI_OBSERVATION_SIZE", source=source.name, region=region.name,
                                                    size=list(size), min_side=MIN_REGION_SIDE,
                                                    effective_long_edge=effective_long_edge)
                    pixel_boxes.append(box)
                regions = ()
                if not request.regions and self.face_detector is not None:
                    try:
                        from .beauty_analysis import query_face_regions
                        detected = query_face_regions(self.face_detector, image, dict(source.photo.beauty), is_cancelled,
                                                      photo=source.photo, workflow=source.workflow)
                    except Exception:
                        valid()
                        logger.exception("助手的人脸检测失败，本轮按未知处理")
                        detected = None
                    if isinstance(detected, (list, tuple)):
                        regions = detected
                valid()
                working = max((box[2] - box[0]) * (box[3] - box[1]) * 12 for box in pixel_boxes)
                with self.core.image_sources.resources.working_memory(working, is_cancelled):
                    with self.core.image_sources.compute.borrow(is_cancelled, threads=1):
                        frame = frame_descriptor(source.photo, source.workflow, source.name, full_size, geometry,
                                                 run_id=request.run_id, goal_version=request.goal_version, regions=regions)
                        frames.append(frame)
                        encoded = []
                        progress("observing_encode")
                        for region, box in zip(boxes, pixel_boxes):
                            valid()
                            size = (box[2] - box[0], box[3] - box[1])
                            with image.crop(box) as crop:
                                data = encode_jpeg(crop, max(size) if request.quality == "native" else edge,
                                                   95 if request.quality == "native" else quality,
                                                   subsampling=0 if request.quality == "native" else None)
                            valid()
                            total_bytes += len(data)
                            if total_bytes > REQUEST_IMAGE_BYTES:
                                raise ImageObservationError("AI_IMAGE_BUDGET", bytes=total_bytes, max_bytes=REQUEST_IMAGE_BYTES)
                            memory.resize(total_bytes * 2, is_cancelled)
                            info = {**jpeg_information(data), "source": source.name, "frame_id": frame["frame_id"],
                                    "region": region.name, "full_size": list(full_size), "box": list(box),
                                    "quality": request.quality, "goal_version": request.goal_version,
                                    "revision": frame["revision"], "workflow_hash": frame["workflow_hash"],
                                    "jpeg_quality": 95 if request.quality == "native" else quality,
                                    "subsampling": "4:4:4" if request.quality == "native" else "default"}
                            encoded.append((data, info))
                        generated.append(encoded)
            finally:
                rendered.data.close()
                if extra_memory is not None:
                    extra_memory.close()
                frame_memory.close()
        valid()
        images, information = [], []
        identity = hashlib.sha256((request.run_id + ":" + str(request.goal_version) + ":"
                                   + ":".join(frame["frame_id"] for frame in frames)).encode("utf-8")).hexdigest()
        for index in range(max(1, len(request.regions))):
            for encoded in generated:
                data, info = encoded[index]
                images.append(data)
                information.append({**info, "group": identity + ":" + str(index)})
        return {"frames": frames, "images": information, "image_count": len(images),
                "quality": request.quality, "effective_long_edge": effective_long_edge}, images
