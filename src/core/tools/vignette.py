from dataclasses import replace
import math

import numpy as np

from .catalog import ParameterContract, ParameterField, ToolDefinition
from ..contracts.result import CoreResult
from ..contracts.tools import ToolDescriptor
from ..execution_memo import memo


def _validate(params):
    center = params["center"]
    if (len(center) != 2 or any(type(value) not in (int, float) or not math.isfinite(value)
                              or not 0 <= value <= 1 for value in center)):
        raise ValueError("暗角中心无效")


PARAMETERS = ParameterContract((
    ParameterField("strength", int, 0, -100, 100),
    ParameterField("center", list, [.5, .5]),
    ParameterField("radius", float, .7, .01, 2),
    ParameterField("feather", float, .5, .01, 2),
), validate=_validate, neutral=lambda params: params["strength"] == 0)


def render(source, params, *, bounds=None, full_size=None):
    if not params["strength"]:
        return source
    width, height = full_size or source.pixels.shape[1::-1]
    left, top, right, bottom = bounds or (0, 0, width, height)
    def weights():
        y, x = np.ogrid[top:bottom, left:right]
        scale = max(1, min(width, height))
        distance = np.sqrt(((x+.5-params["center"][0]*width)/scale)**2
                         + ((y+.5-params["center"][1]*height)/scale)**2)
        weight = np.clip((distance-params["radius"]/2)/params["feather"], 0, 1).astype(np.float32)
        return weight*weight*(3-2*weight)
    field = memo("vignette.field", (width, height, bounds, params["center"], params["radius"], params["feather"]), weights)
    return replace(source, pixels=source.pixels*np.exp2(field[..., None]*np.float32(-params["strength"]/50)))


class VignetteExecutor:
    analyze = None

    def execute(self, source, params, context):
        return CoreResult.ok(render(source, params))

    def close(self):
        pass


def definition():
    return ToolDefinition(ToolDescriptor("vignette", "1", 1, 1, "vignette.linear", "1"),
        PARAMETERS, lambda resources: VignetteExecutor(), supports_proxy=True,
        sampling_behavior="preserve", working_memory=lambda source, params: source.pixels.nbytes*3,
        output_size=lambda params, size: size, region_input=lambda params, bounds, size: bounds,
        execute_region=lambda source, params, context: render(source, params,
            bounds=context["output_bounds"], full_size=context["full_input_size"]))
