"""参数变化的比较，以及 Core 的历史线：从根到当前修订再到可以重做的各步，回到某一步后沿途仍可以重做。"""

from PIL import Image

from src.application.core_api import CoreAPI
from src.application.edit_changes import ParameterChange, StageChange, parameter_changes, stage_changes
from src.core.contracts import WorkflowGraph


def session_at(root, **kwargs):
    path = root / "source.png"
    Image.new("RGB", (12, 10), (60, 100, 180)).save(path)
    core = CoreAPI(artifact_dir=str(root / "artifacts"), **kwargs)
    session = core.create_session(core.open_source(str(path)).unwrap()).unwrap()
    return core, session.session_id


def graph(exposure):
    return WorkflowGraph.single_node("color_adjust", {"exposure": exposure})


def edit(core, sid, exposure):
    return core.edit_workflow(sid, graph(exposure)).unwrap().current_revision.revision_id


def line(core, sid):
    timeline = core.history_timeline(sid).unwrap()
    return [item.revision_id for item in timeline["revisions"]], timeline["current"]


def test_parameter_changes_compare_by_name_and_fill_in_defaults():
    defaults = {"exposure": 0, "shadows": 0, "curves": None, "look": "none"}
    before = {"exposure": 35}
    after = {"exposure": 35, "shadows": 12, "curves": {}, "extra": 1}
    # 没写的参数取默认值；None 与空容器都是“没有内容”；顺序是默认值的声明顺序，其余排在后面
    assert parameter_changes(before, after, defaults) == (
        ParameterChange("shadows", 0, 12), ParameterChange("extra", None, 1))
    assert parameter_changes(after, after, defaults) == ()
    # 中性曲线与没写曲线是同一个画面
    assert parameter_changes({"curves": {"rgb": [[0, 0], [255, 255]]}}, {}, defaults) == ()


def test_curves_and_mixer_are_compared_channel_by_channel_and_component_by_component():
    defaults = {"curves": None, "color_mixer": None}
    panel = {"curves": {channel: [(0, 0), (255, 255)] for channel in ("rgb", "r", "g", "b")},
             "color_mixer": {color: {"hue": 0, "sat": 0, "lum": 0} for color in
                             ("red", "orange", "yellow", "green", "cyan", "blue", "purple", "magenta")}}
    # 面板写出的全量中性取值、元组或列表写法，与没写都没有变化
    assert parameter_changes({}, panel, defaults) == ()
    assert parameter_changes(panel, {"curves": {"rgb": [[0, 0], [255, 255]]}}, defaults) == ()
    changed = {"curves": {**panel["curves"], "rgb": [[0, 8], [128, 140], [255, 250]], "g": [[0, 20], [255, 235]]},
               "color_mixer": {"orange": {"sat": -12}}}
    assert parameter_changes(panel, changed, defaults) == (
        ParameterChange("curves.rgb", [[0, 0], [255, 255]], [[0, 8], [128, 140], [255, 250]]),
        ParameterChange("curves.g", [[0, 0], [255, 255]], [[0, 20], [255, 235]]),
        ParameterChange("color_mixer.orange.sat", 0, -12))


def test_stage_changes_list_added_changed_and_removed_stages_in_processing_order():
    neutral = {"color_adjust": {"exposure": 0, "shadows": 0}, "beauty": {"skin_smooth": 0}, "crop": {"crop_rect": None}}
    before = {"color_adjust": {"exposure": 35, "shadows": 0}, "crop": {"crop_rect": [0, 0, 1, 1]}}
    after = {"color_adjust": {"exposure": 35, "shadows": 12}, "beauty": {"skin_smooth": 40}}
    assert stage_changes(before, after, neutral.__getitem__) == (
        StageChange("color_adjust", "changed", (ParameterChange("shadows", 0, 12),)),
        StageChange("beauty", "added", (ParameterChange("skin_smooth", 0, 40),)),
        StageChange("crop", "removed", (ParameterChange("crop_rect", [0, 0, 1, 1], None),)),
    )
    # 没有变化的步骤不出现
    assert stage_changes(after, after, neutral.__getitem__) == ()
    # 以默认参数新加入的步骤仍然算一步，只是没有参数变化
    assert stage_changes({}, {"beauty": {"skin_smooth": 0}}, neutral.__getitem__) == (
        StageChange("beauty", "added", ()),)


