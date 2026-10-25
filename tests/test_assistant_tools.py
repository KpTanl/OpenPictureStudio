"""AI 可调工具、回复解析、上下文与照片素材：不经界面与网络。"""

import json
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from types import SimpleNamespace

from src.application.assistant_actions import AssistantActionService
from src.application.assistant_context import AssistantContextBuilder, available_tools
from src.application.assistant_models import IMAGE_SOURCES, AssistantContext, AssistantModel, context_defaults
from src.application.assistant_runs import _state
from src.application.assistant_tools import (BEAUTY, TOOLS, ToolArgumentError, agent_arguments, changed_only,
                                             current_values, flat_values, grouped, parse_proposal, proposal_schema)
from src.application.photo_analysis import encode_jpeg, photo_statistics
from src.application.result_types import CoreResult
from src.core.tools.color import COLOR_UI_PARAMETERS, curve_channels, is_neutral_curve, mixer_values


class _Workflow:
    nodes = ()

    def workflow_hash(self):
        return "draft"


def _photo(beauty_state="", **beauty):
    return AssistantContext("session", 3, {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version), "shadows": 10},
                            _Workflow(), "source:x", "x.jpg", beauty=beauty, tools={"beauty": beauty_state})


def test_beauty_exposes_ten_parameters_without_eye_distance():
    assert set(BEAUTY.ranges) == {"skin_smooth", "skin_brighten", "skin_rosy", "sharpen", "spot_remove",
                                  "eye_bag_remove", "teeth_whiten", "eye_enlarge", "face_slim", "nose_slim"}
    assert TOOLS["color"].ranges and "eye_distance" not in BEAUTY.ranges


@pytest.mark.parametrize("text, expected", [
    ('{"summary":"s","adjustments":{"color":{"shadows":20},"beauty":{"skin_smooth":30}}}',
     {"color": {"shadows": 20}, "beauty": {"skin_smooth": 30}}),
    ('{"summary":"s","adjustments":{"shadows":20.4}}', {"color": {"shadows": 20}}),       # 旧的平铺调色结构
    ('好的：\n```json\n{"summary":"s","adjustments":{"color":{"shadows":5}}}\n```\n以上。', {"color": {"shadows": 5}}),
    ('{"summary":"s","adjustments":{"color":{"shadows":null}}}', {}),
])
def test_proposals_are_parsed_by_tool(text, expected):
    assert parse_proposal(text, ("color", "beauty"))["adjustments"] == expected


@pytest.mark.parametrize("text", [
    "只是一段说明，没有调整。",
    '{"summary":"s","adjustments":{"beauty":{"skin_smooth":30}}}',                 # 美颜没有开放
    '{"summary":"s","adjustments":{"color":{"shadows":500}}}',                      # 超出范围
    '{"summary":"s","adjustments":{"beauty":{"eye_distance":10}}}',                 # 眼距不开放
    '{"summary":"s","adjustments":{"color":{"shadows":true}}}',
    '{"summary":"s","adjustments":{},"extra":1}',
])
def test_invalid_proposals_are_rejected(text):
    with pytest.raises(ValueError, match="AI_RESPONSE_INVALID"):
        parse_proposal(text, ("color",))


def test_review_replies_carry_done():
    result = parse_proposal('{"summary":"ok","done":true,"adjustments":{"color":{"shadows":20}}}', ("color",))
    assert result["done"] is True
    schema = proposal_schema(("color", "beauty"), review=True)
    assert schema["required"] == ["summary", "adjustments", "done"]
    assert schema["properties"]["adjustments"]["required"] == ["color", "beauty"]
    assert "done" not in proposal_schema(("color",))["properties"]


def test_unchanged_values_are_dropped():
    photo = _photo(skin_smooth=30)
    assert changed_only(photo, {"color": {"shadows": 10, "contrast_amt": 5}, "beauty": {"skin_smooth": 30}}) == {
        "color": {"contrast_amt": 5}}
    assert grouped({"shadows": 3}) == {"color": {"shadows": 3}}


def test_beauty_needs_the_host_and_a_face():
    assert available_tools(_photo(""), True) == (("color", "beauty"), {})
    assert available_tools(_photo(""), None) == (("color", "beauty"), {})
    assert available_tools(_photo(""), False) == (("color",), {"beauty": "no_face"})
    assert available_tools(_photo("resources"), True) == (("color",), {"beauty": "resources"})


