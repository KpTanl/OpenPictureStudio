from math import ceil, prod
import hashlib
import struct

from ..core.contracts.gpu import GpuExecutionError


class GpuImageOps:
    def __init__(self, backend, cancelled):
        self.backend, self.cancelled = backend, cancelled

    @staticmethod
    def artifact(value):
        return value.gpu_image if hasattr(value, "gpu_image") else value

    def stage(self, image, mode, *, auxiliary=None, field=None, extra=None, args=(), data=(), size=None, kernel="beauty",
              source_dependent=True):
        image = self.artifact(image)
        if source_dependent:
            image = self.backend.resolve(image, self.cancelled)
        supplied = auxiliary, field, extra
        others = tuple(self.backend.resolve(self.artifact(item), self.cancelled) if item is not None else image
                       for item in supplied)
        size = size or image.resident.carrier.size
        args, data = tuple(float(v) for v in args), tuple(float(v) for v in data)
        digest = hashlib.sha256(struct.pack("<" + str(len(data)) + "f", *data)).hexdigest() if data else ""
        identity = (mode, size, args, digest,
                    *(item.resident.identity if value is not None else () for item, value in zip(others, supplied)))
        return self.backend.operate(image, kernel, identity, size, lambda device, abort:
            device.beauty_op(image.resident.carrier, *(item.resident.carrier for item in others),
                kernel, mode, list(args), list(data), *size, abort), self.cancelled,
            input_identity=None if source_dependent else ())

    def view(self, image, bounds):
        image = self.artifact(image)
        if image.patches:
            return self.backend.resolve(image, self.cancelled, bounds)
        left, top, right, bottom = bounds
        return self.backend.operate(image, "image.view", tuple(bounds), (right-left, bottom-top),
            lambda device, abort: device.view(image.resident.carrier, left, top, right-left, bottom-top),
            self.cancelled, view=True, working_bytes=0)

    def channel(self, image, channel=0, scale=1, offset=0):
        return self.stage(image, 3, args=(channel, scale, offset))

    def zero(self, image, size=None):
        return self.stage(image, 9, size=size, source_dependent=False)

    def pack(self, *images, channels=(0, 0, 0, 0)):
        first, *others = images
        others += [None] * (3-len(others))
        return self.stage(first, 16, auxiliary=others[0], field=others[1], extra=others[2], args=channels)

    def gaussian(self, image, sigma):
        import cv2
        sigma = max(1., float(sigma))
        count = max(1, round(sigma*8+1) | 1)
        if count > 1025:
            raise GpuExecutionError("beauty_target_limit", "Gaussian support exceeds GPU submission limits")
        weights = cv2.getGaussianKernel(count, sigma, cv2.CV_32F).ravel().tolist()
        result = image
        for axis in (0, 1):
            result = self.stage(result, 5, args=(count//2, axis), data=weights)
        return result

    def guided(self, image, guide, radius, eps):
        image, guide = (self.backend.resolve(self.artifact(item), self.cancelled) for item in (image, guide))
        size = image.resident.carrier.size
        return self.backend.operate(image, "beauty.guided", (guide.resident.identity, radius, eps), size,
            lambda device, abort: device.guided(image.resident.carrier, guide.resident.carrier, radius, eps, 1, abort),
            self.cancelled, working_bytes=prod(size)*320)

    def region(self, image, mode, *, auxiliary=None, field=None, extra=None, args=(), data=()):
        image = self.backend.resolve(self.artifact(image), self.cancelled)
        others = tuple(self.backend.resolve(self.artifact(item), self.cancelled) if item is not None else image
                       for item in (auxiliary, field, extra))
        identity = (mode, tuple(args), tuple(data), *(item.resident.identity for item in others))
        return self.backend.operate(image, "beauty.region", identity, image.resident.carrier.size,
            lambda device, abort: device.color_op(image.resident.carrier, *(item.resident.carrier for item in others),
                mode, list(args), list(data), abort), self.cancelled)

    def inward(self, image, feather, *, border=True, invert=False, raw=False):
        if feather > 256:
            raise GpuExecutionError("beauty_target_limit", "Face feather exceeds GPU submission limits")
        if border:
            image = self.region(image, 36, args=(int(invert),))
        elif invert:
            image = self.channel(image, scale=-1, offset=1)
        prefix = self.region(image, 33)
        return self.region(image, 35, auxiliary=prefix, args=(max(1., feather), int(raw)))

    def dilate(self, image, radius):
        if radius > 1024:
            raise GpuExecutionError("beauty_target_limit", "Face expansion exceeds GPU submission limits")
        return self.region(image, 34, auxiliary=self.region(image, 33), args=(radius,))

    def embed(self, destination, patch, offset, *, add=False):
        return self.stage(destination, 11, auxiliary=patch, args=(*offset, int(add)))

    def assemble(self, image, parts, positions, size):
        image = self.artifact(image)
        parts = tuple(self.backend.resolve(self.artifact(item), self.cancelled) for item in parts)
        identity = (tuple(item.resident.identity for item in parts), tuple(positions), size)
        return self.backend.operate(self.artifact(image), "image.assemble", identity, size,
            lambda device, abort: device.assemble([item.resident.carrier for item in parts], list(positions), *size, abort),
            self.cancelled, input_identity=())

    def control(self, image):
        return self.backend.face_metadata(self.artifact(image), self.cancelled)

    def selected(self, image, mask, *, limit=None):
        image = self.artifact(image)
        count = prod(image.resident.carrier.size)
        blocks = ceil(count/1024)
        counts = self.stage(image, 0, auxiliary=mask, size=(blocks, 1), kernel="beauty_stats")
        prefix = self.stage(counts, 1, size=(blocks+1, 1), args=(limit or 1,), kernel="beauty_stats")
        total = int(self.control(self.view(prefix, (blocks, 0, blocks+1, 1)))[0, 0, 0])
        if not total:
            return None, 0
        samples = ceil(total/max(1, ceil(total/limit))) if limit else total
        if samples > 4_000_000:
            raise GpuExecutionError("beauty_target_limit", "Face statistics exceed GPU submission limits")
        size = min(samples, 32768), ceil(samples/32768)
        return self.stage(image, 2, auxiliary=mask, field=prefix, size=size, args=(int(limit is not None),),
                          kernel="beauty_stats"), total

    def median(self, samples):
        samples = self.backend.resolve(self.artifact(samples), self.cancelled)
        return self.backend.operate(samples, "beauty.median", (), (3, 1),
            lambda device, abort: device.beauty_median(samples.resident.carrier, abort), self.cancelled,
            working_bytes=ceil(prod(samples.resident.carrier.size)/1024)*64+32768)

    def count(self, mask):
        blocks = ceil(prod(mask.resident.carrier.size)/1024)
        counts = self.stage(mask, 0, auxiliary=mask, size=(blocks, 1), kernel="beauty_stats")
        return self.total(counts)

    def components(self, labels, roots, distance):
        labels, roots = (self.backend.resolve(self.artifact(value), self.cancelled) for value in (labels, roots))
        count = prod(roots.resident.carrier.size)
        return self.backend.operate(labels, "beauty.components", (roots.resident.identity, distance), (4, count),
            lambda device, abort: device.beauty_components(labels.resident.carrier, roots.resident.carrier, distance, abort),
            self.cancelled, working_bytes=count*64)

    def total(self, partial):
        value = self.control(self.stage(partial, 15, size=(1, 1), kernel="beauty_stats"))[0, 0]
        return int(value[0])+(int(value[1]) << 16)

    def robust(self, samples, floors, multiplier):
        center = self.median(samples)
        deviation = self.stage(samples, 4, auxiliary=center, kernel="beauty_stats")
        spread = self.median(deviation)
        filtered = self.stage(samples, 5, auxiliary=center, field=spread, args=(multiplier,), data=floors, kernel="beauty_stats")
        center = self.median(filtered)
        return filtered, center
