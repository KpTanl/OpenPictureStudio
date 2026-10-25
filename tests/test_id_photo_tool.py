"""证件照工具（参数版本 2）：契约、裁切范围与排版的几何、人像对齐、执行顺序与线性合成、应用层的模型绑定。"""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from src.application.managed_tool_modules import id_photo_binding
from src.core.contracts import WorkflowGraph
from src.core.contracts.result import ErrorCode
from src.core.photo_engine import RenderSource, _pil_from, linear_to_srgb, srgb_to_linear
from src.core.tools.effect_migration import reference_params
from src.core.tools.effects import (execute_id_photo, id_photo_memory, make_id_photo, make_id_photo_memory,
                                    resize_arrays)
from src.core.tools.id_photo import (BG_PRESETS, ID_PHOTO_PARAMETERS, ID_PHOTO_RESOURCE_FEATURES, LAYOUT_SHEET,
                                     MANAGED_ID_PHOTO_PARAMETERS, SHEET_SIZE, SIZE_PRESETS,
                                     id_photo_required_resources, photo_bounds, sheet_grid, sheet_positions)
from src.core.tools.id_photo_alignment import (_detect_face_cascade, _detect_face_skin, detect_primary_face,
                                               portrait_crop_rect)

ONE_INCH = SIZE_PRESETS["一寸"]
BLUE = list(BG_PRESETS["蓝色"])
MODELS = Path(__file__).resolve().parents[1] / "models"


def _source(width=400, height=300, *, alpha=None, seed=7):
    pixels = np.random.default_rng(seed).random((height, width, 3), dtype=np.float32)
    pixels.setflags(write=False)
    if alpha is not None:
        alpha.setflags(write=False)
    return RenderSource(pixels, alpha, "srgb8", "", False, (width, height), "srgb-direct")


def _params(**changes):
    return {**MANAGED_ID_PHOTO_PARAMETERS.defaults(2), **changes}


def _linear(color):
    return srgb_to_linear(np.asarray(color, dtype=np.float32) / np.float32(255))


