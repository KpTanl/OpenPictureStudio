#lib concurrency tests
import time
import numpy as np
import pytest
from PIL import Image
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtGui import QImage

from src.core.photo_engine import (
    RenderSource, RenderPipeline, configure_hardware_acceleration,
    is_avx2_available, box_filter, _resize_bilinear, _hue_deg, _gamut_compress
)
from src.core.color_adjuster import ColorAdjuster


@pytest.fixture(scope="session")
def qapp():
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def test_core_count_adaptation():
    for core_target in [4, 8, 16]:
        assigned = configure_hardware_acceleration(core_target)
        assert assigned == core_target

    auto_threads = configure_hardware_acceleration(None)
    assert auto_threads >= 1
    assert auto_threads <= 16


def test_avx2_vs_pure_numpy_consistency():
    w, h = 320, 240
    np.random.seed(42)
    img = np.random.rand(h, w, 3).astype(np.float32)

    try:
        import cv2
        has_cv2 = True
    except ImportError:
        has_cv2 = False

    if has_cv2:
        cv2.setUseOptimized(True)
        resize_opt = _resize_bilinear(img, 120, 160)
        hue_opt = _hue_deg(img)
        gamut_opt = _gamut_compress(img * 1.2)

        cv2.setUseOptimized(False)
        resize_pure = _resize_bilinear(img, 120, 160)
        hue_pure = _hue_deg(img)
        gamut_pure = _gamut_compress(img * 1.2)

        cv2.setUseOptimized(True)

        np.testing.assert_allclose(resize_opt, resize_pure, atol=1e-2)
        np.testing.assert_allclose(hue_opt, hue_pure, atol=1e-5)
        np.testing.assert_allclose(gamut_opt, gamut_pure, atol=1e-5)


def test_render_pipeline_under_different_thread_counts():
    w, h = 640, 480
    np.random.seed(123)
    arr = np.random.rand(h, w, 3).astype(np.float32)
    src = RenderSource(
        pixels=arr, alpha=None, source_format='raw',
        color_pipeline='libraw', source_path='', is_proxy=True, orig_size=(w, h)
    )
    pipeline = RenderPipeline()
    params = {
        'temperature': 15,
        'tint': -10,
        'exposure': 0.3,
        'contrast': 1.2,
        'shadows': 20,
        'highlights': -30,
        'whites': 10,
        'blacks': -10,
        'vibrance': 25,
        'saturation': 1.1,
        'texture': 15,
        'clarity': 10,
        'dehaze': 10,
    }

    results = []
    for threads in [4, 8, 16]:
        configure_hardware_acceleration(threads)
        res = pipeline.render(src, params)
        results.append(res.pixels)

    for i in range(1, len(results)):
        np.testing.assert_allclose(results[0], results[i], atol=1e-5)


def test_latest_frame_state_machine_logic():
    class MockDialog:
        def __init__(self):
            self._is_worker_running = False
            self._has_pending_render = False
            self._generation = 0
            self._displayed_generation = -1
            self._dispatched_generations = []
            self._latest_engine_params = None

        def schedule_preview(self, params):
            self._generation += 1
            gen = self._generation
            self._latest_engine_params = params

            if self._is_worker_running:
                self._has_pending_render = True
                return

            self._dispatch_render_worker(gen, self._latest_engine_params)

        def _dispatch_render_worker(self, gen, params):
            self._is_worker_running = True
            self._has_pending_render = False
            self._dispatched_generations.append(gen)

        def on_worker_finished(self, gen):
            self._is_worker_running = False
            if gen > self._displayed_generation:
                self._displayed_generation = gen

            if self._has_pending_render and self._latest_engine_params is not None:
                self._dispatch_render_worker(self._generation, self._latest_engine_params)

    dlg = MockDialog()
    dlg.schedule_preview({'exposure': 0.1})
    assert dlg._is_worker_running is True
    assert dlg._dispatched_generations == [1]

    for e in [0.2, 0.3, 0.4, 0.5]:
        dlg.schedule_preview({'exposure': e})

    assert dlg._generation == 5
    assert dlg._has_pending_render is True
    assert dlg._dispatched_generations == [1]

    dlg.on_worker_finished(1)
    assert dlg._dispatched_generations == [1, 5]
    assert dlg._is_worker_running is True

    dlg.on_worker_finished(5)
    assert dlg._is_worker_running is False
    assert dlg._has_pending_render is False
    assert dlg._displayed_generation == 5


def test_save_request_id_invalidation_on_image_switch():
    """模拟交错场景：A 保存中，切换到 B，A 的保存回调返回时不得影响当前状态。"""
    import os

    class MockDialogSaveLogic:
        def __init__(self):
            self._save_request_id = 0
            self._image_path = "/path/to/image_A.jpg"
            self._stash = {"/path/to/image_A.jpg": {"exposure": 0.5}}
            self._info_text = ""
            self._btn_save_enabled = True

        def start_save(self):
            self._save_request_id += 1
            self._btn_save_enabled = False
            return self._save_request_id, self._image_path

        def switch_image(self, new_path):
            self._save_request_id += 1
            self._image_path = new_path
            self._btn_save_enabled = True

        def on_save_finished(self, req_id, src_path, save_path, is_overwrite, error):
            is_current_image = bool(src_path and self._image_path and os.path.abspath(self._image_path) == os.path.abspath(src_path))
            is_latest_req = (req_id == self._save_request_id and is_current_image)
            if is_latest_req:
                self._btn_save_enabled = True
                if is_overwrite and src_path:
                    self._stash.pop(src_path, None)
                self._info_text = f"Saved: {os.path.basename(save_path)}"

    dlg = MockDialogSaveLogic()
    req_id_A, src_A = dlg.start_save()
    assert req_id_A == 1
    assert dlg._btn_save_enabled is False

    # 用户切到图片 B
    dlg.switch_image("/path/to/image_B.jpg")
    assert dlg._save_request_id == 2
    assert dlg._btn_save_enabled is True

    # 图片 A 的异步保存回调返回
    dlg.on_save_finished(req_id_A, src_A, "/path/to/image_A.jpg", is_overwrite=True, error="")

    # 验证：A 的旧回调未篡改 B 的提示文本，未破坏 stash 机制
    assert dlg._info_text == ""
    assert dlg._image_path == "/path/to/image_B.jpg"
