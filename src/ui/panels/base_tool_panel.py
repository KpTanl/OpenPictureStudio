"""Base tool panel class for in-window sidebar tools."""

import copy
import logging
import uuid
from typing import Any, Callable, Dict, Mapping, Optional
from PIL import Image
from PySide6.QtCore import QEvent, QTimer, Signal, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget, QVBoxLayout
from resources.styles.theme_variables import SPACING_MD, SPACING_SM, px
from ..themed_mixin import ThemedI18nMixin
from ...core.edit_state import EditStateStore
from ...core.draft_store import DraftStore
from ...application.image_source_service import ImageSourceService, SourceBundle, SourcePreviewSpec
from ...application.result_types import CoreError, CoreOperationError, ErrorCode
from ..services.preview_task_runner import (
    BackgroundResult,
    PreviewResult,
    PreviewTaskRunner,
    SourceLoadResult,
)

logger = logging.getLogger(__name__)


def _canonical(value):
    """参数比较用的规范表示：列表与元组视为同一序列。"""
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


class BaseToolPanel(QWidget, ThemedI18nMixin):
    """单窗口侧栏中所有参数面板的基类。

    提供：
      - preview_ready(QImage) 实时推送到主 viewer
      - request_status(str) 联动状态栏
      - params_changed(dict) 联动暂存
      - 统一的 build_ui, apply, reset, cleanup, update_preview 生命周期
      - 防抖预览机制与图片切换钩子
    """

    preview_ready = Signal(QImage)
    request_status = Signal(str)
    params_changed = Signal(object)
    preparation_changed = Signal(bool)
    edit_group_started = Signal()
    edit_group_finished = Signal()
    # 面板里的模型选择请求打开模型管理页：能力、当前所选模型的标识
    model_manager_requested = Signal(str, str)

    TOOL_ID = "base_tool"
    TOOL_TITLE_KEY = "tool.base"
    TOOL_TITLE_DEFAULT = "Tool"
    TOOL_ADAPTER = None
    PARAMETER_CONTRACT = None
    # 参数在界面上的名称与读数格式：{参数名: (文案键, 读数格式)}，读数格式同 format_slider_value，
    # 不是数值的参数留空。编辑栈、历史与 AI 候选差异用它把参数的变化写成文字。
    # 逐项比较的嵌套参数另按 “参数.下一级” 声明各级名称，如 curves.rgb、color_mixer.orange、color_mixer.sat
    PARAMETER_LABELS: Mapping[str, tuple[str, str]] = {}

    def __init__(
        self,
        image_path: str,
        parent: Optional[QWidget] = None,
        edit_state_store: Optional[EditStateStore] = None,
        draft_store: Optional[DraftStore] = None,
        image_source_service=None,
        preview_runner=None,
        render_service=None,
        output_service=None,
        external_preview=False,
        source_identity=None,
        source_preview=None,
    ):
        super().__init__(parent)
        self.setObjectName(f"{self.TOOL_ID}Panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._image_path = image_path
        if not hasattr(self, "_params"):
            self._params = self._default_params()
        self._published_params = copy.deepcopy(self._params)
        self._parameters_pending = False
        self._bound_tool_reference = None
        self._next_parameter_publication = None
        self._parameter_publication = None
        self._edit_group_prefix = uuid.uuid4().hex
        self._edit_sequence = 0
        self._edit_group = None
        self._edit_active = None
        self._edit_controls = set()
        self._edit_state_store = edit_state_store
        # Prefer an explicit DraftStore; otherwise use the facade's Draft tier.
        self._draft_store = draft_store or getattr(edit_state_store, "drafts", None)
        self._image_source_service = image_source_service or ImageSourceService()
        self._preview_runner: Optional[PreviewTaskRunner] = preview_runner or PreviewTaskRunner()
        self._owns_preview_runner = preview_runner is None
        self._render_service = render_service
        self._owns_render_service = render_service is None
        self._output_service = output_service
        self._source_bundle: Optional[SourceBundle] = None
        self._source_identity = source_identity
        self._source_preview = source_preview or SourcePreviewSpec()
        self._source_job_id: Optional[int] = None
        self._preview_job_id: Optional[int] = None
        self._background_job_id: Optional[int] = None
        self._source_generation = 0
        self._preview_generation = 0
        self._background_generation = 0
        self._needs_reload = False

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)  # 120ms 防抖
        self._preview_timer.timeout.connect(self._do_update_preview)

        self.external_preview = external_preview
        self._init_themed_i18n()
        self._setup_root_layout()
        self.build_ui()

    def take_footer(self) -> Optional[QWidget]:
        """交出需要固定在底部、不随内容滚动的操作区，由宿主放在滚动区之外。

        没有这类操作区的面板返回 None。不调用时操作区留在面板自己的布局末尾，照常显示。
        """
        return None

    def _setup_root_layout(self) -> None:
        self._root_layout = QVBoxLayout(self)
        # 各面板的内容与侧栏边缘的间距、相邻两块之间的间距都在这里定；分组内容的边距在分组标题里定
        margin = px(SPACING_MD)
        self._root_layout.setContentsMargins(margin, margin, margin, margin)
        self._root_layout.setSpacing(px(SPACING_SM))

    @property
    def image_path(self) -> str:
        return self._image_path

    @property
    def parameters(self):
        return copy.deepcopy(self._params)

    @property
    def source_bundle(self) -> Optional[SourceBundle]:
        return self._source_bundle

    @property
    def source_size(self):
        return self._source_bundle.reference_size if self._source_bundle is not None else (0, 0)

    @property
    def parameters_pending(self):
        return self._parameters_pending

    @property
    def edit_group(self):
        return self._edit_group if self._edit_active is not None and self._edit_active() else None

    def track_edit_control(self, control, *, started, finished, is_active):
        self._edit_controls.add(control)
        control.installEventFilter(self)
        started.connect(lambda: self._begin_edit_group(is_active))
        finished.connect(self.end_edit_group)

    def _begin_edit_group(self, is_active):
        self.end_edit_group()
        self._edit_sequence += 1
        self._edit_group = f"{self.TOOL_ID}:{self._edit_group_prefix}:{self._edit_sequence}"
        self._edit_active = is_active
        self.edit_group_started.emit()

    def end_edit_group(self):
        if self._edit_group is not None:
            self._edit_group = None
            self._edit_active = None
            self.edit_group_finished.emit()

    def eventFilter(self, watched, event):
        if watched in self._edit_controls and event.type() in (
                QEvent.Type.UngrabMouse, QEvent.Type.FocusOut, QEvent.Type.Hide,
                QEvent.Type.WindowDeactivate, QEvent.Type.MouseButtonDblClick, QEvent.Type.KeyPress):
            self.end_edit_group()
        return super().eventFilter(watched, event)

    def hideEvent(self, event):
        self.end_edit_group()
        super().hideEvent(event)

    def _set_parameters_pending(self, pending):
        if self._parameters_pending != pending:
            self._parameters_pending = pending
            self.preparation_changed.emit(pending)

    def set_source_identity(self, identity):
        self._source_identity = identity

    def set_tool_reference(self, reference):
        self._bound_tool_reference = reference

    @property
    def parameter_commit_stamp(self):
        return self._parameter_publication.commit_stamp if self._parameter_publication is not None else None

    def set_source_preview(self, spec, *, refresh=False):
        changed = spec != self._source_preview
        self._source_preview = spec
        if changed and refresh and self.external_preview and self._image_path:
            self.load_panel_source(self._image_path)

    def _ensure_preview_runner(self) -> PreviewTaskRunner:
        if self._preview_runner is None:
            self._preview_runner = PreviewTaskRunner()
        return self._preview_runner

    def _ensure_render_service(self):
        if self._render_service is None:
            from ...application.panel_render_service import PanelRenderService
            from ...application.tool_runtime import ToolRuntime
            sources = self._image_source_service
            self._render_service = PanelRenderService(ToolRuntime(compute_budget=sources.compute,
                execution_threads=sources.policy.operator_threads, resource_budget=sources.resources))
        return self._render_service

    def _release_source(self) -> None:
        bundle = self._source_bundle
        self._source_bundle = None
        if bundle is not None:
            bundle.close()
        self._original_image = None

    def load_panel_source(self, path, on_loaded=None):
        if not path:
            return
        def loaded(bundle):
            self._original_image = bundle.original
            if on_loaded is not None:
                on_loaded()
            self.update_preview()
        self.load_source_async(path, loaded, self._processing_error)

    def _processing_error(self, error):
        logger.error("工具 %s 处理失败：%s", self.TOOL_ID, error)
        key = error.error.message_key if isinstance(error, CoreOperationError) else str(error)
        self.request_status.emit(self._tr(key, self._tr("error.workflow.execution_failed")))

    def render_panel_image(self, *, preview=False):
        if self.parameters_pending:
            raise CoreOperationError(CoreError(ErrorCode.INVALID_INPUT, "tool.preparing"))
        if self.source_bundle is None or not self._image_source_service.matches_file(self.source_bundle.identity):
            raise ValueError("error.image.load")
        self._ensure_render_service()
        params = copy.deepcopy(self._params)
        if self.tool_adapter is not None:
            params = self.tool_adapter.ui_to_workflow(params)
        from ..services.background_operation import run_background
        service, identity, tool_id = self._render_service, self.source_bundle.identity, self.TOOL_ID
        sources = self._image_source_service
        return run_background(self, lambda cancellation: service.render(
            identity, tool_id, params, source_service=sources, preview=preview,
            is_cancelled=cancellation.is_cancelled))

    def preview_panel_image(self):
        if self.source_bundle is None or self.external_preview or self.parameters_pending:
            return
        self._ensure_render_service()
        identity = self.source_bundle.identity
        sources = self._image_source_service
        params = copy.deepcopy(self._params)
        if self.tool_adapter is not None:
            params = self.tool_adapter.ui_to_workflow(params)
        service, tool_id = self._render_service, self.TOOL_ID
        self.submit_preview(self.source_bundle.proxy,
                            lambda image, cancelled: service.render(identity, tool_id, params,
                                source_service=sources, preview=True, is_cancelled=cancelled),
                            self._emit_panel_preview, self._processing_error, cancellable=True)

    def _emit_panel_preview(self, image):
        with image.convert("RGBA") as rgba:
            result = QImage(rgba.tobytes(), rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888).copy()
        self.preview_ready.emit(result)
        if hasattr(self, "_histogram_widget"):
            from ...core.color_adjuster import ColorAdjuster
            self._histogram_widget.set_histogram_data(ColorAdjuster.compute_histogram(image))

    def cancel_source_load(self) -> None:
        self._source_generation += 1
        if self._source_job_id is not None and self._preview_runner is not None:
            self._preview_runner.cancel(self._source_job_id)
        self._source_job_id = None

    def load_source_async(
        self,
        path: str,
        on_loaded: Callable[[SourceBundle], None],
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> int:
        """后台加载源图；基类持有结果，切图或清理时释放。"""
        self.cancel_source_load()
        self.cancel_preview()
        self.cancel_background()
        self._release_source()
        self._needs_reload = True
        generation = self._source_generation
        request_path = path

        def finished(result: SourceLoadResult) -> None:
            bundle = result.bundle
            if (
                self._source_job_id != result.job_id
                or self._source_generation != generation
                or self._image_path != request_path
            ):
                if bundle is not None:
                    bundle.close()
                return
            self._source_job_id = None
            if result.error is not None:
                if on_error is not None:
                    on_error(result.error)
                else:
                    logger.error("Source image load failed: %s", result.error)
                return
            if bundle is None:
                raise RuntimeError("Source image load returned no image")
            if not self._image_source_service.matches_file(bundle.identity):
                bundle.close()
                error = CoreOperationError(CoreError(ErrorCode.SOURCE_CHANGED, "error.source.changed"))
                if on_error is not None:
                    on_error(error)
                else:
                    logger.error("Source image changed during load: %s", request_path)
                return
            self._source_bundle = bundle
            self._needs_reload = False
            try:
                on_loaded(bundle)
            except Exception:
                self._release_source()
                raise

        job_id = self._ensure_preview_runner().submit_source(generation, path, finished,
            source_service=self._image_source_service, proxy_only=self.external_preview,
            identity=self._source_identity if self.external_preview else None,
            half_size=self.external_preview and self._source_preview.half_size)
        self._source_job_id = job_id
        return job_id

    def cancel_preview(self) -> None:
        self._preview_generation += 1
        if self._preview_job_id is not None and self._preview_runner is not None:
            self._preview_runner.cancel(self._preview_job_id)
        self._preview_job_id = None

    def submit_preview(
        self,
        image: Image.Image,
        renderer: Callable[..., Image.Image],
        on_ready: Callable[[Image.Image], None],
        on_error: Optional[Callable[[BaseException], None]] = None,
        *, cancellable=False,
    ) -> int:
        """后台渲染固定图像；on_ready 在 GUI 线程调用且仅在回调内使用结果。"""
        self.cancel_preview()
        generation = self._preview_generation
        request_path = self._image_path
        identity = self._source_bundle.identity if self._source_bundle is not None else None

        def finished(result: PreviewResult) -> None:
            if (
                self._preview_job_id != result.job_id
                or self._preview_generation != generation
                or self._image_path != request_path
            ):
                return
            self._preview_job_id = None
            if identity is not None and (self._source_bundle is None
                    or self._source_bundle.identity != identity
                    or not self._image_source_service.matches_file(identity)):
                if on_error is not None:
                    on_error(CoreOperationError(CoreError(ErrorCode.SOURCE_CHANGED, "error.source.changed")))
                return
            if result.error is not None:
                if on_error is not None:
                    on_error(result.error)
                else:
                    logger.error("Preview render failed: %s", result.error)
                return
            if result.image is not None:
                on_ready(result.image)

        job_id = self._ensure_preview_runner().submit(generation, image, renderer, finished, cancellable=cancellable)
        self._preview_job_id = job_id
        return job_id

    def cancel_background(self) -> None:
        self._background_generation += 1
        if self._background_job_id is not None and self._preview_runner is not None:
            self._preview_runner.cancel(self._background_job_id)
        self._background_job_id = None
        self._set_parameters_pending(False)

    def submit_background(
        self,
        image: Optional[Image.Image],
        worker: Callable[[Optional[Image.Image]], Any],
        on_ready: Callable[[Any], None],
        on_error: Optional[Callable[[BaseException], None]] = None,
        *, cancellable=False,
    ) -> int:
        """后台处理图像快照，可返回非图像结果。"""
        self.cancel_background()
        generation = self._background_generation
        request_path = self._image_path
        identity = self._source_bundle.identity if self._source_bundle is not None else None
        service, tool_id = self._render_service, self.TOOL_ID

        def execute(image, cancelled):
            if service is not None:
                available = service.check_available(tool_id)
                if not available.success:
                    raise CoreOperationError(available.error)
            return worker(image, cancelled) if cancellable else worker(image)

        def finished(result: BackgroundResult) -> None:
            if (
                self._background_job_id != result.job_id
                or self._background_generation != generation
                or self._image_path != request_path
            ):
                return
            self._background_job_id = None
            if identity is not None and (self._source_bundle is None
                    or self._source_bundle.identity != identity
                    or not self._image_source_service.matches_file(identity)):
                if on_error is not None:
                    on_error(CoreOperationError(CoreError(ErrorCode.SOURCE_CHANGED, "error.source.changed")))
                return
            if service is not None:
                available = service.check_available(tool_id)
                if not available.success:
                    if on_error is not None:
                        on_error(CoreOperationError(available.error))
                    return
            if result.error is not None:
                if on_error is not None:
                    on_error(result.error)
                else:
                    logger.error("Background image task failed: %s", result.error)
                return
            on_ready(result.value)

        job_id = self._ensure_preview_runner().submit_work(generation, image, execute, finished,
                                                           budget=self._image_source_service.compute, cancellable=True)
        self._background_job_id = job_id
        return job_id

    @property
    def tool_adapter(self):
        """UI <-> workflow parameter mapper, if this tool has one."""
        return self.TOOL_ADAPTER

    def set_image_path(self, path: str, force: bool = False) -> None:
        """图片切换时通知面板加载新路径并恢复暂存。force=True 时强制重新装载。"""
        if (
            not force
            and self._image_path == path
            and not self._needs_reload
            and (
                self._source_bundle is None
                or self._image_source_service.matches_file(self._source_bundle.identity)
            )
        ):
            return
        self.end_edit_group()
        self._preview_timer.stop()
        self.cancel_source_load()
        self.cancel_preview()
        self.cancel_background()
        self._release_source()
        if hasattr(self, "_generation"):
            self._generation += 1
        self._image_path = path
        self._needs_reload = False
        self.on_image_path_changed(path)

    def reload(self) -> None:
        """强制按最新干净状态重载面板。"""
        if self._image_path:
            self.set_image_path(self._image_path, force=True)

    def on_image_path_changed(self, path: str) -> None:
        """子类重写以响应图片切换。"""
        pass

    def on_memory_budget_changed(self) -> None:
        """内存上限在运行中调整之后由宿主调用；界面上写了内存额度的面板重写它来重新投影，参数与预览不动。"""
        pass

    # ---- 核心抽象接口（子类必须实现） ----
    def build_ui(self) -> None:
        """构建面板控件。"""
        raise NotImplementedError

    def apply(self) -> Optional[Image.Image]:
        """执行完整全图渲染并返回 PIL Image（或 None）。"""
        raise NotImplementedError

    def reset(self) -> None:
        """重置参数为默认值。"""
        raise NotImplementedError

    # ---- 可选接口（子类重写） ----
    def update_preview(self) -> None:
        """触发防抖预览更新。"""
        if not self.external_preview and not self.parameters_pending:
            self.cancel_preview()
            if hasattr(self, "_generation"):
                self._generation += 1
            self._preview_timer.start()

    def restore_params(self, params: Dict[str, Any]) -> None:
        self.end_edit_group()
        self.cancel_preview()
        self.cancel_background()
        if self.PARAMETER_CONTRACT is not None:
            checked = self.PARAMETER_CONTRACT.normalize(params, self.PARAMETER_CONTRACT.version)
            if not checked.success:
                raise ValueError(checked.error.message_key)
            params = checked.data
        self._params = copy.deepcopy(params)
        self._sync_ui_from_params()
        self._published_params = copy.deepcopy(self._params)

    def _sync_ui_from_params(self) -> None:
        pass

    def _do_update_preview(self) -> None:
        """实际执行预览渲染。"""
        pass

    def cleanup(self) -> None:
        """面板关闭或切换时调用：停止 worker、释放资源。"""
        self.end_edit_group()
        self._preview_timer.stop()
        self.cancel_source_load()
        self.cancel_preview()
        self.cancel_background()
        self._release_source()
        self._needs_reload = True
        if self._preview_runner is not None and self._owns_preview_runner:
            self._preview_runner.shutdown()
            self._preview_runner = None
        if self._render_service is not None and self._owns_render_service:
            self._render_service.runtime.close()
            self._render_service = None

    def can_undo(self) -> bool:
        return False

    def can_redo(self) -> bool:
        return False

    def undo(self) -> None:
        pass

    def redo(self) -> None:
        pass

    def _push_undo(self) -> None:
        """用户编辑即将替换当前参数时调用；有面板内撤销栈的子类在此记录当前参数。"""

    def _commit_params(self, params: Dict[str, Any], record_stash: bool = True) -> bool:
        """提交一次用户编辑：参数先经契约校验，与当前参数不同才写入并发布。

        校验失败时保留原参数并把界面投影回原参数。返回提交后当前参数是否为所提交的参数；
        校验失败、参数未变或发布被回退时为 False。
        """
        if self.PARAMETER_CONTRACT is not None:
            checked = self.PARAMETER_CONTRACT.normalize(params, self.PARAMETER_CONTRACT.version)
            if not checked.success:
                self.request_status.emit(self._tr(checked.error.message_key))
                self._sync_ui_from_params()
                return False
            params = checked.data
        target = _canonical(params)
        if target == _canonical(self._params):
            return False
        self._push_undo()
        self.notify_params_changed(params, record_stash)
        return _canonical(self._params) == target

    def _apply_params(self, params: Dict[str, Any], record_stash: bool = True, *, publication=None) -> bool:
        """提交一组非控件产生的参数（预设、快捷操作、自动结果、重置），有变化时回填界面。"""
        if publication is not None and not publication.is_current():
            return False
        previous, self._next_parameter_publication = self._next_parameter_publication, publication
        try:
            if not self._commit_params(params, record_stash):
                return False
        finally:
            self._next_parameter_publication = previous
        self._sync_ui_from_params()
        self.update_preview()
        return True

    def notify_params_changed(self, params: Dict[str, Any], record_stash: bool = True) -> None:
        """向外部（包含暂存系统）广播最新参数。"""
        if self.PARAMETER_CONTRACT is not None:
            checked = self.PARAMETER_CONTRACT.normalize(params, self.PARAMETER_CONTRACT.version)
            if not checked.success:
                self.request_status.emit(self._tr(checked.error.message_key))
                return
            params = checked.data
        self.cancel_background()
        service = self._render_service
        publication = self._next_parameter_publication
        if service is not None:
            available = service.check_available(self.TOOL_ID)
            if not available.success:
                self.restore_params(self._published_params)
                self._processing_error(CoreOperationError(available.error))
                return
        self._params = copy.deepcopy(params)
        if service is not None and service.needs_preparation(self.TOOL_ID, params):
            self._preview_timer.stop()
            self.cancel_preview()
            settings, tool_id = copy.deepcopy(params), self.TOOL_ID

            def prepared(reference):
                self._set_parameters_pending(False)
                if self._bound_tool_reference is not None:
                    self._bound_tool_reference = reference
                self._publish_prepared_params(settings, record_stash, publication)

            def failed(error):
                self.restore_params(self._published_params)
                self._processing_error(error)

            self.submit_background(None,
                lambda image, reference=self._bound_tool_reference: service.prepare(tool_id, settings,
                    **({"reference": reference} if reference is not None else {})),
                prepared, failed)
            self._set_parameters_pending(True)
            return
        self._publish_prepared_params(params, record_stash, publication)

    def _publish_prepared_params(self, params, record_stash, publication):
        if publication is not None and not publication.is_current():
            self.restore_params(self._published_params)
            return
        previous, self._parameter_publication = self._parameter_publication, publication
        try:
            self._publish_params(params, record_stash)
        finally:
            self._parameter_publication = previous

    def _publish_params(self, params, record_stash):
        self._published_params = copy.deepcopy(params)
        self.params_changed.emit(params)
        if self._params != params:
            return
        self.update_preview()
        if record_stash and self._draft_store and self._image_path:
            self._draft_store.set_draft(self._image_path, self.TOOL_ID, params)
