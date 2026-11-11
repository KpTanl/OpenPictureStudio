from dataclasses import replace

from ..core.contracts.gpu import DeviceModelSpec, GpuExecutionError
from .gpu_geometry import GpuGeometry
from .gpu_legacy_ops import GpuLegacyOps


class GpuDenoise:
    def __init__(self, backend, model, layout, cancelled):
        self.backend, self.model, self.layout, self.cancelled = backend, model, layout, cancelled
        self.ops = GpuLegacyOps(backend, cancelled)

    @staticmethod
    def tile_spec(layout):
        incoming, _ = layout
        fixed_h, fixed_w = incoming[2:]
        tile = max(64, min(512, fixed_h, fixed_w)) if fixed_h and fixed_w else 512
        overlap = min(24, tile//4)
        return tile, overlap, max(8, tile-overlap*2)

    def channel(self, image, method, strength, luma, version):
        if not strength:
            return image
        ops = self.ops
        amount = strength/100
        if method == "gaussian":
            import cv2
            if version == 3:
                sigma = .35+.65*amount if luma else .5+amount
                count = round(sigma*8+1) | 1
            else:
                sigma = .2+amount*(4.8 if luma else 3)
                count = max(3, min(31, round(sigma*6) | 1))
            weights = cv2.getGaussianKernel(count, sigma, cv2.CV_64F).ravel().tolist()
            filtered = image
            for axis in (0, 1):
                filtered = ops.run(filtered, 15, args=(axis, count//2), data=weights)
        elif method == "median":
            count = (3 if luma else 5) if version == 3 else max(1, min(11, 1+int(strength/10)*2))
            filtered = image if count == 1 else ops.run(image, 16, args=(count//2,))
        else:
            if version == 3:
                radius = 2 if luma else 3
                color = (3+17*amount if luma else 6+34*amount)/255
                space = 1+2*amount
            else:
                radius = max(3, min(15, 5+int(strength/20)*2))//2
                color = 10+strength*(2.8 if luma else 1.9)
                space = 8+strength*(1.3 if luma else .9)
            filtered = ops.run(image, 17, args=(radius, color, space))
        return ops.run(filtered, 18, auxiliary=image, args=(amount if version == 3 else 1, int(version == 3)))

    def ai(self, source, image):
        ops = self.ops
        incoming, outputs = self.layout
        if (incoming[:2] not in ((1, 3), (0, 3)) or len(outputs) != 1
                or outputs[0][:2] not in ((1, 3), (0, 3))):
            raise GpuExecutionError("model_layout", "Legacy denoise requires an RGB image model")
        fixed_h, fixed_w = incoming[2:]
        tile, overlap, stride = self.tile_spec(self.layout)
        width, height = image.resident.carrier.size
        small = width <= tile and height <= tile
        total = ops.zero(image)
        for top in range(0, height, height if small else stride):
            for left in range(0, width, width if small else stride):
                right, bottom = min(width, left+tile), min(height, top+tile)
                actual_w, actual_h = right-left, bottom-top
                run_size = fixed_w or actual_w, fixed_h or actual_h
                if actual_w > run_size[0] or actual_h > run_size[1]:
                    raise GpuExecutionError("model_layout", "Legacy denoise tile exceeds the model input")
                spec = DeviceModelSpec(run_size, run_size, source_colorspace="srgb", destination_colorspace="srgb")
                patch = ops.run(image, 20, size=run_size, args=(left, top, actual_w, actual_h))
                template = replace(GpuGeometry.updated(source, patch, size=run_size, alpha=False), colorspace="srgb")
                prediction = self.model("denoise_model", spec).run(template, patch.resident.identity, self.cancelled).gpu_image
                summary = ops.finite(prediction)
                prediction = ops.run(prediction, 21, auxiliary=summary)
                prediction = ops.view(prediction, (0, 0, actual_w, actual_h))
                total = ops.run(total, 22, auxiliary=prediction, args=(left, top, actual_w, actual_h, 0 if small else overlap))
        return ops.run(total, 23)

    def execute(self, source, params, version):
        if not any(params[key] for key in ("strength", "color_strength", "sharpen")):
            return source
        ops = self.ops
        image = ops.encoded(source)
        if params["strength"] or params["color_strength"]:
            if params["method"] == "ai":
                restored = self.ai(source, image)
                original = ops.run(image, 12)
                restored = ops.run(restored, 12)
                mixed = ops.run(original, 24, auxiliary=restored, args=(params["strength"]/100, params["color_strength"]/100))
                image = ops.run(mixed, 13)
            else:
                original = ops.run(image, 12, args=(int(version == 3),))
                channels = [self.channel(ops.channel(original, index), params["method"], strength, index == 0, version)
                            for index, strength in enumerate((params["strength"], params["color_strength"], params["color_strength"]))]
                mixed = ops.run(channels[0], 14, auxiliary=channels[1], field=channels[2], extra=original)
                image = ops.run(mixed, 13, args=(int(version == 3),))
        if params["sharpen"]:
            image = ops.run(image, 19, args=(params["sharpen"]/100,))
        result = ops.run(image, 25, auxiliary=source.gpu_image)
        return GpuGeometry.updated(source, result)