def test_timeline_runs_from_the_root_to_the_current_revision_and_on_to_what_can_be_redone(tmp_path):
    core, sid = session_at(tmp_path)
    root = core.get_session(sid).unwrap().current_revision.revision_id
    first, second, third = (edit(core, sid, value) for value in (0.1, 0.2, 0.3))
    assert line(core, sid) == ([root, first, second, third], 3)

    assert core.undo(sid).success
    assert line(core, sid) == ([root, first, second, third], 2)
    version = core.history_timeline(sid).unwrap()["state_version"]
    assert version == core.get_session(sid).unwrap().state_version

    # 撤销之后另做一步：原来那一支不在这条线上了
    branch = edit(core, sid, 0.5)
    assert line(core, sid) == ([root, first, second, branch], 3)
    assert not core.history_timeline("missing").success


def test_checkout_back_keeps_every_step_on_the_way_redoable(tmp_path):
    core, sid = session_at(tmp_path)
    root = core.get_session(sid).unwrap().current_revision.revision_id
    first, second, third = (edit(core, sid, value) for value in (0.1, 0.2, 0.3))

    assert core.checkout(sid, first, keep_redo_path=True).success
    assert line(core, sid) == ([root, first, second, third], 1)
    assert core.get_session(sid).unwrap().draft_workflow == graph(0.1)
    assert core.redo(sid).unwrap().revision_id == second
    assert core.redo(sid).unwrap().revision_id == third
    assert not core.redo(sid).success

    # 一路回到打开时的状态，再直接跳到最后一步
    assert core.checkout(sid, root, keep_redo_path=True).success
    assert line(core, sid) == ([root, first, second, third], 0)
    assert core.checkout(sid, third, keep_redo_path=True).success
    assert line(core, sid) == ([root, first, second, third], 3)
    assert core.history_state(sid).unwrap() == {"can_undo": True, "can_redo": False}


def test_checkout_back_prefers_the_path_it_came_from_over_an_older_branch(tmp_path):
    core, sid = session_at(tmp_path)
    first = edit(core, sid, 0.1)
    old = edit(core, sid, 0.2)
    assert core.undo(sid).success
    new = edit(core, sid, 0.3)
    newer = edit(core, sid, 0.4)
    assert core.checkout(sid, first, keep_redo_path=True).success
    assert [core.redo(sid).unwrap().revision_id for _ in range(2)] == [new, newer]
    # 不要求沿途可重做时，检出不改动重做目标
    assert core.checkout(sid, old).success and core.checkout(sid, first).success
    assert core.redo(sid).unwrap().revision_id == new


def test_checkout_redo_path_is_saved_with_the_project(tmp_path):
    core, sid = session_at(tmp_path)
    project = tmp_path / "photo.opsproj"
    assert core.save_project(sid, str(project)).success
    first, second, third = (core.commit(sid, graph(value)).unwrap().revision.revision_id for value in (0.1, 0.2, 0.3))
    assert core.checkout(sid, first, keep_redo_path=True).success
    core.close_session(sid)

    fresh = CoreAPI(artifact_dir=str(tmp_path / "artifacts"))
    opened = fresh.open_project(str(project)).unwrap()
    assert opened.current_revision.revision_id == first
    assert [fresh.redo(opened.session_id).unwrap().revision_id for _ in range(2)] == [second, third]


def test_revision_workflow_returns_a_detached_copy(tmp_path):
    core, sid = session_at(tmp_path)
    first = edit(core, sid, 0.1)
    edit(core, sid, 0.2)
    workflow = core.revision_workflow(sid, first).unwrap()
    assert workflow == graph(0.1)
    assert core.revision_workflow(sid, first).unwrap() is not workflow
    assert core.revision_workflow(sid, "missing").error.code == "REVISION_NOT_FOUND"
    assert not core.revision_workflow("missing", first).success
