from contextlib import ExitStack
from dataclasses import dataclass, replace
from math import floor, prod
from pathlib import Path
import hashlib
import json
import logging

from ..core.beauty_detection import DETECTION_SPECS
from ..core.contracts.gpu import DeviceTensorInput, DeviceTensorModelSpec, GpuExecutionError
from ..core.contracts.beauty import FaceAnalysisSnapshot, FaceLandmarks
from ..core.face_regions import OVAL, EYES
from .gpu_geometry import GpuGeometry


LOGGER = logging.getLogger(__name__)
TASK_DIGEST = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"
REVISION = "mediapipe-original-fp32-onnx17-1"
MODELS = {
    "face_detector": "d2aaa570294798a7b1d8bd7ac04d5ce7962ecc4021fe3f4312634cc893cc21ac",
    "face_landmarks_detector": "7291041148c74128d2e4fda437e5b0911576255443405d1cb7e8d73eeaab3fac",
}
ROW = 486
STATUSES = {1: "ready", 2: "face_too_small", 3: "insufficient_landmarks"}
ANCHORS = (33, 263, 1, 61, 291)


@dataclass(frozen=True)
class DeviceFaceSnapshot:
    identity: tuple
    reference_size: tuple
    detection_size: tuple
    artifact: object
    session: object

    @property
    def backend_identity(self):
        return self.artifact.backend_identity

    def materialize(self, cancelled=lambda: False):
        values = self.session.backend.face_metadata(self.artifact, cancelled, final=True)
        faces = []
        for row in values:
            if row[4, 1]:
                faces.append(FaceLandmarks(tuple((float(point[0]), float(point[1])) for point in row[8:]),
                                           STATUSES[int(row[2, 0])]))
        if not faces and values[0,7,0]:
            faces.append(FaceLandmarks((), "insufficient_landmarks"))
        status = "ready" if any(face.status == "ready" for face in faces) else faces[0].status if faces else "no_face"
        return FaceAnalysisSnapshot(self.identity, self.reference_size, self.detection_size, tuple(faces), status)

    def regions(self, index, size, cancelled=lambda: False, *, include_support=True, required=None):
        from .gpu_face_regions import prepare_regions
        return prepare_regions(self, index, size, cancelled, include_support=include_support, required=required)


class FaceGeometry:
    def __init__(self, backend, identity):
        self.backend, self.identity = backend, identity

    def op(self, source, mode, size, cancelled, *, auxiliary=None, tensor=None, args=(), data=()):
        artifact = self.backend.resolve(source.gpu_image if hasattr(source, "gpu_image") else source, cancelled)
        other = self.backend.resolve(auxiliary or artifact, cancelled)
        identity = (self.identity, mode, size, tuple(args), tuple(data), other.resident.identity,
                    tensor.resident.identity if tensor is not None else ())
        return self.backend.operate(artifact, "face." + str(mode), identity, size, lambda device, abort:
            device.face_op(artifact.resident.carrier, other.resident.carrier, mode, list(args), list(data), *size,
                tensor.resident.carrier if tensor is not None else None, abort), cancelled)


