from functools import lru_cache
import math
import struct

from ..core.contracts.gpu import GpuExecutionError
from ..core.tools.color import color_identity, is_color_neutral
from ..core.local_masks import LOCAL_KEYS, EFFECTIVE_RANGES, prepare_masks
from ..core.mask_components import prepare_components, brush_strokes
from ..core.looks import look_stages
from .gpu_geometry import GpuGeometry


IMPLEMENTATIONS = frozenset({("color_adjust", 1, "color_adjust", "legacy-1"),
    *(("color_adjust", version, "color_adjust.float", str(version)) for version in range(2, 8))})


def capable(implementation, settings):
    if implementation not in IMPLEMENTATIONS:
        return False
    components = [component for mask in settings.get("masks") or () for component in mask.get("components", ())]
    strokes = [stroke for component in components if component["type"] == "brush" for stroke in brush_strokes(component)]
    return max((len(stroke["points"]) for stroke in strokes),default=0) <= 512 and sum(len(stroke["points"]) for stroke in strokes) <= 4096


def workflow_cost(graph, largest):
    cost = 0
    for node in graph.nodes:
        if node.node_type != "tool" or node.params.get("tool_id") != "color_adjust":
            continue
        params = node.params["settings"]
        masks = [mask for mask in params.get("masks") or () if mask.get("enabled",True)]
        components = [component for mask in masks for component in mask.get("components", ()) if component["enabled"]]
        counts = [len(stroke["points"]) for component in components if component["type"] == "brush" for stroke in brush_strokes(component)]
        if largest * max(counts,default=0) > 256_000_000 or largest*len(components) > 2_000_000_000:
            raise ValueError("GPU 蒙版提交规模超限")
        cost += 12 + 12*len(masks) + 4*len(components) + sum(counts)/16
        cost += 18*sum(bool(params.get(key,0) or any(mask.get("params",{}).get(key,0) for mask in masks))
                       for key in ("shadows","highlights","texture","clarity","dehaze"))
    return cost


DEVICE_BLOCK_BYTES = 128 << 20


