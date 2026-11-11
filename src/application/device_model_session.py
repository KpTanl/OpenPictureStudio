from ..core.contracts.gpu import GpuExecutionError
from .gpu_geometry import GpuGeometry


class DeviceModelSession:
    def __init__(self, backend, model, spec, identity, memory=None):
        self.backend, self.model, self.spec, self.identity = backend, model, spec, identity
        self.memory = memory
        self._budget_bytes = 0
        self.enabled = lambda: True

    def run(self, source, identity, cancelled=lambda: False):
        artifact = source.gpu_image
        if not self.enabled() or artifact is None or self.model is None or artifact.backend_identity != self.backend.snapshot().identity:
            raise GpuExecutionError("model_device", "GPU model input belongs to another engine")
        if source.colorspace != self.spec.source_colorspace:
            raise GpuExecutionError("model_colorspace", "GPU model input color specification mismatch", artifact.family)
        artifact = self.backend.resolve(artifact, cancelled)
        if artifact.resident.carrier.size != self.spec.input_size:
            raise GpuExecutionError("model_layout", "GPU model input size mismatch", artifact.family)
        model = self.model
        result = self.backend.operate(artifact, "model", (self.identity, self.spec, identity), self.spec.output_size,
            lambda device, abort: model.run(artifact.resident.carrier, abort), cancelled)
        sx, sy = self.spec.output_size[0]/self.spec.input_size[0], self.spec.output_size[1]/self.spec.input_size[1]
        formal = (max(1, round(source.orig_size[0]*sx)), max(1, round(source.orig_size[1]*sy))) if source.is_proxy else self.spec.output_size
        from dataclasses import replace
        return replace(GpuGeometry.updated(source, result, size=formal, alpha=self.spec.channels == 4 or source.alpha is not None),
                       colorspace=self.spec.destination_colorspace)

    def close(self):
        model, self.model = self.model, None
        if model is not None:
            model.close()
        self.backend.release_device_model(self)
        memory, self.memory = self.memory, None
        if memory is not None:
            memory.close()

    def __del__(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class DeviceTensorSession(DeviceModelSession):
    def run(self, source, identity, cancelled=lambda: False):
        artifact = source.gpu_image
        if not self.enabled() or artifact is None or self.model is None or artifact.backend_identity != self.backend.snapshot().identity:
            raise GpuExecutionError("model_device", "GPU model input belongs to another engine")
        if source.colorspace != self.spec.source_colorspace or getattr(source,"channel_order","rgb") != self.spec.channel_order:
            raise GpuExecutionError("model_colorspace", "GPU tensor input color specification mismatch", artifact.family)
        artifact = self.backend.resolve(artifact, cancelled)
        if artifact.resident.carrier.size != self.spec.input_size:
            raise GpuExecutionError("model_layout", "GPU model input size mismatch", artifact.family)
        from math import prod
        count = sum(prod(shape) for shape in self.spec.output_shapes)
        model = self.model
        return self.backend.operate(artifact, "model.tensors", (self.identity, self.spec, identity), ((count+3)//4, 1),
            lambda device, abort: device.pack_tensors(model.run_tensors(artifact.resident.carrier, abort), abort),
            cancelled, working_bytes=count*8)
