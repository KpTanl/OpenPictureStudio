from dataclasses import dataclass
from functools import cache

from ..core.face_regions import OVAL, EYES, BROWS, LIPS, MOUTH, NOSTRILS
from ..core.contracts.gpu import GpuExecutionError
from .gpu_color import GpuColor


@dataclass(frozen=True)
class DeviceFaceRegions:
    bounds: tuple
    points: object
    distance: float
    skin: object
    features: object
    under_eyes: object
    mouth: object
    support: object
    eyes: object
    lips: object
    index: int
    target_size: tuple


def prepare_regions(snapshot, index, size, cancelled, *, include_support=True, required=None):
    session = snapshot.session
    if not 0 <= index < snapshot.artifact.resident.carrier.size[1] or max(size)>8192:
        raise GpuExecutionError("face_regions", "Unsupported face region target")
    control = session.op(snapshot.artifact,16,(2,1),cancelled,
        args=(index,0,0,0,*size),data=(*EYES[0],*EYES[1]))
    metadata = session.backend.face_metadata(control,cancelled)[0]
    bounds = tuple(int(value) for value in metadata[0])
    distance, feather, expansion, support_radius = (float(value) for value in metadata[1])
    left, top, right, bottom = bounds
    if right<=left or bottom<=top or feather>256 or support_radius>1024:
        raise GpuExecutionError("face_regions", "Face region exceeds submission limits")
    empty = session.op(snapshot.artifact,13,(right-left,bottom-top),cancelled)
    from ..core.photo_engine import RenderSource
    from ..core.contracts.gpu import DevicePixels
    source = RenderSource(DevicePixels((bottom-top,right-left,3)),None,"linear32",empty.source_path,False,
        (right-left,bottom-top),"linear_srgb",gpu_image=empty,backend_identity=empty.backend_identity)
    color = GpuColor(session.backend)
    # 这些算子的名字不在跨帧保留的阶段之列：二十多张与人脸区域同大的中间蒙版用完即放，
    # 要跨帧沿用的调用方自己保留最终蒙版（美颜见 GpuBeauty.regions）
    def op(image, name, mode, *, auxiliary=None, args=(), data=(), field=None, extra=None):
        return color.stage(image,"region."+name,mode,cancelled,auxiliary=auxiliary,args=args,data=data,field=field,extra=extra)
    @cache
    def polygon(indices):
        return op(source,"face.polygon",32,args=(index,len(indices),*size,left,top),
                  data=indices,field=snapshot.artifact)
    def union(images):
        result=images[0]
        for image in images[1:]:
            result=op(result,"face.union",39,auxiliary=image)
        return result
    def inward(image, invert=False):
        binary=op(image,"face.border",36,args=(int(invert),))
        prefix=op(binary,"face.prefix",33)
        return op(binary,"face.inward",35,args=(feather,),auxiliary=prefix)
    def dilate(image,radius):
        prefix=op(image,"face.prefix",33)
        return op(image,"face.dilate",34,args=(radius,),auxiliary=prefix)

    @cache
    def skin():
        protected=dilate(union([polygon(indices) for indices in (*EYES,*BROWS,LIPS,*NOSTRILS)]),expansion)
        return op(inward(polygon(OVAL)),"face.skin",38,extra=inward(protected,True))

    @cache
    def lips():
        return op(polygon(LIPS),"face.lips",37,auxiliary=polygon(MOUTH))

    def features():
        masks=[polygon(indices) for indices in (*EYES,*BROWS)]
        return inward(union([*masks,lips()]))

    def under_eyes():
        under=op(source,"face.under",40,args=(index,0,*size,left,top),data=(*EYES[0],*EYES[1]),field=snapshot.artifact)
        return op(inward(under),"face.under.skin",38,extra=skin())

    builders={"skin":skin,"features":features,"under_eyes":under_eyes,
        "mouth":lambda:inward(polygon(MOUTH)),"support":lambda:inward(dilate(polygon(OVAL),support_radius)),
        "eyes":lambda:inward(union([polygon(eye) for eye in EYES])),"lips":lambda:inward(lips())}
    required=set(builders) if required is None else set(required)
    if not include_support:
        required.discard("support")
    masks={name:builder().gpu_image if name in required else None for name,builder in builders.items()}
    return DeviceFaceRegions(bounds,snapshot.artifact,distance,**masks,index=index,target_size=tuple(size))
