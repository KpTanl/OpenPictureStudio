"""Studio Workspace for OpenPictureStudio.

Dedicated editing workspace separating photo viewing from creative retouching.
Features:
  1. Left: ToolRail (tool entries with modified dots)
  2. Center: Dedicated studio canvas with hold-to-compare and overlay support, filmstrip below
  3. Right: "Adjust / History / AI Assistant" column with notices, the active tool panel and the edit stack;
     photo info takes its place
  4. CoreAPI owns workflows, project history and AI candidate commits.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from datetime import datetime
import json
import logging
import os
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Dict, Optional

from PIL import Image
from resources.styles.theme_variables import (CANVAS_TOOLBAR_HEIGHT, CANVAS_TOOLBAR_MARGIN,
                                              SIDEBAR_MAX_WIDTH, SIDEBAR_MIN_WIDTH, SPACING_MD, SPACING_SM, px)
from PySide6.QtCore import QSize, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QFileDialog,
    QFrame,
    QLineEdit,
    QStackedWidget,
    QHBoxLayout,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..application.studio_service import STUDIO_PREVIEW_MAX_DIMENSION, StudioRenderService, comparison_workflow
from ..application.agent_service import AgentService
from ..application.assistant_models import AssistantContext
from ..application.audit import AuditLog
from ..application.services import ExportRequest
from ..application.result_types import CoreOperationError, CoreResult, ErrorCode, SourceChangedError
from ..application.studio_preview_cache import CanvasPreviewState, PreviewKey, preview_variant
from ..core.contracts.result import memory_error_key
from ..application.studio_preview_queue import StudioPreviewContext, StudioPreviewQueue, StudioPreviewRequest
from ..application.studio_detail import needs_detail
from ..application.local_mask_service import MaskGesture, upgrade_color_masks
from ..core.contracts.preview import PreviewTarget
from ..core.contracts import WorkflowGraph
from ..application.edit_sessions import EditSessionService, EditSession, STAGE_SPECS as _STAGE_SPECS
from ..application.edit_changes import parameter_changes, stage_changes
from ..core.contracts.image_display import (DEFAULT_NON_RAW_DISPLAY_MODE, DEFAULT_RAW_DISPLAY_MODE,
                                            NonRawDisplayMode, RawDisplayMode)
from .adapters.studio_adapter import StudioSession
from .edit_descriptions import changes_summary, stage_title, step_text
from .panels.ai_assistant_panel import AIAssistantPanel
from .widgets.action_button import plain_action_text
from .widgets.edit_stack import StackRow
from .widgets.history_list import HistoryStep
from .widgets.history_panel import HistoryPanel
from .widgets.segmented_control import AppleSegmentedControl
from .widgets.side_dock import SideDock
from .window_layout import WindowLayout, set_splitter_width, sidebar_width, window_layout
from .dialogs.apply_target_dialog import ApplyTarget
from .image_viewer import DisplaySnapshot, ImageViewer
from .panels import BaseToolPanel
from .panels.registry import create_tool_panel, PANEL_TYPES, TOOL_ENTRIES, resolve_tool_id, tool_entry_for
from .services import ApplyService
from .services.export_task import ExportTask
from .services.project_task_runner import ProjectTaskRunner
from .services.studio_preview_runner import StudioHistogramRunner, StudioPreviewWorker, SamplingDelivery, SamplingResources
from .services.studio_original_runner import StudioOriginalRunner
from .services.studio_detail_runner import DetailResources, StudioDetailRunner
from .studio_action_state import StudioActionState
from .themed_mixin import ThemedI18nMixin
from .widgets.apple_button import AppleButton
from .widgets.notice_bar import NoticeBar
from .widgets.photo_info_panel import PhotoInfoSidebar
from .widgets.thumbnail_panel import ThumbnailPanel
from .widgets.toast import DEFAULT_DURATION_MS
from .widgets.tool_rail import ToolRail
from .widgets.tool_sidebar import ToolSidebar
from .widgets.message_dialog import show_error
from .widgets.progress_dialog import ProgressDialog

if TYPE_CHECKING:
    from .main_window import MainWindow

logger = logging.getLogger(__name__)

PIPELINE_TOOLS = frozenset(_STAGE_SPECS)

# 右侧栏顶部分段开关的三页
_TAB_ADJUST, _TAB_HISTORY, _TAB_ASSISTANT = range(3)

class StudioWorkspace(QWidget, ThemedI18nMixin):
    """独立修图工作区。

    管理统一会话、级联渲染管线与工具面板容器。
    """

    applied = Signal(str)           # 新保存的图片路径
    back_requested = Signal()       # 请求切回看图工作区

    resource_management_requested = Signal(str)
    model_management_requested = Signal(str, str)  # 能力、当前所选模型的标识
    action_state_changed = Signal(object)  # StudioActionState
    detail_state_changed = Signal(str, str)
    canvas_preview_changed = Signal(object)
    toast_requested = Signal(str, int)     # 一句话的短暂消息与显示时长（毫秒）
    sidebar_preferences_changed = Signal(bool, int, int)  # 右侧栏是否收起、它的宽度、照片信息栏的宽度（0 为未拖过）

    preview_started = Signal()
    preview_finished = Signal()

    def __init__(
        self,
        main_window: MainWindow,
        parent: Optional[QWidget] = None,
        apply_service: Optional[ApplyService] = None,
        core=None,
        panel_services=None,
        preview_cache=None,
        source_load_request=None,
        source_load_cancel=None,
        browse_source_request=None,
        browse_source_cancel=None,
        raw_display_mode=DEFAULT_RAW_DISPLAY_MODE,
        non_raw_display_mode=DEFAULT_NON_RAW_DISPLAY_MODE,
        assistant_runner=None,
        edit_sessions=None,
        canvas_preview_mode="auto",
        canvas_scale_runner=None,
    ):
        super().__init__(parent)
        self._source_load_request = source_load_request
        self._source_load_cancel = source_load_cancel
        self._browse_source_request = browse_source_request
        self._browse_source_cancel = browse_source_cancel
        self._raw_display_mode = RawDisplayMode(raw_display_mode)
        self._non_raw_display_mode = NonRawDisplayMode(non_raw_display_mode)
        self._window = main_window
        self._assistant_runner = assistant_runner
        self._beauty_readiness = None
        self._assistant_expanded = False
        self.setObjectName("studioWorkspace")

        self._render_service = StudioRenderService(core,
            output_writer=apply_service.output_service.writer if apply_service else None)
        self.core = self._render_service.core
        self._preview_cache = preview_cache
        self._canvas_preview_mode = canvas_preview_mode
        self._canvas_scale_runner = canvas_scale_runner
        self._canvas_preview_state = CanvasPreviewState(mode=canvas_preview_mode)
        self._canvas_image_key = None
        self._canvas_failed_generation = None
        self._canvas_full_frames = []
        frames = self._canvas_full_frames
        self.destroyed.connect(lambda: [frame.close() for frame in frames])
        self._syncing_canvas = False
        self._loading_preview = None
        self._loading_placeholder = None
        self._image_load_generation = 0
        self._image_load_path = None
        self._load_failed = False
        self._load_error = None
        self._awaiting_full_source = False
        self._preserve_loading_view = False
        from ..application.panel_render_service import PanelRenderService
        from .services.preview_task_runner import PreviewTaskRunner
        self._panel_services = panel_services or {"image_source_service": self.core.image_sources,
                                "preview_runner": PreviewTaskRunner(),
                                "render_service": PanelRenderService(self.core.tools)}
        self.destroyed.connect(self._panel_services["preview_runner"].shutdown)
        from .services.model_task_runner import ModelTaskRunner
        self._models_runner = ModelTaskRunner(self.core.models, self)
        self._models_runner.changed.connect(self._on_model_availability_changed)
        self._model_generation = self.core.tools.generation
        self._model_check_job = None
        from ..application.storage_workspaces import ManagedWorkspace
        self._audit_directory = ManagedWorkspace("audit")
        self.agent = AgentService(self.core, AuditLog(str(Path(self._audit_directory.name) / "audit.jsonl"),
                                                    workspace=self._audit_directory))
        self._commit_busy = False
        self._image_loading = False
        self._output_generation = 0
        self._output_tasks = {}
        self._output_context = None
        self._output_progress = None
        self._output_pool = QThreadPool(self)
        self._output_pool.setMaxThreadCount(1)
        self._project_runner = ProjectTaskRunner(self.core, self)
        self._project_runner.finished.connect(self._on_project_finished)
        self.destroyed.connect(self._project_runner.shutdown)
        self._project_request = None
        self._project_origin = None
        self._project_progress = None
        self._exit_save_callback = None
        self._exit_save_snapshots = ()
        self._exit_save_pending = []
        if edit_sessions is None:
            from ..core.draft_store import DraftStore
            state_store = getattr(main_window, "_edit_state_store", None)
            edit_sessions = EditSessionService(self.core, state_store.drafts if state_store else DraftStore(),
                supported_tools=PANEL_TYPES, resolve_tool=resolve_tool_id)
        self.edit_sessions = edit_sessions
        self._preview_source_lease = None
        self.destroyed.connect(self.release_preview_source)
        self.session: Optional[EditSession] = None
        self._current_path: Optional[str] = None

        self._active_tool_id = "color_adjust"
        # 画布工具（自由裁切、形状裁切、套索）：画布上是这一步之后的结果，还是调整时的框与轮廓。界面状态，不是参数
        self._result_view = False
        self._entry_tools: dict[str, tuple[str, ...]] = {}
        self._entry_last_tool: dict[str, str] = {}
        self._tool_panels: dict[str, BaseToolPanel] = {}
        self._panel_sessions: dict[str, EditSession] = {}
        self._grid_settings: dict[str, dict] = {}
        self._mask_ui_states = {}
        self._stage_views = {}
        self._preview_target = PreviewTarget()
        self._preview_target_session = None
        self._pending_stage_view = False
        self._mask_gesture = None
        self._mask_candidate = None
        self._mask_commit_stamp = None
        self._mask_generation = None
        self._mask_tool_bar = None
        self._mask_timer = QTimer(self)
        self._mask_timer.setInterval(100)
        self._mask_timer.setSingleShot(True)
        self._mask_timer.timeout.connect(self._flush_mask_preview)

        self._render_generation = 0
        self._displayed_generation = -1
        self._preview_queue = StudioPreviewQueue()
        self._color_preview_gesture = None
        # 手动预览的工具：请求代次 -> 面板发起时的预览凭据
        self._manual_preview_tickets: dict[int, int] = {}
        self._sampling_counter = 0
        self._sampling_epoch = None
        self._sampling_resources = SamplingResources()
        self.destroyed.connect(self._sampling_resources.close)
        self._displayed_request = None
        self._displayed_frame = None
        self._displayed_histogram = None
        self._comparing = False
        self._original_qimage: Optional[QImage] = None
        self._original_display = None
        self._original_level_requested = None
        self._original_geometry = ""
        self.destroyed.connect(self._clear_original_display)
        self._original_error = None
        self._original_runner = StudioOriginalRunner(self.core.image_sources, self,
            complete_source=self.core.complete_source_preview, render_original=self._render_service.render_original)
        self._original_runner.ready.connect(self._on_original_ready)
        self._original_runner.source_ready.connect(self._on_full_source_ready)
        self.destroyed.connect(self._original_runner.shutdown)
        self._last_rendered_qimage: Optional[QImage] = None
        self._detail_resources = DetailResources()
        self.destroyed.connect(self._detail_resources.close)
        self._detail_request = None
        self._detail_attempted = None
        self._detail_showing = False
        self._detail_state = ("", "")
        self._detail_error_key = ""
        self._detail_runner = StudioDetailRunner(self._render_service, self)
        self._detail_runner.ready.connect(self._on_detail_ready)
        self.destroyed.connect(self._detail_runner.shutdown)
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(200)
        self._detail_timer.timeout.connect(self._start_detail_render)
        self._thread_pool = QThreadPool(self)
        self._thread_pool.setMaxThreadCount(1)
        self._restoring = False
        # 历史列表里各步的文字与参数：修订不会变，按修订标识记着；换会话或换语言时清空
        self._history_session = None
        self._history_stages: dict[str, dict] = {}
        self._history_texts: dict[str, str] = {}

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._do_pipeline_render)
        self._histogram_runner = StudioHistogramRunner(self._render_service, self)
        self._histogram_runner.ready.connect(self._on_histogram_ready)
        self.destroyed.connect(self._histogram_runner.shutdown)

        self._init_themed_i18n()
        self._apply_service = apply_service or ApplyService(self, self._tr)
        self._action_state = StudioActionState()
        self._init_ui()
        self.tool_rail.set_entries(tuple((entry.entry_id, entry.icon, self._tr(entry.title_key, entry.title_default))
                                         for entry in TOOL_ENTRIES))
        self._refresh_tool_navigation()
        self._retranslate_history()

    def _init_ui(self) -> None:
        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. 最左侧的工具栏
        self.tool_rail = ToolRail(self)
        self.tool_rail.entry_requested.connect(self._on_rail_entry_requested)
        root_layout.addWidget(self.tool_rail)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("studioSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(4)

        # 2. 中间：修图画布与主视口
        center_container = QWidget(self)
        center_container.setObjectName("studioCenterContainer")
        center_layout = QVBoxLayout(center_container)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)

        # 画布滚动视口
        self.scroll_area = QScrollArea(center_container)
        self.scroll_area.setObjectName("studioScrollArea")
        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.viewer = ImageViewer(self.scroll_area, image_source_service=self.core.image_sources,
                                  canvas_scale_runner=self._canvas_scale_runner)
        self.viewer.presentation_changed.connect(self._on_presentation_changed)
        self.viewer.view_changed.connect(self._refresh_detail)
        self.viewer.setObjectName("studioViewer")
        self.viewer.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.scroll_area.setWidget(self.viewer)

        # 画布 + 胶片条放入垂直分割器，可上下拖拽调整（与看图模式一致）
        self.canvas_splitter = QSplitter(Qt.Orientation.Vertical, center_container)
        self.canvas_splitter.setObjectName("studioCanvasSplitter")
        self.canvas_splitter.setChildrenCollapsible(False)
        self.canvas_splitter.setHandleWidth(4)
        self.canvas_splitter.addWidget(self.scroll_area)

        # 底部胶片条：与看图工作区缩略图面板保持同步
        self.thumbnail_panel = ThumbnailPanel(self.canvas_splitter, image_source_service=self.core.image_sources)
        self.thumbnail_panel.setObjectName("studioThumbnailPanel")
        self.canvas_splitter.addWidget(self.thumbnail_panel)

        self.canvas_splitter.setStretchFactor(0, 1)
        self.canvas_splitter.setStretchFactor(1, 0)
        center_layout.addWidget(self.canvas_splitter, 1)

        self.splitter.addWidget(center_container)

        # 3. 右侧栏：“调整 / 历史 / AI 助手”、按条件出现的提示条、工具面板容器
        self.tool_sidebar = ToolSidebar(self)
        self.tool_sidebar.reset_requested.connect(self.reset_current_tool)
        self.tool_sidebar.close_requested.connect(self._on_tool_sidebar_close)
        self.tool_sidebar.discard_stash_requested.connect(self.cancel_current_tool)
        self.tool_sidebar.tool_requested.connect(self.switch_tool)
        self.tool_sidebar.stage_reset_requested.connect(self.reset_tool)
        self.right_sidebar = QFrame(self)
        self.right_sidebar.setObjectName("editSidebar")
        self.right_sidebar.setMinimumWidth(SIDEBAR_MIN_WIDTH)
        self.right_sidebar.setMaximumWidth(SIDEBAR_MAX_WIDTH)
        self._sidebar_collapsed = False
        # 窄窗口里右侧栏是浮层；这里记着它停靠时是否收起，回到停靠时恢复
        self._docked_collapsed = False
        self._layout_rules: WindowLayout = window_layout(SIDEBAR_MAX_WIDTH * 4)
        self._edit_width = 0
        self._info_width = 0
        margin = px(SPACING_MD)
        right_layout = QVBoxLayout(self.right_sidebar)
        right_layout.setContentsMargins(0, margin, 0, 0)
        right_layout.setSpacing(px(SPACING_SM))
        self.sidebar_tabs = AppleSegmentedControl(self.right_sidebar)
        self.sidebar_tabs.add_segments([self._tr("assistant.adjust", "Adjust"), self._tr("history.title", "History"),
                                        self._tr("assistant.title", "AI Assistant")])
        tabs_layout = QVBoxLayout()
        tabs_layout.setContentsMargins(margin, 0, margin, 0)
        tabs_layout.addWidget(self.sidebar_tabs)
        right_layout.addLayout(tabs_layout)

        self._notices = QWidget(self.right_sidebar)
        notices_layout = QVBoxLayout(self._notices)
        notices_layout.setContentsMargins(margin, 0, margin, 0)
        notices_layout.setSpacing(px(SPACING_SM))
        self._notice_load = NoticeBar("warning", parent=self._notices)
        self._notice_load.action_triggered.connect(self._retry_image_load)
        self._notice_source = NoticeBar("info", parent=self._notices)
        self._notice_read_only = NoticeBar("warning", parent=self._notices)
        self._notice_read_only.action_triggered.connect(self.recheck_project_tools)
        self._notice_resources = NoticeBar("warning", parent=self._notices)
        self._notice_resources.action_triggered.connect(
            lambda: self.resource_management_requested.emit(self._active_tool_id))
        self._notice_enhance = NoticeBar("info", parent=self._notices)
        self._notice_enhance.action_triggered.connect(self._choose_enhancement_upgrade)
        self._notice_bars = (self._notice_load, self._notice_source, self._notice_read_only,
                             self._notice_resources, self._notice_enhance)
        for bar in self._notice_bars:
            bar.setVisible(False)
            notices_layout.addWidget(bar)
        self._notices.setVisible(False)
        right_layout.addWidget(self._notices)

        self.right_stack = QStackedWidget(self.right_sidebar)
        self.right_stack.addWidget(self.tool_sidebar)
        self.history_panel = HistoryPanel(self.right_stack)
        self.history_panel.step_requested.connect(self.checkout_revision)
        self.right_stack.addWidget(self.history_panel)
        if self._assistant_runner is None:
            from .services.assistant_runner import AssistantRunner
            from ..application.storage_paths import data_root
            directory = data_root() / "assistant"
            self._assistant_runner = AssistantRunner(str(directory), self)
        self._assistant_runner.set_image_services(self.core, self._thread_pool, audit=self.agent.record_external_request,
                                                  face_detector=self._assistant_face_detector(), agent=self.agent)
        self.assistant_panel = AIAssistantPanel(self, self.core, self.agent, self._thread_pool, self.right_stack,
                                              runner=self._assistant_runner)
        self.right_stack.addWidget(self.assistant_panel)
        self.sidebar_tabs.bind_stacked_widget(self.right_stack)
        self.sidebar_tabs.currentIndexChanged.connect(self._sidebar_mode_changed)
        self.assistant_panel.render_requested.connect(self._schedule_pipeline_render)
        self.assistant_panel.committed.connect(self._on_agent_committed)
        self.assistant_panel.comparison_requested.connect(self._on_assistant_comparison)
        self.assistant_panel.commit_busy.connect(self._set_commit_busy)
        self.assistant_panel.settings_requested.connect(lambda: self._window._show_settings("ai_services"))
        self.assistant_panel.expanded_requested.connect(self._set_assistant_expanded)
        right_layout.addWidget(self.right_stack, 1)
        self.splitter.addWidget(self.right_sidebar)

        # 4. 照片信息栏：展开时占用右侧栏的位置，与它在同一个分隔条里，都可以拖动宽度
        self.info_sidebar = PhotoInfoSidebar(self._tr, self, read_summary=False)
        self.photo_info_panel = self.info_sidebar.panel
        self.photo_info_panel.expanded_changed.connect(self._place_right_sidebar)
        self.splitter.addWidget(self.info_sidebar)

        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.splitterMoved.connect(self._on_sidebar_dragged)

        root_layout.addWidget(self.splitter, 1)

        # 窄窗口：两栏都浮在画布上，底部让出工具条
        inset = CANVAS_TOOLBAR_HEIGHT + 2 * CANVAS_TOOLBAR_MARGIN
        self._sidebar_docks = (SideDock(self.right_sidebar, inset), SideDock(self.info_sidebar, inset))
        self._assistant_workbench = QFrame(self)
        self._assistant_workbench.setObjectName("assistantWorkbench")
        workbench_layout = QVBoxLayout(self._assistant_workbench)
        workbench_layout.setContentsMargins(0, 0, 0, 0)
        self._assistant_workbench.hide()

    def _set_assistant_expanded(self, expanded):
        if self._assistant_expanded == expanded:
            return
        self._assistant_expanded = expanded
        if expanded:
            self.right_stack.removeWidget(self.assistant_panel)
            self._assistant_workbench.layout().addWidget(self.assistant_panel)
            self._assistant_workbench.setGeometry(self.rect())
            self._assistant_workbench.show()
            self._assistant_workbench.raise_()
        else:
            self._assistant_workbench.layout().removeWidget(self.assistant_panel)
            self.right_stack.insertWidget(_TAB_ASSISTANT, self.assistant_panel)
            self.sidebar_tabs.setCurrentIndex(_TAB_ASSISTANT)
            self.right_stack.setCurrentWidget(self.assistant_panel)
            self._assistant_workbench.hide()
            self._show_right_sidebar()
        self.assistant_panel.set_expanded(expanded)
        self.assistant_panel.show()

    def _place_right_sidebar(self, *_) -> None:
        """右侧同一时间只有一栏：照片信息展开时参数栏让位，收起后参数栏回到原来的状态。"""
        self.right_sidebar.setVisible(not self._sidebar_collapsed and not self.photo_info_panel.expanded)
        self._apply_sidebar_widths()

    def _show_right_sidebar(self) -> None:
        self._set_sidebar_collapsed(False)
        self.photo_info_panel.set_expanded(False)
        self._place_right_sidebar()

    def _on_tool_sidebar_close(self) -> None:
        """Collapse the right tool sidebar without leaving the studio workspace."""
        self._set_sidebar_collapsed(True)
        self._place_right_sidebar()

    def _set_sidebar_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._sidebar_collapsed:
            return
        self._sidebar_collapsed = collapsed
        if not self._layout_rules.floating_sidebar:
            self._publish_sidebar_preferences()

    # ------------------------------------------------------------------
    # 右侧栏的宽度、浮层与要记住的状态
    # ------------------------------------------------------------------
    def apply_panel_preferences(self, collapsed: bool, edit_width: int, info_width: int) -> None:
        """恢复上次的状态：右侧栏是否收起、两栏各自拖出的宽度（0 为未拖过）。"""
        self._docked_collapsed = collapsed
        self._edit_width, self._info_width = edit_width, info_width
        if not self._layout_rules.floating_sidebar:
            self._sidebar_collapsed = collapsed
        self._place_right_sidebar()

    def apply_window_layout(self, rules: WindowLayout) -> None:
        """窗口宽度变化后的版式：右侧栏停靠还是浮层，以及没有拖过时的默认宽度。"""
        floating, was_floating = rules.floating_sidebar, self._layout_rules.floating_sidebar
        self._layout_rules = rules
        if floating != was_floating:
            if floating:
                self._docked_collapsed = self._sidebar_collapsed
                self._sidebar_collapsed = True
                self.photo_info_panel.set_expanded(False)
                for dock in self._sidebar_docks:
                    dock.attach(self.scroll_area)
            else:
                for dock, widget in zip(self._sidebar_docks, (self.right_sidebar, self.info_sidebar)):
                    dock.detach()
                    self.splitter.addWidget(widget)
                self._sidebar_collapsed = self._docked_collapsed
        self._place_right_sidebar()

    def _apply_sidebar_widths(self) -> None:
        """两栏的宽度：不小于当前这一页需要的宽度，其余按用户拖出的宽度或窗口尺寸的默认值。"""
        page = self.right_stack.currentWidget()
        needed = (self.tool_sidebar.content_min_width() if page is self.tool_sidebar
                  else page.minimumSizeHint().width())
        # 这一栏自己的描边也占宽度
        needed += self.right_sidebar.width() - self.right_sidebar.contentsRect().width()
        self.right_sidebar.setMinimumWidth(max(SIDEBAR_MIN_WIDTH, needed))
        self.right_sidebar.setMaximumWidth(max(SIDEBAR_MAX_WIDTH, needed))
        widths = (sidebar_width(self._edit_width, self._layout_rules.edit_width, needed),
                  sidebar_width(self._info_width, self._layout_rules.info_width))
        for dock, widget, width in zip(self._sidebar_docks, (self.right_sidebar, self.info_sidebar), widths):
            if self._layout_rules.floating_sidebar:
                dock.set_width(width)
            else:
                set_splitter_width(self.splitter, widget, width)

    def _on_sidebar_dragged(self, *_) -> None:
        """用户拖动了分隔条：把当前可见的那一栏的宽度记为新的选择。"""
        if not self.right_sidebar.isHidden():
            self._edit_width = self.right_sidebar.width()
        elif not self.info_sidebar.isHidden():
            self._info_width = self.info_sidebar.width()
        else:
            return
        self._publish_sidebar_preferences()

    def _publish_sidebar_preferences(self) -> None:
        collapsed = self._docked_collapsed if self._layout_rules.floating_sidebar else self._sidebar_collapsed
        self.sidebar_preferences_changed.emit(collapsed, self._edit_width, self._info_width)

    def _set_notice(self, bar: NoticeBar, text: str, action_key: str = "") -> None:
        """显示或收起一条提示；文字为空表示收起。"""
        bar.set_text(text)
        bar.set_action_text(self._tr(action_key) if action_key and text else "")
        bar.setVisible(bool(text))
        self._notices.setVisible(any(not item.isHidden() for item in self._notice_bars))

    def _toast(self, text: str, duration_ms: int = DEFAULT_DURATION_MS) -> None:
        self.toast_requested.emit(text, duration_ms)

    def _retry_image_load(self) -> None:
        if self._image_load_path and self._source_load_request is not None:
            self._source_load_request(self._image_load_path)

    # ------------------------------------------------------------------
    # 会话初始化与图片装载
    # ------------------------------------------------------------------
    def release_preview_source(self):
        lease, self._preview_source_lease = self._preview_source_lease, None
        if lease is not None:
            lease.close()

    def hold_preview_source(self, lease):
        self.release_preview_source()
        self._preview_source_lease = lease

    def cached_session_id(self, path: str):
        session = self.edit_sessions.get(path)
        return session.core_id if session is not None else None

    def prepare_batch_capture(self):
        if not self._parameters_ready():
            return False
        for panel in self._tool_panels.values():
            panel.end_edit_group()
        return self._persist_draft().success

    def refresh_external_edit(self, path):
        if self.session is None or self.edit_sessions.key(path) != self.edit_sessions.key(self.session.image_path):
            return
        self.stop_preview()
        self.session.refresh()
        self._sync_panels_from_session()
        self._refresh_modified_tools()
        self._schedule_pipeline_render()
        self._update_actions_state()

    def set_raw_display_mode(self, mode):
        mode = RawDisplayMode(mode)
        if mode != self._raw_display_mode:
            self._raw_display_mode = mode
            self._reload_initial_display_source(is_raw=True)

    def set_non_raw_display_mode(self, mode):
        mode = NonRawDisplayMode(mode)
        if mode != self._non_raw_display_mode:
            self._non_raw_display_mode = mode
            self._reload_initial_display_source(is_raw=False)

    def _reload_initial_display_source(self, *, is_raw):
        path = self._image_load_path
        if (self._image_loading and path and self._source_load_request is not None
                and self.cached_session_id(path) is None
                and self.core.image_sources.supports_quick_preview(path) == is_raw):
            self._source_load_request(path)

    def initial_workflow(self, path: str):
        return self.edit_sessions.initial_workflow(path, raw_display_mode=self._raw_display_mode,
                                                   non_raw_display_mode=self._non_raw_display_mode)

    def preview_key(self, path: str, workflow=None, *, preview_target=None):
        if self._canvas_preview_mode == "full":
            return None
        session = self.edit_sessions.get(path)
        if workflow is None:
            workflow = self.preview_workflow(path)
        try:
            variant = preview_variant(workflow, self.core.tools.generation)
            variant += ":canvas-" + self._canvas_preview_mode
            if preview_target is not None and preview_target.mode != "full":
                variant += ":" + preview_target.mode + ":" + preview_target.node_id
            if session is not None and self.core.source_preview(session.core_id).half_size:
                variant += ":raw-half-v1"
            if session is not None and session.read_only:
                variant += ":" + session.history_revision
            return PreviewKey.capture(path, variant)
        except OSError:
            return None

    def preview_workflow(self, path: str):
        session = self.edit_sessions.get(path)
        if session is not None:
            return session.render_snapshot(
                self._active_tool_id if self.sidebar_tabs.currentIndex() == 0 and not self._result_view
                else None).workflow
        return self.initial_workflow(path)

    def can_prefetch(self):
        return (self.session is not None and not self._image_loading and not self._commit_busy
                and self._detail_state[0] != "preparing"
                and self._preview_queue.pending is None and self._preview_queue.active is None)

    def show_loading_preview(self, image, reference_size):
        if self._image_loading:
            self._loading_preview = image
            self._show_loading_image(image, QSize(*reference_size),
                                     preserve_zoom=self._preserve_loading_view)
            self._loading_placeholder = None
            self._preserve_loading_view = True

    def show_loading_placeholder(self, snapshot: DisplaySnapshot, *, generation: int):
        if (not self._image_loading or generation != self._image_load_generation
                or os.path.normcase(snapshot.path) != os.path.normcase(self._image_load_path)
                or self._loading_preview is not None):
            return
        self._loading_placeholder = (generation, snapshot.path)
        self._show_loading_image(snapshot.image, snapshot.reference_size, preserve_zoom=False)
        self._preserve_loading_view = True

    def _show_loading_image(self, image, reference_size, *, preserve_zoom):
        horizontal = self.scroll_area.horizontalScrollBar()
        vertical = self.scroll_area.verticalScrollBar()
        position = (horizontal.value(), vertical.value())
        self.viewer.set_display_pair(image, reference_size, preserve_zoom=preserve_zoom)
        self._release_canvas_full_frames()
        if preserve_zoom:
            horizontal.setValue(position[0])
            vertical.setValue(position[1])

    def prepare_image_load(self, path: Optional[str]) -> int | None:
        if self._commit_busy:
            return
        if self._loading_placeholder is not None:
            self.viewer.clear()
        self._loading_placeholder = None
        self._preserve_loading_view = False
        self._image_load_generation += 1
        self._image_load_path = os.path.normpath(path) if path else None
        self.photo_info_panel.set_path(self._image_load_path)
        self._load_failed = False
        self._load_error = None
        self._awaiting_full_source = False
        self.cancel_project_operation(force=True, resume=False)
        self.stop_preview()
        self._detail_resources.release_source()
        self._original_runner.cancel()
        self._original_error = None
        self._render_generation += 1
        self._image_loading = bool(path)
        self.assistant_panel.invalidate(clear=path is not None and os.path.normpath(path) != self._current_path)
        for panel in self._tool_panels.values():
            panel.cleanup()
        if self.session is not None:
            self.release_preview_source()
        self.session = None
        self._comparing = False
        self._loading_preview = None
        if path is None:
            self.viewer.clear()
            self._release_canvas_full_frames()
        else:
            self.viewer.set_overlay(None)
        self._clear_original_display()
        self._last_rendered_qimage = None
        if path and not self.viewer.has_image:
            self.viewer.setText(self._tr("studio.image_loading", "Loading image…"))
        self.tool_sidebar.hide()
        self._update_actions_state()
        self.assistant_panel.refresh()
        return self._image_load_generation

    def show_image_load_error(self, error) -> None:
        if self._loading_placeholder is not None:
            self.viewer.clear()
        self._loading_placeholder = None
        self._preserve_loading_view = False
        self.stop_preview()
        self._detail_resources.release_source()
        self._render_generation += 1
        if self.session is not None:
            self.release_preview_source()
        self.session = None
        self._image_loading = False
        self._loading_preview = None
        self._clear_original_display()
        self._last_rendered_qimage = None
        self.viewer.set_overlay(None)
        self.tool_sidebar.hide()
        self._load_failed = True
        self._load_error = error
        if not self.viewer.has_image:
            self.viewer.setText(self._load_error_text())
        self._update_actions_state()

    def _load_error_text(self) -> str:
        if not self._load_failed:
            return ""
        error = self._load_error
        if getattr(error, "message_key", None):
            return self._tr(error.message_key, self._tr(
                "studio.project_failed", "Project operation failed. Check the source and destination."
            ))
        return self._tr("studio.load_failed", "OPS initialization failed")

    def load_prepared_session(self, path: str, source, project, *,
                              qimage=None, preview_key=None) -> None:
        if preview_key != self.preview_key(path):
            self._loading_preview = None
        normalized = os.path.normpath(path)
        cached = self.edit_sessions.get(normalized)
        if cached is not None:
            if cached.core_id != project.session_id:
                raise ValueError("OPS session changed during loading")
            self.load_image_path(normalized, prepared_qimage=qimage)
            return
        self.load_image_path(
            path, prepared_source=source, prepared_project=project,
            prepared_qimage=qimage,
        )

    def load_image_path(self, path: Optional[str], force: bool = False, *,
                        prepared_source=None, prepared_project=None,
                        prepared_qimage=None) -> None:
        """从主看图区载入图片，并初始化互通会话。"""
        if self._commit_busy:
            return
        self._result_view = False
        if force or (os.path.normpath(path) if path else None) != self._current_path:
            self.cancel_project_operation(force=True, resume=False)
        if path and prepared_source is None and prepared_qimage is None:
            if force:
                self.release_preview_source()
                self.edit_sessions.close(path)
            if self._source_load_request is not None:
                self._source_load_request(path)
                return
            from ..application.studio_source_service import StudioSourceService, StudioLoadRequest
            from .services.background_operation import run_background
            source_service = StudioSourceService(self.core)
            request = StudioLoadRequest(self._render_generation, path, self.cached_session_id(path),
                                         initial_workflow=self.initial_workflow(path),
                                         preview_workflow=self.preview_workflow(path))
            self.prepare_image_load(path)
            from .services.studio_source_runner import source_qimage
            def prepare(cancellation):
                result = source_service.prepare(request, is_cancelled=cancellation.is_cancelled)
                if not result.success:
                    return result, None
                try:
                    image = source_qimage(result.data.image)
                    checked = source_service.validate_prepared(result.data)
                    if not checked.success or cancellation.is_cancelled():
                        result.data.close()
                        return (checked if not checked.success else
                                CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")), None
                    return result, image
                except BaseException:
                    result.data.close()
                    raise
            result, image = run_background(self, prepare)
            if not result.success:
                self.show_image_load_error(result.error)
                return
            prepared = result.data
            try:
                self.load_prepared_session(path, prepared.project.active_source, prepared.project,
                                           qimage=image)
                if self.session is not None and self.session.core_id == prepared.project.session_id:
                    self.hold_preview_source(prepared.proxy_lease)
                    prepared.proxy_lease = None
                    prepared.owns_session = False
            finally:
                prepared.close()
            return
        preserve_view = self._preserve_loading_view
        self._preserve_loading_view = False
        self._loading_placeholder = None
        self._image_loading = False
        normalized_path = os.path.normpath(path) if path else None
        image_changed = force or normalized_path != self._current_path
        if image_changed:
            for panel in self._tool_panels.values():
                panel.cleanup()
        self.assistant_panel.invalidate(clear=path != self._current_path or force)
        self.stop_preview()
        self._detail_resources.release_source()
        self._render_generation += 1
        self._comparing = False
        self._original_runner.cancel()
        self._original_error = None
        self._clear_original_display()
        if self.session and (force or self.session.image_path != path):
            self.release_preview_source()
        if not path or not os.path.isfile(path):
            self._current_path = None
            self.session = None
            self.viewer.clear()
            self._clear_original_display()
            self._last_rendered_qimage = None
            self.photo_info_panel.set_path(None)
            self.viewer.set_overlay(None)
            self.tool_sidebar.hide()
            self._update_actions_state()
            self.assistant_panel.refresh()
            return

        path = os.path.normpath(path)
        self._current_path = path
        if path != self.photo_info_panel.image_path:
            self.photo_info_panel.set_path(path)
        if force:
            self.edit_sessions.close(path)
        if self.edit_sessions.get(path) is None:
            if prepared_source is None or prepared_project is None:
                raise ValueError("加载结果缺少源会话")
            adopted = self.edit_sessions.adopt(prepared_project, path=path)
            if not adopted.success:
                self._show_core_error(adopted)
                return
        self.session = self.edit_sessions.get(path)
        self.session.refresh()
        self.session.refresh_availability()
        if self.core.source_preview(self.session.core_id).half_size:
            full = self._canvas_preview_mode == "full"
            self._original_level_requested = (full, not full, "")
            self._original_runner.submit(self.session.core_id, self.core.source_identity(self.session.core_id), full=full)
        if self.session.read_only:
            self.viewer.clear()
            self.viewer.set_overlay(None)
            self._clear_original_display()
            self._last_rendered_qimage = None
            self.tool_sidebar.hide()
            self._schedule_pipeline_render()
            self._update_actions_state()
            self.assistant_panel.refresh()
            return

        if prepared_qimage is not None:
            image = self._loading_preview if self._loading_preview is not None else prepared_qimage
            self._show_loading_image(image, QSize(*self.session.source_size), preserve_zoom=preserve_view)
            self._last_rendered_qimage = image
        self._loading_preview = None

        # 刷新或装载右侧当前激活工具
        if self.sidebar_tabs.currentIndex() == _TAB_ADJUST:
            self._activate_tool_panel(self._active_tool_id, force_reload=image_changed)
        else:
            self._schedule_pipeline_render()
        self._refresh_modified_tools()
        self._update_actions_state()
        self.assistant_panel.refresh()
        if not preserve_view:
            QTimer.singleShot(0, self, self._refit_canvas)

    def _refit_canvas(self):
        if self.viewer.is_fit_mode():
            self.viewer.zoom_fit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_sidebar_widths()
        QTimer.singleShot(0, self, self._refresh_detail)
        if self.viewer.is_fit_mode() and self.viewer._pixmap:
            QTimer.singleShot(0, self, self._refit_canvas)

    def hideEvent(self, event) -> None:
        self._invalidate_detail(release_source=True)
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._assistant_expanded:
            self._assistant_workbench.setGeometry(self.rect())
        # 分隔条有了实际尺寸之后栏宽才设得上
        self._apply_sidebar_widths()
        if self.viewer.is_fit_mode() and self.viewer._pixmap:
            self.viewer.zoom_fit()

    # ------------------------------------------------------------------
    # 工具切换与面板管理
    # ------------------------------------------------------------------
    def switch_tool(self, tool_id: str) -> None:
        """切换修图工具；右侧栏被收起或让位给照片信息时重新展开。"""
        if self._commit_busy:
            return
        if self._assistant_expanded:
            self._set_assistant_expanded(False)
        self._show_right_sidebar()
        self.sidebar_tabs.setCurrentIndex(_TAB_ADJUST)
        tool_id = resolve_tool_id(tool_id)
        available = self.core.tools.check_enabled(tool_id)
        if not available.success:
            self._toast(self._tr(available.error.message_key), 5000)
            self._sync_tool_navigation()
            return
        if self._active_tool_id == tool_id and self.tool_sidebar.isVisible():
            return
        tool_changed = self._active_tool_id != tool_id
        self._active_tool_id = tool_id
        if self._image_loading and tool_changed:
            self._loading_preview = None
        self._sync_tool_navigation()
        self._activate_tool_panel(tool_id)

    def _on_rail_entry_requested(self, entry_id: str) -> None:
        """点工具栏的入口：回到该入口下上次用过的工具，没有用过时是第一个可用的。"""
        tools = self._entry_tools.get(entry_id, ())
        if not tools:
            return
        remembered = self._entry_last_tool.get(entry_id)
        self.switch_tool(remembered if remembered in tools else tools[0])

    def _sync_tool_navigation(self) -> None:
        """把当前工具投影到工具栏的选中项和侧栏容器里的分段开关。"""
        entry = tool_entry_for(self._active_tool_id)
        if entry is None:
            self.tool_rail.set_current("")
            self.tool_sidebar.set_variants((), "")
            return
        self._entry_last_tool[entry.entry_id] = self._active_tool_id
        self.tool_rail.set_current(entry.entry_id)
        available = self._entry_tools.get(entry.entry_id, ())
        self.tool_sidebar.set_variants(
            tuple((tool_id, self._tr(key, default)) for tool_id, key, default in entry.tools
                  if tool_id in available),
            self._active_tool_id)

    def _activate_tool_panel(self, tool_id: str, force_reload: bool = False) -> None:
        # 进入工具时总是从调整开始
        self._result_view = False
        if self.session is None or self.session.read_only:
            return
        if not self._current_path:
            return
        spec = self.core.source_preview(self.session.core_id)
        self._awaiting_full_source = spec.half_size and not self.core.tools.supports_tool_proxy(tool_id)
        if self._awaiting_full_source:
            self.tool_sidebar.hide()
            self.viewer.set_overlay(None)
            self._update_actions_state()
            return
        available = self.core.tools.check_enabled(tool_id)
        if not available.success:
            self._toast(self._tr(available.error.message_key), 5000)
            self._update_actions_state()
            return

        for other_id, other in self._tool_panels.items():
            if other_id != tool_id:
                other.cleanup()
        panel = self._tool_panels.get(tool_id)
        created = panel is None
        if panel is None:
            panel = self._create_panel(tool_id)
            if panel is None:
                self._toast(self._tr("error.tool.missing"), 5000)
                return
            if panel is not None:
                self._tool_panels[tool_id] = panel
                self.tool_sidebar.register_panel(tool_id, panel)
                panel.params_changed.connect(lambda params, tid=tool_id: self._on_panel_params_changed(tid, params))
                panel.request_status.connect(lambda text, tid=tool_id: self._on_panel_status(tid, text))
                panel.preparation_changed.connect(lambda pending: self._update_actions_state())
                panel.edit_group_started.connect(lambda pnl=panel: self._on_edit_group_started(pnl))
                panel.edit_group_finished.connect(lambda pnl=panel: self._on_edit_group_finished(pnl))
                if hasattr(panel, "result_view"):
                    panel.result_view.requested.connect(
                        lambda viewing, pnl=panel: self._on_result_view_requested(pnl, viewing))
                if hasattr(panel, "sampling_requested"):
                    panel.sampling_requested.connect(lambda active, pnl=panel: self._on_sampling_requested(pnl, active))
                    panel.sampling_picked.connect(lambda x, y, pnl=panel: self._on_sampling_picked(pnl, x, y))
                if hasattr(panel, "mask_mode_changed"):
                    panel.mask_mode_changed.connect(lambda _mode, pnl=panel: self._on_mask_mode(pnl))
                    panel.mask_ui_changed.connect(lambda pnl=panel: self._save_mask_ui(pnl))
                    panel.mask_create_requested.connect(lambda kind, pnl=panel: self._on_mask_create(pnl, kind))
                    panel.mask_upgrade_requested.connect(lambda pnl=panel: self._upgrade_masks(pnl))
                    panel.mask_gesture_started.connect(lambda mask, pnl=panel: self._begin_mask_gesture(pnl, mask))
                    panel.mask_gesture_changed.connect(lambda mask, pnl=panel: self._change_mask_gesture(pnl, mask))
                    panel.mask_gesture_finished.connect(lambda mask, pnl=panel: self._finish_mask_gesture(pnl, mask))
                    panel.mask_gesture_cancelled.connect(self._cancel_mask_gesture)
                    panel.mask_tool_started.connect(lambda kind, op, pnl=panel: self._show_mask_tool(pnl, kind, op))
                    panel.mask_tool_finished.connect(self._hide_mask_tool)
                    panel.mask_generation_cancel_requested.connect(self._cancel_mask_generation)
                if hasattr(panel, "availability_changed"):
                    panel.availability_changed.connect(self._update_actions_state)

        if panel is not None:
            self.tool_sidebar.setVisible(self.right_stack.currentWidget() is self.tool_sidebar)
            self.tool_sidebar.show_panel(tool_id)
            # 换了面板：这一栏最窄能到多少随之变化
            self._apply_sidebar_widths()
            self._restoring = True
            try:
                panel.set_source_identity(self.core.source_identity(self.session.core_id))
                panel.set_source_preview(spec)
                if tool_id == "color_adjust":
                    self._configure_color_panel(panel)
                panel.set_image_path(self._current_path, force=(
                    not created and (force_reload or self._panel_sessions.get(tool_id) is not self.session)
                ))
                self._panel_sessions[tool_id] = self.session
                panel.restore_params(self._params_for_tool(tool_id))
                if tool_id == "beauty":
                    self._configure_beauty_panel(panel)
                if hasattr(panel, "restore_mask_ui"):
                    panel.restore_mask_ui(self._mask_ui_states.get(self._mask_state_key(), (False, None, False)))
            finally:
                self._restoring = False
            if hasattr(panel, "result_view"):
                panel.result_view.set_viewing(False)

            self._schedule_pipeline_render()
            self._bind_overlay(panel)
            self._update_actions_state()

    def _bind_overlay(self, panel) -> None:
        """画布工具在调整时把框或轮廓挂到画布上；看结果、不在“调整”页或没有覆盖层的工具则取下。"""
        overlay = getattr(panel, "overlay", None)
        if self.sidebar_tabs.currentIndex() != _TAB_ADJUST or self._result_view or self._comparing:
            overlay = None
        self.viewer.set_overlay(overlay)

    def _mask_panel(self):
        panel = self._tool_panels.get("color_adjust")
        if (panel is not None and self.session is not None and self._active_tool_id == "color_adjust"
                and self.sidebar_tabs.currentIndex() == _TAB_ADJUST and panel.mask_mode
                and not self.session.read_only and self._panel_sessions.get("color_adjust") is self.session):
            return panel
        return None

    def _mask_node_id(self):
        if self.session is None:
            return "color"
        node = self.session.stage_node("color_adjust")
        return node.node_id if node is not None else "color"

    def _mask_state_key(self):
        return self._current_path, self._mask_node_id()

    def _save_mask_ui(self, panel):
        if self.session is not None and self._panel_sessions.get("color_adjust") is self.session:
            self._mask_ui_states[self._mask_state_key()] = panel.mask_ui_state()

    def _on_mask_mode(self, panel):
        if self._restoring or self._panel_sessions.get("color_adjust") is not self.session:
            return
        self._save_mask_ui(panel)
        self.stop_preview()
        self._bind_overlay(panel)
        self._schedule_pipeline_render()

    def _on_mask_create(self, panel, kind):
        if panel is not self._mask_panel():
            return
        if panel.parameters.get("process_version") != 4:
            self._upgrade_masks(panel)
            return
        requested_kind = kind if isinstance(kind, str) else kind["kind"]
        if requested_kind in ("subject", "background", "skin", "eyes", "lips"):
            self._generate_mask_component(panel, kind)
            return
        panel.begin_mask_creation(kind)

    def _generate_mask_component(self, panel, request):
        """在后台识别一块区域；识别期间面板里显示进度与取消，照片仍可查看。"""
        from ..application.mask_generation import MaskGenerationTask
        from .adapters.studio_adapter import to_workflow
        from .services.background_operation import start_background
        if self._commit_busy or self._image_loading or self._comparing or self.session is None:
            return
        spec = panel.mask_creation_spec(request)
        if spec is None:
            return
        panel.end_edit_group()
        panel.mask_overlay.cancel_interaction()
        self._cancel_mask_gesture(resume=False)
        self._cancel_mask_generation()
        try:
            node_id = self._mask_node_id()
            workflow = to_workflow(self.session.detached(), preview_target=PreviewTarget.after_node(node_id))
            task = MaskGenerationTask.create(self.core, self.session.core_id, node_id,
                panel.mask_reference_size, spec["kind"], spec["name"], spec["operation"], panel.mask_model_token(spec["kind"]),
                workflow=workflow, expected_version=self.session.state_version)
        except CoreOperationError as error:
            if error.error.code != ErrorCode.CANCELLED:
                self._toast(self._tr(error.error.message_key))
            return
        except (ValueError, OSError) as error:
            self._toast(self._tr(str(error)))
            return
        self._mask_generation = task
        panel.set_mask_busy(("background" if spec["fill"] else spec["kind"],
                             spec["parent"]["id"] if spec["parent"] is not None else None))
        start_background(task, lambda value, error: self._finish_mask_generation(panel, task, spec, value, error),
                         cancellation=task.cancellation)

    def _finish_mask_generation(self, panel, task, spec, value, error):
        if self._mask_generation is not task:
            return
        self._mask_generation = None
        if self._tool_panels.get("color_adjust") is panel:
            panel.set_mask_busy(None)
        if task.cancellation.is_cancelled() or panel is not self._mask_panel():
            return
        if error is not None:
            if not isinstance(error, CoreOperationError):
                logger.error("蒙版区域识别失败", exc_info=(type(error), error, error.__traceback__))
            elif error.error.code == ErrorCode.CANCELLED:
                self._toast(self._tr("mask.generation_stale"))
                return
            self._toast(self._tr(error.error.message_key if isinstance(error, CoreOperationError) else "mask.generation_failed"))
            return
        if not value.success:
            # 不是用户取消的中止：识别期间照片或编辑有变化，旧结果不再写入
            self._toast(self._tr("mask.generation_stale" if value.error.code == ErrorCode.CANCELLED else value.error.message_key))
            return
        if not task.is_current():
            self._toast(self._tr("mask.generation_stale"))
            return
        panel.apply_generated_component(value.data, spec, task)
        self._save_mask_ui(panel)

    def _cancel_mask_generation(self):
        task, self._mask_generation = self._mask_generation, None
        if task is None:
            return
        task.cancellation.cancel()
        panel = self._tool_panels.get("color_adjust")
        if panel is not None:
            panel.set_mask_busy(None)

    def _show_mask_tool(self, panel, kind, operation):
        """画布下沿的蒙版工具条：画笔时调大小、硬度与不透明度，渐变时给出提示。"""
        if self._mask_tool_bar is None:
            from .widgets.bottom_dock import BottomDock
            from .widgets.mask_tool_bar import MaskToolBar
            self._mask_tool_bar = MaskToolBar(self._tr, self.scroll_area)
            self._mask_tool_bar.hide()
            self._mask_tool_bar.settings_edited.connect(self._on_mask_brush_edited)
            self._mask_tool_bar.finish_requested.connect(self._finish_mask_tool)
            BottomDock(self._mask_tool_bar, 2 * CANVAS_TOOLBAR_MARGIN + CANVAS_TOOLBAR_HEIGHT).attach(self.scroll_area)
        self._mask_tool_bar.show_tool(kind, operation, panel.brush_settings())

    def _hide_mask_tool(self):
        if self._mask_tool_bar is not None:
            self._mask_tool_bar.hide()

    def _on_mask_brush_edited(self, settings):
        panel = self._tool_panels.get("color_adjust")
        if panel is not None:
            panel.set_brush_settings(settings)

    def _finish_mask_tool(self):
        panel = self._tool_panels.get("color_adjust")
        if panel is not None:
            panel.mask_overlay.cancel_interaction()
        self._hide_mask_tool()

    def _mask_token(self):
        gesture = self._mask_gesture
        return (self.session.core_id, self.session.state_version, self.core.tools.generation,
                self.core.source_identity(self.session.core_id), self._preview_target,
                gesture.identity if gesture is not None else None, gesture.generation if gesture is not None else 0)

    def _begin_mask_gesture(self, panel, mask):
        if panel is not self._mask_panel() or self._commit_busy or self._image_loading or self._comparing:
            panel.mask_overlay.cancel_interaction()
            return
        panel.end_edit_group()
        self._invalidate_detail()
        self._mask_gesture = MaskGesture.begin(self.session.core_id, self.session.state_version,
            self.core.tools.generation, self.core.source_identity(self.session.core_id), panel.parameters)
        try:
            self._mask_candidate = self._mask_gesture.update(mask)
        except ValueError as error:
            self._toast(self._tr(str(error)))
            self._cancel_mask_gesture()
            return
        panel.set_mask_preview(self._mask_candidate, self._mask_token())

    def _change_mask_gesture(self, panel, mask):
        if self._mask_gesture is None or panel is not self._mask_panel():
            return
        self._invalidate_detail()
        try:
            self._mask_candidate = self._mask_gesture.update(mask)
        except ValueError:
            return
        panel.set_mask_preview(self._mask_candidate, self._mask_token())
        if not self._mask_timer.isActive():
            self._mask_timer.start(100)

    def _flush_mask_preview(self):
        if self._mask_gesture is None or self._mask_candidate is None:
            return
        if self._preview_queue.active is not None:
            self._mask_timer.start(16)
            return
        self._schedule_pipeline_render()

    def _finish_mask_gesture(self, panel, mask):
        gesture = self._mask_gesture
        if gesture is None or panel is not self._mask_panel():
            self._cancel_mask_gesture()
            return
        valid = gesture.matches(self.session.core_id, self.session.state_version, self.core.tools.generation,
                                self.core.source_identity(self.session.core_id)) and self.core.image_sources.matches_file(gesture.source_identity)
        try:
            params = gesture.update(mask) if valid else None
        except ValueError:
            params = None
        self._mask_gesture, self._mask_candidate = None, None
        self._mask_timer.stop()
        self._preview_queue.cancel()
        if params is not None:
            self._mask_commit_stamp = gesture.state_version, gesture.tool_generation, gesture.source_identity
            try:
                if not panel.apply_mask_candidate(params, mask["id"], component=panel.mask_overlay.selected_component):
                    self._schedule_pipeline_render()
            finally:
                self._mask_commit_stamp = None
            self._save_mask_ui(panel)
        else:
            panel.set_mask_preview(None)
            self._schedule_pipeline_render()

    def _cancel_mask_gesture(self, *, resume=True):
        panel = self._tool_panels.get("color_adjust")
        active = self._mask_gesture is not None
        self._mask_gesture, self._mask_candidate = None, None
        self._mask_timer.stop()
        if panel is not None:
            # 只停下拖到一半的这一下：参数一变就会到这里，连续涂抹不因此结束
            panel.mask_overlay.cancel_drag()
            panel.set_mask_preview(None)
            self._save_mask_ui(panel)
        if active:
            self._mask_needs_base = True
            self._preview_queue.cancel()
            if resume and self.session is not None and not self._image_loading:
                QTimer.singleShot(0, self, self._schedule_pipeline_render)

    def _upgrade_masks(self, panel):
        from .widgets.message_dialog import show_confirm
        from .services.background_operation import run_background
        if panel is not self._mask_panel() or self._commit_busy or self._image_loading:
            return
        version = panel.parameters.get("process_version", 2)
        message = self._tr("mask.upgrade_confirm_components" if version == 3 else "mask.upgrade_confirm_v1" if version == 1 else "mask.upgrade_confirm")
        if not show_confirm(self, self._tr("mask.upgrade"), message, default_yes=False):
            return
        session_id, state_version, node_id = self.session.core_id, self.session.state_version, self._mask_node_id()
        reference_size = panel.mask_reference_size
        self.stop_preview()
        result = run_background(self, lambda cancellation: upgrade_color_masks(
            self.core, session_id, node_id, reference_size, expected_version=state_version, cancellation=cancellation),
            label=self._tr("mask.upgrade"))
        if not result.success:
            self._show_core_error(result)
            self._schedule_pipeline_render()
            return
        if self.session is not None and self.session.core_id == session_id:
            self.session.refresh()
            self._sync_panels_from_session()
            self._persist_draft()
            self._schedule_pipeline_render()

    def _update_mask_context(self, request=None, frame=None):
        panel = self._mask_panel()
        if panel is None:
            return
        ready = (request is not None and frame is not None and not self._comparing
                 and request.context.preview_target == self._preview_target and self._accept_preview(request))
        reference = (frame.full_size or frame.reference_size) if ready else panel.mask_reference_size
        engine_params = None
        message = "mask.preparing"
        upgrade = panel.parameters.get("process_version") != 4
        if ready:
            node = next((node for node in request.session.workflow.nodes if node.node_id == self._preview_target.node_id), None)
            if node is not None:
                engine_params = node.params["settings"] if node.node_type == "tool" else node.params
            if upgrade:
                message = "mask.legacy_components" if panel.parameters.get("process_version") == 3 else "mask.legacy"
            else:
                complete = self.session.render_snapshot().workflow
                visible = {node.node_id for node in request.session.workflow.nodes}
                hidden = [node for node in complete.nodes if node.node_id not in visible]
                crop = any(node.node_type == "crop" or node.params.get("tool_id") == "crop" for node in hidden)
                message = ("mask.stage_crop" if crop else "mask.stage_hidden") if hidden else ""
        panel.set_mask_context(self._mask_token(), reference, ready=ready, message=message, upgrade=upgrade,
                               engine_params=engine_params)
        if ready and self._pending_stage_view:
            state = self._stage_views.get((self.session.core_id, self._preview_target))
            self.viewer.restore_view_state(state)
            self._pending_stage_view = False

    def _canvas_panel(self):
        """当前可以切换“调整 / 结果”的画布工具面板；不是这类工具或现在不能编辑时为 None。"""
        panel = self._tool_panels.get(self._active_tool_id)
        if (panel is None or not hasattr(panel, "result_view") or self.session is None or self.session.read_only
                or self._commit_busy or self._image_loading or self.sidebar_tabs.currentIndex() != _TAB_ADJUST
                or panel.image_path != self._current_path
                or self._panel_sessions.get(self._active_tool_id) is not self.session):
            return None
        return panel

    def _on_result_view_requested(self, panel, viewing: bool) -> None:
        if panel is self._canvas_panel():
            self._set_result_view(viewing)

    def _set_result_view(self, viewing: bool) -> None:
        """在“调整”与“结果”之间切换：结果是全部各步之后的画面，框与轮廓收起。

        结果还在渲染时回到调整（点“取消”或按 Esc）就停下那次渲染。
        """
        panel = self._tool_panels.get(self._active_tool_id)
        if not viewing and panel.result_view.busy:
            self.stop_preview()
        self._result_view = viewing
        panel.result_view.set_viewing(viewing)
        self._bind_overlay(panel)
        self._schedule_pipeline_render()

    def _canvas_tool_key(self, key) -> bool:
        """回车在调整与结果之间切换；Esc 从结果回到调整；其余交给画布上的框或轮廓。"""
        masks = self._mask_panel()
        if (masks is not None and not self._comparing and QApplication.activePopupWidget() is None
                and not isinstance(QApplication.focusWidget(), (QLineEdit, QAbstractSpinBox))):
            if masks.handle_mask_key(key, canvas_focus=self.viewer.hasFocus()):
                return True
        panel = self._canvas_panel()
        # 正在键入数值时，这些键归输入框
        if panel is None or isinstance(QApplication.focusWidget(), (QLineEdit, QAbstractSpinBox)):
            return False
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return panel.result_view.toggle()
        if self._result_view:
            if key == Qt.Key.Key_Escape:
                self._set_result_view(False)
                return True
            return False
        return panel.overlay.handle_key_press(key)

    def _create_panel(self, tool_id: str) -> Optional[BaseToolPanel]:
        panel = self._build_panel(tool_id)
        if panel:
            panel.model_manager_requested.connect(self.model_management_requested)
        if panel and getattr(panel, "MANUAL_PREVIEW", False):
            panel.preview_requested.connect(self._schedule_pipeline_render)
            panel.cancel_requested.connect(lambda pnl=panel: self._cancel_manual_preview(pnl))
        return panel

    def _configure_color_panel(self, panel):
        from ..core.contracts.tools import ToolReference
        node = self.session.stage_node("color_adjust")
        reference = ToolReference.from_dict(node.params) if node is not None and node.node_type == "tool" else None
        panel.set_tool_reference(reference)
        panel.set_reset_identity(self.session.baseline_color_params)

    def _configure_beauty_panel(self, panel):
        from ..core.contracts.tools import ToolReference
        node = self.session.stage_node("beauty")
        reference = ToolReference.from_dict(node.params) if node is not None and node.node_type == "tool" else None
        panel.set_tool_reference(reference)
        panel.set_beauty_reference(reference)
        panel.beauty_analysis_provider = lambda settings, preset: self._beauty_analysis_job(settings, preset)

    def _beauty_analysis_job(self, settings, preset):
        from .adapters.studio_adapter import to_workflow
        from ..application.beauty_analysis import BeautyAnalysisTask
        mapper = self.session.detached()
        mapper.beauty_params = {**settings, "skin_smooth": max(1, settings["skin_smooth"])}
        workflow = to_workflow(mapper)
        return BeautyAnalysisTask(self.core, self.session.core_id, workflow, self.session.state_version,
                                  self.core.source_identity(self.session.core_id), self.core.tools.generation, preset)

    def _build_panel(self, tool_id: str) -> Optional[BaseToolPanel]:
        panel = create_tool_panel(
            tool_id,
            self._current_path or "",
            parent=self.tool_sidebar,
            edit_state_store=None,
            services={**self._panel_services, "external_preview": True,
                      "source_identity": self.core.source_identity(self.session.core_id),
                      "source_preview": self.core.source_preview(self.session.core_id)},
        )
        if tool_id == "denoise" and panel:
            self._add_enhancement_upgrade(panel, tool_id)
        if tool_id == "grid_crop" and panel:
            panel.export_snapshot_provider = self._grid_export_snapshot
        return panel

    def _add_enhancement_upgrade(self, panel, tool_id):
        button = AppleButton(self._tr("enhance.upgrade"), panel)
        panel.layout().insertWidget(0, button)
        panel.enhancement_upgrade_button = button
        button.clicked.connect(lambda: self._upgrade_enhancement(tool_id))

    def _enhancement_upgrade_nodes(self):
        if self.session is None:
            return ()
        result = self.core.get_session(self.session.core_id)
        if not result.success:
            return ()
        return tuple(node for node in result.data.draft_workflow.nodes
                     if node.node_type == "tool" and node.params.get("tool_id") == "denoise"
                     and node.params.get("params_version") == 2)

    def _choose_enhancement_upgrade(self):
        from .base_window import BaseDialog
        from .widgets.apple_menu import AppleDropDown
        nodes = self._enhancement_upgrade_nodes()
        if not nodes:
            return
        if len(nodes) == 1:
            node = nodes[0]
        else:
            dialog = BaseDialog(title_key="enhance.upgrade", min_width=360, min_height=160,
                                default_width=440, default_height=180, parent=self)
            layout = QVBoxLayout(dialog)
            choices = AppleDropDown(dialog)
            for index, item in enumerate(nodes, 1):
                tool = item.params.get("tool_id") or item.params.get("tool")
                choices.addItem(self._tr("enhance.upgrade_step", index=index,
                                        name=self._tr(PANEL_TYPES.title_key(tool) or tool)), item.node_id)
            layout.addWidget(choices)
            confirm = AppleButton(self._tr("enhance.upgrade"), dialog)
            confirm.clicked.connect(dialog.accept)
            layout.addWidget(confirm)
            if not dialog.exec():
                dialog.deleteLater()
                return
            node = next(item for item in nodes if item.node_id == choices.currentData())
            dialog.deleteLater()
        self._upgrade_enhancement(node.params.get("tool_id") or node.params.get("tool"), node.node_id)

    def _upgrade_enhancement(self, tool_id, node_id=None):
        from .widgets.message_dialog import show_confirm
        from .services.background_operation import run_background
        from .adapters.studio_adapter import mapped_stage_node
        if self.session is None or self._commit_busy:
            return
        node = next((item for item in self._enhancement_upgrade_nodes() if item.node_id == node_id), None) if node_id else mapped_stage_node(self.session, tool_id)
        if node is None:
            return
        if not show_confirm(self, self._tr("enhance.upgrade"), self._tr("enhance.upgrade_confirm"), default_yes=False):
            return
        self.stop_preview()
        session_id, version = self.session.core_id, self.session.state_version
        result = run_background(self, lambda cancellation: self.core.upgrade_enhancement_effect(
            session_id, node.node_id, expected_version=version, cancellation=cancellation))
        if not result.success:
            self._show_core_error(result)
            return
        self.session.refresh()
        panel = self._tool_panels.pop(tool_id, None)
        if panel is not None:
            panel.cleanup()
            self.tool_sidebar.remove_panel(tool_id)
            panel.deleteLater()
        self._active_tool_id = "ai_enhance"
        self._sync_panels_from_session()
        self._activate_tool_panel("ai_enhance")
        self._update_actions_state()

    # ------------------------------------------------------------------
    # 互通参数同步与流水线调度
    # ------------------------------------------------------------------
    def _on_edit_group_started(self, panel):
        if self.session is None or self._panel_sessions.get(panel.TOOL_ID) is not self.session:
            return
        self._color_preview_gesture = None
        shown = self._displayed_request
        if (self._canvas_preview_mode != "full" and panel.TOOL_ID == "color_adjust"
                and shown is not None and shown is self._preview_queue.latest
                and shown.temporary_workflow is None and self._accept_preview(shown)
                and not shown.context.source_preview.half_size
                and self.core.tools.uses_prepared_inputs(shown.session.workflow)
                and self.core.tools.supports_proxy(shown.session.workflow, canonical_inputs=True)):
            self._color_preview_gesture = (panel.edit_group, shown)
        self._detail_timer.stop()
        if (self._detail_request is not None and self._detail_resources.detail is None
                and not self._detail_error_key):
            self._detail_runner.cancel()
            self._detail_request = None
            self._detail_attempted = None
        self._set_detail_state("")

    def _on_edit_group_finished(self, panel):
        if self.session is not None and self._panel_sessions.get(panel.TOOL_ID) is self.session:
            self.core.end_edit_group(self.session.core_id)
            if self._color_preview_gesture is not None:
                self._color_preview_gesture = None
                self._preview_queue.cancel()
                self._schedule_pipeline_render()
            self._refresh_detail()

    def _on_panel_params_changed(self, tool_id: str, params: Dict[str, Any]) -> None:
        if not self.session or self.session.read_only or self._restoring or self._commit_busy:
            return
        panel = self._tool_panels.get(tool_id)
        if panel and panel.image_path != self._current_path:
            return
        self._cancel_mask_gesture(resume=False)
        if tool_id == "grid_crop":
            self._grid_settings[self._current_path] = copy.deepcopy(params)
            return

        input_started = monotonic()
        self._invalidate_detail()
        self.session.set_stage(tool_id, params)

        edit_group = panel.edit_group if panel is not None else None
        persisted = self._persist_draft(edit_group=edit_group, commit_stamp=panel.parameter_commit_stamp if panel is not None else None)
        if not persisted.success:
            return
        self._refresh_modified_tools()
        if not getattr(panel, "MANUAL_PREVIEW", False):
            self._schedule_pipeline_render(input_started=input_started, edit_group=edit_group,
                                           synchronized_state=persisted.data)
        else:
            self._update_actions_state()

    def _on_panel_status(self, tool_id: str, text: str) -> None:
        panel = self._tool_panels.get(tool_id)
        if (tool_id == self._active_tool_id and panel is not None
                and panel.image_path == self._current_path):
            self._toast(text, 5000)

    @property
    def active_tool_id(self):
        return self._active_tool_id

    def resource_manager_context(self):
        from ..application.tool_resource_service import ResourceContext
        from ..core.contracts.tool_resources import ToolParameterRequest
        if self.session is None:
            return (), None
        current = self.core.get_session(self.session.core_id)
        if not current.success:
            return None, None
        workflow = current.data.draft_workflow
        requests = ()
        settings_identity = ""
        if not self.session.read_only:
            entry = next((item for item in self.core.tools.resource_status()
                          if item.tool_id == self._active_tool_id), None)
            needs_resources = entry is not None and any(feature.models for feature in entry.resource_features)
            if needs_resources:
                panel = self._tool_panels.get(self._active_tool_id)
                if (panel is not None and panel.image_path == self._current_path
                        and self._panel_sessions.get(self._active_tool_id) is self.session):
                    settings = panel.parameters
                    version = panel.PARAMETER_CONTRACT.version
                    try:
                        settings_identity = json.dumps((version, settings), ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":"), allow_nan=False)
                    except (TypeError, ValueError):
                        return None, ResourceContext(workflow)
                    requests = (ToolParameterRequest(self._active_tool_id, settings, version),)
        identity = (self.session.core_id, current.data.state_version, self.core.tools.generation,
                    workflow.workflow_hash(), self._active_tool_id, settings_identity)
        return identity, ResourceContext(workflow, requests)

    def _refresh_tool_navigation(self):
        enabled = {entry["tool_id"] for entry in self.core.list_tools() if entry["enabled"]}
        self._entry_tools = {entry.entry_id: tuple(tool_id for tool_id in entry.tool_ids if tool_id in enabled)
                             for entry in TOOL_ENTRIES}
        self.tool_rail.set_available(entry_id for entry_id, tools in self._entry_tools.items() if tools)
        if self._active_tool_id not in enabled:
            self._active_tool_id = "color_adjust"
        self._sync_tool_navigation()

    def _on_model_availability_changed(self):
        generation = self.core.tools.generation
        if generation == self._model_generation:
            return
        self._model_generation = generation
        if self._beauty_readiness is not None:
            self._beauty_readiness[0].refresh()
        if self.session is None or self._commit_busy:
            return
        self.stop_preview()
        session_id, state_version = self.session.core_id, self.session.state_version
        runner = self._panel_services["preview_runner"]
        if self._model_check_job is not None:
            runner.cancel(self._model_check_job)
        def checked(result):
            if (self.session is None or self.session.core_id != session_id
                    or self.session.state_version != state_version):
                return
            self._model_check_job = None
            if result.error is not None:
                self._toast(self._tr("enhance.failed"), 5000)
                return
            inspected, checked_generation = result.value
            if not inspected.success or checked_generation != self.core.tools.generation:
                return
            self._model_generation = checked_generation
            self.session.refresh_availability()
            self._sync_panels_from_session()
            self._update_actions_state()
            # 开始核对时停掉了在途的渲染：按现在的工具与模型补一次
            self._schedule_pipeline_render()
        def inspect_models(image, cancelled):
            inspected = self.core.project_capabilities(session_id, verify_resources=False, is_cancelled=cancelled)
            return inspected, self.core.tools.generation
        self._model_check_job = runner.submit_work(generation, None,
            inspect_models, checked, cancellable=True)

    def refresh_memory_budget(self):
        """内存上限调整之后：请各工具面板按新的额度重新投影。不改参数，不产生编辑记录，也不重启预览。"""
        for panel in self._tool_panels.values():
            panel.on_memory_budget_changed()

    def refresh_tool_availability(self):
        if self._output_context is not None:
            self.cancel_output()
        self.stop_preview()
        self.assistant_panel.invalidate()
        self._refresh_tool_navigation()
        for tool_id, panel in self._tool_panels.items():
            if not self.core.tools.check_enabled(tool_id).success:
                panel.cleanup()
        if self.session is not None:
            self.session.refresh_availability()
            self._sync_panels_from_session()
            if not self.session.read_only and self.core.tools.check_enabled(self._active_tool_id).success:
                self._activate_tool_panel(self._active_tool_id)
            else:
                self.viewer.set_overlay(None)
                self.tool_sidebar.hide()
                self._schedule_pipeline_render()
        self._update_actions_state()

    def _parameters_ready(self):
        return not any(panel.parameters_pending and panel.image_path == self._current_path
                       and self._panel_sessions.get(tool_id) is self.session
                       for tool_id, panel in self._tool_panels.items())

    def _on_presentation_changed(self, sampling):
        if sampling is None or sampling is not self._sampling_resources.sampling:
            self._end_sampling()

    def _end_sampling(self):
        was_sampling = self._sampling_epoch is not None
        self._sampling_epoch = None
        self.viewer.set_sampling(None)
        handle, self._sampling_resources.sampling = self._sampling_resources.sampling, None
        if handle is not None:
            handle.close()
        if (self._sampling_resources.delivery is not None
                and self._sampling_resources.delivery.request.sampling_request is not None):
            self._sampling_resources.delivery.close()
        latest = self._preview_queue.latest
        if latest is not None and latest.sampling_request is not None:
            self._preview_queue.cancel()
        for panel in self._tool_panels.values():
            if hasattr(panel, "set_sampling_state"):
                panel.set_sampling_state("closed")
        if was_sampling:
            self.preview_finished.emit()

    def _on_sampling_requested(self, panel, active):
        if not active:
            was_sampling = self._sampling_epoch is not None
            self._end_sampling()
            if was_sampling:
                self._schedule_pipeline_render()
            return
        if (self.session is None or self.session.read_only or self._image_loading or self._commit_busy
                or self._comparing or not self._parameters_ready()
                or panel is not self._tool_panels.get(self._active_tool_id)):
            panel.set_sampling_state("closed")
            return
        self.stop_preview()
        synced = self.session.sync(self._active_tool_id)
        if not synced.success:
            self._show_core_error(synced)
            return
        snapshot = self.session.render_snapshot(self._active_tool_id)
        self._sampling_counter += 1
        sampling_request = self.session.mixer_sampling_request(snapshot, self._sampling_counter)
        available = self.core.sampling_capability(snapshot.workflow, sampling_request)
        if not available.success:
            panel.set_sampling_state("closed")
            self._toast(self._tr("color_adjust.mixer.unavailable"), 5000)
            return
        self._sampling_epoch = sampling_request.epoch
        panel.set_sampling_state("preparing")
        self._schedule_pipeline_render(sampling_request=sampling_request,
            synchronized_state=(self.session.core_id, self.session.state_version, self.core.tools.generation))

    def _on_sampling_picked(self, panel, x, y):
        handle = self._sampling_resources.sampling
        if handle is None or self.session is None or panel is not self._tool_panels.get(self._active_tool_id):
            return
        current = (handle.request.epoch == self._sampling_epoch
                   and handle.state_version == self.session.state_version
                   and handle.source_identity == self.core.source_identity(self.session.core_id)
                   and handle.source_preview == self.core.source_preview(self.session.core_id)
                   and self.core.tools.is_current(handle.tool_generation))
        source_current = self.core.image_sources.matches_file(handle.source_identity)
        if current and source_current:
            channel = handle.channel_at(x, y)
            if channel is not None:
                panel.select_sampling_channel(channel)
        self._end_sampling()
        if not source_current:
            self._toast(self._tr("error.source.changed"), 5000)
        elif current:
            self._schedule_pipeline_render()

    def _schedule_pipeline_render(self, *, input_started=None, edit_group=None,
                                  synchronized_state: tuple[str, int, int] | None = None,
                                  sampling_request=None) -> None:
        if self._image_loading or self.session is None:
            return
        target = PreviewTarget.after_node(self._mask_node_id()) if self._mask_panel() is not None else PreviewTarget()
        session_changed = self._preview_target_session != self.session.core_id
        if session_changed or target != self._preview_target:
            if not session_changed and self._preview_target_session is not None:
                self._stage_views[self._preview_target_session, self._preview_target] = self.viewer.view_state()
            self._cancel_mask_gesture(resume=False)
            self._preview_queue.cancel()
            self._preview_target = target
            self._pending_stage_view = not session_changed or target.mode == "after_node"
            self._update_mask_context()
        self._preview_target_session = self.session.core_id
        self._invalidate_detail()
        if sampling_request is None:
            self._end_sampling()
        prepare_started = monotonic() if input_started is None else input_started
        self.preview_started.emit()
        beauty_panel = self._tool_panels.get("beauty")
        if beauty_panel is not None and hasattr(beauty_panel, "set_beauty_status"):
            beauty_panel.set_beauty_status("processing")
        if self._image_loading or self.session is None:
            return
        if (not self.session.read_only and self._mask_gesture is None and synchronized_state !=
                (self.session.core_id, self.session.state_version, self.core.tools.generation)):
            stamp = self._mask_commit_stamp
            synced = self.session.sync(self._active_tool_id, edit_group=edit_group,
                expected_version=stamp[0] if stamp is not None else None,
                expected_tools=stamp[1] if stamp is not None else None,
                expected_source=stamp[2] if stamp is not None else None)
            if not synced.success:
                self.session.refresh()
                self._sync_panels_from_session()
                self._show_core_error(synced)
                self._update_actions_state()
                if self.session.read_only:
                    self.load_image_path(self._current_path)
                return
        self._update_actions_state()
        editing_tool = (self._active_tool_id if self.sidebar_tabs.currentIndex() == _TAB_ADJUST
                        and self.core.tools.check_enabled(self._active_tool_id).success else None)
        # 画布工具调整时，渲染到它之前为止；看结果时渲染全部各步
        snapshot = self.session.render_snapshot(None if self._result_view else editing_tool,
            preview_target=target, color_candidate=self._mask_candidate)
        context = StudioPreviewContext(snapshot.core_id, self.core.source_identity(snapshot.core_id),
                                       self.core.tools.generation, editing_tool,
                                       self.core.source_preview(snapshot.core_id),
                                       sampling_epoch=sampling_request.epoch if sampling_request is not None else None,
                                       preview_target=target,
                                       gesture_id=self._mask_gesture.identity if self._mask_gesture is not None else None,
                                       geometry_generation=self._mask_gesture.generation if self._mask_gesture is not None else 0,
                                       preview_mode=self._canvas_preview_mode)
        temporary_workflow = None
        if self._color_preview_gesture is not None and sampling_request is None and self._mask_gesture is None:
            interaction, baseline = self._color_preview_gesture
            panel = self._tool_panels.get("color_adjust")
            if (editing_tool == "color_adjust" and panel is not None and panel.edit_group == interaction
                    and context == baseline.context and not snapshot.read_only):
                temporary_workflow = baseline.session.workflow
                context = replace(context, interaction_id=interaction)
        if self._preview_queue.latest is not None and context != self._preview_queue.latest.context:
            dropped = self._preview_queue.pending
            self._preview_queue.cancel()
            if dropped is not None:
                self._report_manual_preview(dropped, "stopped")
        self._render_generation += 1
        request = StudioPreviewRequest(self._render_generation, context, snapshot,
            self.preview_key(self._current_path, snapshot.workflow, preview_target=target), self.session.source_size,
            sampling_request=sampling_request,
            temporary_workflow=temporary_workflow,
            keep_full_display=self._canvas_preview_mode == "full" or (
                not context.source_preview.half_size and self._view_needs_detail()
                and temporary_workflow is None
                and (snapshot.read_only or not self.core.tools.supports_proxy(snapshot.workflow, canonical_inputs=True))))
        request = self._preview_queue.submit(request)
        self._canvas_failed_generation = None
        self._sync_canvas_display()
        live = {item.generation for item in (self._preview_queue.active, self._preview_queue.pending)
                if item is not None}
        self._manual_preview_tickets = {generation: ticket for generation, ticket
                                        in self._manual_preview_tickets.items() if generation in live}
        # 手动预览的工具还没进入本次编辑时，这次渲染里没有它，不向面板报告
        manual_panel = self._manual_preview_panel(editing_tool)
        if manual_panel is not None and editing_tool in self.modified_tools():
            self._manual_preview_tickets[request.generation] = manual_panel.preview_ticket
            manual_panel.set_preview_state("processing", manual_panel.preview_ticket)
        # 画布工具看结果时，结果出来之前底部是“正在生成结果”
        canvas = self._canvas_panel()
        if canvas is not None:
            canvas.result_view.set_busy(self._result_view)
        logger.debug("OPS 预览入队 request=%s session=%s tool=%s state=%s workflow=%s nodes=%s ui_prepare_ms=%.1f",
            request.generation, context.session_id, editing_tool, snapshot.state_version,
            snapshot.workflow.workflow_hash()[:12], [node.node_id for node in snapshot.workflow.nodes],
            (monotonic() - prepare_started) * 1000)
        self._arm_preview_render()

    def _manual_preview_panel(self, tool_id):
        panel = self._tool_panels.get(tool_id)
        return panel if panel is not None and getattr(panel, "MANUAL_PREVIEW", False) else None

    def _report_manual_preview(self, request, state) -> None:
        """把一次请求的结局告诉手动预览的面板。同一工具还有任务在跑或在排队时不报告，等最后一个。"""
        if request.generation not in self._manual_preview_tickets:
            return
        ticket = self._manual_preview_tickets.pop(request.generation)
        tool_id = request.context.editing_tool
        panel = self._manual_preview_panel(tool_id)
        if panel is None or any(
                other is not None and other is not request and other.context.editing_tool == tool_id
                for other in (self._preview_queue.active, self._preview_queue.pending)):
            return
        panel.set_preview_state(state, ticket)

    def _cancel_manual_preview(self, panel) -> None:
        """面板请求停止预览。还有任务在跑时等它退出后再报告；没有任务时立即确认。"""
        self.stop_preview()
        if not any(request is not None and request.context.editing_tool == panel.TOOL_ID
                   for request in (self._preview_queue.active, self._preview_queue.pending)):
            panel.set_preview_state("stopped", panel.preview_ticket)

    def _arm_preview_render(self):
        if (self._preview_queue.pending is not None and self._preview_queue.active is None
                and not self._render_timer.isActive()):
            self._render_timer.start(0)

    def _do_pipeline_render(self) -> None:
        if self.session is None or self._image_loading:
            return
        request = self._preview_queue.take()
        if request is None:
            return
        delivery = SamplingDelivery(request) if request.sampling_request is not None or request.keep_full_display else None
        self._sampling_resources.delivery = delivery
        worker = StudioPreviewWorker(request, self._render_service, delivery)
        if delivery is None:
            worker.signals.finished.connect(self._on_pipeline_render_finished)
        else:
            worker.signals.sampled_finished.connect(self._on_sampled_render_finished)
        self._thread_pool.start(worker)

    def _accept_preview(self, request):
        return (self.session is not None and not self._image_loading
                and self.session.core_id == request.session.core_id
                and self._preview_queue.accepts(request)
                and request.context.preview_target == self._preview_target
                and (request.context.gesture_id is None or self._mask_gesture is not None
                     and request.context.gesture_id == self._mask_gesture.identity)
                and self.core.source_identity(request.session.core_id) == request.context.source_identity
                and self.core.source_preview(request.session.core_id) == request.context.source_preview
                and self.core.tools.is_current(request.context.tool_generation)
                and (request.sampling_request is None or (
                    request is self._preview_queue.latest
                    and request.context.sampling_epoch == self._sampling_epoch
                    and self.session.state_version == request.session.state_version
                    and not self._comparing)))

    @Slot(object)
    def _on_sampled_render_finished(self, request):
        delivery, self._sampling_resources.delivery = self._sampling_resources.delivery, None
        packet = delivery.take() if delivery is not None else None
        sampling = None
        try:
            if (packet is not None and packet.error is None and packet.image is not None
                    and packet.frame is not None and self._accept_preview(request)):
                sampling = packet.result.take_sampling() if packet.result is not None else None
                if request.sampling_request is not None and (sampling is None or sampling.size != packet.frame.size
                        or sampling.request != request.sampling_request
                        or sampling.source_identity != request.context.source_identity
                        or sampling.source_preview != request.context.source_preview
                        or sampling.state_version != request.session.state_version
                        or sampling.tool_generation != request.context.tool_generation
                        or sampling.workflow_hash != request.session.workflow.workflow_hash()):
                    if sampling is not None:
                        sampling.close()
                    sampling = None
                    packet.error = CoreResult.fail(ErrorCode.INTERNAL_ERROR, "color_adjust.mixer.failed").error
                elif sampling is not None and not self.core.image_sources.matches_file(sampling.source_identity):
                    sampling.close()
                    sampling = None
                    packet.error = CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed").error
            self._on_pipeline_render_finished(request,
                packet.image if packet is not None else None,
                packet.frame if packet is not None else None,
                packet.error if packet is not None else None, sampling=sampling,
                detail_result=packet.result if packet is not None else None,
                detail_image=packet.detail_image if packet is not None else None,
            )
            sampling = None
        finally:
            if sampling is not None:
                sampling.close()
            if packet is not None:
                packet.close()
            if delivery is not None:
                delivery.close()

    @Slot(object, object, object, object)
    def _on_pipeline_render_finished(self, request, qimg, frame, error, *, sampling=None,
                                     detail_result=None, detail_image=None) -> None:
        self._preview_queue.finish(request)
        if self._accept_preview(request):
            self._report_manual_preview(request, "failed" if error is not None else "updated")
            beauty_panel = self._tool_panels.get("beauty")
            if request is self._preview_queue.latest and beauty_panel is not None and hasattr(beauty_panel, "set_beauty_status"):
                status = next((item for item in frame.effect_status if item.node_id == "beauty"), None) if frame is not None else None
                if frame is not None and status is None:
                    status = next(iter(frame.effect_status), None)
                beauty_panel.set_beauty_status("failed" if error is not None and error.details.get("tool_id") == "beauty" else status)
            if error is not None and request is self._preview_queue.latest:
                self._canvas_failed_generation = request.generation
                self._canvas_preview_state = replace(self._canvas_preview_state, kind="failed")
                self.canvas_preview_changed.emit(self._canvas_preview_state)
                self._toast(self._tr(error.message_key, self._tr(
                    "studio.render_failed", "Unable to render changes. Check tool settings and try again.")), 5000)
                if request.sampling_request is not None:
                    self._end_sampling()
            if (qimg is not None and error is None and request.generation > self._displayed_generation
                    and (request.sampling_request is None or sampling is not None)):
                previous_display = (self._displayed_generation, self._displayed_request, self._displayed_frame,
                                    self._displayed_histogram, self._last_rendered_qimage)
                self._invalidate_detail()
                self._displayed_generation = request.generation
                self._displayed_request = request
                self._displayed_frame = frame
                self._displayed_histogram = None
                self._last_rendered_qimage = qimg
                if request.context.gesture_id is None:
                    self._mask_needs_base = False
                if not self._comparing:
                    try:
                        self._show_canvas_image(qimg, sampling=sampling)
                    except MemoryError as failure:
                        (self._displayed_generation, self._displayed_request, self._displayed_frame,
                         self._displayed_histogram, self._last_rendered_qimage) = previous_display
                        self._end_sampling()
                        self._canvas_failed_generation = request.generation
                        self._detail_error_key = memory_error_key(failure)
                        self._set_detail_state("failed")
                        self._toast(self._tr(self._detail_error_key), 5000)
                        self._sync_canvas_display()
                        self._arm_preview_render()
                        return
                    sampling = None
                self._update_mask_context(request, frame)
                if self._pending_stage_view and request.context.preview_target == self._preview_target:
                    self.viewer.restore_view_state(self._stage_views.get((self.session.core_id, self._preview_target)))
                    self._pending_stage_view = False
                # 画布工具调整时画布上是这一步的输入；需要它的工具（证件照按人脸摆框）由此拿到。
                # 源图还在换成完整尺寸时的一帧同样算数：比例相同，框用的是比例坐标
                canvas = self._tool_panels.get(self._active_tool_id)
                if (canvas is not None and hasattr(canvas, "on_step_input") and not self._result_view
                        and request is self._preview_queue.latest
                        and request.context.editing_tool == self._active_tool_id
                        and not self.session.read_only and canvas.image_path == self._current_path
                        and self._panel_sessions.get(self._active_tool_id) is self.session):
                    canvas.on_step_input(qimg)
                logger.debug("OPS 预览交付 request=%s session=%s delivery_ms=%.1f final=%s",
                    request.generation, request.context.session_id,
                    (monotonic() - request.requested_at) * 1000, request is self._preview_queue.latest)
                if request.context.editing_tool == "color_adjust" and request.temporary_workflow is None:
                    self._histogram_runner.submit(request, frame)
                else:
                    self._histogram_runner.cancel()
                if (self._preview_cache is not None and request.preview_key is not None
                        and not request.context.source_preview.half_size and request.temporary_workflow is None
                        and self._canvas_preview_mode != "full"):
                    self._preview_cache.put(request.preview_key, frame,
                        is_cancelled=request.is_cancelled)
                old_frames = self._canvas_full_frames[:]
                self._canvas_full_frames.clear()
                if request.context.preview_mode == "full" and detail_result is not None and detail_result.detail is not None:
                    self._canvas_full_frames.append(detail_result.take_detail())
                for old_frame in old_frames:
                    old_frame.close()
                if request.context.preview_mode != "full" and request is self._detail_candidate() and detail_result is not None:
                    if detail_result.detail is not None and detail_image is not None:
                        self._adopt_detail(request, detail_result.take_detail(), detail_image)
                    elif detail_result.detail_error is not None:
                        self._detail_attempted = request.generation
                        self._detail_error_key = detail_result.detail_error.message_key
                        self._detail_resources.release_source()
        else:
            logger.debug("OPS 预览拒收 request=%s tool=%s cancelled=%s tools_current=%s",
                request.generation, request.context.editing_tool, request.is_cancelled(),
                self.core.tools.is_current(request.context.tool_generation))
            self._report_manual_preview(request, "stopped")
        if sampling is not None:
            sampling.close()
        if request is self._preview_queue.latest and self._preview_queue.pending is None:
            canvas = self._canvas_panel()
            if canvas is not None:
                canvas.result_view.set_busy(False)
        self._arm_preview_render()
        if self._preview_queue.pending is None and request is self._preview_queue.latest:
            if qimg is not None and self._accept_preview(request) and not self.session.read_only:
                self._request_original()
            self.preview_finished.emit()
        self._refresh_detail()

    def canvas_preview_state(self):
        return self._canvas_preview_state

    def set_canvas_preview_mode(self, mode):
        from ..application.studio_preload import StudioPreloadSettings
        if mode not in StudioPreloadSettings.CANVAS_MODES:
            raise ValueError("Invalid canvas preview mode")
        if mode == self._canvas_preview_mode:
            return
        self.stop_preview()
        self._original_runner.cancel()
        self._original_level_requested = None
        self._original_error = None
        self._canvas_preview_mode = mode
        large_source = self.session is not None and max(self.session.source_size) > 1600
        if (mode == "preview1600" and self._original_display is not None
                and (large_source or max(self._original_display.size) > 1600)):
            self._original_display.full = None
            self._original_qimage = self._original_display.preview
            if self._original_display.full_memory is not None:
                self._original_display.full_memory.close()
                self._original_display.full_memory = None
        self._sync_canvas_display()
        if self.session is not None and not self._image_loading:
            self._schedule_pipeline_render()

    def _clear_original_display(self):
        self._original_qimage = None
        self._original_level_requested = None
        self._original_geometry = ""
        display, self._original_display = self._original_display, None
        if display is not None:
            display.close()

    def _comparison_workflow(self):
        request = self._displayed_request
        return comparison_workflow(request.session.workflow) if request is not None else WorkflowGraph()

    def _request_original(self):
        if self.session is None or self.session.read_only or self._image_loading:
            return
        workflow = self._comparison_workflow()
        geometry_key = workflow.canonical_json() if workflow.nodes else ""
        if geometry_key != self._original_geometry:
            self._original_runner.cancel()
            self._clear_original_display()
            self._original_geometry = geometry_key
            self._original_error = None
        if self._original_error is not None:
            return
        full = (self._canvas_preview_mode == "full"
                or self._canvas_preview_mode == "auto" and self._view_needs_detail())
        proxy_input = not full and (self._displayed_frame is None or self._displayed_frame.input_is_proxy is not False)
        display = self._original_display
        if display is not None and (display.full is not None if full else display.preview_proxy_input == proxy_input):
            return
        requested = (full, proxy_input, geometry_key)
        if (self._original_level_requested is not None
                and (self._original_level_requested[0] or self._original_level_requested == requested)):
            return
        self._original_level_requested = requested
        self._original_runner.submit(self.session.core_id, self.core.source_identity(self.session.core_id),
                                     full=full, proxy_input=proxy_input, workflow=workflow)

    def _comparison_image(self):
        display = self._original_display
        if display is None:
            return self._original_qimage
        workflow = self._comparison_workflow()
        geometry_key = workflow.canonical_json() if workflow.nodes else ""
        if display.geometry_key != geometry_key:
            return None
        full = self._canvas_preview_mode == "full" or self._detail_showing
        if full:
            return display.full
        if self._displayed_frame is not None and display.preview_proxy_input != (self._displayed_frame.input_is_proxy is not False):
            return None
        return display.preview

    def _sync_canvas_display(self):
        if self._syncing_canvas:
            return
        self._syncing_canvas = True
        try:
            base, frame = self._last_rendered_qimage, self._displayed_frame
            current = self._displayed_request
            preview_ready = (self._original_display is None or frame is None
                             or self._original_display.preview_proxy_input == (frame.input_is_proxy is not False))
            detail_ready = (self._canvas_preview_mode == "auto" and (self._view_needs_detail() or not preview_ready)
                            and self._detail_request is current and self._detail_resources.image is not None
                            and (self.session.read_only or self._original_display is not None
                                 and self._original_display.full is not None))
            self._detail_showing = bool(detail_ready)
            edited = self._detail_resources.image if detail_ready else base
            before = self._comparison_image()
            image = before if self._comparing else edited
            full_size = frame.full_size if frame is not None and frame.full_size else (
                self.session.source_size if self.session is not None else None)
            reference = full_size
            if image is None or reference is None or self._image_loading:
                state = CanvasPreviewState(mode=self._canvas_preview_mode)
            else:
                size = (image.width(), image.height())
                is_full = detail_ready or size == tuple(full_size or size)
                if self._comparing:
                    is_full = self._original_display is not None and image is self._original_display.full
                    kind = "original" if is_full else "original_preview"
                else:
                    kind = "full" if is_full else "preview"
                    if current is not None and current.temporary_workflow is not None:
                        kind = "temporary"
                    if self._preview_queue.latest is not current:
                        kind = ("failed" if self._preview_queue.latest is not None
                                and self._canvas_failed_generation == self._preview_queue.latest.generation else "updating")
                    elif frame is None:
                        kind = "pending"
                state = CanvasPreviewState(self._canvas_preview_mode, kind, size,
                    self.session.source_size if detail_ready else (frame.input_size if frame is not None else None),
                    False if detail_ready else (frame.input_is_proxy if frame is not None else None))
                key = (image.cacheKey(), tuple(reference), self._comparing)
                companion = edited if self._comparing else before
                companion_reference = full_size
                if key != self._canvas_image_key:
                    try:
                        self._show_canvas_image(image, companion=companion, companion_reference=companion_reference)
                    except MemoryError as error:
                        self._detail_error_key = memory_error_key(error)
                        self._detail_showing = False
                        if detail_ready:
                            self._detail_resources.clear_frame()
                        self._set_detail_state("failed")
                        state = replace(state, kind="failed")
                        self._canvas_preview_state = state
                        self.canvas_preview_changed.emit(state)
                        return
                    self._canvas_image_key = key
                self.viewer.set_comparison_image(companion, companion_reference)
            if state != self._canvas_preview_state:
                self._canvas_preview_state = state
                self.canvas_preview_changed.emit(state)
        finally:
            self._syncing_canvas = False

    def _view_needs_detail(self):
        if self._canvas_preview_mode == "preview1600":
            return False
        if self.session is None or not self.viewer.has_image or self._mask_gesture is not None:
            return False
        frame = self._displayed_frame
        full = frame.full_size if frame is not None and frame.full_size else self.session.source_size
        if frame is not None:
            proxy = frame.size
        else:
            scale = min(1, STUDIO_PREVIEW_MAX_DIMENSION / max(full))
            proxy = tuple(max(1, round(side * scale)) for side in full)
        rect = self.viewer.get_image_display_rect()
        return needs_detail((rect.width(), rect.height()), self.viewer.devicePixelRatioF(), proxy, full)

    def _detail_candidate(self):
        request = self._displayed_request
        if (request is None or request is not self._preview_queue.latest
                or request.temporary_workflow is not None
                or self._preview_queue.active is not None or self._preview_queue.pending is not None
                or not self.isVisible() or self._commit_busy or not self._accept_preview(request)
                or self.session.state_version != request.session.state_version
                or request.context.source_preview.half_size
                or not self.core.image_sources.matches_file(request.context.source_identity)):
            return None
        return request

    def _set_detail_state(self, state):
        status = (state, self._detail_error_key if state == "failed" else "")
        if status != self._detail_state:
            self._detail_state = status
            self.detail_state_changed.emit(*status)

    def detail_state(self):
        return self._detail_state

    def _detail_editing(self):
        return any(panel.edit_group is not None and self._panel_sessions.get(tool) is self.session
                   for tool, panel in self._tool_panels.items())

    def _invalidate_detail(self, *, release_source=False):
        self._detail_timer.stop()
        self._detail_runner.cancel()
        if self._detail_showing and not self._comparing and self._last_rendered_qimage is not None:
            self.viewer.replace_display_image(self._last_rendered_qimage)
        self._detail_showing = False
        self._detail_resources.clear_frame()
        if release_source:
            self._detail_resources.release_source()
        self._detail_request = None
        self._detail_attempted = None
        self._detail_error_key = ""
        self._set_detail_state("")

    def _refresh_detail(self):
        if self._syncing_canvas:
            return
        request = self._detail_candidate()
        if request is None:
            release_source = not self.isVisible() or self.session is None or self._image_loading
            identity = self._detail_resources.source_identity
            if identity is not None and self.session is not None:
                release_source = release_source or identity != self.core.source_identity(self.session.core_id)
                release_source = release_source or not self.core.image_sources.matches_file(identity)
            if self._detail_request is not None or self._detail_resources.detail is not None:
                self._invalidate_detail(release_source=release_source)
            else:
                self._detail_timer.stop()
                self._set_detail_state("")
            if release_source:
                self._detail_resources.release_source()
            self._sync_canvas_display()
            return
        self._request_original()
        if (self._detail_resources.source_identity is not None
                and self._detail_resources.source_identity != request.context.source_identity):
            self._detail_resources.release_source()
        if not self._view_needs_detail():
            self._detail_timer.stop()
            self._detail_runner.cancel()
            if self._detail_showing and not self._comparing:
                self.viewer.replace_display_image(self._last_rendered_qimage)
            self._detail_showing = False
            self._detail_resources.release_source()
            if self._detail_resources.detail is None:
                self._detail_request = None
                if not self._detail_error_key:
                    self._detail_attempted = None
            self._set_detail_state("")
        elif self._detail_editing():
            self._detail_timer.stop()
            self._set_detail_state("")
        elif self._detail_resources.detail is not None and self._detail_request is request:
            if self._detail_resources.source is None and not self.session.read_only:
                try:
                    self._detail_resources.source = self.core.image_sources.borrow_cached_linear(request.context.source_identity)
                except SourceChangedError:
                    self._invalidate_detail(release_source=True)
                    return
                self._detail_resources.source_identity = request.context.source_identity
            if not self._comparing and not self._detail_showing:
                self._show_detail()
            self._set_detail_state("")
        elif self._detail_attempted == request.generation:
            self._detail_timer.stop()
            self._set_detail_state("failed" if self._detail_error_key else "preparing")
        else:
            if not self._detail_timer.isActive():
                self._detail_timer.start()
            self._set_detail_state("preparing")

        self._sync_canvas_display()

    def _show_detail(self):
        self._detail_showing = True
        self._sync_canvas_display()

    def _start_detail_render(self):
        request = self._detail_candidate()
        if request is None or not self._view_needs_detail():
            self._refresh_detail()
            return
        if self._detail_attempted == request.generation:
            return
        if self._detail_editing():
            self._set_detail_state("")
            return
        self._detail_request = request
        self._detail_attempted = request.generation
        self._detail_runner.submit(request)
        self._set_detail_state("preparing")

    def _adopt_detail(self, request, detail, image):
        self._detail_timer.stop()
        self._detail_request = request
        self._detail_attempted = request.generation
        self._detail_resources.adopt(detail, image, request.context.source_identity)

    def _on_detail_ready(self, request, packet):
        if request is not self._detail_request:
            return
        if request is not self._detail_candidate():
            self._refresh_detail()
            return
        if packet.result is None and packet.error is None:
            self._detail_request = None
            self._detail_attempted = None
            self._detail_error_key = ""
            self._set_detail_state("")
            return
        if packet.error is not None:
            self._detail_error_key = packet.error.message_key
            self._detail_resources.release_source()
        elif packet.result is not None and packet.result.detail is not None and packet.image is not None:
            self._adopt_detail(request, packet.result.take_detail(), packet.image)
        else:
            self._detail_error_key = "studio.detail_failed"
        self._refresh_detail()

    def _on_full_source_ready(self, key, result):
        if (self.session is None or self._image_loading or self.session.core_id != key[0]
                or self.core.source_identity(key[0]) != key[1]
                or not self.core.image_sources.matches_file(key[1])):
            return
        self.hold_preview_source(result.data)
        result.data = None
        self._awaiting_full_source = False
        panel = self._tool_panels.get(self._active_tool_id)
        if self.session.read_only:
            self._schedule_pipeline_render()
        elif panel is not None and self._panel_sessions.get(self._active_tool_id) is self.session:
            panel.set_source_preview(self.core.source_preview(key[0]), refresh=True)
            self._schedule_pipeline_render(edit_group=panel.edit_group)
        else:
            self._activate_tool_panel(self._active_tool_id)
        self._update_actions_state()

    def _on_original_ready(self, key, image, error):
        if (self.session is None or self._image_loading or self.session.core_id != key[0]
                or self.core.source_identity(key[0]) != key[1]
                or not self.core.image_sources.matches_file(key[1])):
            if image is not None:
                image.close()
            return
        if image is not None and image.geometry_key != self._original_geometry:
            image.close()
            return
        self._original_level_requested = None
        if error is not None:
            self._original_error = error
            self._toast(self._tr(error.message_key), 5000)
        else:
            previous, self._original_display = self._original_display, image
            if (previous is not None and image.full is not None and self._displayed_frame is not None
                    and previous.preview_proxy_input == (self._displayed_frame.input_is_proxy is not False)):
                if image.memory is not None:
                    image.memory.close()
                image.preview, previous.preview = previous.preview, None
                image.memory, previous.memory = previous.memory, None
                image.preview_proxy_input = previous.preview_proxy_input
            if previous is not None and image.full is None and previous.full is not None:
                image.full, previous.full = previous.full, None
                image.full_memory, previous.full_memory = previous.full_memory, None
            self._original_qimage = image.full if image.full is not None else image.preview
            if previous is not None:
                previous.close()
            self._refresh_detail()
        self._update_actions_state()

    def _on_histogram_ready(self, request, histogram: dict) -> None:
        if not self._accept_preview(request) or request.generation != self._displayed_generation:
            return
        self._displayed_histogram = histogram
        self._update_displayed_histogram()

    def _update_displayed_histogram(self):
        panel = self._tool_panels.get("color_adjust")
        histogram = self._displayed_histogram
        if (not self._comparing and histogram is not None
                and panel and panel.image_path == self._current_path):
            panel._histogram_widget.set_histogram_data(histogram)

    # ------------------------------------------------------------------
    # 对比、重置、撤销、应用
    # ------------------------------------------------------------------
    def start_compare(self) -> None:
        """按住对比：显示与当前几何区域对齐的改前画面。"""
        self.assistant_panel.reset_comparison()
        image = self._comparison_image()
        if image is not None:
            self._end_sampling()
            self._cancel_mask_gesture(resume=False)
            self._compare_view = self.viewer.view_state()
            self._comparing = True
            self.viewer.set_overlay(None)
            self._sync_canvas_display()
            self._update_actions_state()

    def stop_compare(self) -> None:
        """松开对比：恢复当前处理合成图。"""
        self.assistant_panel.reset_comparison()
        self._comparing = False
        self._restore_canvas()
        self.viewer.restore_view_state(getattr(self, "_compare_view", None))
        panel = self._tool_panels.get(self._active_tool_id)
        if panel is not None:
            self._bind_overlay(panel)
        if self._displayed_request is not None:
            self._update_mask_context(self._displayed_request, self._displayed_frame)
        if getattr(self, "_mask_needs_base", False):
            self._schedule_pipeline_render()
        self._update_displayed_histogram()
        self._update_actions_state()

    def undo(self) -> None:
        self._move_in_history(lambda session: session.undo())

    def redo(self) -> None:
        self._move_in_history(lambda session: session.redo())

    def checkout_revision(self, revision_id: str) -> None:
        """回到历史线上的某一步；往回走时沿途各步仍可以重做。"""
        def checkout(session):
            result = session.checkout(revision_id)
            if not result.success and result.error.code != ErrorCode.CANCELLED:
                self._show_core_error(result)
            return result.success

        self._move_in_history(checkout)

    def _move_in_history(self, move) -> None:
        if self._commit_busy:
            return
        self.stop_preview()
        self.assistant_panel.invalidate()
        if self.session and move(self.session):
            self._sync_panels_from_session()
            self._persist_draft()
            self._schedule_pipeline_render()
        self._update_actions_state()

    def reset_current_tool(self) -> None:
        self.reset_tool(self._active_tool_id)

    def reset_tool(self, tool_id: str) -> None:
        """把一个工具的参数放回初始状态；可以是当前工具，也可以是编辑栈里的另一步。"""
        if not self.session or self.session.read_only or self._commit_busy:
            return
        self.stop_preview()
        if tool_id == "grid_crop":
            self._grid_settings.pop(self._current_path, None)
        else:
            self.session.reset_stage(tool_id)
        self._sync_panels_from_session()
        self._persist_draft()
        if self._result_view and tool_id == self._active_tool_id:
            # 重置之后从调整开始；里面会重新渲染
            self._set_result_view(False)
            return
        self._schedule_pipeline_render()
        self._update_actions_state()

    def cancel_current_tool(self) -> None:
        self.reset_current_tool()

    def reset_all(self) -> None:
        if not self.session or self.session.read_only or self._commit_busy:
            return
        self.stop_preview()
        reset = self.session.reset_all()
        if not reset.success:
            self._show_core_error(reset)
            return
        self._grid_settings.pop(self._current_path, None)
        self._sync_panels_from_session()
        self._persist_draft()
        self._schedule_pipeline_render()
        self._update_actions_state()

    def _sync_panels_from_session(self) -> None:
        """Push the session's stage params back into the panel UIs after undo/redo."""
        if not self.session or self.session.read_only:
            return
        for tool_id, panel in tuple(self._tool_panels.items()):
            if panel.image_path != self._current_path:
                continue
            self._restoring = True
            try:
                if tool_id == "color_adjust":
                    self._configure_color_panel(panel)
                panel.restore_params(self._params_for_tool(tool_id))
                if tool_id == "beauty":
                    self._configure_beauty_panel(panel)
            finally:
                self._restoring = False
            if hasattr(panel, "mask_ui_state"):
                self._save_mask_ui(panel)
        self._refresh_modified_tools()

    def _tool_defaults(self, tool_id: str) -> dict:
        """工具还没有进入本次编辑时面板上的参数。"""
        panel = self._tool_panels.get(tool_id)
        defaults = (type(panel) if panel is not None else PANEL_TYPES[tool_id])._default_params()
        if tool_id == "ai_enhance" and "target" in defaults:
            for cap in ("super_resolution", "denoise", "deblur"):
                defaults[cap + "_model"] = self.core.models.default(cap)
        for capability in ("segmentation", "face_detection"):
            if capability + "_model" in defaults:
                defaults[capability + "_model"] = self.core.models.default(capability)
        if "face_locator_model" in defaults:
            from ..application.managed_tool_modules import default_face_locator
            if self.session is None or not (self.session.beauty_params or self.session.stage_node("beauty") is not None):
                defaults["face_locator_model"] = default_face_locator(self.core.models)
        if tool_id == "beauty":
            defaults = {key: 0 if type(value) is int else value for key, value in defaults.items()}
        elif tool_id == "denoise":
            defaults["strength"] = 0
            defaults["color_strength"] = 0
            defaults["sharpen"] = 0
        return defaults

    def _stage_defaults(self, tool_id: str) -> dict:
        """一个步骤没有改动时的参数：编辑栈的摘要、历史的说明与候选差异都以它为参照。"""
        if tool_id == "color_adjust" and self.session is not None and not self.session.read_only:
            return self.session.baseline_color_params
        try:
            return self._tool_defaults(tool_id)
        except (KeyError, ImportError):
            return {}

    def _params_for_tool(self, tool_id: str) -> dict:
        defaults = self._tool_defaults(tool_id)
        if tool_id == "grid_crop":
            return {**defaults, **copy.deepcopy(self._grid_settings.get(self._current_path, {}))}
        if not self.session:
            return defaults
        spec = _STAGE_SPECS.get(tool_id)
        params = getattr(self.session, spec[0]) if spec else self.session.tool_params.get(tool_id, {})
        return {**defaults, **copy.deepcopy(params)}

    def _persist_draft(self, *, edit_group=None, commit_stamp=None) -> CoreResult[tuple[str, int, int]]:
        synchronized_state = None
        if self.session and self.session.read_only:
            return CoreResult.ok()
        if self.session:
            tool_generation = self.core.tools.generation
            stamp = commit_stamp if commit_stamp is not None else self._mask_commit_stamp
            synced = self.session.sync(self._active_tool_id, edit_group=edit_group,
                expected_version=stamp[0] if stamp is not None else None,
                expected_tools=stamp[1] if stamp is not None else None,
                expected_source=stamp[2] if stamp is not None else None)
            if not synced.success:
                self.session.refresh()
                self._sync_panels_from_session()
                self._show_core_error(synced)
                self._update_actions_state()
                if self.session.read_only:
                    self.load_image_path(self._current_path)
                return synced
            synchronized_state = (synced.data.session_id, synced.data.state_version, tool_generation)
            request = self._project_request
            if (request is not None and request.session_id == self.session.core_id
                    and request.state_version != synced.data.state_version):
                self.cancel_project_operation(force=True)
        if self.session is not None:
            written = self.edit_sessions.write_drafts(self.session)
            if not written.success:
                self._show_core_error(written)
                return written

        return CoreResult.ok(synchronized_state)

    def modified_tools(self) -> frozenset[str]:
        """当前图片上参数不在初始状态的工具。"""
        if not self.session:
            return frozenset()
        modified = {tool_id for tool_id, (attr, is_default) in _STAGE_SPECS.items()
                    if (self.session.color_modified() if tool_id == "color_adjust"
                        else not is_default(getattr(self.session, attr)))}
        return frozenset(modified | set(self.session.tool_params))

    def _refresh_modified_tools(self) -> None:
        modified = self.modified_tools()
        self.tool_rail.set_modified(entry.entry_id for entry in TOOL_ENTRIES if modified & set(entry.tool_ids))

    # ------------------------------------------------------------------
    # 编辑栈与历史：都是会话状态的投影
    # ------------------------------------------------------------------
    def _stack_rows(self) -> tuple[StackRow, ...]:
        """这张照片现在依次做的各步，按处理顺序。"""
        session = self.session
        if session is None:
            return ()
        adjusting = self.sidebar_tabs.currentIndex() == _TAB_ADJUST
        rows = []
        for tool_id, params in session.stages().items():
            neutral = self._stage_defaults(tool_id)
            summary = changes_summary(tool_id, parameter_changes(neutral, params, neutral), self._tr) if params else ""
            entry = tool_entry_for(tool_id)
            rows.append(StackRow(tool_id, stage_title(tool_id, self._tr), summary, entry.icon if entry else "",
                                 current=adjusting and tool_id == self._active_tool_id,
                                 resettable=not session.read_only))
        return tuple(rows)

    def _refresh_edit_views(self) -> None:
        self.tool_sidebar.set_edit_stack(self._stack_rows())
        if self.right_stack.currentWidget() is self.history_panel:
            self._refresh_history()

    def _refresh_history(self) -> None:
        session = self.session
        timeline = session.timeline() if session is not None and not self._image_loading else None
        if timeline is None:
            self.history_panel.set_steps(())
            return
        if self._history_session != session.core_id:
            self._history_session = session.core_id
            self._history_stages.clear()
            self._history_texts.clear()
        revisions, current = timeline["revisions"], timeline["current"]
        undone = self._tr("history.undone", "Undone")
        steps = []
        for index, revision in enumerate(revisions):
            text = self._history_texts.get(revision.revision_id)
            if text is None:
                text = self._history_texts[revision.revision_id] = self._revision_text(
                    revision, revisions[index - 1] if index else None)
            state = "current" if index == current else "undone" if index > current else "past"
            steps.append(HistoryStep(revision.revision_id, text,
                                     undone if state == "undone" else self._time_text(revision.created_at), state))
        self.history_panel.set_steps(steps)

    def _revision_stages(self, revision) -> dict:
        stages = self._history_stages.get(revision.revision_id)
        if stages is None:
            stages = self._history_stages[revision.revision_id] = self.session.revision_stages(revision.revision_id)
        return stages

    def _revision_text(self, revision, previous) -> str:
        """历史里一步的说明：相对上一步改了哪个步骤的什么参数。"""
        if previous is None:
            return self._tr("history.opened", "Opened {name}", name=os.path.basename(self.session.image_path))
        if revision.label == "reset":
            return self._tr("history.reset_all", "Reset all")
        text = step_text(stage_changes(self._revision_stages(previous), self._revision_stages(revision),
                                       self._stage_defaults), self._tr)
        if revision.label in ("batch.sync", "batch.exposure", "batch.calibration", "ai.batch.sync", "ai.batch.exposure", "ai.batch.calibration"):
            prefix = self._tr("history." + revision.label.replace(".", "_"))
            return prefix + " · " + text if text else prefix
        if revision.label.startswith("ai."):
            if revision.label.startswith("ai.variant."):
                return self._tr("history.ai_variant", variant=revision.label.rsplit(".", 1)[1]) + " · " + text
            if revision.label == "ai.style.replay":
                return self._tr("history.ai_style_replay") + " · " + text
            prefix = (self._tr("history.ai_step", step=revision.label.rsplit(".", 1)[1]) if revision.label.startswith("ai.tool.")
                      else self._tr("history.ai_round", round=revision.label.rsplit(".", 1)[1])
                      if revision.label.startswith("ai.round.") else self._tr("history.ai"))
            return prefix + " · " + text if text else prefix
        if text:
            return text
        # 参数没有变的一步：升级旧版效果这类操作写的是它的名称
        named = self._tr(revision.label, "") if "." in revision.label else ""
        if named:
            return plain_action_text(named)
        return self._tr("history.committed" if revision.artifact_refs else "history.adjusted")

    @staticmethod
    def _time_text(created_at: float) -> str:
        if created_at <= 0:
            return ""
        moment = datetime.fromtimestamp(created_at)
        return moment.strftime("%H:%M" if moment.date() == datetime.now().date() else "%m-%d %H:%M")

    def can_undo(self) -> bool:
        return self.session is not None and self.session.can_undo()

    def can_redo(self) -> bool:
        return self.session is not None and self.session.can_redo()

    def _update_actions_state(self) -> None:
        has_session = self.session is not None
        can_u, can_r = self.session.undo_redo_state() if has_session else (False, False)
        read_only = has_session and self.session.read_only
        is_dirty = self.is_dirty() and not read_only
        parameters_ready = self._parameters_ready()
        effects_ready = not (has_session and self.session.issues)
        active_panel = self._tool_panels.get(self._active_tool_id)
        if active_panel is not None and hasattr(active_panel, "set_sampling_available"):
            available = (has_session and not read_only and not self._commit_busy and not self._image_loading
                         and parameters_ready and not self._comparing)
            if available:
                snapshot = self.session.render_snapshot(self._active_tool_id)
                request = self.session.mixer_sampling_request(snapshot, 0)
                available = self.core.sampling_capability(snapshot.workflow, request).success
            active_panel.set_sampling_available(bool(available))
        effects_ready = effects_ready and bool(getattr(active_panel, "models_ready", True))
        full_source_ready = not has_session or not self.core.source_preview(self.session.core_id).half_size
        effects_ready = effects_ready and full_source_ready
        tool_available = self.core.tools.check_enabled(self._active_tool_id).success
        self.tool_rail.setEnabled(not read_only and not self._commit_busy and not self._image_loading)
        self.assistant_panel.setEnabled(not self._commit_busy)
        self.assistant_panel.refresh()
        self.tool_sidebar.setEnabled(not read_only and tool_available and not self._commit_busy and not self._image_loading)
        compare_tip = "studio.compare_tip"
        if has_session and self._comparison_image() is None:
            compare_tip = self._original_error.message_key if self._original_error else "studio.original_loading"
        self.history_panel.setEnabled(not self._commit_busy and not self._image_loading)
        self._update_notices(read_only=bool(read_only))
        self._refresh_edit_views()
        self._publish_action_state(StudioActionState(
            busy=self._commit_busy,
            can_undo=can_u,
            can_redo=can_r,
            can_save=bool(has_session and not self._commit_busy and parameters_ready),
            can_export=bool(has_session and not self._commit_busy and parameters_ready and effects_ready),
            can_apply=bool(has_session and not read_only and not self._commit_busy and parameters_ready
                           and effects_ready),
            can_reset_all=bool(is_dirty),
            title=os.path.basename(self.session.project_path or self.session.image_path) if has_session else "",
            unsaved=bool(has_session and self.session.has_unsaved_changes()),
            can_compare=bool(has_session and not read_only and self._comparison_image() is not None),
            compare_hint="" if compare_tip == "studio.compare_tip" else self._tr(compare_tip),
        ))

    def _own_resource_gap(self, issue) -> bool:
        """这条问题是不是当前正在编辑的工具自己缺资源：这种情况由它的面板说明，不占顶部的提示条。"""
        return (self.sidebar_tabs.currentIndex() == _TAB_ADJUST and self._active_tool_id in self._tool_panels
                and issue.get("code") == ErrorCode.TOOL_RESOURCE_MISSING
                and issue.get("details", {}).get("tool_id") == self._active_tool_id)

    def _update_notices(self, *, read_only: bool) -> None:
        """右侧栏顶部按条件出现的提示条，只放项目一级的事：加载失败、等待完整编辑源、只读项目、
        其他步骤缺少资源、旧版效果、可升级为画质增强。"""
        session = self.session
        self._set_notice(self._notice_load, self._load_error_text(),
                         "common.retry" if self._source_load_request is not None else "")
        self._set_notice(self._notice_source,
                         self._tr("studio.full_source_loading") if self._awaiting_full_source else "")
        def described(items):
            return "\n".join(
                self._tr(issue["message_key"], self._tr("studio.unsupported_workflow"))
                + (" (" + str(issue.get("details", {}).get("tool_id")) + ")"
                   if issue.get("details", {}).get("tool_id") else "")
                for issue in items)

        issues = described(session.issues) if session is not None else ""
        read_only_text = ""
        if read_only:
            status_key = "studio.project_read_only" if session.has_history_image else "studio.history_empty"
            read_only_text = "\n".join(text for text in (self._tr(status_key), issues) if text)
        self._set_notice(self._notice_read_only, read_only_text, "studio.tools_recheck")
        resources_text = ""
        if session is not None and not read_only:
            resources_text = described(issue for issue in session.issues if not self._own_resource_gap(issue))
        self._set_notice(self._notice_resources, resources_text, "tool_manager.restore_resources")
        self._set_notice(self._notice_enhance,
                         self._tr("studio.notice.enhancement") if self._enhancement_upgrade_nodes() else "",
                         "enhance.upgrade")
        for bar in (self._notice_load, self._notice_read_only, self._notice_resources):
            bar.action_button.setEnabled(not self._commit_busy)
        self._notice_enhance.action_button.setEnabled(not self._commit_busy and not self._image_loading)

    def action_state(self) -> StudioActionState:
        return self._action_state

    def _publish_action_state(self, state: StudioActionState) -> None:
        if state != self._action_state:
            self._action_state = state
            self.action_state_changed.emit(state)

    def is_dirty(self) -> bool:
        return self.session.is_dirty() if self.session else False

    def has_pending_edits(self) -> bool:
        return any(session.has_unsaved_changes() for session in self.edit_sessions.sessions())

    def stop_preview(self) -> None:
        self._color_preview_gesture = None
        self._canvas_image_key = None
        if self._preview_target_session is not None and self._displayed_request is not None and not self._comparing:
            self._stage_views[self._preview_target_session, self._preview_target] = self.viewer.view_state()
        self._cancel_mask_gesture(resume=False)
        self._cancel_mask_generation()
        panel = self._tool_panels.get("color_adjust")
        if panel is not None:
            panel.clear_mask_range()
        beauty_panel = self._tool_panels.get("beauty")
        if beauty_panel is not None:
            beauty_panel.cancel_beauty_analysis()
            if hasattr(beauty_panel, "set_beauty_status"):
                beauty_panel.set_beauty_status(None)
        self._invalidate_detail()
        self._end_sampling()
        dropped = self._preview_queue.pending
        self._preview_queue.cancel()
        if self._sampling_resources.delivery is not None:
            self._sampling_resources.delivery.close()
        self._render_timer.stop()
        if dropped is not None:
            # 还没开始就被丢弃的请求不会有完成回调
            self._report_manual_preview(dropped, "stopped")
        self._histogram_runner.cancel()
        self._displayed_request = None
        self._displayed_frame = None
        self._displayed_histogram = None

        state = (CanvasPreviewState(mode=self._canvas_preview_mode) if self._image_loading or self.session is None
                 else replace(self._canvas_preview_state, kind="pending"))
        if state != self._canvas_preview_state:
            self._canvas_preview_state = state
            self.canvas_preview_changed.emit(state)

    def _release_canvas_full_frames(self):
        for frame in self._canvas_full_frames:
            frame.close()
        self._canvas_full_frames.clear()

    def shutdown_preview(self):
        self._clear_original_display()
        self.viewer.shutdown()
        self._release_canvas_full_frames()
        self._last_rendered_qimage = None
        self.photo_info_panel.cleanup()
        self.cancel_project_operation(force=True, resume=False)
        self._project_runner.shutdown()
        self.stop_preview()
        self._histogram_runner.shutdown()
        self._original_runner.shutdown()
        self._detail_runner.shutdown()
        self._detail_resources.close()

    def _grid_export_snapshot(self):
        if self.session is None or self.session.read_only:
            raise ValueError("error.project.read_only")
        state = self.core.get_session(self.session.core_id).unwrap()
        core, session_id, version = self.core, self.session.core_id, state.state_version
        def export(params, directory, prefix, extension, cancellation):
            return core.export_grid(session_id, params, directory, prefix, extension,
                                    expected_state=version, cancellation=cancellation).unwrap()
        return export

    def render_full_image(self) -> Optional[Image.Image]:
        if not self.session:
            return None
        if not self._parameters_ready():
            self._toast(self._tr("tool.preparing"))
            return None
        self.stop_preview()
        return self._render_service.render(self.session.render_snapshot(), use_proxy=False)

    def apply_changes(self) -> None:
        """全尺寸高质量运行流水线，叠加一次性工具，按用户选择保存。"""
        if not self.session or self.session.read_only or not self._current_path or self._commit_busy:
            return
        if not self._parameters_ready():
            self._toast(self._tr("tool.preparing"))
            return
        if self._active_tool_id == "grid_crop":
            self._tool_panels["grid_crop"].export_tiles()
            return

        if self.session.project_path:
            self.export_project_image()
            return

        self.assistant_panel.invalidate()
        if not self.is_dirty() and not self.session.render_snapshot().workflow.nodes:
            # 没有实质性修改，直接返回
            self.back_requested.emit()
            return

        target = self._apply_service.ask_target()
        if target == ApplyTarget.CANCEL:
            return

        old_path = self._current_path
        extension = Path(old_path).suffix.lower()
        from ..core.raw_loader import is_raw_file
        default_ext = ".tiff" if extension in (".tif", ".tiff") or is_raw_file(old_path) else ".png"
        final_path = self._apply_service.resolve_target(target, old_path, default_ext=default_ext)
        if final_path:
            self._start_output(ExportRequest(final_path, overwrite=True), apply=True)

    def _complete_apply(self, old_path, final_path):
        store = getattr(self._window, "_edit_state_store", None)
        if store:
            for tool_id, (attr, is_default) in _STAGE_SPECS.items():
                params = getattr(self.session, attr)
                if not is_default(params):
                    store.commit(old_path, final_path, tool_id, params)
            for tool_id, params in self.session.tool_params.items():
                store.commit(old_path, final_path, tool_id, params)
            store.clear_path(old_path)
            store.clear_path(final_path)
        for path in {old_path, os.path.normpath(final_path)}:
            self.edit_sessions.close(path)
        self.load_image_path(None)
        self.applied.emit(final_path)

    def _sidebar_mode_changed(self, index):
        if index == _TAB_ADJUST:
            self._activate_tool_panel(self._active_tool_id)
        else:
            # 历史与 AI 助手两页看的都是全部各步之后的画面，画布工具的框与轮廓收起
            self._result_view = False
            self.viewer.set_overlay(None)
            self._schedule_pipeline_render()
        # 换了一页：这一栏最窄能到多少随之变化
        self._apply_sidebar_widths()
        self._refresh_edit_views()

    def show_assistant(self):
        if self._commit_busy:
            return
        self._show_right_sidebar()
        self.sidebar_tabs.setCurrentIndex(_TAB_ASSISTANT)
        self.assistant_panel.refresh()

    def show_history(self):
        if self._commit_busy:
            return
        self._show_right_sidebar()
        self.sidebar_tabs.setCurrentIndex(_TAB_HISTORY)

    # ------------------------------------------------------------------
    # AI 助手的宿主
    # ------------------------------------------------------------------
    def assistant_context(self):
        """当前照片可以请求建议时的编辑上下文；没有照片、只读或正在载入时为 None。"""
        session = self.session
        if session is None or session.read_only or self._image_loading:
            return None
        state = self.core.get_session(session.core_id)
        if not state.success:
            return None
        if session.render_snapshot().workflow.workflow_hash() != state.data.draft_workflow.workflow_hash():
            return None
        source = state.data.active_source
        identity = self.core.source_identity(session.core_id)
        object_key, name = self.assistant_binding()
        return AssistantContext(session.core_id, state.data.state_version,
                                {**self._stage_defaults("color_adjust"), **session.color_params},
                                state.data.draft_workflow, object_key, name, state.data.document_id,
                                identity.file_hash if identity else source.file_hash,
                                state.data.current_revision.revision_id, source.width, source.height,
                                beauty={**self._stage_defaults("beauty"), **session.beauty_params},
                                tools={"beauty": self._assistant_beauty_state()})

    def _assistant_beauty_state(self) -> str:
        """美颜能不能交给 AI 调：空字符串表示可以，否则是原因键（见 assistant_context.UNAVAILABLE_REASONS）。

        就绪与美颜面板同取工具资源投影；界面每次刷新都会调用，这里不做导入检测库、哈希模型文件一类的环境探测。"""
        if not self.core.tools.check_enabled("beauty").success:
            return "disabled"
        if self._beauty_readiness is None:
            from ..application.tool_feature_readiness import READY, ToolFeatureReadiness
            from ..core.tools.managed_models import BEAUTY_RESOURCE_FEATURES, MANAGED_BEAUTY_PARAMETERS
            self._beauty_readiness = (ToolFeatureReadiness(self.core.tools, self.core.models, "beauty",
                                                           MANAGED_BEAUTY_PARAMETERS, BEAUTY_RESOURCE_FEATURES,
                                                           fields=("face_locator_model",)), READY)
        readiness, ready = self._beauty_readiness
        node = self.session.stage_node("beauty") if self.session else None
        detail = (node is not None and node.node_type == "tool"
                  and node.params.get("implementation_id") == "beauty.detail")
        readiness.set_implementation(node.params.get("implementation_version", "") if detail else "")
        states = readiness.states({**self._stage_defaults("beauty"), **(self.session.beauty_params if self.session else {})})
        return "" if states and all(state == ready for state in states.values()) else "resources"

    def _assistant_face_detector(self):
        from ..application.beauty_analysis import BeautyFaceQuery
        return BeautyFaceQuery(self.core)

    def assistant_binding(self):
        if self.session is None:
            return ("", "")
        state = self.core.get_session(self.session.core_id)
        if not state.success:
            return ("", "")
        source = state.data.active_source
        identity = self.core.source_identity(self.session.core_id)
        digest = identity.file_hash if identity else source.file_hash
        if self.session.project_path:
            key = "project:" + state.data.document_id
        else:
            key = "source:" + (digest or hashlib.sha256(os.path.normcase(os.path.abspath(source.path)).encode("utf-8")).hexdigest())
        return key, os.path.basename(self.session.project_path or source.path)

    def assistant_history_impact(self, affected):
        if self.session is None:
            return ""
        history = self.core.history_state(self.session.core_id, include_revisions=True)
        revisions = {revision.revision_id: revision for revision in history.data["revisions"]} if history.success else {}
        return "\n".join(self._revision_text(revision, revisions.get(revision.parent_id)) for revision in affected)

    def assistant_candidate(self, context, adjustments):
        session = self.session
        if session is None or session.read_only or session.core_id != context.session_id:
            raise ValueError("STALE_CANDIDATE")
        state = self.core.get_session(session.core_id)
        if not state.success or state.data.state_version != context.state_version:
            raise ValueError("STALE_CANDIDATE")
        if adjustments.get("beauty") and context.tools.get("beauty", "disabled"):
            raise ValueError("STALE_CANDIDATE")
        return session.assistant_proposal(adjustments, {"beauty": self._stage_defaults("beauty")},
                                           base_workflow=context.workflow)

    def assistant_candidate_builder(self, context):
        self.assistant_candidate(context, {})
        mapper = self.session.detached()
        defaults = {"beauty": self._stage_defaults("beauty")}
        return lambda values, *, workflow=context.workflow, beauty=None: mapper.assistant_proposal(
            {"color": values, "beauty": beauty or {}}, defaults, base_workflow=workflow)

    def assistant_revision_labels(self):
        timeline = self.session.timeline() if self.session else None
        if not timeline:
            return {}
        revisions = {revision.revision_id: revision for revision in timeline["revisions"]}
        return {revision.revision_id: self._revision_text(revision, revisions.get(revision.parent_id))
                for revision in timeline["revisions"]}

    def _show_canvas_image(self, image: QImage, *, sampling=None, companion=None, companion_reference=None) -> None:
        if sampling is None:
            self._end_sampling()
        else:
            previous, self._sampling_resources.sampling = self._sampling_resources.sampling, sampling
            if previous is not None:
                previous.close()
        actual_size = (self._displayed_frame.full_size if self._displayed_frame is not None
                       else None)
        reference_size = QSize(*(actual_size or self.session.source_size)) if self.session is not None else image.size()
        horizontal = self.scroll_area.horizontalScrollBar()
        vertical = self.scroll_area.verticalScrollBar()
        position = (horizontal.value(), vertical.value())
        if companion is None and not self._comparing:
            companion = self._comparison_image()
            companion_reference = actual_size or (self.session.source_size if self.session is not None else None)
        self.viewer.set_display_pair(image, reference_size, companion, companion_reference, sampling=sampling)
        self._canvas_image_key = (image.cacheKey(), (reference_size.width(), reference_size.height()), self._comparing)
        if sampling is not None:
            panel = self._tool_panels.get(self._active_tool_id)
            if panel is not None and hasattr(panel, "set_sampling_state"):
                panel.set_sampling_state("ready")
        horizontal.setValue(position[0])
        vertical.setValue(position[1])

    def _restore_canvas(self):
        if self._last_rendered_qimage is not None and not self._comparing:
            self._sync_canvas_display()
            self._refresh_detail()

    def _set_commit_busy(self, busy):
        if busy:
            self.cancel_project_operation(force=True, resume=False)
        self._commit_busy = busy
        if busy:
            self.preview_started.emit()
        for widget in (self.tool_rail, self.tool_sidebar, self.sidebar_tabs, self.thumbnail_panel,
                       self._notices, self.viewer):
            widget.setEnabled(not busy)
        self._publish_action_state(replace(self._action_state, busy=busy))
        if not busy:
            self._update_actions_state()

    def _on_agent_committed(self, image):
        # AI 写入了一步或撤销、重做了 AI 的调整：与手动编辑一样同步会话、滑块与草稿。
        # 写入时带着这一步渲染好的画面，先显示它，等这次渲染换上
        if image is not None:
            rgba = image.convert("RGBA")
            self._last_rendered_qimage = QImage(rgba.tobytes(), rgba.width, rgba.height,
                                                rgba.width * 4, QImage.Format.Format_RGBA8888).copy()
            if not self._comparing:
                self._show_canvas_image(self._last_rendered_qimage)
        self.session.refresh()
        self._sync_panels_from_session()
        self._persist_draft()
        self._schedule_pipeline_render()

    def _on_assistant_comparison(self, image):
        if image is None:
            self.stop_compare()
            return
        self._end_sampling()
        with image.convert("RGBA") as rgba:
            frame = QImage(rgba.tobytes(), rgba.width, rgba.height,
                           rgba.width * 4, QImage.Format.Format_RGBA8888).copy()
        self._comparing = True
        self._show_canvas_image(frame)
        self._update_actions_state()

    def _show_core_error(self, result):
        if not result.success:
            show_error(self, self._tr("studio.error_title", "Editing failed"),
                       self._tr(result.error.message_key, self._tr(
                           "studio.project_failed", "Project operation failed. Check the source and destination.")))

    def open_project(self):
        if self._commit_busy or self._exit_save_callback is not None:
            return
        path, _ = QFileDialog.getOpenFileName(self, self._tr("studio.project_open", "Open Project"),
                                             "", "OPS Project (*.opsproj)")
        if not path:
            return
        self._start_project_open(path)

    def _start_project_open(self, path, source_path=None):
        if self._browse_source_cancel is not None:
            self._browse_source_cancel()
        self.cancel_project_operation(force=True, resume=False)
        if self._source_load_cancel is not None:
            self._source_load_cancel()
        self.stop_preview()
        self._detail_resources.release_source()
        self.assistant_panel.invalidate()
        state = self.core.get_session(self.session.core_id).data if self.session else None
        self._project_origin = self._current_path
        self._project_request = self._project_runner.submit(
            "open", path, session_id=state.session_id if state else None,
            source_id=state.active_source.source_id if state else None,
            state_version=state.state_version if state else None, source_path=source_path,
        )
        self._show_project_progress("open")

    def _open_context_current(self, request):
        if self._current_path != self._project_origin:
            return False
        if request.session_id is None:
            return self.session is None
        if self.session is None or self.session.core_id != request.session_id:
            return False
        state = self.core.get_session(request.session_id)
        return (state.success and state.data.state_version == request.state_version
                and state.data.active_source.source_id == request.source_id)

    def _attach_project(self, path, project):
        try:
            session = StudioSession(self.core, project)
        except ValueError:
            self.core.close_session(project.session_id)
            show_error(self, self._tr("studio.error_title", "Editing failed"),
                       self._tr("studio.unsupported_workflow", "This project cannot be edited with the current tool panels."))
            return
        source = os.path.normpath(session.image_path)
        previous = self.edit_sessions.get(source)
        if previous and previous.has_unsaved_changes():
            from .widgets.message_dialog import show_confirm
            if not show_confirm(self, self._tr("studio.unsaved_title", "Unsaved changes"),
                              self._tr("studio.replace_project", "Discard current edits for this photo and open the project?"),
                              yes_text=self._tr("assistant.discard", "Discard"),
                              no_text=self._tr("studio.keep_editing", "Keep editing"), default_yes=False):
                self.core.close_session(session.core_id)
                return
        session.project_path = path
        session.saved_hash = project.draft_workflow.workflow_hash()
        session.saved_version = project.state_version
        if self._browse_source_request is None or not os.path.isfile(source):
            self.core.close_session(session.core_id)
            self._show_core_error(CoreResult.fail(ErrorCode.INTERNAL_ERROR, "studio.project_failed"))
            return
        self.release_preview_source()
        self.edit_sessions.attach(session)
        self._browse_source_request(source)
        self._window.switch_to_studio()
        self._update_actions_state()

    def recheck_project_tools(self):
        if not self.session or self._commit_busy or self._exit_save_callback is not None:
            return
        self.cancel_project_operation(force=True)
        self.stop_preview()
        self.assistant_panel.invalidate()
        state = self.core.get_session(self.session.core_id)
        if not state.success:
            self._show_core_error(state)
            return
        self._project_request = self._project_runner.submit(
            "check", self._current_path, session_id=state.data.session_id,
            source_id=state.data.active_source.source_id, state_version=state.data.state_version,
        )
        self._show_project_progress("check")

    def save_project(self, save_as=False):
        if not self.session or self._commit_busy or self._exit_save_callback is not None:
            return
        if not self._parameters_ready():
            self._toast(self._tr("tool.preparing"))
            return
        path = self.session.project_path
        if save_as or not path:
            path, _ = QFileDialog.getSaveFileName(self, self._tr("studio.project_save", "Save Project"),
                path or str(Path(self._current_path).with_suffix(".opsproj")), "OPS Project (*.opsproj)")
            if not path:
                return
            if not path.lower().endswith(".opsproj"):
                path += ".opsproj"
        self.cancel_project_operation(force=True)
        captured = self.core.capture_project_save(self.session.core_id, path)
        if not captured.success:
            self._show_core_error(captured)
            return
        self._start_project_save(captured.data)

    def _start_project_save(self, snapshot, *, purpose="save"):
        self.assistant_panel.invalidate()
        self._project_origin = self._current_path
        self._project_request = self._project_runner.submit(
            purpose, snapshot.path, snapshot=snapshot, session_id=snapshot.session_id,
            source_id=snapshot.source_id, state_version=snapshot.state_version,
        )
        self._show_project_progress("save")

    def _show_project_progress(self, purpose):
        self._close_project_progress()
        key = {"open": "studio.project_opening", "check": "studio.project_checking"}.get(purpose, "studio.project_saving")
        title = {"open": "studio.project_open", "check": "studio.tools_recheck"}.get(purpose, "studio.project_save")
        progress = ProgressDialog(self, title=self._tr(title), text=self._tr(key), modal=False)
        progress.cancelled.connect(self.cancel_project_operation)
        self._project_progress = progress
        progress.show()

    def _close_project_progress(self):
        if self._project_progress is not None:
            self._project_progress.cancelled.disconnect(self.cancel_project_operation)
            self._project_progress.finish()
            self._project_progress.deleteLater()
            self._project_progress = None

    def cancel_project_operation(self, *, force=False, resume=True):
        request = self._project_request
        if request is None and self._exit_save_callback is None:
            return
        if request is not None and not request.cancellation.cancel() and not force:
            return
        self._project_runner.cancel()
        self._project_request = None
        self._project_origin = None
        self._close_project_progress()
        if self._exit_save_callback is not None:
            self._finish_exit_save(False)
        if resume:
            self._resume_project_preview()

    def _resume_project_preview(self):
        if self._image_loading and self._source_load_request is not None:
            self._source_load_request(self._window.manager.current_path)
        else:
            self._schedule_pipeline_render()

    @Slot(object, object)
    def _on_project_finished(self, request, result):
        if self._project_request is not request:
            if request.purpose == "open" and result.success:
                self.core.close_session(result.data.session_id)
            elif request.purpose in ("save", "exit_save") and result.success:
                self._record_project_save(request, result)
            return
        context_current = request.purpose != "open" or self._open_context_current(request)
        self._project_request = None
        self._project_origin = None
        self._close_project_progress()
        if not context_current:
            if result.success:
                self.core.close_session(result.data.session_id)
            self._resume_project_preview()
            return
        if not result.success:
            if (request.purpose == "open" and request.source_path is None
                    and result.error.code in ("SOURCE_NOT_FOUND", "SOURCE_CHANGED")):
                source, _ = QFileDialog.getOpenFileName(self, self._tr("studio.relocate_source", "Locate original photo"))
                if source:
                    self._start_project_open(request.path, source)
                    return
            if result.error.code == "CANCELLED":
                self._toast(self._tr("error.cancelled"))
            else:
                self._show_core_error(result)
            if request.purpose == "exit_save":
                self._finish_exit_save(False)
            self._resume_project_preview()
            return
        if request.purpose == "check":
            state = self.core.get_session(request.session_id)
            if (self.session is not None and self.session.core_id == request.session_id and state.success
                    and state.data.state_version == request.state_version
                    and state.data.active_source.source_id == request.source_id):
                self.session.refresh()
                self.load_image_path(self._current_path)
            return
        if request.purpose == "open":
            self._attach_project(request.path, result.data)
            self._resume_project_preview()
            return
        recorded = self._record_project_save(request, result)
        self._update_actions_state()
        self._toast(self._tr("studio.project_saved", "Project saved"))
        if request.purpose == "exit_save":
            if not recorded:
                self._finish_exit_save(False)
            elif self._exit_save_pending:
                self._start_project_save(self._exit_save_pending.pop(0), purpose="exit_save")
            else:
                self._finish_exit_save(True)

    def _record_project_save(self, request, result):
        from ..core.contracts import WorkflowGraph
        snapshot = request.snapshot
        saved = self.core.get_project_saved_state(snapshot.session_id)
        if not saved.success or saved.data is None:
            return False
        state = saved.data
        if (os.path.normcase(state["path"]) != os.path.normcase(result.data)
                or state["state_version"] != snapshot.state_version or state["source_id"] != snapshot.source_id):
            return False
        session = next((item for item in self.edit_sessions.sessions() if item.core_id == snapshot.session_id), None)
        if session is None:
            return False
        session.project_path = state["path"]
        session.saved_hash = WorkflowGraph.from_dict(snapshot.document.draft).workflow_hash()
        session.source_relocated = False
        session.saved_version = state["state_version"]
        if self.session is session:
            self._update_actions_state()
        return True

    @property
    def saving_projects_for_exit(self):
        return self._exit_save_callback is not None

    def save_projects_for_exit(self, callback):
        self.cancel_project_operation(force=True)
        snapshots = []
        for session in self.edit_sessions.sessions():
            if session.project_path and session.has_unsaved_changes():
                captured = self.core.capture_project_save(session.core_id, session.project_path)
                if not captured.success:
                    self._show_core_error(captured)
                    callback(False)
                    return True
                snapshots.append(captured.data)
        if not snapshots:
            return False
        self._exit_save_callback = callback
        self._exit_save_snapshots = tuple(snapshots)
        self._exit_save_pending = snapshots[1:]
        self._start_project_save(snapshots[0], purpose="exit_save")
        return True

    def _finish_exit_save(self, success):
        if success:
            for snapshot in self._exit_save_snapshots:
                state = self.core.get_session(snapshot.session_id)
                if not state.success or state.data.state_version != snapshot.state_version:
                    success = False
                    self._toast(self._tr("error.cancelled"))
                    break
        callback, self._exit_save_callback = self._exit_save_callback, None
        self._exit_save_snapshots = ()
        self._exit_save_pending = []
        if callback is not None:
            callback(success)

    def export_project_image(self):
        if not self.session or self._commit_busy:
            return
        if not self._parameters_ready():
            self._toast(self._tr("tool.preparing"))
            return
        path, selected = QFileDialog.getSaveFileName(self, self._tr("studio.project_export", "Export Image"),
            str(Path(self._current_path).with_name(Path(self._current_path).stem + "-edited.png")),
            "PNG (*.png);;JPEG (*.jpg);;WebP (*.webp);;TIFF (*.tiff)")
        if not path:
            return
        if not Path(path).suffix:
            path += {"JPEG": ".jpg", "WebP": ".webp", "TIFF": ".tiff"}.get(selected.split(" ")[0], ".png")
        self._start_output(ExportRequest(path, overwrite=True))

    def _start_output(self, request, *, apply=False):
        if not self._parameters_ready():
            self._toast(self._tr("tool.preparing"))
            return
        state = self.core.get_session(self.session.core_id)
        if not state.success:
            self._show_core_error(state)
            return
        self.stop_preview()
        self.assistant_panel.invalidate()
        self._output_generation += 1
        task_id = self._output_generation
        task = ExportTask(task_id, self.core, self.session.core_id, state.data.state_version,
                          request, apply=apply)
        self._output_tasks[task_id] = task
        self._output_context = (self.session.core_id, self._current_path, state.data.state_version, apply)
        task.signals.finished.connect(self._on_output_finished)
        progress = ProgressDialog(self, title=self._tr("studio.project_export", "Export Image"),
                                  text=self._tr("studio.output_running", "Saving image…"), modal=False)
        progress.cancelled.connect(self.cancel_output)
        self._output_progress = progress
        self._set_commit_busy(True)
        progress.show()
        self._output_pool.start(task)

    def cancel_output(self):
        task = self._output_tasks.get(self._output_generation)
        if task is not None:
            if not task.cancellation.cancel():
                return
        self._output_generation += 1
        self._output_context = None
        self._close_output_progress()
        self._set_commit_busy(False)
        self._schedule_pipeline_render()

    def _close_output_progress(self):
        if self._output_progress is not None:
            self._output_progress.cancelled.disconnect(self.cancel_output)
            self._output_progress.finish()
            self._output_progress.deleteLater()
            self._output_progress = None

    @Slot(int, object)
    def _on_output_finished(self, task_id, result):
        self._output_tasks.pop(task_id, None)
        if task_id != self._output_generation:
            return
        context = self._output_context
        self._output_context = None
        self._close_output_progress()
        self._set_commit_busy(False)
        if context is None or self.session is None:
            return
        sid, old_path, version, apply = context
        state = self.core.get_session(sid)
        if (self.session.core_id != sid or self._current_path != old_path
                or not state.success or state.data.state_version != version):
            return
        if not result.success:
            if (apply and result.error.code == "UNSUPPORTED_FORMAT"
                    and result.error.message_key == "error.export.alpha_format"):
                path = self._apply_service.resolve_target(
                    ApplyTarget.SAVE_AS, old_path, default_ext=".png", has_alpha=True)
                state = self.core.get_session(sid)
                if (task_id != self._output_generation or self.session is None
                        or self.session.core_id != sid or self._current_path != old_path
                        or not state.success or state.data.state_version != version):
                    return
                if path:
                    self._start_output(ExportRequest(path, overwrite=True), apply=True)
                    return
            elif result.error.code != "CANCELLED":
                self._show_core_error(result)
            self._schedule_pipeline_render()
            return
        if apply:
            self._complete_apply(old_path, result.data.path)
        else:
            self._toast(self._tr("studio.exported", "Image exported"))
            self._schedule_pipeline_render()

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        for panel in self._tool_panels.values():
            if hasattr(panel, "enhancement_upgrade_button"):
                panel.enhancement_upgrade_button.setText(self._tr("enhance.upgrade"))
        for entry in TOOL_ENTRIES:
            self.tool_rail.set_entry_text(entry.entry_id, self._tr(entry.title_key, entry.title_default))
        self.info_sidebar.retranslate_ui()
        self.tool_sidebar.retranslate_ui()
        self.assistant_panel.retranslate_ui()
        self.sidebar_tabs.set_segment_text(_TAB_ADJUST, self._tr("assistant.adjust", "Adjust"))
        self.sidebar_tabs.set_segment_text(_TAB_HISTORY, self._tr("history.title", "History"))
        self.sidebar_tabs.set_segment_text(_TAB_ASSISTANT, self._tr("assistant.title", "AI Assistant"))
        self._retranslate_history()
        self._sync_tool_navigation()
        self._update_actions_state()
        self._apply_sidebar_widths()

    def _retranslate_history(self) -> None:
        self.history_panel.set_texts(
            self._tr("history.title", "History"), self._tr("history.hint"),
            self._tr("history.empty_title", "No history yet"), self._tr("history.empty_text"))
        # 各步的说明要按新的语言重写
        self._history_texts.clear()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Backslash and not event.isAutoRepeat():
            self.start_compare()
            event.accept()
            return
        if self._canvas_tool_key(event.key()):
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Backslash and not event.isAutoRepeat():
            self.stop_compare()
            event.accept()
            return
        super().keyReleaseEvent(event)
