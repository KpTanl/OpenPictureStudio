from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass, field, replace

import numpy as np

from .catalog import ParameterContract, ParameterField
from PIL import Image
from ..image_resize import ResizeOptions, apply_resize, plan_resize
from .effects import resize_arrays
from ..photo_engine import linear_to_srgb, srgb_to_linear
from ..contracts.inference import CPU
from ..contracts.result import CoreOperationError, CoreResult, ErrorCode
from ..contracts.tool_resources import ModelRequirement, RequiredResource, ResourceFeature


ENHANCE_RESOURCE_FEATURES = tuple(ResourceFeature(role, (ModelRequirement(
    role, role, ("rgb_restoration",), role + "_model"),))
    for role in ("denoise", "deblur", "super_resolution"))


ENHANCE_PARAMETERS = ParameterContract((
    ParameterField("target", str, "original", choices=("original", "2x", "4x", "4k")),
    ParameterField("fit", str, "contain", choices=("contain", "cover", "pad")),
    ParameterField("super_resolution_model", str, ""),
    ParameterField("denoise_model", str, ""),
    ParameterField("deblur_model", str, ""),
    ParameterField("luminance", int, 0, minimum=0, maximum=100),
    ParameterField("chroma", int, 0, minimum=0, maximum=100),
    ParameterField("deblur", int, 0, minimum=0, maximum=100),
), version=3, neutral=lambda params: params["target"] == "original" and not any(
    params[key] for key in ("luminance", "chroma", "deblur")))


def enhancement_required_resources(params):
    stages = []
    active = {"denoise": bool(params["luminance"] or params["chroma"]),
              "deblur": bool(params["deblur"]), "super_resolution": params["target"] != "original"}
    for feature in ENHANCE_RESOURCE_FEATURES:
        if active[feature.feature_id]:
            model = feature.models[0]
            stages.append(RequiredResource(feature.feature_id, model.role, model.capability, model.adapters,
                                           params[model.selection_field]))
    return tuple(stages)


def required_models(params):
    return tuple((resource.role, resource.model_token) for resource in enhancement_required_resources(params))


# 选了模型、模型暂时不在时仍可以接着编辑的步骤：受管模型的实现与它的参数版本
_GAP_EDITABLE = frozenset({("ai_enhance.neural", 3), ("bg_remove.managed", 3),
                           ("id_photo.managed", 2)})


def model_gaps_editable(workflow, issues):
    nodes = {node.node_id: node for node in workflow.nodes}
    return bool(issues) and all(issue["code"] == ErrorCode.TOOL_RESOURCE_MISSING
        and (node := nodes.get(issue.get("details", {}).get("node_id"))) is not None
        and node.node_type == "tool"
        and (node.params.get("implementation_id"), node.params.get("params_version")) in _GAP_EDITABLE
        for issue in issues)


def upgrade_denoise_settings(params, defaults):
    upgraded = ENHANCE_PARAMETERS.defaults(3)
    upgraded.update({cap + "_model": token for cap, token in defaults.items()})
    upgraded.update(luminance=int(params.get("strength", 40)), chroma=int(params.get("color_strength", 30)))
    checked = ENHANCE_PARAMETERS.normalize(upgraded, 3)
    if not checked.success:
        raise ValueError(checked.error.message_key)
    return checked.data


def output_plan(size, params):
    if params["target"] == "original":
        return plan_resize(size, ResizeOptions())
    if params["target"] == "4k":
        width, height = (3840, 2160) if size[0] >= size[1] else (2160, 3840)
    else:
        factor = int(params["target"][0])
        width, height = size[0] * factor, size[1] * factor
    return plan_resize(size, ResizeOptions(width, height, params["fit"]))


def _upscales(plan):
    return any(target > original for target, original in zip(plan.content_size, plan.source_size))


# 打开一项处理时的起始强度；零强度表示不做这一项
ENHANCE_START_STRENGTHS = {"denoise": {"luminance": 40, "chroma": 30}, "deblur": {"deblur": 50}}