def _build(photo, *, mode="edit", analysis=None, faces=None, review=None, image=False, model=None):
    preferences = {"context": {**context_defaults(), "image": image}, "context_start": ""}
    messages = [{"id": "u", "role": "user", "text": "阴影太暗了", "status": "completed"}]
    return AssistantContextBuilder().build(messages, preferences, model or AssistantModel(connection_id="c", model="m"),
                                           photo, language="zh-CN", mode=mode, analysis=analysis, faces=faces,
                                           review=review, image_attached=image)


def test_context_lists_tools_analysis_and_review():
    stats = {"luminance_percentiles": {"p50": 0.4}}
    context = _build(_photo(""), analysis=stats, faces=True,
                     review={"round": 2, "max_rounds": 5, "goal": "阴影太暗了", "applied": {"color": {"shadows": 30}}})
    assert context["tools"] == ("color", "beauty") and context["review"]
    assert context["data"]["photo_analysis"] == stats
    assert context["data"]["current_adjustments"]["beauty"]["skin_smooth"] == 0
    assert context["data"]["self_review"]["applied_adjustments"] == {"color": {"shadows": 30}}
    assert "self-review round 2 of at most 5" in context["instructions"]
    assert "have been applied" in context["instructions"] and "candidate" not in context["instructions"]
    assert [item["kind"] for item in context["manifest"]] == ["color", "analysis", "review"]
    # 关掉照片分析时不发送；对话模式不带分析与调整说明
    photo = _photo("")
    preferences = {"context": {**context_defaults(), "analysis": False, "image": False}, "context_start": ""}
    built = AssistantContextBuilder().build([{"id": "u", "role": "user", "text": "hi", "status": "completed"}],
                                            preferences, AssistantModel(connection_id="c", model="m"), photo,
                                            language="zh-CN", mode="edit", analysis=stats)
    assert "photo_analysis" not in built["data"]
    chat = _build(photo, mode="chat", analysis=stats)
    assert "photo_analysis" not in chat["data"] and "Return only a JSON" not in chat["instructions"]


def test_image_manifest_states_the_long_edge():
    model = AssistantModel(connection_id="c", model="m", image_input=True)
    context = _build(_photo(""), image=True, model=model)
    image = next(item for item in context["manifest"] if item["kind"] == "image")
    assert image["longest_edge"] == IMAGE_SOURCES["current"][0] == 1536
    assert IMAGE_SOURCES["original"][0] == 2048


def test_photo_is_reencoded_without_metadata():
    source = Image.new("RGB", (4000, 3000), (120, 90, 60))
    exif = Image.Exif()
    exif[0x010F] = "Camera"
    buffer = BytesIO()
    source.save(buffer, format="JPEG", exif=exif.tobytes())
    with Image.open(BytesIO(buffer.getvalue())) as tagged:
        data = encode_jpeg(tagged, 1536, 90)
    with Image.open(BytesIO(data)) as encoded:
        assert max(encoded.size) == 1536 and encoded.format == "JPEG"
        assert not encoded.getexif() and "exif" not in encoded.info


def test_photo_statistics_follow_measurement_spec_1():
    image = np.zeros((200, 300, 3), np.uint8)
    image[:, :100] = 0                       # 死黑
    image[:, 100:200] = (200, 150, 110)      # 暖色
    image[:, 200:] = 255                     # 过曝
    stats = photo_statistics(image)
    assert stats["spec"] == {"name": "ops-display-srgb8", "version": 1} and stats["status"] == "available"
    assert stats["size"] == [300, 200] and stats["sample_count"] == 60000
    assert stats["near_black_share"] == pytest.approx(1 / 3, abs=.01)
    assert stats["near_white_share"] == pytest.approx(1 / 3, abs=.01)
    assert sum(stats["histogram"]["luma"]) == 60000 and stats["luma_percentiles"]["p1"] == 0
    assert stats["split_tone"]["shadows"]["status"] == "weak" and stats["split_tone"]["highlights"]["status"] == "weak"
    assert stats["hues"]["orange"]["share"] > .2 and stats["hues"]["blue"]["status"] == "missing"
    json.dumps(stats)
    # 透明像素不计入
    rgba = np.dstack([image, np.full((200, 300), 255, np.uint8)])
    rgba[:, 200:, 3] = 0
    assert photo_statistics(rgba)["sample_count"] == 40000


