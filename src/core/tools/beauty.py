"""美颜强度参数合同。"""

from __future__ import annotations

from .catalog import ParameterContract, ParameterField


BEAUTY_PARAMETERS = ParameterContract(
    (
        ParameterField("skin_smooth", int, 0, 0, 100),
        ParameterField("skin_brighten", int, 0, 0, 100),
        ParameterField("skin_rosy", int, 0, 0, 100),
        ParameterField("sharpen", int, 0, 0, 100),
        ParameterField("eye_enlarge", int, 0, 0, 100),
        ParameterField("eye_distance", int, 0, -100, 100),
        ParameterField("face_slim", int, 0, 0, 100),
        ParameterField("nose_slim", int, 0, 0, 100),
        ParameterField("teeth_whiten", int, 0, 0, 100),
        ParameterField("spot_remove", int, 0, 0, 100),
        ParameterField("eye_bag_remove", int, 0, 0, 100),
    ),
    version=2,
    neutral=lambda params: not any(params.values()),
)
