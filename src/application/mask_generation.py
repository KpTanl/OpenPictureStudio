from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
from PIL import Image

from ..core.contracts.result import CoreError, CoreOperationError, ErrorCode
from ..core.contracts.tool_resources import RequiredResource
from ..core.face_regions import face_regions
from ..core.photo_engine import linear_to_srgb
from ..core.tools.managed_models import MANAGED_BEAUTY_PARAMETERS
from .managed_tool_modules import bind_model_resources


@dataclass(frozen=True)
class MaskGenerationTask:
    core: object
    session_id: str
    workflow: object
    node_id: str
    version: int
    source_identity: object
    tool_generation: int
    reference_size: tuple
    kind: str
    name: str
    operation: str
    model_identity: str
    paths: dict
    face_settings: dict
    face_reference: object
    model_key: object
    cancellation: object

    @classmethod
    def create(cls, core, session_id, node_id, reference_size, kind, name, operation, token, *, workflow, expected_version):
        from .output_writer import OperationCancellation
        checked = core.get_session(session_id)
        if not checked.success:
            raise CoreOperationError(checked.error)
        state = checked.data
        if state.state_version != expected_version:
            raise CoreOperationError(CoreError(ErrorCode.CANCELLED, "error.cancelled"))
        paths, face_settings, reference = {}, {}, None
        if kind == "subject":
            requirement = RequiredResource("subject", "model", "segmentation", ("u2net",), token)
            models, paths = bind_model_resources((requirement,), core.models, tool_id="color_adjust", implementation_id="mask.subject")
            if not core.models.snapshot(models["model"].key).usable:
                raise CoreOperationError(CoreError(ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing",
                                                   {"tool_id": "color_adjust", "model_token": token}))
            model_identity = "u2net:1:" + models["model"].files[0].digest
        else:
            face_settings = {**MANAGED_BEAUTY_PARAMETERS.defaults(3), "face_detection_model": token, "skin_smooth": 1}
            checked = core.tools.effect_reference("beauty", face_settings)
            if not checked.success:
                raise CoreOperationError(checked.error)
            reference = checked.data
            model_identity = reference.implementation_id + ":" + reference.implementation_version + ":" + dict(reference.resources).get("face_model", "")
        return cls(core, session_id, workflow, node_id, state.state_version,
                   core.source_identity(session_id), core.tools.generation, tuple(reference_size), kind,
                   name, operation, model_identity, paths, face_settings, reference,
                   models["model"].key if kind == "subject" else None, OperationCancellation())

    def is_current(self):
        return (not self.cancellation.is_cancelled()
                and self.core.matches_edit_state(self.session_id, self.version, self.source_identity)
                and self.core.tools.is_current(self.tool_generation)
                and self.core.image_sources.matches_file(self.source_identity))

    @property
    def commit_stamp(self):
        return self.version, self.tool_generation, self.source_identity

    def __call__(self, cancellation):
        cancelled = lambda: cancellation.is_cancelled() or not self.core.matches_edit_state(
            self.session_id, self.version, self.source_identity) or not self.core.tools.is_current(self.tool_generation)
        return self.core.analyze_node_input(self.session_id, self.workflow, self.node_id, self._generate,
                                           expected_version=self.version, is_cancelled=cancelled)

    def _generate(self, source, context, cancelled):
        if source.orig_size != self.reference_size:
            raise CancelledError()
        height, width = source.pixels.shape[:2]
        scale = min(1, 1600 / max(width, height))
        size = max(1, round(width * scale)), max(1, round(height * scale))
        estimate = width * height * (20 if source.alpha is not None else 0) + size[0] * size[1] * 160 + 256 * 1024 ** 2
        with self.core.image_sources.resources.working_memory(estimate, cancelled):
            rgb = source.pixels
            if source.alpha is not None:
                rgb = rgb * source.alpha[..., None] + 1 - source.alpha[..., None]
            rgb = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA) if size != (width, height) else rgb
            srgb = np.clip(linear_to_srgb(rgb), 0, 1)
            if self.kind == "subject":
                with Image.fromarray(np.rint(srgb * 255).astype(np.uint8)) as image:
                    weights = self._subject_weights(image, size, cancelled)
            else:
                snapshot = self.core.tools.analyze("beauty", source, self.face_settings, reference=self.face_reference,
                    purpose="snapshot", context=context, is_cancelled=cancelled)
                if not snapshot.success:
                    raise CoreOperationError(snapshot.error)
                if snapshot.data.processed == 0:
                    raise CoreOperationError(CoreError(ErrorCode.INVALID_INPUT, "beauty.detail." + snapshot.data.status))
                weights = np.zeros((size[1], size[0]), np.float32)
                for face in snapshot.data.faces:
                    if cancelled():
                        raise CancelledError()
                    if face.status != "ready":
                        continue
                    regions = face_regions(face.points, size)
                    left, top, right, bottom = regions.bounds
                    region = getattr(regions, self.kind)
                    if self.kind == "skin":
                        from ..core.beauty_v2 import _skin_color, linear_lab
                        lab = linear_lab(rgb[top:bottom, left:right])
                        region = region * _skin_color(lab)
                    np.maximum(weights[top:bottom, left:right], region, out=weights[top:bottom, left:right])
            if cancelled():
                raise CancelledError()
            digest = self.core.tools.mask_assets.put(weights, cancelled)
            return {"id": str(uuid4()), "name": self.name, "enabled": True, "op": self.operation,
                    "type": self.kind, "asset": digest, "size": list(size), "model": self.model_identity,
                    "source": self.source_identity.file_hash + ":" + context["upstream_identity"][1]}

    def _subject_weights(self, image, size, cancelled):
        from ..core.tools.segmentation import U2NetMask
        factory = lambda threads: U2NetMask(self.paths["model"], execution_threads=threads,
                                           create_session=self.core.inference.create_session)
        try:
            with self.core.models.borrow(self.model_key), self.core.tools.borrow_processor(
                    "color_adjust", "mask.subject", factory, resources=self.paths, is_cancelled=cancelled,
                    memory_bytes=sum(Path(path).stat().st_size * 4 for path in self.paths.values()) + 16 * 1024 ** 2) as processor:
                return cv2.resize(processor.weights(image), size, interpolation=cv2.INTER_LINEAR)
        except OSError as error:
            raise CoreOperationError(CoreError(ErrorCode.TOOL_RESOURCE_MISSING, "error.tool.resource_missing",
                                               {"tool_id": "color_adjust", "resource_id": "model"})) from error
        except ValueError as error:
            raise CoreOperationError(CoreError(ErrorCode.MODEL_LOAD_FAILED, "mask.generation_failed")) from error