def test_rounds_written_as_several_steps_can_still_be_undone_and_redone():
    """循环调整按轮写入后，应用前与应用后之间隔着几步：回到应用前时仍认得出“已撤销、可重做”。"""
    class _Core:
        def __init__(self):
            self.current = 3

        def get_session(self, session_id):
            ids = ["rev-1", "rev-2", "rev-3", "rev-4"]
            workflow = SimpleNamespace(workflow_hash=lambda: "after" if self.current == 3 else "before")
            return CoreResult.ok(SimpleNamespace(current_revision=SimpleNamespace(revision_id=ids[self.current]),
                                                 draft_workflow=workflow, state_version=9))

        def history_timeline(self, session_id):
            return CoreResult.ok({"revisions": tuple(SimpleNamespace(revision_id=f"rev-{index}") for index in range(1, 5)),
                                  "current": self.current})

    core = _Core()
    actions = AssistantActionService(core, None)
    receipt = {"before_revision": "rev-1", "after_revision": "rev-4", "before_workflow_hash": "before",
               "after_workflow_hash": "after"}
    assert actions.history_status("s", receipt)["state"] == "applied"
    core.current = 0
    assert actions.history_status("s", receipt)["state"] == "undone"


# ---------------------------------------------------------------------------
# 曲线与八色混合器
# ---------------------------------------------------------------------------
_MANY = [[0, 0]] + [[x, x + 1] for x in range(14, 241, 14)] + [[255, 255]]     # 19 个点，手动拉出来的多点曲线


def _curved(**color):
    state = {**COLOR_UI_PARAMETERS.defaults(COLOR_UI_PARAMETERS.version), **color}
    return AssistantContext("session", 3, state, _Workflow(), "source:x", "x.jpg", beauty={}, tools={"beauty": ""})


def _manual():
    """面板写出的形式：四条曲线都在（含元组），八色三项都在。"""
    return _curved(curves={"rgb": [(0, 0), (255, 255)], "r": [[0, 20], [255, 235]],
                           "g": [(0, 0), (128.5, 140), (255, 255)], "b": _MANY},
                   color_mixer={**mixer_values(None), "orange": {"hue": 0, "sat": 15, "lum": 0}})


def test_neutral_curve_is_compared_by_value():
    assert is_neutral_curve(None) and is_neutral_curve([]) and is_neutral_curve([(0, 0), (255, 255)])
    assert is_neutral_curve([[0.0, 0.0], [255, 255]])
    assert not is_neutral_curve([[0, 20], [255, 235]])                    # 改了黑白场的直线
    assert not is_neutral_curve([[0, 0], [128, 128], [255, 255]])          # 数学上恒等但多于两点


def test_reading_lists_only_non_neutral_curves_and_components_without_losing_points():
    values = current_values(_manual(), "color")
    assert values["curves"] == {"r": [[0, 20], [255, 235]], "g": [[0, 0], [128.5, 140], [255, 255]], "b": _MANY}
    assert values["color_mixer"] == {"orange": {"sat": 15}}
    assert values["shadows"] == 0
    assert current_values(_curved(), "color")["curves"] == {} and current_values(_curved(), "color")["color_mixer"] == {}
    json.dumps(values)
    assert flat_values({"shadows": 3, "curves": {"r": [[0, 20], [255, 235]]}, "color_mixer": {"orange": {"sat": 15}}}) == {
        "shadows": 3, "curves.r": [[0, 20], [255, 235]], "color_mixer.orange.sat": 15}


def test_partial_edits_expand_to_complete_values_and_keep_the_rest():
    photo = _manual()
    result = changed_only(photo, {"color": TOOLS["color"].normalize({
        "curves": {"rgb": [[0, 8], [128, 132], [255, 250]], "g": [[0, 0], [255, 255]]},
        "color_mixer": {"orange": {"hue": -5}}})})
    curves, mixer = result["color"]["curves"], result["color"]["color_mixer"]
    assert set(curves) == {"rgb", "r", "g", "b"}
    assert curves["rgb"] == [[0, 8], [128, 132], [255, 250]] and curves["g"] == [[0, 0], [255, 255]]
    assert curves["r"] == [[0, 20], [255, 235]] and curves["b"] == _MANY      # 没给出的通道原样，含多于 16 点的曲线
    assert mixer["orange"] == {"hue": -5, "sat": 15, "lum": 0} and mixer["red"] == {"hue": 0, "sat": 0, "lum": 0}
    # 按顶层键合并后读回：只替换了给出的通道与分量
    merged = current_values(photo, "color", result["color"])
    assert merged["curves"]["r"] == [[0, 20], [255, 235]] and "g" not in merged["curves"]
    assert merged["color_mixer"] == {"orange": {"hue": -5, "sat": 15}}


