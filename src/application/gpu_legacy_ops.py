from math import ceil, floor, sin, pi, sqrt

import numpy as np

from ..core.contracts.gpu import GpuExecutionError
from .gpu_image_ops import GpuImageOps

# 输出取到 1..255 级的最小线性值，与 CPU 显示量化同一张表
DISPLAY_THRESHOLDS = tuple(float(np.float32(encoded/12.92 if encoded <= .04045 else ((encoded+.055)/1.055)**2.4))
                           for encoded in ((level-.5)/255 for level in range(1, 256)))


def _coefficients(length, target):
    scale = length / target
    support = 3 * max(1., scale)
    entries, weights = [], []
    for index in range(target):
        center = (index + .5) * scale
        first, last = max(0, int(center-support+.5)), min(length, int(center+support+.5))
        values = []
        for position in range(first, last):
            x = (position-center+.5) / max(1., scale)
            values.append(1. if x == 0 else sin(pi*x)*sin(pi*x/3)/(pi*pi*x*x/3) if -3 <= x < 3 else 0.)
        total = sum(values)
        entries.extend((first, len(values), len(weights)))
        weights.extend(int(value/total*(1 << 22) + (.5 if value >= 0 else -.5)) for value in values)
    offset = len(entries)
    for index in range(target):
        entries[index*3+2] += offset
    if len(entries)+len(weights) > 4_000_000:
        raise GpuExecutionError("target_limit", "Image sampling metadata exceeds its budget")
    return tuple((*entries, *weights))


class GpuLegacyOps(GpuImageOps):
    def run(self, image, mode, **options):
        return self.stage(image, mode, kernel="legacy", **options)

    def encoded(self, source):
        return self.run(source, 0, data=DISPLAY_THRESHOLDS)

    def resize_u8(self, image, size):
        width, height = image.resident.carrier.size
        if size[0] != width:
            image = self.run(image, 1, size=(size[0], height), args=(0,), data=_coefficients(width, size[0]))
        if size[1] != height:
            image = self.run(image, 1, size=size, args=(1,), data=_coefficients(height, size[1]))
        return image

    def blur_u8(self, image, radius):
        import numpy as np
        radius = float(np.float32(radius))
        if not radius:
            return image
        variance = float(np.float32(np.float32(radius*radius)/3))
        length = float(np.float32(sqrt(12*variance+1)))
        base = floor((length-1)/2)
        fraction = float(np.float32((2*base+1)*(base*(base+1)-3*variance)))
        fraction = float(np.float32(fraction/(6*(variance-(base+1)**2))))
        box = float(np.float32(base+fraction))
        integer = int(box)
        weight = int(np.float32(1 << 24)/np.float32(np.float32(box*2)+1))
        edge = ((1 << 24)-(2*integer+1)*weight)//2
        for axis in (0, 1):
            for _ in range(3):
                image = self.run(image, 2, args=(axis, integer, weight, edge))
        return image

    def reduce(self, image, *, tensor=False):
        width, height = image.resident.carrier.size
        blocks = ceil(width*height/1024)
        partial = self.run(image, 3, size=(min(32768, blocks), ceil(blocks/32768)), args=(int(tensor),))
        return self.run(partial, 4, size=(1, 1))

    def finite(self, image):
        summary = self.reduce(image)
        if self.control(summary)[0, 0, 2]:
            raise GpuExecutionError("model_numeric", "Model produced nonfinite output")
        return summary