@dataclass(frozen=True)
class TargetOutcome:
    """一个目标尺寸在这张图片上的规划结果。error 为空表示可以执行，否则是原因的文案键。

    memory_bytes 是放大时估算的图像内存，用于向用户说明；不放大或没有超分模型时为 0。
    """
    target: str
    output_size: tuple[int, int] | None
    enlarges: bool
    error: str = ""
    memory_bytes: int = field(default=0, compare=False)


def target_outcomes(size, params, models, memory_limit=None):
    """逐个给出“原尺寸”以外各目标的规划结果，其余参数取自 params。

    memory_limit 是这台机器的工作内存上限（字节）；给出时，放大所需内存超过它的目标记为做不了。
    """
    outcomes = []
    for target in next(parameter.choices for parameter in ENHANCE_PARAMETERS.fields if parameter.name == "target"):
        if target == "original":
            continue
        settings = {**params, "target": target}
        needed = 0
        try:
            plan = output_plan(size, settings)
            upscales = _upscales(plan)
            if upscales and "super_resolution" in models:
                needed = _image_memory(size, settings, plan, models, size[0] * size[1] * 12)
            execution_plan(size, settings, models, memory_limit)
        except ValueError as error:
            key = str(error) if str(error).startswith("error.") else "error.export.size"
            outcomes.append(TargetOutcome(target, None, False, key, needed))
        else:
            outcomes.append(TargetOutcome(target, plan.output_size, upscales, memory_bytes=needed))
    return tuple(outcomes)


def execution_plan(size, params, models, memory_limit=None):
    """输出尺寸的规划。输出多大没有固定上限：给出 memory_limit 时，放大所需的图像内存超过它即拒绝；

    执行时的准入由宿主按 estimate_memory() 做。
    """
    plan = output_plan(size, params)
    if (memory_limit is not None and "super_resolution" in models and _upscales(plan)
            and _image_memory(size, params, plan, models, size[0] * size[1] * 12) > memory_limit):
        raise ValueError("error.enhance.upscale_source")
    return plan


# Lanczos 的支撑半径（以采样间隔为单位）
_LANCZOS_SUPPORT = 3


def _final_plan(size, plan, params):
    """从放大或修复后的尺寸缩放到输出尺寸的规划。"""
    if params["target"] == "4k":
        return output_plan(size, {**params, "target": "4k"})
    return plan_resize(size, ResizeOptions(*plan.output_size, params["fit"]))


def _banded(size, final_plan):
    """放大结果能否分带处理并与整幅处理逐位相同。

    不需要再缩放时可以；需要缩放时，竖直方向的取样起点与间隔都必须是整数（例如 4 倍模型做 2×），
    这样每一带里各输出行的取样位置与整幅时是同一组可以精确表示的数。补边的情形不分带。
    """
    if any(final_plan.padding):
        return False
    if final_plan.crop_box is None and final_plan.content_size == size:
        return True
    top, bottom = (0.0, float(size[1])) if final_plan.crop_box is None else final_plan.crop_box[1::2]
    step = (bottom - top) / final_plan.content_size[1]
    return top == int(top) and step == int(step) and step >= 1


def _image_memory(size, params, plan, models, source_bytes):
    """图像数据占用的工作内存，规划与执行准入共用。

    不放大：源图与输出中较大者每像素 48 字节。整幅放大：放大结果（源图乘以模型倍数的平方）每像素
    48 字节。分带放大：放大结果只按行带存在，输出每像素 24 字节，另加两个行带各每像素 48 字节。
    三种情形都再加源图缓冲的 4 倍。
    """
    width, height = size
    output = plan.output_size[0] * plan.output_size[1]
    model = models.get("super_resolution")
    if model is None or not _upscales(plan):
        return max(width * height, output) * 48 + source_bytes * 4
    scale = model.scale
    upscaled = (width * scale, height * scale)
    if not _banded(upscaled, _final_plan(upscaled, plan, params)):
        return max(output, upscaled[0] * upscaled[1]) * 48 + source_bytes * 4
    band = min(height, model.tile - model.overlap * 2) * scale * upscaled[0]
    return output * 24 + band * 2 * 48 + source_bytes * 4


