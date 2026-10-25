"""Unit and integration tests for UnifiedEditSession and EditPipeline."""

from __future__ import annotations

import os
import tempfile
from PIL import Image
import pytest

from src.core.edit_pipeline import (
    EditPipeline,
    UnifiedEditSession,
    _is_color_default,
    _is_denoise_default,
    _is_beauty_default,
    _is_crop_default,
)


@pytest.fixture
def sample_image():
    """Create a temporary test RGB image."""
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img = Image.new("RGB", (300, 200), color=(100, 150, 200))
    img.save(tmp.name)
    tmp.close()
    yield tmp.name
    if os.path.exists(tmp.name):
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


class TestUnifiedEditSession:
    """测试统一编辑会话的状态、参数管理与撤销重做。"""

    def test_initial_state_clean(self, sample_image):
        session = UnifiedEditSession(sample_image)
        assert session.is_dirty() is False
        assert session.can_undo() is False
        assert session.can_redo() is False
        assert session.original_image is not None
        assert session.original_image.size == (300, 200)

    def test_is_dirty_detection(self, sample_image):
        session = UnifiedEditSession(sample_image)

        # 调色变动
        session.color_params = {"exposure_amt": 20}
        assert session.is_dirty() is True
        session.color_params.clear()
        assert session.is_dirty() is False

        # 降噪变动
        session.denoise_params = {"strength": 30}
        assert session.is_dirty() is True
        session.denoise_params.clear()
        assert session.is_dirty() is False

        # 美颜变动
        session.beauty_params = {"skin_smooth": 50}
        assert session.is_dirty() is True
        session.beauty_params.clear()
        assert session.is_dirty() is False

        # 裁切变动
        session.crop_params = {"crop_rect": (10, 10, 200, 150)}
        assert session.is_dirty() is True
        session.crop_params.clear()
        assert session.is_dirty() is False

    def test_undo_redo_across_stages(self, sample_image):
        session = UnifiedEditSession(sample_image)

        # Step 1: 调色
        session.push_undo()
        session.color_params = {"exposure_amt": 15, "temperature": 10}

        # Step 2: 美颜
        session.push_undo()
        session.beauty_params = {"skin_smooth": 40}

        # Step 3: 降噪
        session.push_undo()
        session.denoise_params = {"strength": 25}

        assert session.can_undo() is True
        assert session.denoise_params.get("strength") == 25

        # 撤销到 Step 2
        assert session.undo() is True
        assert session.denoise_params == {}
        assert session.beauty_params.get("skin_smooth") == 40
        assert session.color_params.get("exposure_amt") == 15

        # 撤销到 Step 1
        assert session.undo() is True
        assert session.beauty_params == {}
        assert session.color_params.get("exposure_amt") == 15

        # 重做回 Step 2
        assert session.redo() is True
        assert session.beauty_params.get("skin_smooth") == 40

    def test_reset_all_and_stage(self, sample_image):
        session = UnifiedEditSession(sample_image)
        session.color_params = {"contrast_amt": 30}
        session.beauty_params = {"face_slim": 20}
        session.denoise_params = {"strength": 50}

        # 重置单个阶段
        session.reset_stage("beauty")
        assert session.beauty_params == {}
        assert session.color_params.get("contrast_amt") == 30

        # 重置全部
        session.reset_all()
        assert session.is_dirty() is False
        assert session.color_params == {}
        assert session.denoise_params == {}


class TestEditPipeline:
    """测试级联渲染流水线与多阶段互通。"""

    def test_pipeline_default_transparent(self, sample_image):
        session = UnifiedEditSession(sample_image)
        pipeline = EditPipeline()

        # 全默认参数时渲染
        rendered = pipeline.render_pipeline(session, use_proxy=False)
        assert rendered is not None
        assert rendered.size == (300, 200)

    def test_pipeline_cascaded_color_and_crop(self, sample_image):
        session = UnifiedEditSession(sample_image)
        pipeline = EditPipeline()

        # 调色 + 裁切同时生效
        session.color_params = {"exposure_amt": 20}
        session.crop_params = {"crop_rect": (50, 50, 250, 150)}

        rendered = pipeline.render_pipeline(session, use_proxy=False)
        assert rendered is not None
        # 裁剪框尺寸: (250 - 50) x (150 - 50) = 200 x 100
        assert rendered.size == (200, 100)

    def test_pipeline_proxy_render(self, sample_image):
        session = UnifiedEditSession(sample_image)
        pipeline = EditPipeline()

        session.color_params = {"saturation_amt": 25}
        session.beauty_params = {"skin_smooth": 30}

        proxy_res = pipeline.render_pipeline(session, use_proxy=True)
        assert proxy_res is not None
        assert proxy_res.mode in ("RGB", "RGBA")

    def test_pipeline_skip_crop_for_overlay(self, sample_image):
        session = UnifiedEditSession(sample_image)
        pipeline = EditPipeline()

        session.crop_params = {"crop_rect": (10, 10, 100, 100)}
        # skip_crop=True 时输出裁切前完整底图供 overlay 交互
        res = pipeline.render_pipeline(session, use_proxy=False, skip_crop=True)
        assert res.size == (300, 200)
