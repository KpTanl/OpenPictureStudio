from dataclasses import replace
from math import prod, ceil

from ..core.contracts.gpu import GpuExecutionError
from ..core.tools.enhancement import execution_plan, required_models, _upscales, _final_plan, _banded
from .gpu_geometry import GpuGeometry
from .gpu_image_ops import GpuImageOps


class GpuEnhancement:
    def __init__(self, backend, models, session, cancelled):
        self.backend, self.models, self.session, self.cancelled = backend, models, session, cancelled
        self.ops, self.geometry = GpuImageOps(backend, cancelled), GpuGeometry(backend)

    @staticmethod
    def source(template, image, *, encoded=False, alpha=False):
        return replace(GpuGeometry.updated(template, image, size=image.resident.carrier.size, alpha=alpha),
                       colorspace="srgb" if encoded else "linear_srgb", is_proxy=False)

    def resize(self, template, image, size, *, box=None, alpha=False):
        value = self.source(template, image, alpha=alpha)
        return self.geometry.resize(value, size, (image.resident.identity, size, box, alpha), self.cancelled, box).gpu_image

    def alpha_rows(self, template, alpha, scale, top, bottom):
        width, height = alpha.resident.carrier.size
        first, last = max(0, top//scale-3), min(height, bottom//scale+4)
        part = self.ops.view(alpha, (0, first, width, last))
        box = (0, top//scale-first, width, bottom//scale-first)
        return self.resize(template, part, (width*scale, bottom-top), box=box)

    def rows(self, template, image, role):
        ops = self.ops
        descriptor = self.models[role]
        tile, pad, scale = descriptor.tile, descriptor.overlap, descriptor.scale
        if tile <= 2*pad or tile % descriptor.alignment or scale < 1 or role != "super_resolution" and scale != 1:
            raise GpuExecutionError("model_layout", "Unsupported restoration model layout")
        width, height = image.resident.carrier.size
        step = tile-2*pad
        session = self.session(role)
        for top in range(0, height, step):
            bottom = min(top+step, height)
            parts, positions = [], []
            for left in range(0, width, step):
                right = min(left+step, width)
                patch = ops.stage(image, 3, size=(tile, tile), kernel="enhance",
                    args=(left, top, right, bottom, pad))
                prediction = session.run(self.source(template, patch, encoded=True), (role, descriptor.key, patch.resident.identity), self.cancelled)
                count = ceil(prod(prediction.gpu_image.resident.carrier.size)/1024)
                finite = ops.stage(prediction, 5, size=(min(32768, count), ceil(count/32768)), kernel="enhance")
                if ops.count(finite):
                    raise GpuExecutionError("model_numeric", "Restoration model produced nonfinite output")
                cropped = ops.stage(prediction, 4, size=((right-left)*scale, (bottom-top)*scale),
                    kernel="enhance", args=(pad*scale, pad*scale))
                parts.append(cropped)
                positions.append((left*scale, 0))
            yield top*scale, ops.assemble(image, parts, positions, (width*scale, (bottom-top)*scale))

    def restoration(self, template, image, role):
        rows = list(self.rows(template, image, role))
        scale = self.models[role].scale
        width, height = image.resident.carrier.size
        return self.ops.assemble(image, [value for _, value in rows], [(0, top) for top, _ in rows], (width*scale, height*scale))

    def layout(self, source, image, plan, *, alpha):
        if plan.crop_box is not None or plan.content_size != plan.source_size:
            image = self.resize(source, image, plan.content_size, box=plan.crop_box, alpha=alpha)
        if any(plan.padding):
            image = self.ops.stage(image, 9, size=plan.output_size, kernel="enhance", args=plan.padding[:2])
            alpha = True
        return GpuGeometry.updated(source, image, size=plan.output_size, alpha=alpha)

    def upscale_banded(self, source, image, alpha, plan):
        ops = self.ops
        scale = self.models["super_resolution"].scale
        width, height = image.resident.carrier.size
        full_size = width*scale, height*scale
        out_width, out_height = plan.content_size
        resized = plan.crop_box is not None or plan.content_size != full_size
        left, top, right, bottom = plan.crop_box or (0., 0., float(full_size[0]), float(full_size[1]))
        step = (bottom-top)/out_height
        support = 3*max(step, 1.)
        window, window_start, done = None, 0, 0
        output, positions = [], []
        for band_top, band in self.rows(source, image, "super_resolution"):
            band_height = band.resident.carrier.size[1]
            band_bottom = band_top+band_height
            linear = ops.stage(band, 8, kernel="enhance")
            if alpha is not None:
                linear = ops.stage(linear, 10, auxiliary=self.alpha_rows(source, alpha, scale, band_top, band_bottom), kernel="enhance")
            if not resized:
                output.append(linear)
                positions.append((0, band_top))
                continue
            if window is None:
                window = linear
            else:
                previous_height = window.resident.carrier.size[1]
                window = ops.assemble(window, [window, linear], [(0, 0), (0, previous_height)], (full_size[0], previous_height+band_height))
            ready = done
            while ready < out_height and min(full_size[1], int(top+(ready+.5)*step+support+.5)) <= band_bottom:
                ready += 1
            if ready > done:
                box = (left, top+done*step-window_start, right, top+ready*step-window_start)
                output.append(self.resize(source, window, (out_width, ready-done), box=box, alpha=alpha is not None))
                positions.append((0, done))
                done = ready
            if done < out_height:
                keep = max(0, int(top+(done+.5)*step-support+.5))
                window_height = window.resident.carrier.size[1]
                if keep > window_start:
                    window = ops.view(window, (0, keep-window_start, full_size[0], window_height))
                    window_start = keep
        if resized and done != out_height:
            raise GpuExecutionError("model_layout", "Restoration row layout is incomplete")
        result = ops.assemble(image, output, positions, plan.content_size)
        return GpuGeometry.updated(source, result, size=plan.content_size, alpha=alpha is not None)

    def execute(self, source, params):
        if source.is_proxy:
            raise GpuExecutionError("model_input", "Restoration requires the complete input")
        ops = self.ops
        size = source.pixels.shape[1::-1]
        plan = execution_plan(size, params, self.models)
        if not required_models(params):
            return source
        upscale = _upscales(plan)
        if not upscale and not any(params[key] for key in ("luminance", "chroma", "deblur")):
            return self.layout(source, source.gpu_image, plan, alpha=source.alpha is not None)
        image = ops.stage(source, 0, kernel="enhance")
        alpha = ops.stage(source, 12, kernel="enhance") if source.alpha is not None else None
        if alpha is not None:
            blocks = ceil(prod(size)/1024)
            count = ops.stage(image, 13, size=(min(32768, blocks), ceil(blocks/32768)), kernel="enhance")
            hidden = ops.total(count)
            if hidden == prod(size):
                image = ops.stage(image, 14, kernel="enhance")
            elif hidden:
                nearest = self.backend.operate(image, "enhance.nearest", (), size,
                    lambda device, abort: device.nearest_visible(image.resident.carrier, abort), self.cancelled,
                    working_bytes=prod(size)*64)
                image = ops.stage(image, 2, auxiliary=nearest, kernel="enhance")
        if params["luminance"] or params["chroma"]:
            restored = self.restoration(source, image, "denoise")
            image = ops.stage(image, 6, auxiliary=restored, args=(params["luminance"]/100, params["chroma"]/100), kernel="enhance")
        if params["deblur"]:
            restored = self.restoration(source, image, "deblur")
            image = ops.stage(image, 7, auxiliary=restored, args=(params["deblur"]/100,), kernel="enhance")
        if not upscale:
            linear = ops.stage(image, 8, kernel="enhance")
            if alpha is not None:
                linear = ops.stage(linear, 10, auxiliary=alpha, kernel="enhance")
            return self.layout(source, linear, _final_plan(size, plan, params), alpha=alpha is not None)
        scale = self.models["super_resolution"].scale
        upscaled_size = size[0]*scale, size[1]*scale
        final = _final_plan(upscaled_size, plan, params)
        if _banded(upscaled_size, final):
            return self.upscale_banded(source, image, alpha, final)
        restored = self.restoration(source, image, "super_resolution")
        linear = ops.stage(restored, 8, kernel="enhance")
        if alpha is not None:
            alpha = self.resize(source, alpha, upscaled_size)
            linear = ops.stage(linear, 10, auxiliary=alpha, kernel="enhance")
        return self.layout(source, linear, final, alpha=alpha is not None)
