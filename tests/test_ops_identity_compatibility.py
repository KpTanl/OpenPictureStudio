"""历史成像格式与调色参数版本的兼容读取。"""
import json

import numpy as np
import pytest
import tifffile
from src.application.tool_modules import module_for
from src.core.contracts.image_display import NonRawDisplayMode, RawDisplayMode
from src.core.looks import look_stages
from src.core.render_codec import read_linear
from src.core.tools.color import (COLOR_PARAMETERS, LEGACY_COLOR_PARAMETERS, color_engine_contract,
                                  color_look_options, color_ui_contract)


def test_saved_display_mode_keeps_working():
    assert RawDisplayMode("opv") is RawDisplayMode.OPS
    assert NonRawDisplayMode("opv") is NonRawDisplayMode.OPS


def test_legacy_linear_render_is_readable(tmp_path):
    path = tmp_path / "legacy.tif"
    pixels = np.full((2, 3, 3), 0.25, dtype=np.float32)
    tifffile.imwrite(path, pixels, photometric="rgb",
                     metadata={"opv_render": 1, "colorspace": "linear_srgb", "white_point": "D65"})
    assert json.loads(tifffile.TiffFile(path).pages[0].description)["opv_render"] == 1
    np.testing.assert_array_equal(read_linear(path).pixels, pixels)


@pytest.mark.parametrize("pipeline", ["libraw", "srgb-direct", "dcp:X"])
def test_legacy_look_values_render_like_current(pipeline):
    assert look_stages("opv_v2", pipeline) == look_stages("ops_v2", pipeline)
    assert look_stages("opv_base", pipeline) == look_stages("ops_base", pipeline)


def test_legacy_look_values_keep_their_contract():
    current = COLOR_PARAMETERS.defaults(COLOR_PARAMETERS.version)
    legacy = LEGACY_COLOR_PARAMETERS[6].defaults(6)
    assert color_engine_contract({**legacy, "look": "opv_v2"}) is LEGACY_COLOR_PARAMETERS[6]
    assert color_engine_contract({**current, "look": "ops_v2"}) is COLOR_PARAMETERS
    assert not LEGACY_COLOR_PARAMETERS[6].normalize({**legacy, "look": "ops_v2"}, 6).success
    assert not COLOR_PARAMETERS.normalize({**current, "look": "opv_v2"}, 7).success
    assert color_ui_contract({**legacy, "look": "opv_v2"}).version == 6
    assert module_for("color_adjust", {**legacy, "look": "opv_v2"}).parameters.version == 6
    assert color_ui_contract({**legacy, "look": "none"}, 6).version == 6
    assert color_look_options("opv_v2", 4) == ("none", "opv_v2")
    assert color_look_options("none", 4) == ("none", "ops_v2")
    assert color_look_options("opv_base", 4) == ("none", "opv_v2", "opv_base")
    assert color_look_options("none", 2) == ("none", "opv_v2")
    assert color_look_options("none", 4, version=6) == ("none", "opv_v2")
