from dataclasses import replace
import numpy as np

from ..core.contracts.gpu import DevicePixels, GpuArtifact, GpuResident
from ..core.contracts.tools import ToolReference
from ..core.photo_engine import RenderSource, display_rgba8
from ..core.tools.geometry import execute_crop, execute_shape, execute_lasso, CROP_PARAMETERS, SHAPE_PARAMETERS, LASSO_PARAMETERS
from .gpu_geometry import GpuGeometry


class _Qualification:
    def __init__(self, device, cancelled):
        self.device, self.cancelled = device, cancelled

    def operate(self, artifact, name, identity, size, operation, cancelled, **kwargs):
        return GpuArtifact(GpuResident(operation(self.device, cancelled), artifact.backend_identity,
                           identity=(artifact.resident.identity, name, identity)),
                           self, artifact.family, artifact.source_path)

    @staticmethod
    def resolve(artifact, cancelled):
        return artifact


def qualify_color(device, compute, cancelled):
    from uuid import UUID
    from ..core.photo_engine import render_source
    from ..core.tools.color import color_engine_contract
    from .gpu_color import GpuColor
    y, x = np.mgrid[:41, :47]
    with compute.borrow(cancelled, priority=2, threads=1):
        pixels = np.stack(((x % 19)/18, (y % 17)/16, ((x*7+y*11)%37)/36), axis=-1).astype(np.float32)
        pixels[0, :3] = ((0, 0, 0), (2.5, .03, -.1), (.0003, .0008, .004))
        alpha = ((x+y)%13).astype(np.float32)/12
        source = RenderSource(pixels, alpha, "linear32", "qualification", False, (47, 41), "srgb-direct")
        cases = []
        for version in (1, 2, 3, 4):
            base = {"process_version": version, "look": "ops_v2" if version == 4 else "opv_v2", "temperature": 19, "tint": -13,
                "exposure": 1.27, "brightness": 1.17, "contrast": 1.16, "saturation": 1.11,
                "vibrance": 33, "shadows": 28, "highlights": -41, "whites": 17, "blacks": -9,
                "texture": 21, "clarity": -16, "dehaze": 25, "sharpness": 1.02,
                "curves": {"rgb": [[0, 0], [53, 41], [180, 197], [255, 255]], "g": [[0, 0], [127, 142], [255, 255]]},
                "color_mixer": {"orange": {"hue": -13, "sat": 21, "lum": -17}, "blue": {"hue": 11, "sat": -19}}}
            cases.append(base)
            local = {"id": str(UUID(int=1)), "name": "1", "enabled": True, "type": "radial",
                "center": [.43, .57], "radius": [.37, .23], "angle": 23, "feather": .57,
                "params": {"exposure": -.73, "shadows": 29, "saturation": .21}}
            if version == 4:
                shape = {key: value for key, value in local.items() if key != "params"}
                shape["op"] = "add"
                local = {"id": str(UUID(int=2)), "name": "2", "enabled": True,
                    "params": {"temperature": -31, "texture": -21, "clarity": 25, "dehaze": -14},
                    "components": [shape, {"id": str(UUID(int=3)), "name": "3", "enabled": True,
                        "op": "subtract", "type": "brush", "points": [[.1, .3], [.8, .6]],
                        "radius": .12, "hardness": .4, "opacity": .63}]}
            cases.append({**base, "masks": [local]})
        expected = [display_rgba8(render_source(source, case, threads=1, cancelled=cancelled)) for case in cases]
    backend = _Qualification(device, cancelled)
    carrier = device.upload(pixels, alpha, cancelled)
    artifact = GpuArtifact(GpuResident(carrier, ("gpu", "qualification", "qualification"), identity=("color",)),
                           backend, (), "qualification")
    gpu_source = replace(source, pixels=DevicePixels((41,47,3)), alpha=DevicePixels((41,47)), gpu_image=artifact)
    difference = 0
    for case, reference in zip(cases, expected):
        contract = color_engine_contract(case)
        checked = contract.normalize(case, contract.version)
        if not checked.success:
            return False, "color_parameters"
        result = GpuColor(backend).execute(ToolReference("color_adjust", contract.version,
            "color_adjust.float", str(contract.version)), gpu_source, checked.data, ("qualification",), cancelled)
        actual = device.display(result.gpu_image.resident.carrier, 47, 41, cancelled)
        if actual.shape != reference.shape:
            return False, "color_shape"
        difference = max(difference, int(np.abs(actual.astype(np.int16)-reference.astype(np.int16)).max()))
        if difference > 1:
            return False, "color_numeric_" + str(difference)
    return True, ""


def qualify_geometry(device, compute, cancelled):
    with compute.borrow(cancelled, priority=2, threads=1):
        y, x = np.mgrid[:27, :33]
        pixels = np.stack(((x % 17)/16, (y % 13)/12, ((x*7+y*11)%31)/30), axis=-1).astype(np.float32)
        alpha = ((x+y)%11).astype(np.float32)/10
        source = RenderSource(pixels, alpha, "linear32", "qualification", False, (33,27), "linear_srgb")
        cases = []
        for angle in (0, 90, 180, 270, 2, -17):
            params = {**CROP_PARAMETERS.defaults(2), "rotation_angle":angle, "crop_rect":[0.1,0.07,0.91,0.93]}
            cases.append(("crop",params,execute_crop(source,params)))
        for shape in ("rect","circle","ellipse","round_rect"):
            params={**SHAPE_PARAMETERS.defaults(2),"shape":shape,"corner_radius":3.25,"rect":[0.12,0.11,0.73,0.77]}
            cases.append(("shape_crop",params,execute_shape(source,params)))
        for feather in (0,2.25):
            params={**LASSO_PARAMETERS.defaults(2),"feather":feather,"keep_inside":False,"regions":[
                {"points":[[0.03,0.12],[0.8,0.06],[0.65,0.91],[0.2,0.7]],"op":"add"},
                {"points":[[0.3,0.3],[0.52,0.27],[0.4,0.6]],"op":"subtract"}]}
            cases.append(("lasso",params,execute_lasso(source,params)))
        expected=[display_rgba8(result) for _,_,result in cases]
    backend=_Qualification(device,cancelled)
    carrier=device.upload(pixels,alpha,cancelled)
    artifact=GpuArtifact(GpuResident(carrier,("gpu","qualification","qualification")),backend,(),"qualification")
    gpu_source=replace(source,pixels=DevicePixels((27,33,3)),alpha=DevicePixels((27,33)),gpu_image=artifact)
    geometry=GpuGeometry(backend)
    difference=0
    for index,((kind,params,_),reference) in enumerate(zip(cases,expected)):
        result=geometry.execute(ToolReference(kind,2,kind+".float","2"),gpu_source,params,("qualification",index),cancelled)
        width,height=result.gpu_image.resident.carrier.size
        actual=device.display(result.gpu_image.resident.carrier,width,height,cancelled)
        if actual.shape!=reference.shape:
            return False, "geometry_shape"
        with compute.borrow(cancelled,priority=2,threads=1):
            difference=max(difference,int(np.abs(actual.astype(np.int16)-reference.astype(np.int16)).max()))
        if difference>1:
            return False, "geometry_numeric:"+kind
    return True, ""
