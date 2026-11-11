from math import ceil, floor

from ..core.contracts.gpu import DeviceTensorInput, DeviceTensorModelSpec, GpuExecutionError
from ..core.tools.id_photo import photo_bounds, sheet_positions, SHEET_SIZE, SHEET_BORDER, LAYOUT_SHEET
from ..core.photo_engine import srgb_to_linear
from .gpu_geometry import GpuGeometry
from .gpu_legacy_ops import GpuLegacyOps


class GpuSegmentation:
    def __init__(self, backend, model, layout, cancelled):
        self.backend, self.model, self.layout, self.cancelled = backend, model, layout, cancelled
        self.ops, self.geometry = GpuLegacyOps(backend, cancelled), GpuGeometry(backend)

    def mask(self, source):
        incoming, outputs = self.layout
        if (incoming not in ((1, 3, 320, 320), (0, 3, 320, 320))
                or any(shape != (1, 1, 320, 320) for shape in outputs)):
            raise GpuExecutionError("model_layout", "Unsupported segmentation tensor layout")
        ops = self.ops
        image = ops.resize_u8(ops.encoded(source), (320, 320))
        image = ops.run(image, 5, auxiliary=ops.reduce(image))
        spec = DeviceTensorModelSpec((320, 320), outputs, layout="nchw", input_colorspace="srgb", source_colorspace="srgb")
        tensors = self.model("model", spec).run(DeviceTensorInput(image, "srgb"), image.resident.identity, self.cancelled)
        mask = ops.run(tensors, 6, size=(320, 320))
        summary = ops.finite(mask)
        mask = ops.run(mask, 7, auxiliary=summary)
        return ops.resize_u8(mask, source.pixels.shape[1::-1])

    def background(self, source, params):
        ops = self.ops
        if params["mode"] == "ai":
            if source.is_proxy:
                raise GpuExecutionError("model_input", "Segmentation requires complete input")
            mask = self.mask(source)
        else:
            mask = ops.run(source, 8, args=(*params["target_color"], params["tolerance"]))
        ratio = min(source.pixels.shape[1]/source.orig_size[0], source.pixels.shape[0]/source.orig_size[1])
        mask = ops.blur_u8(mask, params["edge_radius"]*ratio)
        background = {"transparent": -1, "white": 1, "black": 0}[params["bg_type"]]
        result = ops.run(source, 9, auxiliary=mask, args=(background,))
        return self.geometry.updated(source, result, alpha=background < 0)

    def photo(self, source, params):
        if source.is_proxy:
            raise GpuExecutionError("model_input", "ID photo requires complete input")
        ops, geometry = self.ops, self.geometry
        size = params["custom_width"], params["custom_height"]
        width, height = source.pixels.shape[1::-1]
        bounds = photo_bounds(params["crop_rect"], (width, height), size[0]/size[1])
        left, top = max(0, floor(bounds[0])), max(0, floor(bounds[1]))
        right, bottom = max(left+1, min(width, ceil(bounds[2]))), max(top+1, min(height, ceil(bounds[3])))
        source = geometry.view(source, (left, top, right, bottom), (bounds, "id_photo"), self.cancelled,
                               formal_size=(right-left, bottom-top))
        if params["background"] is not None:
            if params["ai_matting"]:
                source = geometry.updated(source, ops.run(source, 27, auxiliary=self.mask(source)), alpha=True)
            if source.alpha is not None:
                source = geometry.updated(source, ops.run(source, 10, args=params["background"]), alpha=False)
        box = (max(0., bounds[0]-left), max(0., bounds[1]-top), min(float(right-left), bounds[2]-left), min(float(bottom-top), bounds[3]-top))
        source = geometry.resize(source, size, ("id_photo", size, box), self.cancelled, box)
        if params["layout"] != LAYOUT_SHEET:
            return source
        if source.alpha is not None:
            source = geometry.updated(source, ops.run(source, 10, args=(255, 255, 255)), alpha=False)
        import numpy as np
        border = srgb_to_linear(np.asarray(SHEET_BORDER, dtype=np.float32)/np.float32(255))
        positions = sheet_positions(size)
        result = ops.run(source, 11, size=SHEET_SIZE, args=(len(positions), *border), data=tuple(v for pos in positions for v in pos))
        return geometry.updated(source, result, size=SHEET_SIZE, alpha=False)