def packing(nbytes):
    """显存按 128 MiB 一块分（与 native 的 memory_hints 一致）：比块小的整幅图一块放下几张，剩下的空隙也被占着；
    比块大的单独分配，没有空隙。返回一张 nbytes 大的图实际占用的倍数。"""
    return 1 if nbytes > DEVICE_BLOCK_BYTES else DEVICE_BLOCK_BYTES / (DEVICE_BLOCK_BYTES // nbytes * nbytes)


def working_memory(graph, steps):
    """各调色步里最大的显存峰值。每像素的字节数按 3277×2048 上实测的峰值拟合，取偏大的一侧：
    不带滤波 100，每个蒙版 64，阴影高光 16，纹理、清晰度、去雾用到任一 64，细档和去雾的粗档都用到再加 48，
    蒙版里带滤波再加 44；降采样后算系数的中间结果每个低分辨率像素 160。"""
    from ..core.tools.color import color_engine_contract
    import ops_accel
    peak = 0
    for node in graph.nodes:
        if node.node_id not in steps or node.node_type != "tool" or node.params.get("tool_id") != "color_adjust":
            continue
        settings = node.params["settings"]
        version = node.params["params_version"]
        params = color_engine_contract(settings, version).normalize(settings, version).data
        width, height = steps[node.node_id][0]
        side = min(width, height)
        subsamples = []
        tone = fine = coarse = masked = False
        masks = [mask.get("params", {}) for mask in params.get("masks") or () if mask.get("enabled", True)]
        for number, local in enumerate((params, *masks)):
            filtered = False
            if local.get("shadows") or local.get("highlights"):
                subsamples.append(max(2, min(6, side//256)))
                tone = filtered = True
            texture, clarity, dehaze = ops_accel.color_detail_parameters(local)
            if texture or clarity:
                subsamples.append(max(1, min(4, side//512)))
                fine = filtered = True
            if clarity:
                subsamples.append(max(2, min(6, side//200)))
            if dehaze:
                subsamples.append(max(4, min(12, side//128)))
                coarse = filtered = True
            masked = masked or filtered and number > 0
        per_pixel = 100 + 64*len(masks) + 16*tone + 64*(fine or coarse) + 48*(fine and coarse) + 44*masked
        low_pixels = max((max(1, width//s)*max(1, height//s) for s in subsamples), default=0)
        peak = max(peak, width*height*per_pixel*packing(width*height*16) + low_pixels*160)
    return int(peak)


def _parameters(settings, version):
    from ..core.local_color import effective_globals
    params, masks = dict(settings), ()
    if version in (3, 4):
        masks = prepare_components(settings.get("masks")) if version == 4 else prepare_masks(settings.get("masks"))
    if masks:
        params = effective_globals(params)
    else:
        import ops_accel
        for key in ("temperature", "tint", "highlights", "shadows", "blacks", "whites", "vibrance"):
            params[key] = params.get(key, 0) if type(params.get(key, 0)) is int else 0
        params["texture"], params["clarity"], params["dehaze"] = ops_accel.color_detail_parameters(params)
        ev = max(-5, min(5, float(params.get("exposure", 0)))) if version == 1 else float(params.get("exposure", 0))
        brightness = float(params.get("brightness", 1))
        if brightness > 0 and brightness != 1:
            ev += math.log2(brightness)
        params["exposure"] = max(-10, min(10, ev))
    return params, masks


def strip_capable(settings):
    """能在 GPU 分条计算的调色。旧版本局部蒙版里的阴影、高光各自要一遍整幅滤波，不分条。"""
    _, version = color_identity(settings)
    return version not in (1, 2) or not any(int(mask["params"].get("shadows", 0)) or int(mask["params"].get("highlights", 0))
        for mask in settings.get("masks") or () if mask.get("enabled", True) and mask.get("params"))


def tone_filter(size):
    """阴影、高光用的引导滤波 (半径, eps, 降采样倍数)；size 是整幅短边。"""
    return max(4, size//48), float32(.04*.04), max(2, min(6, int(size/256)))


def detail_filters(size):
    """纹理和清晰度共用的细档、清晰度的中档、去雾的粗档。"""
    return ((max(2, size//256), float32(float32(.10)*float32(.10)), max(1, min(4, int(size/512)))),
            (max(4, size//24), float32(float32(.14)*float32(.14)), max(2, min(6, int(size/200)))),
            (max(8, size//8), float32(float32(.20)*float32(.20)), max(4, min(12, int(size/128)))))


def strip_filters(settings, size):
    """分条时这次调色要的引导滤波：(阴影高光那一个或 None, 细节的几个)。"""
    _, version = color_identity(settings)
    if is_color_neutral(settings, include_look=False):
        return None, ()
    params, masks = _parameters(settings, version)
    side = min(size)
    tone = tone_filter(side) if GpuColor.active(params, masks, ("shadows", "highlights")) else None
    active = tuple(GpuColor.active(params, masks, (key,)) for key in ("texture", "clarity", "dehaze"))
    fine, mid, coarse = detail_filters(side)
    details = tuple(spec for spec, used in ((fine, active[0] or active[1]), (mid, active[1]), (coarse, active[2])) if used)
    return tone, details


# 滤波之后才用到的参数：改它们不改滤波的输入，系数可以沿用
_AFTER_TONE_INPUT = ("shadows", "highlights", "blacks", "whites", "contrast", "vibrance", "saturation", "curves",
                     "color_mixer", "texture", "clarity", "dehaze", "sharpness")
_AFTER_DETAIL_INPUT = ("texture", "clarity", "dehaze", "sharpness")


def filter_input_settings(settings, stage):
    """去掉只在滤波之后才用到的参数，剩下的决定 stage（"tone" 或 "detail"）这一步滤波的输入。"""
    later = _AFTER_TONE_INPUT if stage == "tone" else _AFTER_DETAIL_INPUT
    result = {key: value for key, value in settings.items() if key not in later}
    if settings.get("masks"):
        result["masks"] = [{**mask, "params": {key: value for key, value in (mask.get("params") or {}).items()
                                               if key not in later}} for mask in settings["masks"]]
    return result


class FilterInput(Exception):
    """分条准备时算到了还没有系数的引导滤波：source 是它的输入这一条，spec 是 (半径, eps, 降采样倍数)。"""

    def __init__(self, source, spec):
        super().__init__(spec)
        self.source, self.spec = source, spec


@lru_cache(maxsize=2)
def _transfer(inverse):
    import ops_accel
    return tuple(ops_accel.GpuDevice.color_transfer_lut(inverse))


class GpuColor:
    def __init__(self, backend, assets=None, coefficients=None):
        """coefficients：分条时各引导滤波在整幅低分辨率图上算好的系数，按 (半径, eps, 降采样倍数) 取。"""
        self.backend, self.assets = backend, assets
        self.coefficients = coefficients or {}
        self._weights, self._keep = {}, False

    def stage(self, source, name, mode, cancelled, *, args=(), data=(), auxiliary=None, field=None, extra=None,
              source_dependent=True):
        artifact = self.backend.resolve(source.gpu_image, cancelled) if source_dependent else source.gpu_image
        supplied = auxiliary, field, extra
        others = tuple(self.backend.resolve(item.gpu_image if hasattr(item, "gpu_image") else item, cancelled)
                       if item is not None else artifact for item in supplied)
        data = tuple(float(value) for value in data)
        identity = (mode, tuple(args), data,
                    *(item.resident.identity if value is not None else () for item, value in zip(others, supplied)))
        size = source.pixels.shape[1], source.pixels.shape[0]
        frame = self.backend.strip_frame()
        result = self.backend.operate(artifact, "color." + name, identity, size, lambda device, abort:
            device.color_op(artifact.resident.carrier, *(item.resident.carrier for item in others),
                            mode, list(args), list(data), abort, frame), cancelled,
            input_identity=None if source_dependent else ())
        return GpuGeometry.updated(source, result)

    def weight(self, source, mask, cancelled, *, legacy=False):
        width, height = source.orig_size
        geometry = [float32(value) for value in mask.geometry]
        if mask.kind == 0:
            if not legacy:
                geometry = [float32(value * extent) for value, extent in zip(geometry, (width, height, width, height))]
            geometry += [0, 0, 0, 0]
        else:
            if not legacy:
                geometry[:4] = [float32(value * extent) for value, extent in zip(geometry, (width, height, width, height))]
            else:
                geometry[2:4] = [max(1e-4, value) for value in geometry[2:4]]
                geometry[5] = max(.01, min(1, geometry[5]))
            geometry += [0] * (8-len(geometry))
        return self.stage(source, "weight", 12, cancelled,
            args=(*geometry, mask.kind, float(legacy), 0, 0, width, height, 0, 0), source_dependent=False)

    def composite(self, source, mask, cancelled):
        result = self.stage(source, "mask.empty", 13, cancelled, source_dependent=False)
        for component in mask.components:
            if not component["enabled"]:
                continue
            kind = component["type"]
            if kind in ("linear", "radial"):
                geometry = {key: value for key, value in component.items() if key != "op"}
                geometry["params"] = {}
                weight = self.weight(source, prepare_masks([geometry], active_only=False)[0], cancelled)
            elif kind == "all":
                weight = self.stage(source, "mask.all", 9, cancelled, args=(1, 1, 1, 1), source_dependent=False)
            elif kind == "brush":
                weight = self.stage(source, "brush.empty", 13, cancelled, source_dependent=False)
                for stroke in brush_strokes(component):
                    width, height = source.orig_size
                    points = [v*extent for point in stroke["points"] for v, extent in zip(point, (width, height))]
                    part = self.stage(source, "brush.stroke", 15, cancelled, data=points,
                        args=(width, height, stroke["radius"]*min(width, height), stroke["hardness"],
                              len(stroke["points"]), stroke["opacity"]), source_dependent=False)
                    weight = self.stage(weight, "brush.union", 14, cancelled, auxiliary=part, args=(0,))
            else:
                if self.assets is None:
                    raise GpuExecutionError("mask_asset", "GPU mask asset store is unavailable")
                weight = self.backend.mask_asset(component, source, self.assets, cancelled)
                weight = self.stage(source, "asset.sample", 16, cancelled, auxiliary=weight, source_dependent=False)
            result = self.stage(result, "mask.compose", 14, cancelled, auxiliary=weight,
                                args=(int(component["op"] == "subtract"),))
        return result

    def mask_weight(self, source, mask, cancelled):
        """一个蒙版的权重，同一次调色里各组参数共用。分条时整幅一张跨帧保留，这时每 4 个像素存成一组。
        返回权重和是否这样存。"""
        held = self._weights.get(id(mask))
        if held is None or held[0] is not mask:
            def compute():
                return self.composite(source, mask, cancelled) if mask.kind == 2 else self.weight(source, mask, cancelled)
            frame = self.backend.strip_frame()
            if self._keep and frame is not None:
                key = (source.source_path, "color.mask.rows", self.backend._stages.key("gpu.mask.rows", (
                    mask.kind, getattr(mask, "geometry", None), getattr(mask, "components", None),
                    source.orig_size, frame[:2])), source.backend_identity)
                held = mask, self.backend.mask_rows(source, key, compute, cancelled), True
            else:
                held = mask, compute(), False
            self._weights[id(mask)] = held
        return held[1:]

    def fields(self, source, params, masks, keys, cancelled):
        pad = (0,) * (4-len(keys))
        defaults = tuple(float(params.get(key, 1 if key in ("contrast", "saturation") else 0)) for key in keys) + pad
        lows, highs = (tuple(values) + pad for values in zip(*(EFFECTIVE_RANGES[key] for key in keys))) if masks else ((), ())
        deltas = [delta for delta in (tuple(mask.delta[LOCAL_KEYS.index(key)] for key in keys) + pad for mask in masks)]
        active = [(mask, delta) for mask, delta in zip(masks, deltas) if any(delta)]
        if not active:
            if masks:
                defaults = tuple(float(min(max(float32(value), float32(low)), float32(high)))
                                 for value, low, high in zip(defaults, lows, highs))
            artifact = source.gpu_image
            def constant(device, abort):
                pixel = device.view(artifact.resident.carrier, 0, 0, 1, 1)
                return device.color_op(pixel, pixel, pixel, pixel, 9, list(defaults), [], abort)
            result = self.backend.operate(artifact, "color.field.base", ("constant", defaults), (1, 1),
                constant, cancelled, input_identity=())
            return GpuGeometry.updated(source, result)
        result = source
        for index, (mask, delta) in enumerate(active):
            last = index == len(active) - 1
            weight, packed = self.mask_weight(source, mask, cancelled)
            result = self.stage(result, "field.add", 10, cancelled, auxiliary=weight,
                args=(*delta, *defaults, int(index == 0), int(last), int(packed), 0,
                      *(lows if last else (0,) * 4), *(highs if last else (0,) * 4)),
                source_dependent=index > 0)
        return result

    def filtered(self, source, radius, eps, subsample, cancelled):
        artifact = source.gpu_image
        frame = self.backend.strip_frame()
        if frame is None:
            result = self.backend.operate(artifact, "color.filter", (radius, eps, subsample), source.pixels.shape[1::-1],
                lambda device, abort: device.guided(artifact.resident.carrier, artifact.resident.carrier, radius, eps,
                                                    subsample, abort), cancelled)
            return GpuGeometry.updated(source, result)
        spec = radius, eps, subsample
        held = self.coefficients.get(spec)
        if held is None:
            raise FilterInput(source, spec)
        result = self.backend.operate(artifact, "color.filter.apply", (spec, held.a.identity, held.b.identity),
            source.pixels.shape[1::-1], lambda device, abort: device.guided_apply(artifact.resident.carrier,
                held.a.carrier, held.b.carrier, held.center.carrier, eps, frame, abort), cancelled)
        return GpuGeometry.updated(source, result)

    @staticmethod
    def active(params, masks, keys):
        return any(params.get(key, 1 if key in ("contrast", "saturation") else 0)
            != (1 if key in ("contrast", "saturation") else 0)
            or any(mask.delta[LOCAL_KEYS.index(key)] for mask in masks) for key in keys)

    def tone(self, source, params, masks, cancelled):
        keys = ("shadows", "highlights", "blacks", "whites")
        if not self.active(params, masks, keys):
            return source
        need_base = self.active(params, masks, keys[:2])
        base = None
        if need_base:
            frame = self.backend.strip_frame()
            size = min(frame[:2]) if frame is not None else min(source.pixels.shape[:2])
            luma = self.stage(source, "tone.luma", 5, cancelled)
            base = self.filtered(luma, *tone_filter(size), cancelled)
        return self.stage(source, "tone", 6, cancelled, args=(int(need_base),), auxiliary=base,
                          field=self.fields(source, params, masks, keys, cancelled))

    def details(self, source, params, masks, cancelled):
        keys = ("texture", "clarity", "dehaze")
        active = tuple(self.active(params, masks, (key,)) for key in keys)
        if not any(active):
            return source
        frame = self.backend.strip_frame()
        size = min(frame[:2]) if frame is not None else min(source.pixels.shape[:2])
        fields = self.fields(source, params, masks, keys, cancelled)
        luma = self.stage(source, "detail.luma", 5, cancelled)
        target, fine = luma, None
        fine_filter, mid_filter, coarse_filter = detail_filters(size)
        if active[0] or active[1]:
            fine = self.filtered(luma, *fine_filter, cancelled)
        if active[0]:
            target = self.stage(target, "detail.texture", 7, cancelled, args=(0, 1), auxiliary=luma, extra=fine, field=fields)
        if active[1]:
            mid = self.filtered(luma, *mid_filter, cancelled)
            target = self.stage(target, "detail.clarity", 7, cancelled, args=(1, .8), auxiliary=fine, extra=mid, field=fields)
        if active[2]:
            coarse = self.filtered(luma, *coarse_filter, cancelled)
            target = self.stage(target, "detail.dehaze", 7, cancelled, args=(2, .5), auxiliary=luma, extra=coarse, field=fields)
        return self.stage(source, "details", 8, cancelled, auxiliary=target, field=fields)

    def execute(self, reference, source, settings, upstream_identity, cancelled):
        if not capable((reference.tool_id, reference.params_version, reference.implementation_id,
                        reference.implementation_version), settings) or source.gpu_image is None:
            raise GpuExecutionError("chain_capability_missing", "GPU color implementation is unavailable")
        from ..core.photo_engine import curve_lut
        from ..core.mixer_sampling import MIXER_CHANNELS, MIXER_CENTERS, MIXER_WIDTHS
        look, version = color_identity(settings)
        stages = look_stages(look, source.color_pipeline)
        if not stages and is_color_neutral(settings, include_look=False):
            return source
        params, masks = _parameters(settings, version)
        frame = self.backend.strip_frame()
        self._keep = bool(masks) and frame is not None and self.backend.keeps_masks(len(masks) * -(-frame[0] // 4) * 16 * frame[1])
        initial = self.fields(source, params, masks, ("temperature", "tint", "exposure"), cancelled)
        result = self.stage(source, "initialize", 0, cancelled, args=(1 if version == 1 else 2,), data=_transfer(False), field=initial)
        for stage in stages:
            result = self.stage(result, "look", 1, cancelled, args=(len(stage.tone_lut), len(stage.hue_sat), stage.tone_strength, stage.sat_strength),
                                data=(*stage.tone_lut, *stage.hue_sat))
        result = self.tone(result, params, masks, cancelled)
        offsets, lengths, curves = [], [], []
        for key in ("rgb", "r", "g", "b"):
            points = (params.get("curves") or {}).get(key)
            values = curve_lut(points).tolist() if points and list(map(tuple, points)) != [(0, 0), (255, 255)] else []
            offsets.append(len(curves))
            lengths.append(len(values))
            curves.extend(values)
        mixer = params.get("color_mixer") or {}
        channels = [key for key in MIXER_CHANNELS if any(mixer.get(key, {}).get(part, 0) for part in ("hue", "sat", "lum"))]
        data = [value for key in channels for value in (MIXER_CENTERS[key], MIXER_WIDTHS[key],
            *(mixer[key].get(part, 0) for part in ("hue", "sat", "lum")))]
        shaped = any(lengths) or self.active(params, masks, ("contrast",))
        mixed = bool(channels) or self.active(params, masks, ("vibrance", "saturation"))
        if shaped or mixed:
            fields = self.fields(source, params, masks, ("contrast", "vibrance", "saturation"), cancelled)
            if shaped:
                result = self.stage(result, "curves", 2, cancelled, args=(*offsets, *lengths), data=curves, field=fields)
            if mixed:
                result = self.stage(result, "mixer", 3, cancelled, args=(len(channels),), data=data, field=fields)
        result = self.details(result, params, masks, cancelled)
        result = self.stage(result, "finish", 4, cancelled, data=_transfer(True))
        if version in (1, 2):
            result = self.legacy_masks(result, settings, version, cancelled)
        return result

    def legacy_masks(self, source, params, version, cancelled):
        from types import SimpleNamespace
        result = source
        masks = [mask for mask in params.get("masks") or () if mask.get("enabled", True) and mask.get("params")]
        if not masks:
            return result
        result = self.stage(result, "legacy.encode", 0, cancelled, args=(version,), data=_transfer(False),
                            field=self.fields(result, {}, (), ("temperature", "tint", "exposure"), cancelled))
        for mask in masks:
            local = mask["params"]
            kind = mask.get("type", "linear")
            geometry = ((*mask.get("p0", (.5, 0)), *mask.get("p1", (.5, 1))) if kind == "linear" else
                (*mask.get("center", (.5, .5)), *mask.get("radius", (.3, .3)), mask.get("angle", 0), mask.get("feather", .5), 0))
            weight = self.weight(result, SimpleNamespace(kind=int(kind == "radial"), geometry=geometry), cancelled, legacy=True)
            points = self.fields(result, {**local, "temperature": int(local.get("temperature", 0)), "tint": int(local.get("tint", 0))},
                                 (), ("temperature", "tint", "exposure"), cancelled)
            adjusted = self.stage(result, "legacy.adjust", 17, cancelled, args=(version,), field=points)
            adjusted = self.tone(adjusted, {key: int(local.get(key, 0)) for key in ("shadows", "highlights", "blacks", "whites")}, (), cancelled)
            adjusted = self.stage(adjusted, "legacy.saturation", 18, cancelled,
                field=self.fields(result, local, (), ("contrast", "vibrance", "saturation"), cancelled))
            result = self.stage(result, "legacy.mix", 19, cancelled, auxiliary=adjusted, field=weight)
        return self.stage(result, "legacy.decode", 20, cancelled)


def float32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]
