from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .execution import CPU_BACKEND_IDENTITY


display_target = ContextVar("ops_display_target", default=None)
cpu_recovery = ContextVar("ops_cpu_recovery", default=None)


@dataclass(frozen=True)
class ImageOutputSpec:
    resize: object = None
    boundary: str = "export"

    @property
    def max_dimension(self):
        return "linear-f32", self.boundary, self.resize

    def size_for(self, width, height):
        if self.resize is None:
            return width, height
        from ..image_resize import plan_resize
        return plan_resize((width, height), self.resize).output_size


class GpuExecutionError(RuntimeError):
    def __init__(self, code, detail, family=None):
        super().__init__(detail)
        self.code, self.family = code, family


@dataclass(frozen=True)
class DevicePixels:
    shape: tuple

    @property
    def ndim(self):
        return len(self.shape)

    @property
    def dtype(self):
        import numpy as np
        return np.dtype("float32")

    @property
    def nbytes(self):
        from math import prod
        return prod(self.shape) * 4


@dataclass(frozen=True)
class DeviceModelSpec:
    input_size: tuple
    output_size: tuple
    channels: int = 3
    layout: str = "nchw"
    input_colorspace: str = "srgb"
    output_colorspace: str = "srgb"
    scale: float = 1.0
    bias: float = 0.0
    source_colorspace: str = "linear_srgb"
    destination_colorspace: str = "linear_srgb"

    def __post_init__(self):
        import math
        if (any(not isinstance(size, tuple) or len(size) != 2 or any(type(v) is not int or not 1 <= v <= 32768 for v in size)
                for size in (self.input_size, self.output_size)) or self.channels not in (3, 4)
                or self.layout not in ("nchw", "nhwc") or self.input_colorspace not in ("srgb", "linear_srgb")
                or self.output_colorspace not in ("srgb", "linear_srgb") or not math.isfinite(self.scale) or not math.isfinite(self.bias)):
            raise ValueError("GPU 模型张量规格无效")
        if (self.source_colorspace not in ("srgb", "linear_srgb")
                or self.destination_colorspace not in ("srgb", "linear_srgb")
                or self.source_colorspace == "srgb" and self.input_colorspace != "srgb"
                or self.destination_colorspace == "srgb" and self.output_colorspace != "srgb"):
            raise ValueError("GPU 模型颜色传递规格无效")

    @property
    def tensor_bytes(self):
        return sum(width * height for width, height in (self.input_size, self.output_size)) * self.channels * 4


@dataclass(frozen=True)
class GpuResident:
    carrier: object
    backend_identity: tuple
    shared_memory: object = None
    identity: tuple = ()
    raw_meta: object = None


@dataclass(frozen=True)
class DeviceTensorModelSpec:
    input_size: tuple
    output_shapes: tuple
    channels: int = 3
    layout: str = "nhwc"
    input_colorspace: str = "linear_srgb"
    scale: float = 1.0
    bias: float = 0.0
    source_colorspace: str = "linear_srgb"
    channel_order: str = "rgb"

    def __post_init__(self):
        DeviceModelSpec(self.input_size, (1, 1), self.channels, self.layout,
                        self.input_colorspace, "linear_srgb", self.scale, self.bias)
        if (self.source_colorspace not in ("linear_srgb", "srgb") or self.channel_order not in ("rgb", "bgr")
                or self.source_colorspace == "srgb" and self.input_colorspace != "srgb"):
            raise ValueError("GPU 模型输入颜色规格无效")
        if (not isinstance(self.output_shapes, tuple) or not 1 <= len(self.output_shapes) <= 32
                or any(not isinstance(shape, tuple) or not 1 <= len(shape) <= 8
                    or any(type(v) is not int or not 1 <= v <= 100_000_000 for v in shape)
                    for shape in self.output_shapes)):
            raise ValueError("GPU 模型输出张量规格无效")

    @property
    def tensor_bytes(self):
        from math import prod
        return (prod(self.input_size)*self.channels + sum(prod(shape) for shape in self.output_shapes))*4


@dataclass(frozen=True)
class DeviceTensorInput:
    gpu_image: object
    colorspace: str
    channel_order: str = "rgb"


@dataclass(frozen=True)
class GpuArtifact:
    resident: GpuResident
    service: object
    family: tuple
    source_path: str
    patches: tuple = ()

    @property
    def backend_identity(self):
        return self.resident.backend_identity

    def display(self, size, cancelled, region=None):
        return self.service.display(self, size, cancelled, region)

    def read_region(self, bounds, cancelled):
        return self.service.read_region(self, bounds, cancelled)

    def face_input(self, bounds, size, cancelled):
        return self.service.face_input(self, bounds, size, cancelled)

    def materialize(self, source, cancelled):
        return self.service.materialize(self, source, cancelled)

    def resize(self, source, size, box, cancelled):
        return self.service.resize(source, size, box, cancelled)


@dataclass(frozen=True)
class DevicePatch:
    bounds: tuple
    artifact: GpuArtifact
    identity: tuple
    reference_size: tuple
    has_alpha: bool = False


@contextmanager
def display_request(spec):
    token = display_target.set(spec)
    try:
        yield
    finally:
        display_target.reset(token)


@contextmanager
def recover_cpu(reason):
    token = cpu_recovery.set(reason)
    try:
        yield CPU_BACKEND_IDENTITY
    finally:
        cpu_recovery.reset(token)
