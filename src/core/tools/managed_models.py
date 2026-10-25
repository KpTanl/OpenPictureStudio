from .catalog import ParameterContract, ParameterField
from .effects import BG_PARAMETERS
from .beauty import BEAUTY_PARAMETERS
from ..contracts.beauty import BEAUTY_MODEL_FIELDS, DETAIL_BEAUTY_PROFILES
from ..contracts.tool_resources import ModelRequirement, RequiredResource, ResourceFeature


BACKGROUND_RESOURCE_FEATURES = (
    ResourceFeature("color"),
    ResourceFeature("ai", (ModelRequirement("model", "segmentation", ("u2net",), "segmentation_model"),)),
)
BEAUTY_RESOURCE_FEATURES = (
    ResourceFeature("effect", (ModelRequirement("face_model", "face_detection", ("face_landmarker",), "face_detection_model"),)),
    ResourceFeature("analysis", (ModelRequirement("face_model", "face_detection", ("face_landmarker",), "face_detection_model"),)),
)
# 美颜可选的人脸定位模型：参数 face_locator_model 选了它才开启多人脸检测（检测规格 3）
FACE_LOCATOR_MODEL = "face-detector-yunet@opencv-2023mar"
FACE_LOCATOR_REQUIREMENT = ModelRequirement("face_locator", "face_localization", ("yunet",))


def beauty_uses_locator(algorithm):
    """beauty.detail 的这个效果版本能否使用人脸定位模型；None 表示旧的受管实现。"""
    return algorithm is not None and DETAIL_BEAUTY_PROFILES[algorithm][2] >= 3


def beauty_effect_algorithm(algorithm, params):
    """实际执行的效果版本：计算配方已是最新的记录选了人脸定位模型时，按支持多人脸的最新版本执行。"""
    if algorithm is None or not params.get("face_locator_model") or beauty_uses_locator(algorithm):
        return algorithm
    latest = max(DETAIL_BEAUTY_PROFILES)
    return latest if DETAIL_BEAUTY_PROFILES[algorithm][1] == DETAIL_BEAUTY_PROFILES[latest][1] else algorithm


def effect_settings(params, *fields):
    return {key: value for key, value in params.items() if key not in fields}


def validate_background(params):
    checked = BG_PARAMETERS.normalize(effect_settings(params, "segmentation_model"), 2)
    if not checked.success:
        raise ValueError(checked.error.message_key)


MANAGED_BG_PARAMETERS = ParameterContract((*BG_PARAMETERS.fields,
    ParameterField("segmentation_model", str, "")), version=3, validate=validate_background)
MANAGED_BEAUTY_PARAMETERS = ParameterContract((*BEAUTY_PARAMETERS.fields,
    ParameterField("face_detection_model", str, ""), ParameterField("face_locator_model", str, "")), version=3,
    neutral=lambda params: not any(effect_settings(params, *BEAUTY_MODEL_FIELDS).values()))


def background_required_resources(params):
    if params["mode"] != "ai":
        return ()
    feature = BACKGROUND_RESOURCE_FEATURES[1]
    model = feature.models[0]
    return (RequiredResource(feature.feature_id, model.role, model.capability, model.adapters,
                             params[model.selection_field]),)


def beauty_required_resources(params, algorithm=None):
    """美颜实际用到的模型；algorithm 为 beauty.detail 的效果版本，选了人脸定位模型时另需它。"""
    if MANAGED_BEAUTY_PARAMETERS.is_neutral(params, 3):
        return ()
    feature = BEAUTY_RESOURCE_FEATURES[0]
    model = feature.models[0]
    required = (RequiredResource(feature.feature_id, model.role, model.capability, model.adapters,
                                 params[model.selection_field]),)
    if params.get("face_locator_model") and beauty_uses_locator(beauty_effect_algorithm(algorithm, params)):
        locator = FACE_LOCATOR_REQUIREMENT
        required += (RequiredResource(feature.feature_id, locator.role, locator.capability, locator.adapters,
                                      params["face_locator_model"]),)
    return required