def estimate_memory(source, params, models):
    height, width = source.pixels.shape[:2]
    plan = output_plan((width, height), params)
    weights = sum(file.disk_size for model in models.values() for file in model.files)
    activations = max((model.tile ** 2 * 1024 for model in models.values()), default=0)
    return _image_memory((width, height), params, plan, models, source.pixels.nbytes) + weights + activations


class RestorationModel:
    def __init__(self, descriptor, path, threads, create_session=None):
        self.descriptor = descriptor
        if create_session is None:
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            self._session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
            self.backend = CPU
        else:
            self._session, self.backend = create_session(path, threads)
        inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()
        if (len(inputs) != 1 or not outputs or inputs[0].type != "tensor(float)"
                or len(inputs[0].shape) != 4 or inputs[0].shape[1] != 3
                or isinstance(inputs[0].shape[0], int) and inputs[0].shape[0] != 1):
            raise ValueError("models.error.structure")
        self._input = inputs[0].name
        self._output = outputs[0].name
        self._shape = inputs[0].shape

    def close(self):
        self._session = None

    def run(self, rgb, cancelled):
        """整幅结果，取值在 0 到 1 之间。"""
        scale = self.descriptor.scale
        self._check_structure()
        result = np.empty((rgb.shape[0] * scale, rgb.shape[1] * scale, 3), dtype=np.float32)
        for top, band in self.run_rows(rgb, cancelled):
            result[top:top + band.shape[0]] = band
        return result

    def _check_structure(self):
        descriptor = self.descriptor
        fixed = isinstance(self._shape[2], int) and isinstance(self._shape[3], int)
        if fixed and self._shape[2:] != [descriptor.tile, descriptor.tile] or descriptor.tile % descriptor.alignment:
            raise ValueError("models.error.structure")

    def run_rows(self, rgb, cancelled):
        """自上而下逐个块行产出 (起始行, 这一行带的结果)，行号与尺寸都按模型倍数放大，取值在 0 到 1 之间。

        各块的划分、补边与推理和整幅处理相同，产出的行带可以由调用方用完即弃。
        """
        descriptor = self.descriptor
        tile, pad, scale = descriptor.tile, descriptor.overlap, descriptor.scale
        height, width = rgb.shape[:2]
        self._check_structure()
        step = tile - pad * 2
        for top in range(0, height, step):
            result = np.empty(((min(top + step, height) - top) * scale, width * scale, 3), dtype=np.float32)
            for left in range(0, width, step):
                if cancelled():
                    raise CancelledError()
                bottom, right = min(top + step, height), min(left + step, width)
                y0, x0 = top - pad, left - pad
                y1, x1 = bottom + pad, right + pad
                patch = rgb[max(0, y0):min(height, y1), max(0, x0):min(width, x1)]
                padding = ((max(0, -y0), max(0, y1 - height)),
                           (max(0, -x0), max(0, x1 - width)), (0, 0))
                patch = np.pad(patch, padding, mode="edge")
                ph, pw = patch.shape[:2]
                patch = np.pad(patch, ((0, tile - ph), (0, tile - pw), (0, 0)), mode="edge")
                tensor = np.ascontiguousarray(patch.transpose(2, 0, 1)[None], dtype=np.float32)
                predicted = self._session.run([self._output], {self._input: tensor})[0]
                if (predicted.shape != (1, 3, tile * scale, tile * scale)
                        or predicted.dtype != np.float32 or not np.all(np.isfinite(predicted))):
                    raise ValueError("models.error.structure")
                cropped = predicted[0, :, pad * scale:(pad + bottom - top) * scale,
                                    pad * scale:(pad + right - left) * scale]
                result[:, left * scale:right * scale] = cropped.transpose(1, 2, 0)
            yield top * scale, np.clip(result, 0, 1, out=result)