def upload_face_snapshot(backend, source, snapshot, cancelled):
    import numpy as np
    from concurrent.futures import CancelledError
    if not isinstance(snapshot, FaceAnalysisSnapshot) or snapshot.reference_size != source.orig_size:
        raise GpuExecutionError("beauty_input", "CPU face snapshot reference size mismatch")
    if len(snapshot.faces) > 50:
        raise GpuExecutionError("face_metadata", "CPU face snapshot exceeds the face limit")
    count = max(1, len(snapshot.faces))
    with backend.compute.suspend(cancelled), backend.resources.working_memory(ROW * count * 16, cancelled):
        with backend.compute.borrow(cancelled, threads=1):
            records = np.zeros((count, ROW, 4), np.float32)
            for index, face in enumerate(snapshot.faces):
                if cancelled():
                    raise CancelledError()
                records[index, 4, 1] = 1
                records[index, 2, 0] = {"ready": 1, "face_too_small": 2, "insufficient_landmarks": 3}[face.status]
                if face.status != "ready":
                    continue
                points = np.asarray(face.points, np.float32)
                if points.shape != (478, 2) or not np.isfinite(points).all():
                    raise GpuExecutionError("face_metadata", "CPU face landmarks do not match the GPU geometry contract")
                records[index, 8:, :2] = points
                records[index, 8:, 3] = 1
                records[index, 0, :2], records[index, 0, 2:] = points.min(axis=0), points.max(axis=0)
                oval = points[list(OVAL)]
                records[index, 1, :2], records[index, 1, 2:] = oval.min(axis=0), oval.max(axis=0)
            if not snapshot.faces and snapshot.status == "insufficient_landmarks":
                records[0, 7, 0] = 1
            records.setflags(write=False)
            identity = ("cpu-face-snapshot-v2", snapshot.reference_size, snapshot.detection_size,
                        hashlib.sha256(records).hexdigest())
        artifact = backend.operate(source.gpu_image, "face.cpu_snapshot", identity, (ROW, count),
            lambda device, abort: device.upload_packed(records, abort), cancelled, input_identity=())
    return DeviceFaceSnapshot(snapshot.identity, snapshot.reference_size, snapshot.detection_size, artifact,
        FaceGeometry(backend, identity))


