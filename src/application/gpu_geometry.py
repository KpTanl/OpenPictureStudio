from dataclasses import replace
import math

from ..core.contracts.gpu import DevicePatch, DevicePixels, GpuExecutionError
from ..core.contracts.tools import ToolReference
from ..core.tools.geometry import CROP_PARAMETERS, SHAPE_PARAMETERS, LASSO_PARAMETERS, crop_box, rotated_size


IMPLEMENTATIONS = frozenset((name, 2, name + ".float", "2") for name in ("crop", "shape_crop", "lasso"))
CONTRACTS = {"crop": CROP_PARAMETERS, "shape_crop": SHAPE_PARAMETERS, "lasso": LASSO_PARAMETERS}


def capable(implementation, settings):
    if implementation not in IMPLEMENTATIONS:
        return False
    if implementation[0] == "lasso":
        counts = [len(region["points"]) for region in settings["regions"]] + [len(settings["draft_points"])]
        return max(counts, default=0) <= 512 and sum(counts) <= 4096
    return True


def _tool(node):
    if node.node_type in ("source", "export"):
        return None
    if node.node_type != "tool":
        raise ValueError("GPU 几何节点不受支持")
    ref = ToolReference.from_dict(node.params)
    implementation = ref.tool_id, ref.params_version, ref.implementation_id, ref.implementation_version
    if ref.tool_id == "color_adjust":
        from .gpu_color import capable as color_capable
        from ..core.tools.color import color_engine_contract
        contract = color_engine_contract(node.params["settings"], ref.params_version)
        checked = contract.normalize(node.params["settings"], ref.params_version)
        if not checked.success or not color_capable(implementation, checked.data):
            raise ValueError("GPU 调色参数不受支持")
        return ref.tool_id, checked.data
    if ref.tool_id in ("beauty", "ai_enhance", "denoise", "bg_remove", "id_photo"):
        from .gpu_tool_routes import GpuToolRoutes
        if not GpuToolRoutes.supported(ref):
            raise ValueError("GPU 工具实现不受支持")
        contract = GpuToolRoutes.contract(ref)
        checked = contract.normalize(node.params["settings"], ref.params_version)
        if not checked.success:
            raise ValueError("GPU 工具参数不受支持")
        return ref.tool_id, checked.data
    if implementation not in IMPLEMENTATIONS:
        raise ValueError("GPU 几何实现不受支持")
    checked = CONTRACTS[ref.tool_id].normalize(node.params["settings"], ref.params_version)
    if not checked.success or not capable(implementation, checked.data):
        raise ValueError("GPU 几何参数不受支持")
    return ref.tool_id, checked.data


def workflow_capable(workflow):
    try:
        return all(node.version == 1 and (_tool(node) is not None or node.node_type in ("source", "export"))
                   for node in workflow.nodes)
    except (KeyError, TypeError, ValueError):
        return False


def target_plan(source, graph, order, steps=None):
    width, height = source.pixels.shape[1], source.pixels.shape[0]
    largest = width * height
    for node in (graph.node(identity) for identity in order) if order is not None else graph.nodes:
        item = _tool(node)
        if item is None:
            continue
        kind, params = item
        incoming_size = width, height
        if kind == "crop":
            width, height = rotated_size((width, height), params["rotation_angle"])
            if width * height > 100_000_000 or max(width, height) > 32768:
                raise ValueError("GPU 旋转尺寸超限")
            largest = max(largest, width * height)
            left, top, right, bottom = crop_box(params["crop_rect"], (width, height))
            width, height = right-left, bottom-top
        elif kind == "shape_crop":
            x, y, w, h = params["rect"]
            left, top, right, bottom = crop_box((x, y, min(1, x+w), min(1, y+h)), (width, height))
            width, height = right-left, bottom-top
        elif kind == "ai_enhance":
            from ..core.tools.enhancement import output_plan
            width, height = output_plan((width, height), params).output_size
            largest = max(largest, width*height)
        elif kind == "id_photo":
            from ..core.tools.id_photo import SHEET_SIZE, LAYOUT_SHEET
            photo_size = params["custom_width"], params["custom_height"]
            largest = max(largest, photo_size[0]*photo_size[1])
            width, height = SHEET_SIZE if params["layout"] == LAYOUT_SHEET else photo_size
            largest = max(largest, width*height)
        if width * height > 100_000_000 or max(width, height) > 32768:
            raise ValueError("GPU 几何尺寸超限")
        if steps is not None:
            steps[node.node_id] = incoming_size, (width, height)
    if largest > 100_000_000 or max(source.pixels.shape[:2]) > 32768:
        raise ValueError("GPU 输入尺寸超限")
    return (width, height), largest