def _half_mask(image):
    """左半保留、右半去掉的蒙版。"""
    mask = Image.new("L", image.size, 0)
    mask.paste(255, (0, 0, image.width // 2, image.height))
    return mask


# ----------------------------------------------------------------------
# 契约
# ----------------------------------------------------------------------
def test_contract_checks_size_range_colour_and_layout():
    contract = MANAGED_ID_PHOTO_PARAMETERS
    assert contract.version == 2 and contract.normalize(_params(), 2).success
    assert ID_PHOTO_PARAMETERS.version == 1
    assert set(ID_PHOTO_PARAMETERS.defaults(1)) == {"preset_size", "custom_width", "custom_height", "bg_preset",
                                                    "change_bg"}

    def ok(**changes):
        return contract.normalize(_params(**changes), 2).success

    # 选了预设，宽高就是预设的；自定义时在范围内任取
    assert not ok(custom_width=300)
    assert ok(preset_size="二寸", custom_width=413, custom_height=626)
    assert ok(preset_size="自定义", custom_width=600, custom_height=800)
    assert not ok(preset_size="自定义", custom_width=49)

    assert ok(crop_rect=[0.1, 0.2, 0.6, 0.9])
    for bad in ([0.1, 0.2, 0.6], [0.6, 0.2, 0.1, 0.9], [0.1, 0.9, 0.6, 0.9], [0, 0, 1.2, 1], [True, 0, 1, 1]):
        assert not ok(crop_rect=bad), bad
    assert ok(background=BLUE) and ok(background=None)
    for bad in ([255, 255], [255, 255, 256], [1.0, 2, 3], "blue"):
        assert not ok(background=bad), bad

    # 排版要至少放得下一张
    assert ok(layout=LAYOUT_SHEET)
    assert not ok(layout=LAYOUT_SHEET, preset_size="自定义", custom_width=2000, custom_height=600)
    assert not ok(layout="a4") and not ok(bg_preset="白色")


def test_model_is_required_only_when_a_background_is_laid_after_matting():
    assert id_photo_required_resources(_params()) == ()
    assert id_photo_required_resources(_params(background=BLUE, ai_matting=False)) == ()
    resource, = id_photo_required_resources(_params(background=BLUE, segmentation_model="models/u2net"))
    assert (resource.feature_id, resource.role, resource.capability, resource.adapters, resource.model_token) == (
        "ai", "model", "segmentation", ("u2net",), "models/u2net")
    assert [feature.feature_id for feature in ID_PHOTO_RESOURCE_FEATURES] == ["basic", "ai"]


# ----------------------------------------------------------------------
# 几何
# ----------------------------------------------------------------------
def test_photo_bounds_take_the_largest_centred_range_of_the_ratio():
    ratio = ONE_INCH[0] / ONE_INCH[1]
    left, top, right, bottom = photo_bounds(None, (400, 300), ratio)
    assert (top, bottom) == (0, 300) and right - left == pytest.approx(300 * ratio)
    assert (left + right) / 2 == pytest.approx(200)

    # 给出的范围偏宽：高度用足，左右各收一些；偏高则相反。比例正好时原样
    left, top, right, bottom = photo_bounds([0.0, 0.5, 1.0, 1.0], (400, 300), ratio)
    assert (top, bottom) == (150, 300) and right - left == pytest.approx(150 * ratio)
    left, top, right, bottom = photo_bounds([0.25, 0.0, 0.5, 1.0], (400, 300), ratio)
    assert (left, right) == (100, 200) and bottom - top == pytest.approx(100 / ratio)
    assert (top + bottom) / 2 == pytest.approx(150)
    exact = [0.25, 0.1, 0.25 + 150 * ratio / 400, 0.6]
    assert photo_bounds(exact, (400, 300), ratio) == pytest.approx((100, 30, 100 + 150 * ratio, 180))


def test_sheet_grid_and_positions():
    assert sheet_grid(SIZE_PRESETS["一寸"]) == (5, 2)
    assert sheet_grid(SIZE_PRESETS["二寸"]) == (4, 1)
    assert sheet_grid(SIZE_PRESETS["签证"]) == (3, 1)
    assert 0 in sheet_grid((2000, 600))
    positions = sheet_positions(ONE_INCH)
    assert len(positions) == 10 and positions[0] == (122, 177)
    assert positions[1][0] - positions[0][0] == ONE_INCH[0] + 20
    assert positions[5] == (122, 177 + ONE_INCH[1] + 20)
    # 整组在相纸上居中，除不尽的一像素留在右下
    for axis in (0, 1):
        before = positions[0][axis]
        after = SHEET_SIZE[axis] - positions[-1][axis] - ONE_INCH[axis]
        assert after - before in (0, 1)


# ----------------------------------------------------------------------
# 人像对齐
# ----------------------------------------------------------------------
def test_portrait_crop_rect_puts_head_and_chin_on_the_guides():
    left, top, right, bottom = portrait_crop_rect((1200, 1600), ONE_INCH[0] / ONE_INCH[1], (450, 380, 760, 800))
    face_height = 800 - 380
    head_top, chin = 380 - face_height * 0.18, 800 + face_height * 0.08
    assert (head_top - top) / (bottom - top) == pytest.approx(0.12, abs=0.01)
    assert (chin - top) / (bottom - top) == pytest.approx(0.68, abs=0.01)
    assert (right - left) / (bottom - top) == pytest.approx(ONE_INCH[0] / ONE_INCH[1], abs=0.005)


def test_face_detection_finds_skin_and_real_faces():
    # 肤色回退：灰底上一块肤色的椭圆
    image = Image.new("RGB", (900, 1200), (120, 130, 140))
    ImageDraw.Draw(image).ellipse((330, 260, 590, 600), fill=(224, 172, 140))
    found = _detect_face_skin(image)
    assert found is not None and found[0] < 330 < 590 < found[2] and found[1] < 260 < 600 < found[3]
    assert _detect_face_skin(Image.new("RGB", (400, 400), (30, 60, 200))) is None

    # 级联检测器：真实的人像
    data = pytest.importorskip("skimage.data")
    portrait = Image.fromarray(data.astronaut())
    face = _detect_face_cascade(portrait)
    assert face is not None and detect_primary_face(portrait) == face
    assert face[0] < 256 < face[2] and face[1] < 110 < face[3]


# ----------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------
def test_plain_photo_is_one_resample_of_the_fitted_range():
    source = _source()
    result = make_id_photo(source, _params(crop_rect=[0.2, 0.1, 0.7, 0.95]))
    bounds = photo_bounds([0.2, 0.1, 0.7, 0.95], (400, 300), ONE_INCH[0] / ONE_INCH[1])
    left, top = int(np.floor(bounds[0])), int(np.floor(bounds[1]))
    right, bottom = int(np.ceil(bounds[2])), int(np.ceil(bounds[3]))
    expected, _ = resize_arrays(source.pixels[top:bottom, left:right], None, ONE_INCH,
                                box=(bounds[0] - left, bounds[1] - top, bounds[2] - left, bounds[3] - top))
    assert np.array_equal(result.pixels, expected)
    assert result.pixels.shape == (ONE_INCH[1], ONE_INCH[0], 3) and result.pixels.dtype == np.float32
    assert result.alpha is None and result.orig_size == ONE_INCH

    # 没有给范围时取整幅里居中的最大范围
    whole = make_id_photo(source, _params())
    bounds = photo_bounds(None, (400, 300), ONE_INCH[0] / ONE_INCH[1])
    left, right = int(np.floor(bounds[0])), int(np.ceil(bounds[2]))
    expected, _ = resize_arrays(source.pixels[:, left:right], None, ONE_INCH,
                                box=(bounds[0] - left, 0.0, bounds[2] - left, 300.0))
    assert np.array_equal(whole.pixels, expected)


def test_background_is_laid_on_encoded_values_after_matting():
    seen = []

    def provider(image):
        seen.append((image.mode, image.size))
        return _half_mask(image)

    source = _source()
    params = _params(crop_rect=[0.25, 0.0, 0.75, 1.0], background=BLUE)
    result = make_id_photo(source, params, mask_provider=provider)
    bounds = photo_bounds(params["crop_rect"], (400, 300), ONE_INCH[0] / ONE_INCH[1])
    left, top = int(np.floor(bounds[0])), int(np.floor(bounds[1]))
    right, bottom = int(np.ceil(bounds[2])), int(np.ceil(bounds[3]))
    # 抠图看到的是裁切之后、缩放之前的画面：这个范围偏宽，按比例收窄的是高度
    assert seen == [("RGB", (right - left, bottom - top))] and bottom - top < 300
    assert result.alpha is None and result.pixels.shape == (ONE_INCH[1], ONE_INCH[0], 3)
    # 蒙版去掉的一侧是背景色，保留的一侧不是
    blue = _linear(BLUE)
    assert np.allclose(result.pixels[:, -20:], blue, atol=1e-5)
    assert not np.allclose(result.pixels[:, :20], blue, atol=0.05)

    # 半透明处按 sRGB 编码值混合，而不是按线性值
    flat = RenderSource(np.full((300, 400, 3), 0.05, dtype=np.float32), None, "srgb8", "", False, (400, 300),
                        "srgb-direct")
    half = make_id_photo(flat, _params(background=BLUE), mask_provider=lambda image: Image.new("L", image.size, 128))
    coverage = np.float32(128) / np.float32(255)
    encoded = coverage * linear_to_srgb(np.full(3, 0.05, dtype=np.float32)) + (1 - coverage) * (
        np.asarray(BLUE, dtype=np.float32) / 255)
    assert np.allclose(half.pixels, srgb_to_linear(encoded), atol=1e-5)
    assert not np.allclose(half.pixels, coverage * 0.05 + (1 - coverage) * blue, atol=1e-3)

    # 完全不透明的地方不经编码转换，与不换背景时逐位相同
    opaque = make_id_photo(source, params, mask_provider=lambda image: Image.new("L", image.size, 255))
    assert np.array_equal(opaque.pixels, make_id_photo(source, {**params, "background": None}).pixels)


def test_existing_transparency_is_filled_without_a_model_and_kept_without_a_background():
    alpha = np.zeros((300, 400), dtype=np.float32)
    alpha[:, :200] = 1
    source = _source(alpha=alpha)
    filled = make_id_photo(source, _params(background=BLUE, ai_matting=False))
    assert filled.alpha is None
    assert np.allclose(filled.pixels[:, -20:], _linear(BLUE), atol=1e-5)
    assert not np.allclose(filled.pixels[:, :20], _linear(BLUE), atol=0.05)

    kept = make_id_photo(source, _params())
    assert kept.alpha is not None and kept.alpha.shape == (ONE_INCH[1], ONE_INCH[0])
    assert kept.alpha[:, :20].min() > 0.99 and kept.alpha[:, -20:].max() < 0.01

    # 已有的透明与抠图的蒙版相乘
    both = make_id_photo(source, _params(background=BLUE),
                         mask_provider=lambda image: Image.new("L", image.size, 255))
    assert np.allclose(both.pixels, filled.pixels, atol=1e-6)


def test_matting_needs_a_model_and_a_mask_of_the_cropped_size():
    with pytest.raises(FileNotFoundError):
        make_id_photo(_source(), _params(background=BLUE))
    with pytest.raises(ValueError):
        make_id_photo(_source(), _params(background=BLUE), mask_provider=lambda image: Image.new("L", (10, 10), 255))
    with pytest.raises(ValueError):
        make_id_photo(_source(), {**_params(), "custom_width": 300})


def _old_layout_sheet(photo, photo_size, paper=(1800, 1200), margin=30, gap=20):
    """旧证件照窗口的排版写法，作参照：白底居中排满，每张外加一条浅灰细边。"""
    sheet = Image.new("RGB", paper, (255, 255, 255))
    width, height = photo_size
    columns = (paper[0] - 2 * margin + gap) // (width + gap)
    rows = (paper[1] - 2 * margin + gap) // (height + gap)
    start_x = (paper[0] - (columns * width + (columns - 1) * gap)) // 2
    start_y = (paper[1] - (rows * height + (rows - 1) * gap)) // 2
    photo = photo.resize(photo_size, Image.Resampling.LANCZOS)
    for row in range(rows):
        for column in range(columns):
            x, y = start_x + column * (width + gap), start_y + row * (height + gap)
            sheet.paste(photo, (x, y))
            ImageDraw.Draw(sheet).rectangle([x, y, x + width, y + height], outline=(200, 200, 200), width=1)
    return sheet


def test_sheet_is_the_old_layout_of_the_single_photo():
    source = _source()
    for preset in ("一寸", "二寸", "签证"):
        size = SIZE_PRESETS[preset]
        params = _params(preset_size=preset, custom_width=size[0], custom_height=size[1], background=BLUE)
        single = make_id_photo(source, params, mask_provider=_half_mask)
        sheet = make_id_photo(source, {**params, "layout": LAYOUT_SHEET}, mask_provider=_half_mask)
        assert sheet.pixels.shape == (SHEET_SIZE[1], SHEET_SIZE[0], 3) and sheet.orig_size == SHEET_SIZE
        assert sheet.alpha is None
        old = _old_layout_sheet(_pil_from(single), size)
        assert np.array_equal(np.asarray(_pil_from(sheet)), np.asarray(old)), preset

    # 透明的照片落在白底上
    alpha = np.zeros((300, 400), dtype=np.float32)
    alpha[:, :200] = 1
    sheet = make_id_photo(_source(alpha=alpha), _params(layout=LAYOUT_SHEET))
    x, y = sheet_positions(ONE_INCH)[0]
    assert sheet.alpha is None and np.allclose(sheet.pixels[y + 50, x + ONE_INCH[0] - 10], 1.0, atol=1e-5)


def test_sampling_follows_crop_mask_resize_and_layout():
    class Recorder:
        active = True

        def __init__(self):
            self.calls = []

        def crop(self, bounds):
            self.calls.append(("crop", bounds))

        def apply_mask(self, mask):
            self.calls.append(("mask", mask.shape))

        def resize(self, size, *, box=None):
            self.calls.append(("resize", size, tuple(round(value, 3) for value in box)))

        def pad(self, size, padding):
            self.calls.append(("pad", size, padding))

    recorder = Recorder()
    params = _params(crop_rect=[0.25, 0.0, 0.75, 1.0], background=BLUE, layout=LAYOUT_SHEET)
    make_id_photo(_source(), params, mask_provider=_half_mask, sampling=recorder)
    bounds = photo_bounds(params["crop_rect"], (400, 300), ONE_INCH[0] / ONE_INCH[1])
    left, top = int(np.floor(bounds[0])), int(np.floor(bounds[1]))
    right, bottom = int(np.ceil(bounds[2])), int(np.ceil(bounds[3]))
    x, y = sheet_positions(ONE_INCH)[0]
    assert recorder.calls == [
        ("crop", (left, top, right, bottom)),
        ("mask", (bottom - top, right - left)),
        ("resize", ONE_INCH, tuple(round(value, 3) for value in (
            bounds[0] - left, bounds[1] - top, bounds[2] - left, bounds[3] - top))),
        ("pad", SHEET_SIZE, (x, y, SHEET_SIZE[0] - x - ONE_INCH[0], SHEET_SIZE[1] - y - ONE_INCH[1])),
    ]


def test_working_memory_grows_with_what_is_asked():
    source = _source()
    plain = make_id_photo_memory(source, _params())
    filled = make_id_photo_memory(source, _params(background=BLUE, ai_matting=False))
    matted = make_id_photo_memory(source, _params(background=BLUE))
    sheet = make_id_photo_memory(source, _params(background=BLUE, layout=LAYOUT_SHEET))
    assert plain == ONE_INCH[0] * ONE_INCH[1] * 40 + source.pixels.nbytes
    assert plain < filled < matted < sheet
    assert sheet - matted == SHEET_SIZE[0] * SHEET_SIZE[1] * 12 + ONE_INCH[0] * ONE_INCH[1] * 12


def test_version_1_still_resizes_the_whole_image():
    source = _source()
    params = {**ID_PHOTO_PARAMETERS.defaults(1), "custom_width": 120, "custom_height": 160}
    result = execute_id_photo(source, params)
    expected, _ = resize_arrays(source.pixels, None, (120, 160))
    assert np.array_equal(result.pixels, expected) and result.orig_size == (120, 160)
    assert id_photo_memory(source, params) == 120 * 160 * 40 + source.pixels.nbytes


# ----------------------------------------------------------------------
# 应用层：按参数里有没有模型一项分到两个版本
# ----------------------------------------------------------------------
def _run(runtime, settings, source):
    reference = runtime.effect_reference("id_photo", settings)
    assert reference.success, reference.error
    graph = WorkflowGraph.single_node("tool", reference_params(reference.data, settings))
    assert runtime.validate(graph).success
    return reference.data, graph, runtime.snapshot().executor.execute(source, graph)


def test_runtime_routes_the_two_versions_and_runs_them(tool_services):
    from tests.support import wait_for_model_discovery

    runtime = tool_services["render_service"].runtime
    # 模型目录在后台发现完成时运行时会换代，正在执行的渲染按过期处理；先等它完成
    wait_for_model_discovery(runtime.models)
    source = _source()

    old = {**ID_PHOTO_PARAMETERS.defaults(1), "custom_width": 120, "custom_height": 160}
    reference, graph, result = _run(runtime, old, source)
    assert (reference.implementation_id, reference.params_version) == ("id_photo.float", 1)
    assert result.success and np.array_equal(result.data.pixels, execute_id_photo(source, old).pixels)

    new = _params(crop_rect=[0.2, 0.1, 0.7, 0.95])
    reference, graph, result = _run(runtime, new, source)
    assert (reference.implementation_id, reference.implementation_version, reference.params_version,
            reference.resources) == ("id_photo.managed", "1", 2, ())
    assert result.success and np.array_equal(result.data.pixels, make_id_photo(source, new).pixels)
    assert result.data.orig_size == ONE_INCH
    # 不用模型时可以走代理，也不需要准备；要抠图时相反
    assert runtime.supports_proxy(graph) and not runtime.needs_preparation("id_photo", new)
    assert runtime.needs_preparation("id_photo", _params(background=BLUE))
    assert not runtime.needs_preparation("id_photo", _params(background=BLUE, ai_matting=False))
    assert runtime.validate_tool_params("id_photo", new).success
    assert not runtime.validate_tool_params("id_photo", {**new, "custom_width": 300}).success

    # 已有透明时铺背景色不需要模型
    alpha = np.zeros((300, 400), dtype=np.float32)
    alpha[:, :200] = 1
    _, _, filled = _run(runtime, _params(background=BLUE, ai_matting=False), _source(alpha=alpha))
    assert filled.success and filled.data.alpha is None

    # 要抠图而这台电脑上没有模型：说明缺的是资源
    missing = runtime.effect_reference("id_photo", _params(background=BLUE))
    assert not missing.success and missing.error.code == ErrorCode.TOOL_RESOURCE_MISSING

    entry = next(item for item in runtime.resource_status() if item.tool_id == "id_photo")
    assert entry.implementation_id == "id_photo.managed"
    assert [feature.feature_id for feature in entry.resource_features] == ["basic", "ai"]


class _Models:
    """只有一个分割模型的模型服务。"""

    def __init__(self, path, *, capability="segmentation", runtime=True):
        self.model = SimpleNamespace(key=SimpleNamespace(token="models/u2net"), capability=capability,
                                     adapter="u2net", runtime="onnxruntime",
                                     files=(SimpleNamespace(name="u2net.onnx", digest="d" * 64),))
        self._path, self._runtime = str(path), runtime

    def describe(self, token):
        if token != "models/u2net":
            raise KeyError(token)
        return self.model, {"u2net.onnx": self._path}

    def runtime_available(self, model):
        return self._runtime


class _Session:
    """上半是人、下半是背景的分割结果。"""

    def get_inputs(self):
        return [SimpleNamespace(name="input")]

    def run(self, _outputs, feeds):
        assert feeds["input"].shape == (1, 3, 320, 320)
        result = np.zeros((1, 1, 320, 320), dtype=np.float32)
        result[..., :160, :] = 1
        return [result]


def test_binding_pins_the_model_and_feeds_its_mask_to_the_photo(tmp_path):
    from src.core.contracts.result import CoreOperationError

    path = tmp_path / "u2net.onnx"
    path.write_bytes(b"model")
    inference = SimpleNamespace(create_session=lambda model, threads: (_Session(), "cpu"))
    params = _params(background=BLUE, segmentation_model="models/u2net")

    definition, paths = id_photo_binding(params, _Models(path), 2, inference)
    reference = definition.descriptor.reference
    assert (reference.tool_id, reference.params_version, reference.implementation_id) == ("id_photo", 2,
                                                                                         "id_photo.managed")
    assert reference.resources == (("model", "d" * 64),) and paths == {"model": str(path)}
    assert not definition.supports_proxy and definition.reusable_executor
    assert definition.required_resources(params)[0].role == "model"
    assert definition.descriptor.image.geometry == "resize" and definition.sampling_behavior == "transport"

    executor = definition.create_executor(paths)
    try:
        result = executor.execute(_source(), params, {"is_cancelled": lambda: False, "threads": 2})
    finally:
        executor.close()
    assert result.success and result.data.alpha is None
    assert np.allclose(result.data.pixels[-40:], _linear(BLUE), atol=1e-4)
    assert not np.allclose(result.data.pixels[:40], _linear(BLUE), atol=0.05)

    # 不要抠图时不绑定模型，可以走代理
    definition, paths = id_photo_binding(_params(), _Models(path), 2, inference)
    assert paths == {} and definition.descriptor.reference.resources == () and definition.supports_proxy
    assert definition.create_executor({}).execute(
        _source(), _params(), {"is_cancelled": lambda: False, "threads": 1}).success

    # 模型不是分割模型，或运行环境不可用：不绑定并说明原因
    for models in (_Models(path, capability="restoration"), _Models(path, runtime=False)):
        with pytest.raises(CoreOperationError) as error:
            id_photo_binding(params, models, 2, inference)
        assert error.value.error.code == ErrorCode.TOOL_INCOMPATIBLE
    for token in ("models/other", ""):
        with pytest.raises(CoreOperationError) as error:
            id_photo_binding({**params, "segmentation_model": token}, _Models(path), 2, inference)
        assert error.value.error.code == ErrorCode.TOOL_RESOURCE_MISSING


@pytest.fixture
def local_u2net(monkeypatch):
    """用仓库里本机的 U2Net 权重；没有时跳过。"""
    from src.utils import model_paths
    if not (MODELS / "u2net.onnx").is_file():
        pytest.skip("需要本地的 U2Net 权重 models/u2net.onnx")
    monkeypatch.setattr(model_paths, "get_legacy_model_roots", lambda: (MODELS,))


def test_real_model_replaces_the_background_of_a_portrait(local_u2net, tool_services):
    from tests.support import wait_for_model_discovery

    data = pytest.importorskip("skimage.data")
    runtime = tool_services["render_service"].runtime
    wait_for_model_discovery(runtime.models)
    portrait = Image.fromarray(data.astronaut())
    pixels = srgb_to_linear(np.asarray(portrait, dtype=np.float32) / np.float32(255))
    source = RenderSource(pixels, None, "srgb8", "", False, portrait.size, "srgb-direct")
    face = detect_primary_face(portrait)
    left, top, right, bottom = portrait_crop_rect(portrait.size, ONE_INCH[0] / ONE_INCH[1], face)
    settings = _params(crop_rect=[left / 512, top / 512, right / 512, bottom / 512], background=BLUE,
                       segmentation_model=runtime.models.default("segmentation"))

    reference, graph, result = _run(runtime, settings, source)
    assert reference.implementation_id == "id_photo.managed" and len(reference.resources) == 1
    assert not runtime.supports_proxy(graph)
    assert result.success, result.error
    photo = result.data
    assert photo.pixels.shape == (ONE_INCH[1], ONE_INCH[0], 3) and photo.alpha is None
    blue = _linear(BLUE)
    # 上方两角是背景，换成了蓝色；脸在参考线之间，不是蓝色
    assert np.allclose(photo.pixels[4:24, 4:24], blue, atol=0.02)
    assert np.allclose(photo.pixels[4:24, -24:-4], blue, atol=0.02)
    face_patch = photo.pixels[int(ONE_INCH[1] * 0.35):int(ONE_INCH[1] * 0.5), 120:175]
    assert np.abs(face_patch - blue).max(axis=-1).mean() > 0.15