def test_unchanged_or_empty_nested_edits_are_no_changes():
    photo = _manual()
    same = TOOLS["color"].normalize({"curves": {"r": [[0, 20], [255, 235]]}, "color_mixer": {"orange": {"sat": 15}}})
    assert changed_only(photo, {"color": same}) == {}
    assert changed_only(photo, {"color": TOOLS["color"].normalize({"curves": {}, "color_mixer": {}})}) == {}
    # 复位：曲线写中性，分量写 0
    reset = changed_only(photo, {"color": TOOLS["color"].normalize({"curves": {"r": [[0, 0], [255, 255]]},
                                                                    "color_mixer": {"orange": {"sat": 0}}})})["color"]
    assert reset["curves"]["r"] == [[0, 0], [255, 255]] and reset["color_mixer"]["orange"]["sat"] == 0
    assert current_values(photo, "color", reset)["color_mixer"] == {}


def test_run_state_treats_partial_and_complete_forms_alike():
    photo = _manual()
    complete = {"color": {"curves": curve_channels(photo.color["curves"]),
                          "color_mixer": mixer_values(photo.color["color_mixer"])}}
    assert _state(photo, complete) == _state(photo)


@pytest.mark.parametrize("values, field", [
    ({"curves": {"luma": [[0, 0], [255, 255]]}}, "curves.luma"),
    ({"curves": {"rgb": [[0, 0]]}}, "curves.rgb"),
    ({"curves": {"rgb": [[0, 0]] + [[x, x] for x in range(1, 16)] + [[255, 255]]}}, "curves.rgb"),
    ({"curves": {"rgb": [[0, 0], [128.5, 130], [255, 255]]}}, "curves.rgb[1]"),
    ({"curves": {"rgb": [[0, 0], [True, 1], [255, 255]]}}, "curves.rgb[1]"),
    ({"curves": {"rgb": [[0, 0], [300, 1], [255, 255]]}}, "curves.rgb[1]"),
    ({"curves": {"rgb": [[10, 0], [255, 255]]}}, "curves.rgb"),
    ({"curves": {"rgb": [[0, 0], [128, 120], [128, 140], [255, 255]]}}, "curves.rgb"),
    ({"curves": {"rgb": [[0, 0], [200, 120], [100, 140], [255, 255]]}}, "curves.rgb"),
    ({"color_mixer": {"teal": {"sat": 1}}}, "color_mixer.teal"),
    ({"color_mixer": {"orange": {"chroma": 1}}}, "color_mixer.orange.chroma"),
    ({"color_mixer": {"orange": {"sat": 101}}}, "color_mixer.orange.sat"),
    ({"color_mixer": {"orange": {"sat": 1.5}}}, "color_mixer.orange.sat"),
    ({"color_mixer": {"orange": {"sat": False}}}, "color_mixer.orange.sat"),
    ({"shadows": 500}, "shadows"),
    ({"mask": 1}, "mask"),
])
def test_invalid_curve_and_mixer_values_name_the_field(values, field):
    with pytest.raises(ToolArgumentError) as error:
        TOOLS["color"].normalize(values)
    assert str(error.value) == "AI_TOOL_ARGUMENTS" and error.value.details["field"] == field
    assert error.value.details["reason"]


def test_agent_arguments_check_the_nested_schema_and_reject_the_whole_call():
    allowed = {"adjust_color"}
    good = agent_arguments("adjust_color", json.dumps({"values": {
        "exposure_amt": 20, "curves": {"rgb": [[0, 0], [128, 140], [255, 255]]},
        "color_mixer": {"orange": {"sat": -12}}}}), allowed)
    assert good["values"] == {"exposure_amt": 20, "curves": {"rgb": [[0, 0], [128, 140], [255, 255]]},
                              "color_mixer": {"orange": {"sat": -12}}}
    for arguments, field in (
            ({"values": {"exposure_amt": 20, "curves": {"rgb": [[0, 0], [128, 999], [255, 255]]}}},
             "values.curves.rgb[1][1]"),
            ({"values": {"exposure_amt": 20, "curves": {"rgb": [[0, 0, 1], [255, 255]]}}}, "values.curves.rgb[0]"),
            ({"values": {"color_mixer": {"orange": {"sat": -101}}}}, "values.color_mixer.orange.sat"),
            ({"values": {"color_mixer": {"teal": {}}}}, "values.color_mixer.teal"),
            ({}, "values")):
        with pytest.raises(ToolArgumentError) as error:
            agent_arguments("adjust_color", json.dumps(arguments), allowed)
        assert error.value.details["field"] == field
    with pytest.raises(ToolArgumentError):
        agent_arguments("adjust_color", "{not json", allowed)
    assert TOOLS["color"].nested == ("curves", "color_mixer") and BEAUTY.nested == ()


