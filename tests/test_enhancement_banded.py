"""画质增强的分带放大：与先得到完整放大结果再缩放逐位相同；输出尺寸与各处缩放由内存估算把关。"""

from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from src.application.image_output_service import ImageOutputService
from src.application.image_resources import ImageResourcePolicy, ImageResourcePool
from src.application.tool_modules import MODULES
from src.core.image_resize import ResizeOptions, plan_resize, resize_memory
from src.core.photo_engine import load_render_source
from src.core.tools import enhancement
from src.core.tools.effects import id_photo_memory, legacy_ai_memory
from src.core.tools.enhancement import ENHANCE_PARAMETERS, EnhancementExecutor, estimate_memory, output_plan

TILE, OVERLAP, SCALE = 32, 4, 4


class _Session:
    """替身推理会话：把输入放大 4 倍，并叠加一层随块内位置变化、会越出 0 到 1 的纹理。"""

    def __init__(self):
        yy, xx = np.mgrid[0:TILE * SCALE, 0:TILE * SCALE].astype(np.float32)
        self.texture = (0.08 * np.sin(yy * 0.37) * np.cos(xx * 0.23)).astype(np.float32)
        self.calls = 0

    def get_inputs(self):
        return [SimpleNamespace(name="input", type="tensor(float)", shape=[1, 3, TILE, TILE])]

    def get_outputs(self):
        return [SimpleNamespace(name="output")]

    def run(self, _outputs, feeds):
        self.calls += 1
        tensor = feeds["input"]
        return [(np.repeat(np.repeat(tensor, SCALE, axis=2), SCALE, axis=3) * np.float32(1.02)
                 + self.texture[None, None]).astype(np.float32)]


def _descriptor():
    return SimpleNamespace(tile=TILE, overlap=OVERLAP, scale=SCALE, alignment=1,
                           files=(SimpleNamespace(disk_size=1000),))


def _executor():
    session = _Session()
    executor = EnhancementExecutor({"super_resolution": _descriptor()}, {"super_resolution": "model.onnx"}, 1,
                                   lambda path, threads: (session, "cpu"))
    return executor, session


def _source(tmp_path, size, *, alpha=False):
    rng = np.random.default_rng(7)
    data = rng.integers(0, 256, (size[1], size[0], 4 if alpha else 3), dtype=np.uint8)
    if alpha:
        # 透明、半透明与不透明都有
        data[..., 3] = np.clip(rng.integers(-80, 400, (size[1], size[0])), 0, 255)
    path = str(tmp_path / ("rgba.png" if alpha else "rgb.png"))
    Image.fromarray(data, "RGBA" if alpha else "RGB").save(path)
    return load_render_source(path, for_preview=False)


def _params(target, fit="contain"):
    return {**ENHANCE_PARAMETERS.defaults(3), "target": target, "fit": fit, "super_resolution_model": "sr@1"}


def _run(executor, source, params):
    return executor._execute(source, params, lambda: False)


@pytest.mark.parametrize("size", [(70, 53), (96, 48), (25, 90)])
@pytest.mark.parametrize("alpha", [False, True])
@pytest.mark.parametrize("target,fit", [("2x", "contain"), ("2x", "cover"), ("2x", "pad"), ("4x", "contain")])
def test_banded_upscale_matches_the_whole_image_path_bit_for_bit(tmp_path, monkeypatch, size, alpha, target, fit):
    source = _source(tmp_path, size, alpha=alpha)
    params = _params(target, fit)
    executor, session = _executor()
    plan = output_plan(size, params)
    upscaled = (size[0] * SCALE, size[1] * SCALE)
    assert enhancement._banded(upscaled, enhancement._final_plan(upscaled, plan, params))
    banded = _run(executor, source, params)
    calls = session.calls

    monkeypatch.setattr(enhancement, "_banded", lambda *_: False)
    whole = _run(executor, source, params)
    assert session.calls == calls * 2
    factor = int(target[0])
    assert banded.pixels.shape == whole.pixels.shape == (size[1] * factor, size[0] * factor, 3)
    assert banded.pixels.dtype == whole.pixels.dtype == np.float32
    assert np.array_equal(banded.pixels, whole.pixels)
    assert banded.orig_size == whole.orig_size == (size[0] * factor, size[1] * factor)
    if alpha:
        assert banded.alpha.dtype == np.float32 and np.array_equal(banded.alpha, whole.alpha)
    else:
        assert banded.alpha is None and whole.alpha is None


def test_non_integer_scale_keeps_the_whole_image_path(tmp_path):
    size = (70, 53)
    params = _params("4k")
    plan = output_plan(size, params)
    upscaled = (size[0] * SCALE, size[1] * SCALE)
    final = enhancement._final_plan(upscaled, plan, params)
    # 4K 对这张小图是放大，缩放比不是整数，分带不能保证逐位相同
    assert final.content_size != upscaled and not enhancement._banded(upscaled, final)
    executor, _session = _executor()
    result = _run(executor, _source(tmp_path, size), params)
    assert (result.pixels.shape[1], result.pixels.shape[0]) == plan.output_size