def shape_primitives(size, kind, radius):
    width, height = size
    x1, y1 = width-1, height-1
    commands = []
    def rect(bounds):
        left, top, right, bottom = bounds
        if left <= right and top <= bottom:
            commands.append([0, left, top, right, bottom, 0, 0, x1, y1])
    def ellipse(bounds, clip=None):
        bounds = [int(value) for value in bounds]
        commands.append([1, *bounds, *(clip or [0, 0, x1, y1])])
    if kind in ("ellipse", "circle"):
        w, h = (min(size),) * 2 if kind == "circle" else size
        left, top = (width-w)//2, (height-h)//2
        ellipse((left, top, left+w-1, top+h-1))
    elif kind == "rect" or radius == 0:
        rect((0, 0, x1, y1))
    else:
        diameter = radius * 2
        full_x = diameter >= x1-1
        if full_x:
            diameter = x1
        full_y = diameter >= y1-1
        if full_y:
            diameter = y1
        if full_x and full_y:
            ellipse((0, 0, x1, y1))
            return commands
        r = int(diameter // 2)
        if full_x:
            ellipse((0, 0, diameter, diameter), [0, 0, x1, r])
            ellipse((0, y1-diameter, diameter, y1), [0, y1-r, x1, y1])
            rect((0, r+1, x1, y1-r-1))
        elif full_y:
            ellipse((0, 0, diameter, diameter), [0, 0, r, y1])
            ellipse((x1-diameter, 0, x1, diameter), [x1-r, 0, x1, y1])
            rect((r+1, 0, x1-r-1, y1))
        else:
            ellipse((0, 0, diameter, diameter), [0, 0, r, r])
            ellipse((x1-diameter, 0, x1, diameter), [x1-r, 0, x1, r])
            ellipse((x1-diameter, y1-diameter, x1, y1), [x1-r, y1-r, x1, y1])
            ellipse((0, y1-diameter, diameter, y1), [0, y1-r, r, y1])
            rect((r+1, 0, x1-r-1, y1))
            rect((0, r+1, r, y1-r-1))
            rect((x1-r, r+1, x1, y1-r-1))
    return commands


class GpuGeometry:
    def __init__(self, backend):
        self.backend = backend

    @staticmethod
    def updated(source, artifact, *, size=None, alpha=None):
        width, height = artifact.resident.carrier.size
        has_alpha = source.alpha is not None if alpha is None else alpha
        return replace(source, pixels=DevicePixels((height, width, 3)),
            alpha=DevicePixels((height, width)) if has_alpha else None, gpu_image=artifact,
            orig_size=source.orig_size if size is None else size, patches=(), patch_base_identity=(),
            execution_ownership=None, backend_identity=artifact.backend_identity)

    def op(self, source, name, identity, mode, args, size, cancelled, other=None):
        artifact = self.backend.resolve(source.gpu_image, cancelled) if source.gpu_image.patches else source.gpu_image
        secondary = artifact if other is None else self.backend.resolve(other, cancelled) if other.patches else other
        return self.backend.operate(artifact, name, (identity, secondary.resident.identity), size, lambda device, abort:
            device.image_op(artifact.resident.carrier, secondary.resident.carrier, mode, args, *size, abort), cancelled)

    def view(self, source, bounds, identity, cancelled, *, formal_size=None):
        left, top, right, bottom = bounds
        artifact = source.gpu_image
        size = right-left, bottom-top
        if artifact.patches:
            result = self.backend.resolve(artifact, cancelled, bounds)
            return self.updated(source, result, size=formal_size)
        result = self.backend.operate(artifact, "view", (identity, bounds), size, lambda device, abort:
            device.view(artifact.resident.carrier, left, top, *size), cancelled, view=True)
        return self.updated(source, result, size=formal_size)

    def execute(self, reference, source, params, upstream_identity, cancelled):
        implementation = reference.tool_id, reference.params_version, reference.implementation_id, reference.implementation_version
        if not capable(implementation, params) or source.gpu_image is None:
            raise GpuExecutionError("chain_capability_missing", "GPU geometry implementation is unavailable")
        key = upstream_identity, source.pixels.shape, source.orig_size, source.is_proxy
        if CONTRACTS[reference.tool_id].is_neutral(params, 2):
            return source
        if reference.tool_id == "crop":
            return self.crop(source, params, key, cancelled)
        if reference.tool_id == "shape_crop":
            return self.shape(source, params, key, cancelled)
        return self.lasso(source, params, key, cancelled)

    def crop(self, source, params, key, cancelled):
        angle = params["rotation_angle"]
        width, height = source.pixels.shape[1], source.pixels.shape[0]
        if angle:
            size = rotated_size((width, height), angle)
            radians = -math.radians(angle % 360)
            a, b = round(math.cos(radians), 15), round(math.sin(radians), 15)
            d, e = -b, a
            c = width/2-a*size[0]/2-b*size[1]/2
            f = height/2-d*size[0]/2-e*size[1]/2
            matrix = [float((round(value * (1 << 40)) >> shift) & 65535)
                      for value in (a,b,c,f) for shift in (0,16,32,48)]
            artifact = self.op(source, "crop.rotation", (key, angle), 1, matrix, size, cancelled)
            formal = rotated_size(source.orig_size, angle) if source.is_proxy else size
            source = self.updated(source, artifact, size=formal, alpha=True)
            width, height = size
        bounds = crop_box(params["crop_rect"], (width, height))
        fl, ft, fr, fb = crop_box(params["crop_rect"], source.orig_size)
        return self.view(source, bounds, (key, angle, params["crop_rect"]), cancelled, formal_size=(fr-fl,fb-ft))

    def shape(self, source, params, key, cancelled):
        input_ratio=min(source.pixels.shape[1]/source.orig_size[0],source.pixels.shape[0]/source.orig_size[1])
        x,y,w,h = params["rect"]
        normalized = x,y,min(1,x+w),min(1,y+h)
        bounds = crop_box(normalized, (source.pixels.shape[1],source.pixels.shape[0]))
        fl,ft,fr,fb = crop_box(normalized, source.orig_size)
        source = self.view(source,bounds,(key,"shape.bounds",normalized),cancelled,formal_size=(fr-fl,fb-ft))
        width,height = source.pixels.shape[1],source.pixels.shape[0]
        primitives = shape_primitives((width,height),params["shape"],params["corner_radius"]*input_ratio)
        mask = self.backend.operate(source.gpu_image,"shape.mask",(key,params),(width,height),
            lambda device,abort:device.shape_mask(width,height,primitives,abort),cancelled)
        result = self.op(source,"shape.alpha",(key,params),4,[],(width,height),cancelled,mask)
        return self.updated(source,result,alpha=True)

    def lasso(self, source, params, key, cancelled):
        width,height = source.pixels.shape[1],source.pixels.shape[0]
        regions = list(params["regions"])
        if len(params["draft_points"])>=3:
            regions.append({"points":params["draft_points"],"op":"add"})
        normalized = []
        for region in regions:
            points=[]
            for x,y in region["points"]:
                point=max(0,min(width-1,round(x*width))),max(0,min(height-1,round(y*height)))
                if not points or points[-1]!=point:
                    points.append(point)
            normalized.append((region["op"]=="subtract",points))
        radius=params["feather"]*min(width/source.orig_size[0],height/source.orig_size[1])
        mask=self.backend.operate(source.gpu_image,"lasso.mask",(key,params),(width,height),lambda device,abort:
            device.lasso_mask(width,height,normalized,not params["keep_inside"],radius,abort),cancelled)
        result=self.op(source,"lasso.alpha",(key,params),4,[],(width,height),cancelled,mask)
        return self.updated(source,result,alpha=True)

    def resize(self, source, size, identity, cancelled, box=None):
        width,height=source.pixels.shape[1],source.pixels.shape[0]
        left,top,right,bottom=box or (0,0,width,height)
        working=source
        if source.alpha is not None:
            working=self.updated(source,self.op(source,"resize.premultiply",identity,2,[],(width,height),cancelled))
        horizontal=self.op(working,"resize.horizontal",(identity,box,size),8,[left,right],(size[0],height),cancelled)
        working=self.updated(working,horizontal)
        vertical=self.op(working,"resize.vertical",(identity,box,size),9,[top,bottom],size,cancelled)
        working=self.updated(working,vertical,size=size)
        if source.alpha is not None:
            working=self.updated(working,self.op(working,"resize.straight",(identity,box,size),3,[],size,cancelled),size=size)
        return working

    def remap(self, source, field, identity, cancelled):
        size=source.pixels.shape[1],source.pixels.shape[0]
        result=self.op(source,"remap",identity,6,[],size,cancelled,field)
        return self.updated(source,result)

    def guided(self, source, guide, radius, eps, identity, cancelled, subsample=1):
        size=source.pixels.shape[1],source.pixels.shape[0]
        artifact=self.backend.resolve(source.gpu_image,cancelled)
        guide=self.backend.resolve(guide,cancelled)
        result=self.backend.operate(artifact,"guided",(identity,guide.resident.identity,radius,eps,subsample),size,lambda device,abort:
            device.guided(artifact.resident.carrier,guide.resident.carrier,radius,eps,subsample,abort),cancelled)
        return self.updated(source,result)

    def with_patch(self, source, patch, bounds, identity, *, has_alpha=False):
        left,top,right,bottom=bounds
        if (not 0 <= left < right <= source.pixels.shape[1] or not 0 <= top < bottom <= source.pixels.shape[0]
                or patch.resident.carrier.size != (right-left,bottom-top)):
            raise ValueError("GPU 补丁范围无效")
        item=DevicePatch(tuple(bounds),patch,tuple(identity),source.orig_size,has_alpha)
        artifact=replace(source.gpu_image,patches=source.gpu_image.patches+(item,))
        return self.updated(source,artifact,alpha=source.alpha is not None or has_alpha)

    def compose(self, source, patch, bounds, identity, cancelled, *, has_alpha=True):
        left,top,right,bottom=bounds
        if (not 0 <= left < right <= source.pixels.shape[1] or not 0 <= top < bottom <= source.pixels.shape[0]
                or patch.resident.carrier.size!=(right-left,bottom-top)):
            raise ValueError("GPU 补丁范围无效")
        size=source.pixels.shape[1],source.pixels.shape[0]
        result=self.op(source,"patch.compose",(identity,bounds,has_alpha),5,[left,top,float(not has_alpha)],size,cancelled,patch)
        return self.updated(source,result)