def test_beauty_does_not_take_curves():
    with pytest.raises(ToolArgumentError):
        BEAUTY.normalize({"curves": {"rgb": [[0, 0], [255, 255]]}})


def test_written_changes_list_each_channel_and_component():
    class _Agent:
        def propose(self, *args, **kwargs):
            return CoreResult.ok({"candidate_id": "c"})

        def preview(self, candidate, **kwargs):
            return CoreResult.ok(SimpleNamespace(image=None))

        def commit(self, candidate, **kwargs):
            return CoreResult.ok(SimpleNamespace(revision=SimpleNamespace(revision_id="rev-2")))

        def discard(self, candidate):
            pass

    class _Core:
        def get_session(self, session_id):
            return CoreResult.ok(SimpleNamespace(state_version=4, current_revision=SimpleNamespace(revision_id="rev-2"),
                                                 draft_workflow=_Workflow()))

    photo = _manual()
    adjustments = changed_only(photo, {"color": TOOLS["color"].normalize({
        "exposure_amt": 20, "curves": {"rgb": [[0, 8], [255, 250]]}, "color_mixer": {"orange": {"sat": -12}}})})
    result = AssistantActionService(_Core(), _Agent()).apply(
        photo, _Workflow(), adjustments, label="ai.tool.1", conversation_id="c", message_id="m", request_id="r",
        model="m", is_cancelled=lambda: False)
    assert [(change["key"], change["before"], change["after"]) for change in result["changes"]] == [
        ("exposure_amt", 0, 20),
        ("curves.rgb", [[0, 0], [255, 255]], [[0, 8], [255, 250]]),
        ("color_mixer.orange.sat", 15, -12)]
    context = result["context"]
    assert context.color["curves"]["r"] == [[0, 20], [255, 235]] and context.color["color_mixer"]["orange"]["sat"] == -12


# ---------------------------------------------------------------------------
# 参考拟合
# ---------------------------------------------------------------------------
def test_tone_simplification_follows_the_measured_range_not_the_clipped_tails():
    from src.application.style_fit import simplify_curve
    grid = np.arange(256)
    # 区间内线性提亮，区间外截在 255：截断处的折角不在像素所在的区间里
    mapping = np.clip(grid * 1.12 + 6, 0, 255)
    points = simplify_curve(mapping, (60, 160))
    assert points is not None and len(points) <= 6
    assert points[0][0] == 0 and points[-1][0] == 255 and [60, 160] == [x for x, _y in points if x in (60, 160)]


def test_chroma_fit_compares_levels_at_the_measurement_size():
    """响应用小图求，与目标的差取验证尺寸的统计：小图的彩度整体偏低时不会因此多调。"""
    from src.application.style_fit import FIT_RESPONSE_EDGE, ReferenceFitter
    from src.application.photo_analysis import MEASUREMENT_SPEC

    def stats(edge, saturation):
        offset = (-5, -8) if edge == FIT_RESPONSE_EDGE else (0, 0)
        return {"status": "available", "spec": dict(MEASUREMENT_SPEC), "histogram": {"luma": [1] * 256},
                "chroma_percentiles": {"p50": 30 + saturation * .2 + offset[0], "p90": 40 + saturation * .25 + offset[1]},
                "split_tone": {name: {"status": "missing"} for name in ("shadows", "midtones", "highlights")},
                "hues": {name: {"share": 0.0} for name in ("red", "orange", "yellow", "green", "cyan", "blue",
                                                           "purple", "magenta")}}

    fitter = ReferenceFitter(lambda values: values, lambda values, edge: stats(edge, values["saturation_amt"]),
                             lambda: False)
    target = stats(0, 20)
    result = fitter.fit({"saturation_amt": 0}, target, dimensions=["chroma"], strength=100,
                        preserve=("vibrance_amt",))
    assert result["values"] == {"saturation_amt": 20}
    assert result["steps"][0]["accepted"] and result["after"]["chroma"] == {"p50": 0.0, "p90": 0.0}