def test_rows_come_out_one_tile_row_at_a_time_and_match_the_whole_run(tmp_path):
    executor, _session = _executor()
    model = executor._model("super_resolution")
    rgb = np.random.default_rng(3).random((53, 70, 3), dtype=np.float32)
    whole = model.run(rgb, lambda: False)
    step = (TILE - OVERLAP * 2) * SCALE
    tops = []
    for top, band in model.run_rows(rgb, lambda: False):
        tops.append(top)
        assert band.min() >= 0 and band.max() <= 1
        assert np.array_equal(band, whole[top:top + band.shape[0]])
    assert tops == list(range(0, 53 * SCALE, step))


def test_banded_upscale_stops_when_cancelled(tmp_path):
    from concurrent.futures import CancelledError
    executor, session = _executor()
    checks = []

    def cancelled():
        checks.append(True)
        return len(checks) > 6

    with pytest.raises(CancelledError):
        executor._execute(_source(tmp_path, (96, 96)), _params("2x"), cancelled)
    assert 0 < session.calls < 16


def test_banded_estimate_is_far_below_the_whole_image_estimate():
    models = {"super_resolution": SimpleNamespace(scale=4, tile=256, overlap=32,
                                                  files=(SimpleNamespace(disk_size=67_000_000),))}
    source = SimpleNamespace(pixels=SimpleNamespace(shape=(4000, 6000, 3), nbytes=6000 * 4000 * 12))
    gib = 1024 ** 3
    banded = estimate_memory(source, _params("2x"), models)
    # 输出 9600 万像素每像素 24 字节，两个行带，源图缓冲的 4 倍
    image = 12000 * 8000 * 24 + 192 * 4 * 24000 * 2 * 48 + 6000 * 4000 * 12 * 4
    assert banded == image + 67_000_000 + 256 ** 2 * 1024
    whole = 6000 * 4000 * 16 * 48 + 6000 * 4000 * 12 * 4
    assert banded < 6 * gib < 17 * gib < whole
    # 4× 的输出就是放大结果：不再另存一份中间结果
    assert estimate_memory(source, _params("4x"), models) < whole * 0.7


def test_resize_has_no_fixed_pixel_ceiling_and_reports_its_memory():
    options = ResizeOptions(40000, 30000, "stretch")
    plan = plan_resize((4000, 3000), options)
    assert plan.output_size == (40000, 30000)
    assert resize_memory(plan) == 4000 * 3000 * 64 + 40000 * 30000 * 48
    with pytest.raises(ValueError, match="compress.status.invalid_dimensions"):
        ResizeOptions(0, 10)
    with pytest.raises(ValueError, match="compress.status.invalid_dimensions"):
        ResizeOptions.from_text("1234567890", "", "contain")


def _pool(limit, *, seat=None, reserve=0, available=None):
    return ImageResourcePool(ImageResourcePolicy(memory_bytes=limit, memory_seat_bytes=seat,
                                                 memory_reserve_bytes=reserve, available_memory=available))


def test_format_processing_refuses_sizes_beyond_the_memory_limit(tmp_path):
    source, target = tmp_path / "in.png", tmp_path / "out.png"
    Image.new("RGB", (100, 80), (10, 120, 200)).save(source)
    options = ResizeOptions(400, 320, "stretch")
    need = 100 * 80 * 64 + 400 * 320 * 48

    # 没有资源池时不判断
    assert ImageOutputService().describe_resize((100, 80), options).output_size == (400, 320)
    # 事前：估算超过上限的尺寸不给规划
    service = ImageOutputService(resource_budget=_pool(need - 1))
    with pytest.raises(ValueError, match="error.resize.memory"):
        service.describe_resize((100, 80), options)
    assert ImageOutputService(resource_budget=_pool(need)).describe_resize((100, 80), options).output_size == (400, 320)

    # 执行时：超出座位的部分要给系统留够保留量，不够就不运行，也不产生文件
    short = ImageOutputService(resource_budget=_pool(need * 2, seat=1000, reserve=500, available=lambda: need + 499))
    with pytest.raises(MemoryError, match="error.image.memory_available"):
        short.resize_and_save(str(source), str(target), options=options)
    assert not target.exists()
    enough = _pool(need * 2, seat=1000, reserve=500, available=lambda: need + 500)
    ImageOutputService(resource_budget=enough).resize_and_save(str(source), str(target), options=options)
    with Image.open(target) as image:
        assert image.size == (400, 320)
    assert enough.usage()["reserved_bytes"] == 0


def test_legacy_resizing_tools_declare_their_working_memory():
    source = SimpleNamespace(pixels=SimpleNamespace(shape=(300, 400, 3), nbytes=400 * 300 * 12))
    assert MODULES["id_photo"].working_memory is id_photo_memory
    assert MODULES["ai_enhance"].working_memory is legacy_ai_memory
    assert id_photo_memory(source, {"custom_width": 1000, "custom_height": 2000}) == 1000 * 2000 * 40 + 400 * 300 * 12
    small = legacy_ai_memory(source, {"upscale": "1x"})
    large = legacy_ai_memory(source, {"upscale": "4x"})
    assert large - small == (1600 * 1200 - 400 * 300) * 48