class EnhancementExecutor:
    analyze = None

    def __init__(self, models, resources, threads, create_session=None):
        self._descriptors = models
        self._resources = resources
        self._threads = threads
        self._create_session = create_session
        self._models = {}

    def _model(self, role):
        if role not in self._models:
            self._models[role] = RestorationModel(self._descriptors[role], self._resources[role], self._threads,
                                                  self._create_session)
        return self._models[role]

    def close(self):
        for model in self._models.values():
            model.close()
        self._models.clear()

    def execute(self, source, params, context):
        cancelled = context["is_cancelled"]
        try:
            return CoreResult.ok(self._execute(source, params, cancelled, sampling=context.get("sampling")))
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except CoreOperationError as error:
            return CoreResult.from_exception(error)
        except (ValueError, RuntimeError) as error:
            key = str(error) if str(error).startswith(("models.", "error.")) else "error.workflow.execution_failed"
            return CoreResult.fail(ErrorCode.INVALID_INPUT, key)

    def _execute(self, source, params, cancelled, *, sampling=None):
        plan = execution_plan((source.pixels.shape[1], source.pixels.shape[0]), params, self._descriptors)
        if not required_models(params):
            return source
        if cancelled():
            raise CancelledError()
        needs_upscale = _upscales(plan)
        if not needs_upscale and not any(params[key] for key in ("luminance", "chroma", "deblur")):
            return apply_resize(source, plan, sampling=sampling)
        rgb = linear_to_srgb(np.clip(source.pixels, 0, 1))
        if source.alpha is not None:
            hidden = source.alpha <= 1e-6
            if np.any(hidden) and not np.all(hidden):
                import cv2
                _, labels = cv2.distanceTransformWithLabels(hidden.astype(np.uint8),
                    cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
                visible = rgb[~hidden]
                rgb[hidden] = visible[labels[hidden] - 1]
            elif np.all(hidden):
                rgb.fill(0)
        if params["luminance"] or params["chroma"]:
            restored = self._model("denoise").run(rgb, cancelled)
            weights = np.array([0.299, 0.587, 0.114], dtype=np.float32)
            luma = rgb @ weights
            target_luma = restored @ weights
            delta_y = (target_luma - luma)[..., None]
            rgb = np.clip(rgb + delta_y * (params["luminance"] / 100)
                          + (restored - rgb - delta_y) * (params["chroma"] / 100), 0, 1)
        if params["deblur"]:
            restored = self._model("deblur").run(rgb, cancelled)
            rgb = rgb + (restored - rgb) * (params["deblur"] / 100)
        if not needs_upscale:
            if cancelled():
                raise CancelledError()
            current = replace(source, pixels=srgb_to_linear(rgb))
            return apply_resize(current, _final_plan((rgb.shape[1], rgb.shape[0]), plan, params), sampling=sampling)
        model = self._model("super_resolution")
        size = (rgb.shape[1] * model.descriptor.scale, rgb.shape[0] * model.descriptor.scale)
        final_plan = _final_plan(size, plan, params)
        if not _banded(size, final_plan):
            return self._upscale_whole(source, rgb, model, final_plan, cancelled, sampling)
        pixels, alpha = _upscale_banded(model, rgb, source.alpha, final_plan, cancelled)
        if sampling is not None and sampling.active:
            sampling.resize(size)
            if final_plan.crop_box is not None or final_plan.content_size != size:
                sampling.resize(final_plan.content_size, box=final_plan.crop_box)
        return replace(source, pixels=pixels, alpha=alpha, orig_size=final_plan.content_size)

    @staticmethod
    def _upscale_whole(source, rgb, model, final_plan, cancelled, sampling):
        """先得到完整的放大结果，再按完整域结果计算目标尺寸输出。"""
        rgb = model.run(rgb, cancelled)
        size = (rgb.shape[1], rgb.shape[0])
        alpha = None
        if source.alpha is not None:
            with Image.fromarray(source.alpha) as mask:
                with mask.resize(size, Image.Resampling.LANCZOS) as scaled:
                    alpha = np.clip(np.asarray(scaled, dtype=np.float32), 0, 1)
        current = replace(source, pixels=srgb_to_linear(rgb), alpha=alpha, orig_size=size)
        if sampling is not None and sampling.active:
            sampling.resize(size)
        if cancelled():
            raise CancelledError()
        return apply_resize(current, final_plan, sampling=sampling)


def _upscaled_alpha_rows(alpha, scale, top, bottom):
    """透明通道放大到模型倍数后的第 top 到 bottom 行，与整幅一次放大的对应行逐位相同。

    放大时每个输出行只用到上下各 3 行输入；行带的起止都是模型倍数的整数倍，取样位置可以精确表示。
    """
    height, width = alpha.shape
    first = max(0, top // scale - _LANCZOS_SUPPORT)
    last = min(height, bottom // scale + _LANCZOS_SUPPORT + 1)
    box = (0, top // scale - first, width, bottom // scale - first)
    with Image.fromarray(alpha[first:last]) as mask:
        with mask.resize((width * scale, bottom - top), Image.Resampling.LANCZOS, box=box) as scaled:
            return np.clip(np.asarray(scaled, dtype=np.float32), 0, 1)


def _upscale_banded(model, rgb, alpha, final_plan, cancelled):
    """放大一个行带、缩到输出尺寸、释放，再处理下一带；返回线性图像与透明通道。

    只在 _banded() 判定可以时使用，结果与先得到完整放大结果再缩放逐位相同：各块的推理不变，
    每个输出行取到的输入行与系数也不变，只是不再同时保存整幅放大结果。
    """
    scale = model.descriptor.scale
    upscaled_width, upscaled_height = rgb.shape[1] * scale, rgb.shape[0] * scale
    out_width, out_height = final_plan.content_size
    resized = final_plan.crop_box is not None or final_plan.content_size != (upscaled_width, upscaled_height)
    pixels = np.empty((out_height, out_width, 3), dtype=np.float32)
    out_alpha = None if alpha is None else np.empty((out_height, out_width), dtype=np.float32)
    left, top, right, bottom = final_plan.crop_box or (0.0, 0.0, float(upscaled_width), float(upscaled_height))
    step = (bottom - top) / out_height
    support = _LANCZOS_SUPPORT * max(step, 1.0)

    def first_input_row(row):
        """这一输出行最早用到的输入行，与缩放实现的取整方式相同。"""
        return max(0, int(top + (row + 0.5) * step - support + 0.5))

    def rows_ready(done, available):
        """输入已经有了前 available 行时，可以定下来的输出行数。"""
        row = done
        while row < out_height and min(upscaled_height, int(top + (row + 0.5) * step + support + 0.5)) <= available:
            row += 1
        return row

    window = window_alpha = None
    window_start = done = 0
    for band_top, band in model.run_rows(rgb, cancelled):
        band_bottom = band_top + band.shape[0]
        band_alpha = None if alpha is None else _upscaled_alpha_rows(alpha, scale, band_top, band_bottom)
        if not resized:
            srgb_to_linear(band, out=pixels[band_top:band_bottom])
            if out_alpha is not None:
                out_alpha[band_top:band_bottom] = band_alpha
            continue
        linear = srgb_to_linear(band)
        del band
        window = linear if window is None else np.concatenate((window, linear))
        if alpha is not None:
            window_alpha = band_alpha if window_alpha is None else np.concatenate((window_alpha, band_alpha))
        del linear, band_alpha
        ready = rows_ready(done, band_bottom)
        if ready > done:
            box = (left, top + done * step - window_start, right, top + ready * step - window_start)
            pixels[done:ready], part_alpha = resize_arrays(window, window_alpha, (out_width, ready - done), box=box)
            if out_alpha is not None:
                out_alpha[done:ready] = part_alpha
            done = ready
        if done < out_height:
            keep = first_input_row(done)
            # 复制出还要用的几行，前面的随旧缓冲一起释放
            window = window[keep - window_start:].copy()
            if window_alpha is not None:
                window_alpha = window_alpha[keep - window_start:].copy()
            window_start = keep
    return pixels, out_alpha