class GpuFaceSession(FaceGeometry):
    def __init__(self, inference, task_path, *, bundle=None, detection_spec=None, locator_path=None,
                 runtime_path=None, directml_path=None, cancelled=lambda: False):
        self.inference, self.backend = inference, inference.gpu
        self.spec = detection_spec or DETECTION_SPECS[2]
        if self.spec not in DETECTION_SPECS.values() or (self.spec[0] == 3) != (locator_path is not None):
            raise ValueError("人脸定位模型与检测规格不匹配")
        self.stack, self.locator = ExitStack(), None
        self._closed = False
        self.runtime_options = {"runtime_path":runtime_path,"directml_path":directml_path}
        task_path = Path(task_path).resolve()
        bundle = Path(bundle) if bundle is not None else task_path.parent / "gpu-face" / TASK_DIGEST
        with self.backend.compute.borrow(cancelled, threads=1):
            if hashlib.sha256(task_path.read_bytes()).hexdigest() != TASK_DIGEST:
                raise GpuExecutionError("face_model_identity", "Face model weights do not match the original task")
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            if manifest["revision"] != REVISION or manifest["source_sha256"] != TASK_DIGEST:
                raise GpuExecutionError("face_model_identity", "Face conversion revision mismatch")
            for name, digest in MODELS.items():
                record = manifest[name]
                path = bundle / (name + ".onnx")
                if record["sha256"] != digest or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise GpuExecutionError("face_model_identity", "Converted face model digest mismatch")
        self.identity = (TASK_DIGEST, REVISION, tuple(sorted(MODELS.items())), self.spec,
                         self.backend.snapshot().identity)
        try:
            self.detector = self.stack.enter_context(inference.create_device_session(bundle / "face_detector.onnx",
                DeviceTensorModelSpec((128,128), ((1,896,16),(1,896,1)), input_colorspace="srgb",
                    source_colorspace="srgb",scale=2., bias=-1.),
                cancelled=cancelled,**self.runtime_options))
            self.landmarks = self.stack.enter_context(inference.create_device_session(bundle / "face_landmarks_detector.onnx",
                DeviceTensorModelSpec((256,256), ((1,1,1,1434),(1,1,1,1),(1,1)), input_colorspace="srgb",source_colorspace="srgb"),
                cancelled=cancelled,**self.runtime_options))
            if locator_path is not None:
                self.locator_path = Path(locator_path).resolve()
                with self.backend.compute.borrow(cancelled, threads=1):
                    locator_manifest = json.loads((bundle / "locator.json").read_text(encoding="utf-8"))
                    if (locator_manifest["revision"] != REVISION
                            or locator_manifest["source_sha256"] != "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
                            or locator_manifest["sha256"] != "a9e3c4d917ff991ab5df1dee4a8ba4d3b89c40ae60fd35f13cfcf20b99306072"):
                        raise GpuExecutionError("face_model_identity", "YuNet conversion revision mismatch")
                    if hashlib.sha256(self.locator_path.read_bytes()).hexdigest() != locator_manifest["source_sha256"]:
                        raise GpuExecutionError("face_model_identity", "YuNet weights do not match the original model")
                    self.locator_path = bundle / "face_locator.onnx"
                    if hashlib.sha256(self.locator_path.read_bytes()).hexdigest() != locator_manifest["sha256"]:
                        raise GpuExecutionError("face_model_identity", "Converted YuNet model digest mismatch")
                self.locator_identity = locator_manifest["sha256"]
            self.identity = (*self.identity[:-1],self.detector.identity,self.landmarks.identity,
                getattr(self,"locator_identity",None),self.backend.snapshot().identity)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def size_for(bounds):
        left, top, right, bottom = bounds
        ratio = min(1., 1600/max(right-left, bottom-top))
        return max(1, floor((right-left)*ratio+.5)), max(1, floor((bottom-top)*ratio+.5))

    def input(self, source, bounds, cancelled):
        size = self.size_for(bounds)
        return self.op(source, 0, size, cancelled, args=bounds), size

    def window(self, source, bounds, whole_size, window_index, cancelled):
        image, size = self.input(source, bounds, cancelled)
        model_input = self.op(image, 1, (128,128), cancelled)
        detection = self.detector.run(DeviceTensorInput(model_input,"srgb"), (bounds, self.identity), cancelled)
        boxes = self.op(image, 2, (5,896), cancelled, tensor=detection, args=(self.spec[3],))
        rois = self.op(boxes, 3, (2,5), cancelled, args=(0,.3,0,0,*size))
        flags = self.backend.face_metadata(self.op(rois,18,(1,5),cancelled),cancelled)
        width, height = source.orig_size
        left, top, right, bottom = bounds
        normalized = (left/width, top/height, (right-left)/width, (bottom-top)/height)
        candidates = []
        for face_index in range(5):
            if not flags[face_index,0,0]:
                continue
            image_input = self.op(image, 4, (256,256), cancelled, auxiliary=rois, args=(face_index,))
            tensors = self.landmarks.run(DeviceTensorInput(image_input,"srgb"), (bounds, face_index, self.identity), cancelled)
            points = self.op(image, 5, (478,1), cancelled, auxiliary=rois, tensor=tensors,
                args=(face_index,0,0,0,*normalized,*size))
            border = self.spec[10] if self.spec[0] == 2 else -1
            candidates.append(self.op(points, 6, (ROW,1), cancelled,
                args=(window_index,face_index,border,int(window_index == 0),*normalized,width,height,*whole_size,*size),
                data=(*OVAL,*EYES[0],*EYES[1])))
        return candidates

    def merge(self, state, candidate, size, cancelled):
        kind = 0 if self.spec[0] == 1 else 1 if self.spec[0] == 2 else 2
        return self.op(state, 7, state.resident.carrier.size, cancelled, auxiliary=candidate,
            args=(self.spec[8] if self.spec[0]>1 else .5,self.spec[9] if self.spec[0]>1 else .35,kind,0,*size), data=ANCHORS)

    def count(self, state, cancelled):
        control = self.op(state, 11, (1,1), cancelled)
        return self.backend.face_metadata(control, cancelled)[0,0]

    def prepare(self, source, identity, cancelled=lambda: False):
        if not self.backend.face_capable(self.identity):
            raise GpuExecutionError("face_qualification", "Face model route requires manual qualification")
        return self._prepare(source,identity,cancelled)

    def qualify(self, source, identity, reference, case, cancelled=lambda: False):
        if case not in {"single", "small", "multiple", "edge", "alpha", "crop", "rotation", "no_face"}:
            raise ValueError("GPU 人脸验收场景无效")
        try:
            return self._qualify(source,identity,reference,case,cancelled)
        except BaseException:
            if self.identity[-1] == self.backend.snapshot().identity:
                self.backend.record_face_qualification(self.identity,case,False)
            raise

    def _qualify(self, source, identity, reference, case, cancelled):
        import numpy as np
        result = self._prepare(source,identity,cancelled)
        actual = result.materialize(cancelled)
        passed = (actual.reference_size == reference.reference_size and actual.detection_size == reference.detection_size
                  and actual.status == reference.status and len(actual.faces) == len(reference.faces))
        maximum = 0.
        if passed:
            for first, second in zip(actual.faces,reference.faces):
                if first.status != second.status or len(first.points) != len(second.points):
                    passed = False
                    break
                if first.points:
                    maximum = max(maximum,float(np.max(np.abs((np.asarray(first.points)-np.asarray(second.points))*reference.detection_size))))
        passed = passed and maximum <= .5
        if case in ("single", "edge", "alpha", "crop", "rotation"):
            passed = passed and reference.processed >= 1
        elif case == "multiple":
            passed = passed and reference.processed >= 2
        elif case == "no_face":
            passed = passed and reference.status == "no_face"
        if case == "alpha":
            passed = passed and source.alpha is not None
        region_maximum = 0
        if passed:
            from ..core.face_regions import face_regions
            for index, face in enumerate(reference.faces):
                if face.status != "ready":
                    continue
                with self.backend.compute.borrow(cancelled,threads=1):
                    expected = face_regions(face.points,reference.reference_size)
                regions = result.regions(index,reference.reference_size,cancelled)
                if regions.bounds != expected.bounds:
                    passed = False
                    break
                for name in ("skin","features","under_eyes","mouth","support","eyes","lips"):
                    actual_mask = self.backend.face_metadata(getattr(regions,name),cancelled,final="region")[...,0]
                    expected_mask = getattr(expected,name)
                    region_maximum = max(region_maximum,int(np.max(np.abs(
                        np.rint(actual_mask*255)-np.rint(expected_mask*255)))))
        passed = passed and region_maximum <= 1
        self.backend.record_face_qualification(self.identity,case,passed)
        return {"passed":passed,"case":case,"max_point_pixels":maximum,"max_mask_lsb":region_maximum,"identity":self.identity,
                "actual":actual,"device_result":result}

    def _prepare(self, source, identity, cancelled):
        if (self._closed or not self.inference.device_models_enabled() or source.gpu_image is None or source.is_proxy
                or source.colorspace != "linear_srgb" or source.orig_size != source.pixels.shape[1::-1]):
            raise GpuExecutionError("face_input", "Face analysis requires the complete GPU input")
        width, height = source.orig_size
        whole = (0,0,width,height)
        size = self.size_for(whole)
        state = self.op(source, 13, (ROW,50), cancelled)
        windows = [whole]
        if self.spec[0] == 2:
            starts, fraction = self.spec[6:8]
            cw, ch = max(1,floor(width*fraction+.5)), max(1,floor(height*fraction+.5))
            for y in starts:
                for x in starts:
                    left, top = min(width-cw,floor(width*x+.5)), min(height-ch,floor(height*y+.5))
                    bounds = (left,top,left+cw,top+ch)
                    if bounds not in windows:
                        windows.append(bounds)
        for index, bounds in enumerate(windows):
            for candidate in self.window(source,bounds,size,index,cancelled):
                state = self.merge(state,candidate,size,cancelled)
            if self.spec[0] == 2 and self.count(state,cancelled)[0] >= 5:
                break
        if self.spec[0] == 2:
            selected = self.op(state,8,(ROW,5),cancelled,args=(1,))
            controls = self.op(selected,9,(2,5),cancelled,args=(width,height,self.spec[11]))
            metadata = self.backend.face_metadata(controls,cancelled)
            state = self.op(selected,13,(ROW,50),cancelled,args=(1,))
            confirmations = 0
            for index, row in enumerate(metadata):
                candidate = self.op(selected,10,(ROW,1),cancelled,
                    args=(self.spec[8],self.spec[9],index,0,*size), auxiliary=selected, data=ANCHORS)
                if row[1,0] and row[1,1] == 1 and not row[1,2]:
                    if confirmations >= self.spec[12]:
                        continue
                    confirmed = self.op(source,13,(ROW,50),cancelled)
                    for item in self.window(source,tuple(int(v) for v in row[0]),size,0,cancelled):
                        confirmed = self.merge(confirmed,item,size,cancelled)
                    confirmations += 1
                    candidate = self.op(selected,10,(ROW,1),cancelled,auxiliary=confirmed,
                        args=(self.spec[8],self.spec[9],index,0,*size),data=ANCHORS)
                state = self.merge(state,candidate,size,cancelled)
        if self.spec[0] == 3:
            state = self.located(source,state,size,cancelled)
        count = self.spec[14] if self.spec[0] == 3 else 5
        selected = self.op(state,8,(ROW,count),cancelled,args=(2 if self.spec[0] == 3 else 1,))
        result = self.op(selected,8,(ROW,count),cancelled,args=(0,))
        LOGGER.debug("GPU 人脸准备 spec=%s identity=%s device=%s",self.spec[0],self.identity,result.backend_identity)
        return DeviceFaceSnapshot((identity,self.identity),source.orig_size,size,result,self)

    def located(self, source, state, size, cancelled):
        image, _ = self.input(source,(0,0,*source.orig_size),cancelled)
        padded = tuple((value+31)//32*32 for value in size)
        counts = tuple(padded[0]//stride*(padded[1]//stride) for stride in (8,16,32))
        shapes = tuple((1,n,channels) for channels in (1,1,4,10) for n in counts)
        spec = DeviceTensorModelSpec(padded,shapes,layout="nchw",input_colorspace="srgb",source_colorspace="srgb",channel_order="bgr",scale=255.)
        with self.inference.create_device_session(self.locator_path,spec,cancelled=cancelled,**self.runtime_options) as session:
            from .gpu_color import GpuColor
            color = GpuColor(self.backend)
            encoded = replace(GpuGeometry.updated(source,image),colorspace="srgb")
            input_image = self.op(image, 1, padded, cancelled, args=(1,))
            input_image = color.stage(GpuGeometry.updated(encoded,input_image), "locator.bgr", 21, cancelled)
            tensor = session.run(DeviceTensorInput(input_image.gpu_image,"srgb","bgr"),(self.identity,self.locator_identity,padded),cancelled)
        offsets, offset = [], 0
        for shape in shapes:
            offsets.append(offset)
            offset += prod(shape)
        boxes = self.op(input_image,12,(5,sum(counts)),cancelled,tensor=tensor,
            args=(*counts[:2],self.spec[7],self.spec[11],*size),data=offsets)
        all_rois = self.op(boxes,3,(2,self.spec[13]+5000),cancelled,args=(1,self.spec[10],self.spec[11],self.spec[13],*size))
        rois = self.backend.operate(all_rois,"face.locator.view",(self.spec[13],),(2,self.spec[13]),
            lambda device,abort:device.view(all_rois.resident.carrier,0,0,2,self.spec[13]),cancelled,view=True,working_bytes=0)
        for index in range(self.spec[13]):
            for factor in self.spec[12]:
                control = self.op(rois,14,(2,1),cancelled,auxiliary=state,
                    args=(index,factor,0,0,*source.orig_size))
                metadata = self.backend.face_metadata(control,cancelled)[0]
                if metadata[1,2] < 0:
                    raise GpuExecutionError("face_target_limit", "YuNet selection exceeds GPU submission limits")
                if not metadata[1,0] or not metadata[1,1]:
                    break
                bounds = tuple(int(v) for v in metadata[0])
                candidates = self.window(source,bounds,size,index+1,cancelled)
                matching = self.op(source,13,(ROW,50),cancelled)
                for candidate in candidates:
                    candidate = self.op(candidate,15,(ROW,1),cancelled,auxiliary=rois,args=(index,))
                    matching = self.merge(matching,candidate,size,cancelled)
                if self.count(matching,cancelled)[1]:
                    candidate = self.op(matching,8,(ROW,1),cancelled,args=(3,))
                    state = self.merge(state,candidate,size,cancelled)
                    break
        return state

    def close(self):
        self._closed = True
        self.stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
