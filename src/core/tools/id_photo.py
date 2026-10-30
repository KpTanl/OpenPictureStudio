from __future__ import annotations

from .catalog import ParameterContract, ParameterField
from ..contracts.tool_resources import ModelRequirement, RequiredResource, ResourceFeature


# 常见证件照尺寸预设 (宽, 高) 单位：像素 @300dpi
SIZE_PRESETS = {
    "一寸": (295, 413),
    "二寸": (413, 626),
    "小一寸": (260, 378),
    "大一寸": (390, 567),
    "护照": (354, 472),
    "签证": (531, 709),
    "社保": (358, 441),
    "自定义": None,
}

# 常见背景色预设
BG_PRESETS = {
    "白色": (255, 255, 255),
    "蓝色": (67, 142, 219),
    "红色": (255, 0, 0),
    "浅蓝": (135, 206, 250),
    "灰色": (211, 211, 211),
}


# 人像参考线：头顶线与下巴线距裁切范围上沿的比例、头部范围的宽度，
# 以及从人脸框推到头顶与下巴时各留的余量（相对人脸的高度）
PORTRAIT_GUIDE = {
    "head_top_ratio": 0.12,
    "chin_ratio": 0.68,
    "head_width_ratio": 0.40,
    "head_top_pad_ratio": 0.18,
    "chin_bottom_pad_ratio": 0.08,
}

# 6 寸相纸（300 dpi）：纸张尺寸、页边距、照片间距，以及每张照片外加的一条细边的颜色（8 位 sRGB）
SHEET_SIZE = (1800, 1200)
SHEET_MARGIN = 30
SHEET_GAP = 20
SHEET_BORDER = (200, 200, 200)

LAYOUT_SINGLE = "single"
LAYOUT_SHEET = "sheet_6inch"


def sheet_grid(photo_size):
    """一张 6 寸相纸上排几列、几行。"""
    width, height = photo_size
    return ((SHEET_SIZE[0] - 2 * SHEET_MARGIN + SHEET_GAP) // (width + SHEET_GAP),
            (SHEET_SIZE[1] - 2 * SHEET_MARGIN + SHEET_GAP) // (height + SHEET_GAP))


def sheet_positions(photo_size):
    """相纸上各张照片左上角的位置：整组居中，自上而下、自左至右。"""
    width, height = photo_size
    columns, rows = sheet_grid(photo_size)
    start_x = (SHEET_SIZE[0] - (columns * width + (columns - 1) * SHEET_GAP)) // 2
    start_y = (SHEET_SIZE[1] - (rows * height + (rows - 1) * SHEET_GAP)) // 2
    return tuple((start_x + column * (width + SHEET_GAP), start_y + row * (height + SHEET_GAP))
                 for row in range(rows) for column in range(columns))


def photo_bounds(crop_rect, image_size, ratio):
    """裁切范围在输入画面上的位置（左、上、右、下，输入像素坐标，可以是小数）。

    在 crop_rect 给出的范围里取符合宽高比 ratio 的居中最大范围；crop_rect 为空时范围是整幅。
    给出的范围与目标比例不一致时只取其中符合比例的部分，画面不会被拉伸。
    """
    width, height = image_size
    left, top, right, bottom = crop_rect or (0, 0, 1, 1)
    left, top, right, bottom = left * width, top * height, right * width, bottom * height
    box_width, box_height = right - left, bottom - top
    if box_width > box_height * ratio:
        fitted = box_height * ratio
        left += (box_width - fitted) / 2
        right = left + fitted
    else:
        fitted = box_width / ratio
        top += (box_height - fitted) / 2
        bottom = top + fitted
    return left, top, right, bottom


def validate_id_photo(params):
    size = (params["custom_width"], params["custom_height"])
    preset = SIZE_PRESETS[params["preset_size"]]
    if preset is not None and size != preset:
        raise ValueError("尺寸与规格不一致")
    box = params["crop_rect"]
    if box is not None:
        if len(box) != 4 or any(type(value) not in (int, float) or not 0 <= value <= 1 for value in box):
            raise ValueError("裁切范围无效")
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("裁切范围无效")
    color = params["background"]
    if color is not None and (len(color) != 3 or any(type(value) is not int or not 0 <= value <= 255
                                                    for value in color)):
        raise ValueError("背景色无效")
    if params["layout"] == LAYOUT_SHEET and 0 in sheet_grid(size):
        raise ValueError("相纸放不下这个尺寸")


# 参数版本 2：在输入画面上按规格的比例裁切，可选抠图并铺背景色，缩放到规格的像素尺寸，可选排满一张相纸。
# crop_rect 是相对输入画面的 [左, 上, 右, 下]，空为整幅；background 是 8 位 sRGB 的 [R, G, B]，空为保留原背景；
# ai_matting 为假时只在已有的透明处铺背景色；segmentation_model 是抠图模型的标识，由面板填入当前默认的分割模型，
# 与移除背景同一种写法。
MANAGED_ID_PHOTO_PARAMETERS = ParameterContract((
    ParameterField("preset_size", str, "一寸", choices=tuple(SIZE_PRESETS)),
    ParameterField("custom_width", int, 295, 50, 4000),
    ParameterField("custom_height", int, 413, 50, 4000),
    ParameterField("crop_rect", (list, tuple, type(None)), None),
    ParameterField("background", (list, tuple, type(None)), None),
    ParameterField("ai_matting", bool, True),
    ParameterField("layout", str, LAYOUT_SINGLE, choices=(LAYOUT_SINGLE, LAYOUT_SHEET)),
    ParameterField("segmentation_model", str, ""),
), version=2, validate=validate_id_photo)

ID_PHOTO_RESOURCE_FEATURES = (
    ResourceFeature("basic"),
    ResourceFeature("ai", (ModelRequirement("model", "segmentation", ("u2net",), "segmentation_model"),)),
)


def id_photo_required_resources(params):
    """只有要铺背景色并且先抠图时才需要模型。"""
    if params["background"] is None or not params["ai_matting"]:
        return ()
    feature = ID_PHOTO_RESOURCE_FEATURES[1]
    model = feature.models[0]
    return (RequiredResource(feature.feature_id, model.role, model.capability, model.adapters,
                             params[model.selection_field]),)


def normalized_rect(rect, image_size):
    """像素矩形（左、上、右、下）换成相对画面的比例。"""
    width, height = image_size
    return [rect[0] / width, rect[1] / height, rect[2] / width, rect[3] / height]


def refit_crop_rect(crop_rect, image_size, ratio):
    """换规格时的裁切范围：保持中心与高度，按新的宽高比收放；画面放不下时整体缩小，再收进画面以内。"""
    width, height = image_size
    left, top, right, bottom = crop_rect or (0, 0, 1, 1)
    center_x, center_y = (left + right) / 2 * width, (top + bottom) / 2 * height
    box_height = (bottom - top) * height
    box_width = box_height * ratio
    if box_width > width:
        box_width, box_height = width, width / ratio
    if box_height > height:
        box_width, box_height = height * ratio, height
    left = min(max(0.0, center_x - box_width / 2), width - box_width)
    top = min(max(0.0, center_y - box_height / 2), height - box_height)
    return [left / width, top / height, min(1.0, (left + box_width) / width), min(1.0, (top + box_height) / height)]
