
import os
import logging
from dataclasses import replace
from pathlib import Path
from concurrent.futures import CancelledError
from threading import Event
from time import monotonic

from PIL import Image
from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QRunnable, Signal, Slot, QThreadPool, QTimer, QSettings, QPropertyAnimation
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QWidget,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QProgressBar,
    QGraphicsOpacityEffect,
    QFileDialog,
)

from ..utils.app_info import APP_NAME
from .base_window import BaseWindow
from .image_viewer import ImageViewer
from .widgets.thumbnail_panel import ThumbnailPanel
from .widgets.photo_info_panel import PhotoInfoSidebar
from .widgets.message_dialog import (DialogButton, ask_choice, confirm_destructive, show_error, show_info,
                                     show_warning)
from .widgets.progress_dialog import ProgressDialog
from .dialogs.compress_dialog import ImageProcessingDialog
from .dialogs.pdf_dialog import PDFDialog
from .dialogs.settings_dialog import SettingsDialog, SettingsValues
from .dialogs.enhancement_dialog import EnhancementDialog
from .dialogs.wallpaper_dialog import WallpaperDialog
from .services import ApplyService
from .services.transform_save import TransformSaveRequest, TransformSaveWorker
from .shortcut_manager import ShortcutManager
from .theme import ThemeManager, get_theme_manager
from .window_actions import WindowActions
from .window_layout import PanelPreferences, set_splitter_width, sidebar_width, window_layout
from .widgets.bottom_dock import BottomDock
from .widgets.canvas_toolbar import CanvasToolbar, CanvasToolbarActions
from .widgets.empty_state import EmptyState
from .widgets.notice_bar import NoticeBar
from .widgets.side_dock import SideDock
from .widgets.toast import DEFAULT_DURATION_MS, Toast
from .widgets.top_bar import EDITOR, TopBar, TopBarActions
from resources.styles.theme_variables import (CANVAS_TOOLBAR_HEIGHT, CANVAS_TOOLBAR_MARGIN, NARROW_WINDOW_WIDTH,
                                              SPACING_MD, SPACING_SM, px)
from ..core.constants import SUPPORTED_OUTPUT_FORMATS_FILTER, SUPPORTED_FORMATS_FILTER
from ..core.image_manager import ImageManager
from ..core.image_processor import ImageProcessor
from ..core.raw_loader import load_image
from ..core.edit_state import EditStateStore
from ..application.studio_preload import StudioPreloadSettings
from ..application.image_display_settings import ImageDisplaySettings
from ..application.color_initialization import viewer_matches_baseline
from ..application.studio_source_service import StudioPreviewTarget
from ..application.result_types import CoreOperationError, CoreResult, ErrorCode
from ..application.studio_preview_cache import CanvasPreviewState, StudioPreviewCache
from ..application.draft_persistence import DraftPersistenceService
from .services.draft_persistence_runner import DraftPersistenceRunner
from .panels import BaseToolPanel
from .panels.registry import create_tool_panel, resolve_tool_id
from .studio_action_state import StudioActionState

logger = logging.getLogger(__name__)



class _StudioInitSignals(QObject):
    ready = Signal(object)
    failed = Signal(object)


class _CacheCloseSignals(QObject):
    finished = Signal(object)


class _CacheCloseWorker(QRunnable):
    def __init__(self, operation):
        super().__init__()
        self._operation = operation
        self.signals = _CacheCloseSignals()

    def run(self):
        error = None
        try:
            self._operation()
        except Exception as exc:
            error = exc
            logger.exception("缓存关闭失败")
        finally:
            try:
                self.signals.finished.emit(error)
            except RuntimeError:
                logger.debug("缓存关闭接收方已释放")


class _StudioInitWorker(QRunnable):
    def __init__(self, output_writer, image_sources, inference, runtime_query):
        super().__init__()
        self.output_writer = output_writer
        self.image_sources = image_sources
        self.inference = inference
        self.runtime_query = runtime_query
        self.signals = _StudioInitSignals()
        self._cancelled = Event()

    def cancel(self):
        self._cancelled.set()

    def run(self):
        started = monotonic()
        try:
            from ..application.core_api import CoreAPI
            core = CoreAPI(autosave_projects=False, output_writer=self.output_writer,
                           image_source_service=self.image_sources, inference_backend=self.inference,
                           runtime_query=self.runtime_query)
        except Exception as error:
            self.signals.failed.emit(error)
        else:
            if self._cancelled.is_set():
                core.tools.close()
                return
            logger.debug("OPS 核心初始化 duration_ms=%.1f", (monotonic() - started) * 1000)
            self.signals.ready.emit(core)
            try:
                core.tools.snapshot(is_cancelled=self._cancelled.is_set)
            except CancelledError:
                if self._cancelled.is_set():
                    core.tools.close()
            except Exception:
                logger.exception("OPS 工具预热失败")


class MainWindow(BaseWindow):
    def __init__(self, *, resource_policy=None, runtime_activation=None, memory=None):
        """resource_policy 给出线程与缓存额度；内存上限、座位与系统保留量由内存偏好经 memory 解析，
        memory 缺省时按本机物理内存创建并向系统登记座位。"""
        super().__init__(
            title_key="app.title",
            title_default=APP_NAME,
            min_width=800,
            min_height=600,
            default_width=1600,
            default_height=900,
            persist_geometry=True,
            settings_group=APP_NAME,
            settings_key="MainWindow",
        )

        self._closing = False
        self._version_runner = None
        self._cache_exit_pending = False
        self._caches_closed_for_exit = False
        self._cache_close_worker = None
        self._projects_saved_for_exit = False
        self.manager = ImageManager()
        from ..application.browse_service import BrowseService
        from .services.browse_task_runner import BrowseTaskRunner
        self._browse_runner = BrowseTaskRunner(BrowseService(), self)
        self._browse_runner.finished.connect(self._on_browse_finished)
        self._browse_runner.busy_changed.connect(self._foreground_preparation_changed)
        self.destroyed.connect(self._browse_runner.shutdown)
        self._browse_request = None
        self._browse_progress = None
        from ..application.image_source_service import ImageSourceService
        from ..application.image_resources import system_resource_policy
        from ..application.thumbnail_cache import ThumbnailCache
        from ..application.linear_proxy_cache import LinearProxyCache
        from .services.thumbnail_cache_runner import CacheMaintenanceRunner
        from ..application.storage_paths import storage_path, default_data_root
        data_root = storage_path("data", default_data_root())
        self._preload_settings = StudioPreloadSettings.load(QSettings(APP_NAME, "OPSPreload"))
        self._thumbnail_cache = ThumbnailCache(storage_path("thumbnails", data_root / "thumbnails"),
                                               max_bytes=self._preload_settings.thumbnail_mib * 1024 ** 2)
        self._edit_cache = LinearProxyCache(storage_path("edit_cache", data_root / "edit_cache"),
                                           max_bytes=self._preload_settings.disk_mib * 1024 ** 2)
        self._thumbnail_cache_runner = CacheMaintenanceRunner(
            self._thumbnail_cache, ImageSourceService.THUMBNAIL_PIPELINE_VERSION, self,
            message_keys={"clear": "settings.thumbnails.clear_failed",
                          "initialize": "settings.thumbnails.unavailable",
                          "limit": "settings.thumbnails.limit_failed",
                          "query": "settings.thumbnails.unavailable"})
        self._edit_cache_runner = CacheMaintenanceRunner(
            self._edit_cache, LinearProxyCache.VERSION, self,
            message_keys={"clear": "settings.edit_cache.clear_failed",
                          "initialize": "settings.edit_cache.unavailable",
                          "limit": "settings.edit_cache.limit_failed",
                          "query": "settings.edit_cache.unavailable"})
        self.destroyed.connect(self._thumbnail_cache_runner.shutdown)
        self.destroyed.connect(self._edit_cache_runner.shutdown)
        self._thumbnail_cache_runner.initialize()
        self._edit_cache_runner.initialize()
        for runner in (self._thumbnail_cache_runner, self._edit_cache_runner):
            runner.maintenance_failed.connect(lambda key: self.show_toast(self._tr(key), 5000))
        from ..application.memory_settings import MIB, MemoryConfigurator, MemorySettings
        if memory is None:
            from ..application.system_profile import memory_status, set_working_set_minimum
            status = memory_status()
            memory = MemoryConfigurator(status.total_bytes if status is not None else None,
                                        set_working_set_minimum)
        # 座位在资源池创建之前登记
        self._memory = memory
        self._memory_state = memory.apply(MemorySettings.load(QSettings(APP_NAME, "Memory")))
        logger.info("内存策略 total=%s limit=%s reserve=%s seat=%s seat_refused=%s",
                    self._memory_state.budget.total_bytes, self._memory_state.budget.limit_bytes,
                    self._memory_state.budget.reserve_bytes, self._memory_state.seat_bytes,
                    self._memory_state.seat_refused)
        self._image_sources = ImageSourceService(
            policy=replace(resource_policy or system_resource_policy(),
                           preview_cache_bytes=self._preload_settings.memory_mib * MIB,
                           **self._memory_state.policy_fields()),
            thumbnail_cache=self._thumbnail_cache, linear_proxy_cache=self._edit_cache)
        from .services.canvas_scale_runner import CanvasScaleRunner
        self._canvas_scale_runner = CanvasScaleRunner(self._image_sources.resources, self)
        self.destroyed.connect(self._canvas_scale_runner.shutdown)
        self.destroyed.connect(self._close_source_caches)
        from ..application.inference_backend import InferenceBackend
        from ..application.runtime_service import RuntimeService
        from ..application.system_profile import detect_profile
        from ..utils.model_paths import get_project_root, get_runtime_root
        self._inference = InferenceBackend()
        self._runtime_service = RuntimeService(
            get_runtime_root(), get_project_root() / "resources" / "runtimes" / "catalog.json",
            activation=runtime_activation, inference=self._inference, profile_provider=detect_profile)
        self.destroyed.connect(self._runtime_service.close)
        self._image_display_settings = ImageDisplaySettings.load(QSettings(APP_NAME, "ImageDisplay"))
        self.viewer = ImageViewer(image_source_service=self._image_sources,
                                  canvas_scale_runner=self._canvas_scale_runner,
                                  raw_display_mode=self._image_display_settings.raw_mode,
                                  non_raw_display_mode=self._image_display_settings.non_raw_mode)
        self.viewer.loading_changed.connect(self._foreground_preparation_changed)
        self.thumbnail_panel = ThumbnailPanel(image_source_service=self._image_sources)
        self.thumbnail_panel.building_changed.connect(self._foreground_preparation_changed)
        self.thumbnail_panel.listing_ready.connect(self._on_thumbnail_listing_ready)

        # 各栏的展开状态与拖出的宽度下次打开时恢复；版式随窗口宽度变化，先按标准版式搭建
        self._panel_prefs = PanelPreferences.load(QSettings(APP_NAME, "MainWindow"))
        self._layout_rules = window_layout(NARROW_WINDOW_WIDTH)
        self.info_sidebar = PhotoInfoSidebar(self._tr, self, histogram=True)
        from ..application.image_info import ImageInfoService
        from .services.image_info_runner import ImageInfoRunner
        self._info_service = ImageInfoService(compute_budget=self._image_sources.compute,
                                             resource_pool=self._image_sources.resources)
        self._info_runner = ImageInfoRunner(self._info_service.read, self._image_sources.file_stamp, self)
        self._connect_photo_info_panel(self.info_sidebar.panel)
        self._viewer_image_info = None
        self.info_sidebar.panel.metadata_changed.connect(self._on_viewer_metadata)
        from ..application.display_histogram import DisplayHistogramService
        from .services.display_histogram_runner import DisplayHistogramRunner
        self._histogram_runner = DisplayHistogramRunner(
            DisplayHistogramService(compute_budget=self._image_sources.compute), self)
        self.info_sidebar.panel.histogram_requested.connect(self._request_viewer_histogram)
        self.info_sidebar.panel.histogram_stopped.connect(self._histogram_runner.cancel)
        self._histogram_runner.started.connect(self.info_sidebar.panel.set_histogram_request)
        self._histogram_runner.ready.connect(self.info_sidebar.panel.set_histogram)
        self.viewer.loading_changed.connect(self._on_viewer_frame_changed)
        self.info_sidebar.panel.set_active(True)
        self.destroyed.connect(self._info_runner.shutdown)
        self.destroyed.connect(self._histogram_runner.shutdown)
        from ..application.image_output_service import ImageOutputService
        self._output_service = ImageOutputService(compute_budget=self._image_sources.compute,
                                                  resource_budget=self._image_sources.resources)
        from ..application.wallpaper_service import WallpaperService
        from ..application.windows_wallpaper import WindowsDesktopWallpaper
        self._wallpaper_service = WallpaperService(
            data_root / "wallpaper",
            image_sources=self._image_sources, writer=self._output_service.writer,
            desktop_factory=WindowsDesktopWallpaper,
        )
        self._apply_service = ApplyService(self, self._tr, output_service=self._output_service)
        self.studio_workspace = None
        self._studio_placeholder = None
        self._studio_loading_snapshot = None
        self._studio_init_worker = None
        self._studio_source_runner = None
        self._studio_preview_cache = None
        self._studio_mount_scheduled = False
        self._studio_init_started = False
        self._studio_init_error = None
        self._pending_studio_tool_id = None
        self._pending_studio_action = None
        self._batch_runner = None
        self._batch_dialog = None
        self._batch_exit_pending = False
        self._calibration_runner = None
        self._calibration_dialog = None
        self._calibration_exit_pending = False
        self._export_runner = None
        self._export_dialog = None
        self._export_exit_pending = False
        # 正开着的设置窗口；编辑内核就绪或装配失败时要告诉它
        self._settings_dialog = None
        self._assistant_runner = None
        self._panel_services = None
        self._current_workspace_index = 0
        self._prewarm_requested = False
        self._prewarm_timer = QTimer(self)
        self._prewarm_timer.setSingleShot(True)
        self._prewarm_timer.timeout.connect(self._try_prewarm_studio)

        self.shortcut_manager = ShortcutManager()

        self._current_pil_image = None
        self._current_pil_revision: int | None = None
        self._save_generation = 0
        self._temp_paths: set[str] = set()  # 追踪本窗口创建的所有临时文件/目录
        self._temp_workspaces = {}

        self.viewer.set_zoom_changed_callback(self._sync_canvas_toolbar)
        self.thumbnail_panel.image_selected.connect(self._on_thumbnail_selected)
        self.thumbnail_panel.batch_requested.connect(self._open_photo_batch)
        self.thumbnail_panel.order_changed.connect(self._on_thumbnail_reordered)

        # 接入评分系统：评分变化时刷新底部工具条（当前图的）
        from ..core.rating_store import get_rating_store
        get_rating_store().rating_changed.connect(self._on_rating_changed)

        # 接入编辑状态管理与右侧参数工具栏
        draft_dir = data_root
        self._edit_state_store = EditStateStore(self)
        self._drafts_saved_for_exit = False
        self._draft_exit_pending = False
        self._draft_exit_discard = False
        self._draft_runner = DraftPersistenceRunner(
            DraftPersistenceService(os.path.join(draft_dir, "drafts.json")), self._edit_state_store, self)
        self._draft_runner.ready.connect(self._on_drafts_ready)
        self._draft_runner.failed.connect(self._on_draft_failed)
        self._draft_runner.saved.connect(self._on_draft_saved)
        self._edit_state_store.stash_changed.connect(self._on_draft_changed)
        self.destroyed.connect(self._draft_runner.shutdown)
        self._draft_error_key = ""
        self._viewer_load_status = ("", False)
        self.viewer.load_status_changed.connect(self._on_viewer_load_status)
        self.thumbnail_panel.set_edit_state_store(self._edit_state_store)
        from .services.photo_browser_controller import PhotoBrowserController
        self._photo_browser = PhotoBrowserController(self.manager, get_rating_store(), self._image_sources, self._tr,
            drafts_ready=lambda: self._draft_runner.loaded,
            foreground_busy=lambda: self.active_viewer.is_loading or self._browse_runner.is_busy, parent=self)
        from ..application.photo_library import PhotoLibraryService
        from ..application.photo_library_store import PhotoLibraryStore
        from .services.photo_library_runner import PhotoLibraryRunner
        self._library_dialog = None
        self._library_deferred_open = None
        self._library_refresh_current = False
        self._library_exit_pending = False
        library = PhotoLibraryService(PhotoLibraryStore(data_root / "library"), self._image_sources, self._photo_browser.service)
        self._library_runner = PhotoLibraryRunner(library, get_rating_store(), self._draft_runner, self,
            before_relocation=self._prepare_library_relocation, lock_ui=self._lock_library_relocation)
        self._photo_browser.service.library = library
        get_rating_store().set_library(library.metadata_for_path, self._library_runner.write_metadata,
            unavailable=self._library_runner.unavailable, has_ratings=lambda: any(p["rating"] for p in library.photos.values()),
            defer=self._library_runner.defer)
        get_rating_store().save_failed.connect(lambda key: self.show_toast(self._tr(key), 6000))
        self._photo_browser.service.metadata_command = self._library_runner.browser_metadata
        self._library_runner.changed.connect(self._on_library_changed)
        self._library_runner.evaluated.connect(self._on_library_evaluated)
        self._library_runner.session_released.connect(self._on_library_session_released)
        self._library_runner.failed.connect(lambda key: self.show_toast(self._tr(key), 6000))
        self._library_runner.relocated.connect(self._on_library_relocated)
        self._library_runner.browse_requested.connect(self._browse_library_photos)
        self._library_runner.idle.connect(self._on_library_idle)
        self.destroyed.connect(self._library_runner.shutdown)
        self._photo_browser.image_requested.connect(self._on_thumbnail_selected)
        self._photo_browser.single_requested.connect(self._close_photo_browser)
        self._photo_browser.empty.connect(self._on_browser_empty)
        self._photo_browser.add_panel(self.thumbnail_panel)
        self.thumbnail_panel.browser_requested.connect(self._open_photo_browser)
        self._browser_exit_pending = False
        self._photo_browser.runner.idle.connect(self._on_browser_idle)
        self.destroyed.connect(self._photo_browser.shutdown)
        self.tool_sidebar = None
        self._active_tool_id: str | None = None
        self._tool_panels: dict[str, BaseToolPanel] = {}
        self._tool_sampling_epoch = 0
        from .services.studio_preview_runner import SamplingResources
        self._tool_sampling_resources = SamplingResources()
        self.destroyed.connect(self._tool_sampling_resources.close)
        self._tool_sampling_panel = None
        self.viewer.presentation_changed.connect(self._on_tool_presentation_changed)

        self.scroll_area = None
        self.top_bar = None
        self.canvas_toolbar = None

        # 顺序构建 UI 组件
        self._setup_central()
        self._setup_actions()
        self._setup_canvas_toolbar()
        self._setup_top_bar()
        self._update_menu_actions()

        # 开启拖放支持
        self.setAcceptDrops(True)
        self._apply_window_layout()
        self._setup_title_bar()
        from .services.version_check_runner import VersionCheckRunner
        self._version_runner = VersionCheckRunner(self)
        self._version_runner.changed.connect(self._on_update_state)
        self._version_runner.failed.connect(lambda key: self.show_toast(self._tr(key), 6000))
        self._notice_update.action_triggered.connect(self._version_runner.open_download)
        self._notice_update.dismissed.connect(self._version_runner.dismiss)
        self.destroyed.connect(self._version_runner.shutdown)
        self._sync_window_notices()

    def retranslate_ui(self):
        """刷新主窗口所有动态文案 —— 语言切换时自动调用。"""
        super().retranslate_ui()
        if hasattr(self, "_photo_browser"):
            self._photo_browser.view.retranslate()
        if getattr(self, "_library_dialog", None) is not None:
            self._library_dialog.retranslate()
            self._library_dialog.refresh()
        self.info_sidebar.retranslate_ui()
        if hasattr(self, "studio_workspace") and self.studio_workspace:
            self.studio_workspace.retranslate_ui()
        if self._browse_progress is not None and self._browse_request is not None:
            self._translate_browse_progress()
        self.window_actions.retranslate()
        self.top_bar.retranslate_ui()
        self.canvas_toolbar.retranslate_ui()
        self._retranslate_viewer_empty()
        self._sync_window_notices()
        self._update_top_bar_title()

    def _retranslate_viewer_empty(self) -> None:
        empty = self.viewer_empty
        empty.set_text(self._tr("viewer.empty.title", "Drop images or a folder here"),
                       self._tr("viewer.empty.formats", "JPEG, PNG, WebP, TIFF and common RAW formats"))
        empty.set_action_text("open_file", self._tr("viewer.empty.open_file", "Open Images"))
        empty.set_action_text("open_folder", self._tr("viewer.empty.open_folder", "Open Folder"))

    def _sync_viewer_empty(self) -> None:
        """没有图片时看图工作区只显示打开图片的引导；有图片后换回画布、胶片条与照片信息栏。"""
        empty = self.manager.current_path is None
        if (self.viewer_empty.isHidden(), self.viewer_splitter.isHidden()) == (not empty, empty):
            return
        self.viewer_empty.setVisible(empty)
        self.viewer_splitter.setVisible(not empty)
        if not empty:
            self.viewer_empty.set_drag_active(False)
            # 分隔条刚回到布局里：先排好，再把照片信息栏放回用户选的宽度
            self.viewer_workspace.layout().activate()
            self._apply_info_width()

    def _setup_central(self):
        """设置中心区域：QStackedWidget 容纳【看图工作区】与【独立修图工作区】"""
        self.workspace_stack = QStackedWidget(self)
        self.workspace_stack.setObjectName("workspaceStack")

        # -------------------------------------------------------------
        # 工作区 0: 看图工作区 (Viewer Workspace)
        # -------------------------------------------------------------
        self.viewer_workspace = QWidget(self)
        self.viewer_workspace.setObjectName("viewerWorkspace")
        vw_layout = QHBoxLayout(self.viewer_workspace)
        vw_layout.setContentsMargins(0, 0, 0, 0)
        vw_layout.setSpacing(0)

        # 1. 画布与下方的胶片条
        self.splitter = QSplitter(Qt.Orientation.Vertical, self.viewer_workspace)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(4)

        scroll = QScrollArea()
        scroll.setObjectName("imageScrollArea")
        scroll.setWidget(self.viewer)
        scroll.setWidgetResizable(False)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll_area = scroll

        self.splitter.addWidget(scroll)
        self.splitter.addWidget(self.thumbnail_panel)

        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)

        # 2. 右侧可收起的照片信息栏：与画布之间的分隔条可以拖动；窄窗口里改为浮在画布上
        self.viewer_splitter = QSplitter(Qt.Orientation.Horizontal, self.viewer_workspace)
        self.viewer_splitter.setObjectName("viewerSplitter")
        self.viewer_splitter.setChildrenCollapsible(False)
        self.viewer_splitter.setHandleWidth(4)
        self.viewer_splitter.addWidget(self.splitter)
        self.viewer_splitter.addWidget(self.info_sidebar)
        self.viewer_splitter.setStretchFactor(0, 1)
        self.viewer_splitter.setStretchFactor(1, 0)
        self.viewer_splitter.splitterMoved.connect(self._on_info_sidebar_dragged)
        self._info_dock = SideDock(self.info_sidebar, CANVAS_TOOLBAR_HEIGHT + 2 * CANVAS_TOOLBAR_MARGIN)
        self.info_sidebar.panel.expanded_changed.connect(lambda _expanded: self._apply_info_width())
        vw_layout.addWidget(self.viewer_splitter, 1)

        # 3. 没有图片时的引导：占用整个看图工作区，画布、胶片条与照片信息栏让出位置
        self.viewer_empty = EmptyState("photo", self.viewer_workspace,
                                       actions=("open_file", "open_folder"), drop_target=True,
                                       on_canvas=True)
        self.viewer_empty.action_triggered.connect(lambda name: self.window_actions.action(name).trigger())
        vw_layout.addWidget(self.viewer_empty, 1)
        self._retranslate_viewer_empty()
        self.workspace_stack.addWidget(self.viewer_workspace)

        self._studio_placeholder = QWidget(self.workspace_stack)
        placeholder_layout = QVBoxLayout(self._studio_placeholder)
        placeholder_layout.setContentsMargins(0, 0, 0, 0)
        self._studio_placeholder_label = QLabel(self._tr("studio.loading", "Loading OPS…"), self._studio_placeholder)
        self._studio_placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addWidget(self._studio_placeholder_label)
        self._studio_loading_bar = QProgressBar(self._studio_placeholder)
        self._studio_loading_bar.setRange(0, 0)
        self._studio_loading_bar.setFixedWidth(180)
        self._studio_loading_bar.setTextVisible(False)
        placeholder_layout.addWidget(self._studio_loading_bar, 0, Qt.AlignmentFlag.AlignCenter)
        opacity = QGraphicsOpacityEffect(self._studio_placeholder_label)
        self._studio_placeholder_label.setGraphicsEffect(opacity)
        self._studio_loading_animation = QPropertyAnimation(opacity, b"opacity", self._studio_placeholder_label)
        self._studio_loading_animation.setStartValue(0.45)
        self._studio_loading_animation.setEndValue(1.0)
        self._studio_loading_animation.setDuration(900)
        self._studio_loading_animation.setLoopCount(-1)
        self._studio_loading_animation.start()
        self.workspace_stack.addWidget(self._studio_placeholder)

        # 顶栏下方按条件出现的提示条：看图的加载状态、草稿保存失败
        self._notices = QWidget(self)
        self._notices.setObjectName("windowNotices")
        self._notices.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        notices_layout = QVBoxLayout(self._notices)
        notices_layout.setContentsMargins(px(SPACING_MD), px(SPACING_SM), px(SPACING_MD), px(SPACING_SM))
        notices_layout.setSpacing(px(SPACING_SM))
        self._notice_viewer_load = NoticeBar("warning", parent=self._notices)
        self._notice_viewer_load.action_triggered.connect(self.viewer.retry_load)
        self._notice_draft = NoticeBar("warning", parent=self._notices)
        self._notice_draft.action_triggered.connect(self._draft_runner.retry)
        self._notice_update = NoticeBar("info", parent=self._notices, closable=True)
        for bar in (self._notice_viewer_load, self._notice_draft, self._notice_update):
            bar.hide()
            notices_layout.addWidget(bar)
        self._notices.hide()

        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._notices)
        self._content_stack = QStackedWidget(central)
        self._content_stack.addWidget(self.workspace_stack)
        self._content_stack.addWidget(self._photo_browser.view)
        central_layout.addWidget(self._content_stack, 1)
        self.setCentralWidget(central)

    # ------------------------------------------------------------------
    # 随窗口宽度变化的版式，以及下次打开时恢复的各栏状态
    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_window_layout()

    def _apply_window_layout(self) -> None:
        """按窗口宽度决定右侧栏停靠还是浮层、默认栏宽与胶片条的默认高度。"""
        rules, previous = window_layout(self.width()), self._layout_rules
        self._layout_rules = rules
        panel = self.info_sidebar.panel
        if rules.floating_sidebar != previous.floating_sidebar:
            if rules.floating_sidebar:
                panel.set_expanded(False)
                self._info_dock.attach(self.scroll_area)
            else:
                self._info_dock.detach()
                self.viewer_splitter.addWidget(self.info_sidebar)
                self._restore_info_expanded()
        self._apply_info_width()
        if rules != previous:
            for thumbnails in self._thumbnail_panels():
                thumbnails.set_default_height(rules.filmstrip_height)
            if self.studio_workspace is not None:
                self.studio_workspace.apply_window_layout(rules)

    def _apply_info_width(self) -> None:
        width = sidebar_width(self._panel_prefs.info_width, self._layout_rules.info_width)
        if self._layout_rules.floating_sidebar:
            self._info_dock.set_width(width)
        else:
            set_splitter_width(self.viewer_splitter, self.info_sidebar, width)

    def _on_info_sidebar_dragged(self, *_) -> None:
        """用户拖动了看图的分隔条：记住照片信息栏的宽度，编辑里的照片信息栏用同一个宽度。"""
        if self.info_sidebar.isHidden():
            return
        self._panel_prefs.info_width = self.info_sidebar.width()
        self._push_panel_preferences()

    def _restore_info_expanded(self) -> None:
        """有图片且右侧栏是停靠的时候，照片信息栏回到用户上次的展开状态。"""
        panel = self.info_sidebar.panel
        if (self._panel_prefs.info_expanded and not self._layout_rules.floating_sidebar
                and self._current_workspace_index == 0 and panel.image_path is not None):
            panel.set_expanded(True)

    def _push_panel_preferences(self) -> None:
        if self.studio_workspace is not None:
            prefs = self._panel_prefs
            self.studio_workspace.apply_panel_preferences(prefs.edit_collapsed, prefs.edit_width, prefs.info_width)

    def _on_studio_sidebar_preferences(self, collapsed: bool, edit_width: int, info_width: int) -> None:
        prefs = self._panel_prefs
        prefs.edit_collapsed, prefs.edit_width, prefs.info_width = collapsed, edit_width, info_width
        self._apply_info_width()

    def _save_geometry(self) -> None:
        super()._save_geometry()
        self._panel_prefs.save(QSettings(APP_NAME, "MainWindow"))

    @Slot(str, bool)
    def _on_viewer_load_status(self, key, retry):
        self._viewer_load_status = (key, retry)
        self._sync_window_notices()

    def _sync_window_notices(self) -> None:
        """把看图的加载状态与草稿保存失败投影到顶栏下方的提示条；没有内容时整条收起。"""
        key, retry = self._viewer_load_status
        if self._current_workspace_index != 0:
            key = ""
        retry_text = self._tr("common.retry", "Retry")
        for bar, text, action in (
                (self._notice_viewer_load, self._tr(key) if key else "", retry_text if retry else ""),
                (self._notice_draft, self._tr(self._draft_error_key) if self._draft_error_key else "", retry_text)):
            bar.set_text(text)
            bar.set_action_text(action if text else "")
            bar.setVisible(bool(text))
        state = self._version_runner.state if self._version_runner is not None else None
        self._notice_update.setVisible(bool(state and state.notification))
        self._notice_update.set_text(self._tr("update.available", version=state.version) if state else "")
        self._notice_update.set_action_text(self._tr("update.download"))
        self._notice_update.close_button.setAccessibleName(self._tr("common.close", "Close"))
        self._notices.setVisible(any(not bar.isHidden() for bar in (
            self._notice_viewer_load, self._notice_draft, self._notice_update)))

    def _on_update_state(self, state):
        if not self._closing:
            self._sync_window_notices()

    def _setup_actions(self):
        """创建动作表。动作登记在窗口上，快捷键不依赖菜单或按钮是否存在。"""
        studio = self.run_studio_action
        self.window_actions = WindowActions(self, self.shortcut_manager, self._tr, {
            "open_file": self._open_file,
            "open_folder": self._open_folder,
            "project_open": lambda: studio("open_project"),
            "project_save": lambda: studio("save_project"),
            "project_save_as": lambda: studio("save_project", save_as=True),
            "project_export": lambda: studio("export_project_image"),
            "save_as": self._save_as,
            "set_wallpaper": self._set_as_wallpaper,
            "exit": self.close,
            "undo": lambda: studio("undo"),
            "redo": lambda: studio("redo"),
            "reset_all": lambda: studio("reset_all"),
            "apply_all": lambda: studio("apply_changes"),
            "assistant": self._open_assistant,
            "photo_batch": self._open_photo_batch,
            "photo_export": self._open_photo_export,
            "photo_browser": self._open_photo_browser,
            "photo_calibration": self._open_photo_calibration,
            "photo_library": self._open_photo_library,
            "photo_keep": lambda: self._photo_browser.view.mark("keep"),
            "photo_reject": lambda: self._photo_browser.view.mark("reject"),
            "photo_unmark": lambda: self._photo_browser.view.mark("unmarked"),
            "crop": self._crop_image,
            "grid_crop": self._grid_crop_image,
            "shape_crop": self._open_shape_crop,
            "image_processing": self._open_image_processing,
            "image_processing_alias": self._open_image_processing,
            "export_pdf": self._open_pdf_exporter,
            "bg_remove": self._open_bg_remover,
            "lasso": self._open_lasso_tool,
            "id_photo": self._open_id_photo,
            "color_adjust": self._open_color_adjust,
            "ai_enhance": self._open_ai_enhance,
            "beauty": self._open_beauty,
            "rotate_cw": self._rotate_90,
            "rotate_180": self._rotate_180,
            "rotate_ccw": self._rotate_270,
            "flip_horizontal": self._flip_horizontal,
            "flip_vertical": self._flip_vertical,
            "zoom_in": self._zoom_in,
            "zoom_out": self._zoom_out,
            "zoom_100": self._zoom_100,
            "zoom_fit": self._zoom_fit,
            "next_image": self._next_image,
            "previous_image": self._prev_image,
            "preferences": self._show_settings,
            "clear_ratings": self._clear_all_ratings,
            "about": lambda: self._show_settings("about"),
        })

    def _setup_top_bar(self):
        action = self.window_actions.action
        self.top_bar = TopBar(TopBarActions(
            open_menu=(action("open_file"), action("open_folder")),
            editor_buttons=((action("project_open"), "folder-open"), (action("project_save"), "device-floppy"), None,
                            (action("undo"), "arrow-back-up"), (action("redo"), "arrow-forward-up")),
            export_default=(action("save_as"), action("project_export")),
            export_menus=((action("save_as"), action("photo_export"), action("image_processing"), action("export_pdf"), None,
                           action("set_wallpaper")),
                          (action("project_export"), action("photo_export"), action("apply_all"), None, action("project_save_as"))),
            more_menu=(action("photo_browser"), action("photo_library"), action("photo_batch"), action("photo_calibration"), None, action("preferences"), None, action("clear_ratings"), action("reset_all"), None,
                       action("about")),
        ), self._tr, self)
        self.top_bar.workspace_requested.connect(self._on_workspace_index_changed)
        self.top_bar.photo_info_toggled.connect(self._set_photo_info_visible)
        self.top_bar.minimize_requested.connect(self.showMinimized)
        self.top_bar.maximize_requested.connect(self.toggle_maximized)
        self.top_bar.close_requested.connect(self.close)
        self.setMenuWidget(self.top_bar)
        self._refresh_editor_actions()

    def _setup_title_bar(self):
        """顶栏兼作标题栏：系统标题栏的位置交给顶栏，窗口按钮在它的最右侧。不支持时保持系统标题栏。"""
        merged = self.install_window_chrome(self._window_region_at, self.top_bar.set_maximize_state)
        self.top_bar.set_window_controls_visible(merged)
        self.top_bar.set_window_maximized(self.isMaximized())
        self._update_top_bar_title()

    def _window_region_at(self, position: QPoint) -> str:
        return self.top_bar.region_at(self.top_bar.mapFrom(self, position))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and getattr(self, "top_bar", None) is not None:
            self.top_bar.set_window_maximized(self.isMaximized())

    def _setup_canvas_toolbar(self):
        """底部工具条与轻提示：贴在当前工作区的画布下沿，两个工作区共用一份。"""
        action = self.window_actions.action
        self.canvas_toolbar = CanvasToolbar(CanvasToolbarActions(
            zoom_out=action("zoom_out"), zoom_in=action("zoom_in"),
            zoom_fit=action("zoom_fit"), zoom_actual=action("zoom_100"),
            rotations=(action("rotate_cw"), action("rotate_ccw"), action("rotate_180")),
            flips=(action("flip_horizontal"), action("flip_vertical")),
        ), self._tr, self.scroll_area)
        self.canvas_toolbar.hide()
        self.canvas_toolbar.preview_mode_selected.connect(self._set_canvas_preview_mode)
        self.canvas_toolbar.zoom_percent_edited.connect(lambda percent: self.active_viewer.set_zoom_percent(percent))
        self.canvas_toolbar.rating_edited.connect(self._on_toolbar_rating)
        self.canvas_toolbar.filmstrip_toggled.connect(self._on_toolbar_filmstrip)
        self.canvas_toolbar.compare_pressed.connect(lambda: self._on_toolbar_compare(True))
        self.canvas_toolbar.compare_released.connect(lambda: self._on_toolbar_compare(False))
        self._toolbar_dock = BottomDock(self.canvas_toolbar, CANVAS_TOOLBAR_MARGIN)
        self.toast = Toast(self.scroll_area)
        self._toast_dock = BottomDock(self.toast, 2 * CANVAS_TOOLBAR_MARGIN + CANVAS_TOOLBAR_HEIGHT)
        self.viewer.set_bottom_inset(CANVAS_TOOLBAR_HEIGHT + CANVAS_TOOLBAR_MARGIN)
        self.thumbnail_panel.collapsed_changed.connect(lambda _collapsed: self._sync_canvas_toolbar())
        self.thumbnail_panel.set_navigation_actions(action("previous_image"), action("next_image"))
        self.thumbnail_panel.rating_filter_requested.connect(self.set_rating_filter)
        self._sync_canvas_toolbar()

    def _canvas_host(self):
        """当前工作区里承载画布的滚动区；编辑工作区尚未就绪时没有。"""
        if self._current_workspace_index == EDITOR:
            return self.studio_workspace.scroll_area if self.studio_workspace is not None else None
        return self.scroll_area if self.manager.current_path is not None else None

    def _sync_canvas_toolbar(self):
        """把当前画布的倍率、评分与胶片条状态投影到底部工具条，并把它贴到当前画布上。"""
        toolbar = self.canvas_toolbar
        if toolbar is None:
            return
        host = self._canvas_host()
        path = self.manager.current_path
        self._toast_dock.attach(host if host is not None else self.workspace_stack)
        if host is None or path is None:
            toolbar.hide()
            return
        from ..core.rating_store import get_rating_store
        viewer = self.active_viewer
        toolbar.set_mode(self._current_workspace_index)
        toolbar.set_preview_available_width(host.viewport().width())
        toolbar.set_zoom_state(viewer.get_zoom_percent(), viewer.is_fit_mode())
        detail_state, detail_error_key = (self.studio_workspace.detail_state()
                                        if self._current_workspace_index == EDITOR else ("", ""))
        toolbar.set_detail_state(detail_state, detail_error_key)
        toolbar.set_preview_state(self.studio_workspace.canvas_preview_state()
            if self._current_workspace_index == EDITOR and self.studio_workspace is not None
            else CanvasPreviewState(mode=self._preload_settings.canvas_preview_mode))
        toolbar.set_rating(get_rating_store().get_rating(path))
        toolbar.set_filmstrip_visible(not self.active_thumbnail_panel.is_collapsed())
        self._toolbar_dock.attach(host)
        toolbar.show()

    def _on_toolbar_rating(self, rating: int) -> None:
        if self.manager.current_path:
            from ..core.rating_store import get_rating_store
            get_rating_store().set_rating(self.manager.current_path, rating)

    def _on_toolbar_filmstrip(self, visible: bool) -> None:
        for panel in self._thumbnail_panels():
            panel.set_collapsed(not visible)

    def _on_toolbar_compare(self, pressed: bool) -> None:
        if self._is_studio_active() and self.studio_workspace is not None:
            if pressed:
                self.studio_workspace.start_compare()
            else:
                self.studio_workspace.stop_compare()

    def show_toast(self, text: str, duration_ms: int = DEFAULT_DURATION_MS) -> None:
        """在画布下沿显示一句短暂的消息。"""
        self.toast.show_message(text, duration_ms)
        self._toast_dock.place()

    def _open_assistant(self):
        self.switch_to_studio()
        if not self.manager.current_path:
            return
        if self.studio_workspace is not None:
            self.studio_workspace.show_assistant()
        else:
            self._pending_studio_action = lambda: self.studio_workspace.show_assistant()

    def _refresh_editor_actions(self):
        """把编辑工作区的状态投影到动作表：不在编辑时相关动作不可用，提交进行中全部动作暂停。"""
        studio = self.studio_workspace
        state = studio.action_state() if studio is not None else StudioActionState()
        active = self._current_workspace_index == EDITOR
        for name, enabled in (("undo", state.can_undo), ("redo", state.can_redo),
                              ("project_save", state.can_save), ("project_save_as", state.can_save),
                              ("project_export", state.can_export), ("apply_all", state.can_apply),
                              ("reset_all", state.can_reset_all)):
            self.window_actions.set_enabled(name, active and enabled)
        self.window_actions.set_blocked(state.busy)
        self.top_bar.set_actions_enabled(not state.busy)
        self.canvas_toolbar.setEnabled(not state.busy)
        self.canvas_toolbar.set_compare_state(active and state.can_compare, state.compare_hint)

    def _on_viewer_metadata(self, info) -> None:
        self._viewer_image_info = info
        self._update_top_bar_title()

    def _update_top_bar_title(self):
        """顶栏标题：看图是文件名与尺寸，编辑是项目或图片名与保存状态。"""
        path = self.manager.current_path
        # 顶栏兼作标题栏时，没有图片就显示程序的名称
        name = os.path.basename(path) if path else self.windowTitle() if self.top_bar.has_window_controls() else ""
        detail = ""
        if self._current_workspace_index == EDITOR:
            if self.studio_workspace is not None:
                state = self.studio_workspace.action_state()
                name = state.title or name
                detail = self._tr("topbar.unsaved", "Unsaved") if state.unsaved else ""
        else:
            info = self._viewer_image_info
            if info is not None and path and info.path == path:
                detail = f"{info.width} × {info.height}"
        self.top_bar.set_title(name, detail)

    @property
    def active_viewer(self) -> ImageViewer:
        if getattr(self, "_current_workspace_index", 0) == 1 and self.studio_workspace is not None:
            return self.studio_workspace.viewer
        return self.viewer

    @Slot()
    def _zoom_in(self):
        self.active_viewer.zoom_in()

    @Slot()
    def _zoom_out(self):
        self.active_viewer.zoom_out()

    @Slot()
    def _zoom_100(self):
        self.active_viewer.set_zoom_percent(100)

    @Slot()
    def _zoom_fit(self):
        self.active_viewer.zoom_fit()

    @Slot()
    def _open_file(self):
        """执行打开文件对话框操作 (多选)"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, self._tr("dialog.open_file", "Open Images"), "", SUPPORTED_FORMATS_FILTER)
        if paths:
            self._begin_browse(paths=paths, append=bool(self.manager.image_list), origin="file_dialog")

    @Slot()
    def _open_folder(self):
        """打开文件夹选择对话框"""
        from PySide6.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(self, self._tr("dialog.open_folder", "Select Folder"))
        if folder:
            self._begin_browse(directory=folder, origin="folder_dialog")

    def _begin_browse(self, *, paths=(), directory=None, append=True, target=None, origin="files"):
        self._library_runner.service.library_view = False
        self._library_runner.service.active_collection = ""
        self._library_runner.service.invalidate()
        if self._closing:
            return
        self._browse_request = self._browse_runner.submit(
            list_version=self.manager.list_version, selection_revision=self.manager.current_revision,
            paths=paths, directory=directory, append=append, target=target, origin=origin)
        if self._browse_progress is None:
            self._browse_progress = ProgressDialog(self, title="", modal=False)
            self._browse_progress.cancelled.connect(self._cancel_browse)
        self._translate_browse_progress()
        self._browse_progress.show_after(400)

    def _translate_browse_progress(self):
        self._browse_progress.set_title(self._tr("browse.title", "Open Images"))
        self._browse_progress.set_text(self._tr("browse.preparing", "Preparing image list…"))
        self._browse_progress.set_cancel_text(self._tr("browse.cancel", "Cancel"))

    def _cancel_browse(self):
        self._browse_request = None
        self._browse_runner.cancel()
        if self._browse_progress is not None:
            self._browse_progress.finish()

    def open_image_path(self, path, *, load_siblings=True, origin="image"):
        if self._closing:
            return False
        if self._library_runner.path_blocked(path):
            if not self._library_runner.service.loaded:
                self._library_deferred_open = (path, load_siblings, origin)
                return True
            self.show_toast(self._tr("library.recovery_required"), 6000)
            return False
        if origin != "library":
            self._library_runner.service.library_view = False
            self._library_runner.service.active_collection = ""
            self._library_runner.service.invalidate()
        path = os.path.normpath(path)
        if not os.path.isfile(path):
            self.show_toast(self._tr("error.source.not_found"), 5000)
            return False
        self._cancel_browse()
        self.manager.publish_images([path], preferred_path=path)
        self._library_runner.resolve_paths((path,))
        self._load_current()
        if load_siblings:
            self._begin_browse(directory=os.path.dirname(path) or ".", append=False,
                               target=path, origin=origin)
        self._load_images_into_panels()
        return True

    @Slot(object, object)
    def _on_browse_finished(self, request, result):
        if self._closing or self._browse_request is not request:
            return
        self._browse_request = None
        if self._browse_progress is not None:
            self._browse_progress.finish()
        if request.list_version != self.manager.list_version:
            logger.debug("浏览列表拒收 request=%s expected=%s current=%s",
                         request.request_id, request.list_version, self.manager.list_version)
            return
        if not result.success:
            self.show_toast(self._tr(result.error.message_key), 5000)
            return
        listing = result.data
        if not listing.paths:
            self.show_toast(self._tr("browse.empty", "No images found."), 5000)
            return
        previous = self.manager.current_path
        preferred = None
        if request.target and request.selection_revision == self.manager.current_revision:
            preferred = request.target
        elif previous not in listing.paths and not request.append:
            preferred = listing.preferred_path
        elif previous is None:
            preferred = listing.preferred_path
        if not self.manager.publish_images(listing.paths, append=request.append, preferred_path=preferred,
                                           expected_version=request.list_version):
            return
        self._library_runner.install_paths(listing.path_keys, replace=not request.append)
        if self.manager.current_path != previous:
            self._load_current()
        self._load_images_into_panels()
        self._sync_canvas_toolbar()
        self._update_menu_actions()
        self._refresh_studio_prefetch()
        if self._photo_browser.view.preferred_grid and self._content_stack.currentWidget() is self.workspace_stack:
            QTimer.singleShot(0, self, self._open_photo_browser)

    @Slot()
    def _save_as(self):
        from pathlib import Path
        from .services.background_operation import run_background, batch_message
        paths = tuple(self.active_thumbnail_panel.get_selected_paths() or ([self.manager.current_path] if self.manager.current_path else []))
        if not paths:
            return
        image = None
        if (len(paths) == 1 and paths[0] == self.manager.current_path
                and self._current_pil_revision == self.manager.current_revision):
            image = self._current_pil_image
        jobs = []
        overwrite = len(paths) == 1
        if overwrite:
            target, _ = QFileDialog.getSaveFileName(self, self._tr("dialog.save_as"), paths[0], SUPPORTED_OUTPUT_FORMATS_FILTER)
            if not target:
                return
            jobs.append((paths[0], target))
        else:
            directory = QFileDialog.getExistingDirectory(self, self._tr("dialog.save_selected_to"))
            if not directory:
                return
            for source in paths:
                suffix = Path(source).suffix.lower()
                if suffix not in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"):
                    suffix = ".png"
                jobs.append((source, str(Path(directory) / (Path(source).stem + suffix))))
        service = self._output_service
        def save(cancellation):
            if image is not None:
                path = jobs[0][1]
                return service.batch([(path, lambda: service.save(
                    image, path, overwrite=True, cancellation=cancellation))], cancellation)
            return service.convert_many(jobs, overwrite=overwrite, cancellation=cancellation)
        try:
            result = run_background(self, save)
            show_info(self, self._tr("dialog.save_as"), batch_message(result))
        except Exception as error:
            show_error(self, self._tr("dialog.save_as"), self._tr(str(error), str(error)))


    @Slot()
    def _set_as_wallpaper(self):
        if not self.manager.current_path:
            return

        image_path = self.manager.current_path
        dialog = WallpaperDialog(image_path, self)
        if dialog.exec() != WallpaperDialog.DialogCode.Accepted:
            return
        position = dialog.selected_position()
        service = self._wallpaper_service
        from .services.background_operation import run_background
        from ..application.result_types import ErrorCode
        try:
            result = run_background(
                self, lambda cancellation: service.apply(image_path, position, cancellation=cancellation),
                label=self._tr("dialog.wallpaper.applying", "Setting wallpaper..."),
            )
        except Exception:
            logger.exception("壁纸后台任务失败：%s", image_path)
            show_warning(
                self, self._tr("dialog.error.title", "Error"),
                self._tr("dialog.error.wallpaper_failed", "Failed to set wallpaper."),
            )
            return
        if result.success:
            self.show_toast(self._tr("message.wallpaper.set_success", "Wallpaper set successfully"))
        elif result.error.code != ErrorCode.CANCELLED:
            show_warning(
                self, self._tr("dialog.error.title", "Error"),
                self._tr(
                    "dialog.error.wallpaper_failed_with_error", "Failed to set wallpaper:\n{error}",
                    error=self._tr(result.error.message_key),
                ),
            )


    @Slot()
    def _next_image(self):
        path = self.manager.next_image()
        if path:
            self._load_current()

    @Slot()
    def _prev_image(self):
        path = self.manager.prev_image()
        if path:
            self._load_current()

    @Slot()
    def _crop_image(self):
        """打开/切换右侧矩形裁剪侧栏。"""
        self.toggle_tool("crop")

    def _apply_transform(
        self, transform_func, label, display_rotate=0,
        display_flip_h=False, display_flip_v=False,
    ):
        """通用变换处理：即时显示更新 + 后台保存。

        即时更新通过 QTransform 作用于当前 QPixmap（无 I/O），
        PIL 变换在主线程中执行后缓存，文件写入在后台线程异步完成。
        """
        source_path = self.manager.current_path
        source_revision = self.manager.current_revision
        if not source_path or self.viewer.is_loading:
            return

        if (
            self._current_pil_image is not None
            and self._current_pil_revision == source_revision
        ):
            source = self._current_pil_image
        else:
            try:
                source = load_image(source_path)
            except Exception:
                logger.exception("Failed to open image for transform: %s", source_path)
                return

        try:
            result = transform_func(source)
        except Exception:
            logger.exception(
                "Failed to apply transform %s to %s",
                getattr(transform_func, "__name__", str(transform_func)),
                source_path,
            )
            return

        if result is None:
            return

        self._current_pil_image = result
        self._current_pil_revision = source_revision
        self._save_generation += 1
        gen = self._save_generation

        if display_rotate:
            self.viewer.rotate_display(display_rotate)
        elif display_flip_h:
            self.viewer.flip_display(horizontal=True)
        elif display_flip_v:
            self.viewer.flip_display(horizontal=False)

        self.show_toast(
            self._tr(
                "status.transform.result",
                "{label}: {width} × {height} px",
                label=label,
                width=result.width,
                height=result.height,
            )
        )
        try:
            self._start_transform_save(result, source_path, source_revision, gen)
        except OSError as exc:
            logger.exception("Failed to start transform save for %s", source_path)
            self.show_toast(self._tr("status.save_failed", "Save failed: {error}", error=str(exc)), 5000)

    def _invalidate_transform_save(self) -> None:
        self._save_generation += 1
        self._current_pil_image = None
        self._current_pil_revision = None

    def _start_transform_save(
        self,
        pil_image: Image.Image,
        source_path: str,
        source_revision: int,
        generation: int,
    ) -> None:
        from ..application.storage_workspaces import ManagedWorkspace
        workspace = ManagedWorkspace("transforms")
        tmp_dir = Path(workspace.name)
        self._temp_workspaces[str(tmp_dir)] = workspace
        self._temp_paths.add(str(tmp_dir))
        ext = Path(source_path).suffix.lower()
        if ext not in (".png", ".webp", ".bmp", ".tiff", ".tif"):
            ext = ".png"
        request = TransformSaveRequest(
            source_path=source_path,
            source_revision=source_revision,
            generation=generation,
            target_path=tmp_dir / f"transformed{ext}",
        )
        worker = TransformSaveWorker(pil_image, request, self._output_service, workspace=workspace)
        worker.signals.finished.connect(self._on_transform_saved)
        QThreadPool.globalInstance().start(worker)

    def _reclaim_orphaned_temp(self, old_path: str | None) -> None:
        """若 old_path 是被本窗口追踪的临时产物且已脱离 image_list，立即回收。

        用于 transform/crop/lasso 三类操作的链式替换：当新临时文件成为 current_path 后，
        上一代临时文件就再无引用，应当立刻释放磁盘，不必等到关窗。
        """
        if not old_path or old_path in self.manager.image_list:
            return
        old_dir = os.path.dirname(old_path)
        if old_dir and old_dir in self._temp_paths:
            self._safe_remove_temp(old_dir)
        elif old_path in self._temp_paths:
            self._safe_remove_temp(old_path)

        if hasattr(self, "_edit_state_store") and self._edit_state_store:
            self._edit_state_store.clear_path(old_path)

    def _safe_remove_temp(self, path: str) -> None:
        """安全删除单个临时文件或目录（忽略错误）。"""
        if not path or not os.path.exists(path):
            return
        try:
            if path in self._temp_workspaces:
                self._temp_workspaces.pop(path).cleanup()
            elif os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                import shutil
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            logger.debug("Failed to remove temp path: %s", path, exc_info=True)
        finally:
            self._temp_paths.discard(path)

    @Slot(object, str)
    def _on_transform_saved(self, request: TransformSaveRequest, error: str) -> None:
        tmp_dir = str(request.target_path.parent)
        if (
            request.generation != self._save_generation
            or request.source_revision != self.manager.current_revision
            or request.source_path != self.manager.current_path
        ):
            self._safe_remove_temp(tmp_dir)
            return

        if error:
            self._safe_remove_temp(tmp_dir)
            if error.startswith("storage."):
                error = self._tr(error)
            self.show_toast(self._tr("status.save_failed", "Save failed: {error}", error=error), 5000)
            return

        tmp_path = str(request.target_path)
        if not self.manager.replace_current_image(tmp_path):
            self._safe_remove_temp(tmp_dir)
            return
        self._update_image_path_in_panels(request.source_path, tmp_path)
        self._load_current()
        self._reclaim_orphaned_temp(request.source_path)

    @Slot()
    def _rotate_90(self):
        self._apply_transform(
            ImageProcessor.rotate_90,
            self._tr("status.transform.rotate_90_cw", "Rotated 90° CW"),
            display_rotate=-90,
        )

    @Slot()
    def _rotate_180(self):
        self._apply_transform(
            ImageProcessor.rotate_180,
            self._tr("status.transform.rotate_180", "Rotated 180°"),
            display_rotate=180,
        )

    @Slot()
    def _rotate_270(self):
        self._apply_transform(
            ImageProcessor.rotate_270,
            self._tr("status.transform.rotate_90_ccw", "Rotated 90° CCW"),
            display_rotate=90,
        )

    @Slot()
    def _flip_horizontal(self):
        self._apply_transform(
            ImageProcessor.flip_horizontal,
            self._tr("status.transform.flip_h", "Flipped Horizontal"),
            display_flip_h=True,
        )

    @Slot()
    def _flip_vertical(self):
        self._apply_transform(
            ImageProcessor.flip_vertical,
            self._tr("status.transform.flip_v", "Flipped Vertical"),
            display_flip_v=True,
        )

    @Slot()
    def _grid_crop_image(self):
        """打开/切换右侧九宫格裁剪侧栏。"""
        self.toggle_tool("grid_crop")

    @Slot()
    def _open_shape_crop(self):
        """打开/切换右侧几何形状裁剪侧栏。"""
        self.toggle_tool("shape_crop")

    @Slot()
    def _open_image_processing(self):
        selected_paths = self.active_thumbnail_panel.get_selected_paths()
        if not selected_paths:
            selected_paths = (
                [self.manager.current_path] if self.manager.current_path else []
            )
        dialog = ImageProcessingDialog(initial_paths=selected_paths, parent=self,
                                       output_service=self._output_service)
        dialog.exec()

    @Slot()
    def _open_pdf_exporter(self):
        """打开 PDF 导出对话框"""
        selected_paths = self.active_thumbnail_panel.get_selected_paths()
        if not selected_paths:
            selected_paths = (
                [self.manager.current_path] if self.manager.current_path else []
            )
        if not selected_paths:
            return
        dialog = PDFDialog(selected_paths, self)
        dialog.exec()

    @Slot()
    def _open_bg_remover(self):
        """打开/切换右侧背景移除侧栏。"""
        self.toggle_tool("bg_remove")

    @Slot()
    def _open_lasso_tool(self):
        """打开/切换右侧套索抠图侧栏。"""
        self.toggle_tool("lasso")

    # ------------------------------------------------------------------
    # Tool Sidebar Lifecycle Management
    # ------------------------------------------------------------------
    def activate_tool(self, tool_id: str) -> None:
        """激活并展开右侧工具参数面板。"""
        if self.studio_workspace is None:
            self._pending_studio_tool_id = tool_id
            self._on_workspace_index_changed(1)
            return
        tool_id = resolve_tool_id(tool_id)
        available = self.studio_workspace.core.tools.check_enabled(tool_id)
        if not available.success:
            self.show_toast(self._tr(available.error.message_key), 5000)
            return
        if not self.manager.current_path:
            self.show_toast(self._tr("status.open_image_first", "Please open an image first"))
            return

        if self._active_tool_id == tool_id:
            return

        # 若此前有其它工具处于激活态，先清理前一个（保留暂存）
        if self._active_tool_id is not None:
            self.deactivate_tool(discard_stash=False)

        # 准备或获取面板实例
        panel = self._tool_panels.get(tool_id)
        if panel is None:
            panel = self._create_tool_panel(tool_id)
            if panel is None:
                logger.error("Unknown tool id: %s", tool_id)
                return
            self._tool_panels[tool_id] = panel
            self.tool_sidebar.register_panel(tool_id, panel)
            panel.preview_ready.connect(self._on_tool_preview_ready)
            if hasattr(panel, "sampling_requested"):
                panel.sampling_requested.connect(lambda active, pnl=panel: self._on_tool_sampling_requested(pnl, active))
                panel.sampling_picked.connect(lambda x, y, pnl=panel: self._on_tool_sampling_picked(pnl, x, y))
                panel.sampled_preview_ready.connect(self._on_tool_sampled_preview)
            panel.request_status.connect(lambda text: self.show_toast(text, 5000))
            panel.model_manager_requested.connect(self._show_model_settings)
        else:
            panel.set_image_path(self.manager.current_path, force=True)

        self.tool_sidebar.switch_to_panel(tool_id)
        has_stash = self._edit_state_store.has_tool_stash(self.manager.current_path, tool_id)
        self.tool_sidebar.set_stash_banner_visible(has_stash)

        # 空间交互工具（B类）自动挂载画布覆盖层
        if hasattr(panel, "overlay") and panel.overlay:
            self.viewer.install_overlay(panel.overlay)
        else:
            self.viewer.uninstall_overlay()

        self._active_tool_id = tool_id
        self.tool_sidebar.show()

    def prewarm_studio(self) -> None:
        self._prewarm_requested = True
        self._prewarm_timer.start(0)

    def _foreground_preparation_changed(self, *_):
        if self._prewarm_requested and not self._closing:
            self._prewarm_timer.start(0)

    def _try_prewarm_studio(self):
        if not self._prewarm_requested or self._closing:
            return
        if self.studio_workspace is not None or self._studio_init_started:
            self._prewarm_requested = False
            return
        if (self._browse_runner.is_busy or self.viewer.is_loading
                or any(panel.is_building for panel in self._thumbnail_panels())):
            return
        self._prewarm_requested = False
        self._start_studio_initialization(silent=True)

    def _start_studio_initialization(self, *, silent: bool = False) -> None:
        if self._studio_init_started or self.studio_workspace is not None:
            return
        self._studio_init_started = True
        self._studio_init_error = None
        if not silent and self._studio_placeholder_label is not None:
            self._studio_placeholder_label.setText(self._tr("studio.loading", "Loading OPS…"))
        worker = _StudioInitWorker(self._output_service.writer, self._image_sources, self._inference,
                                   self._runtime_service.library)
        worker.signals.ready.connect(self._on_studio_core_ready)
        worker.signals.failed.connect(self._on_studio_init_failed)
        self._studio_init_worker = worker
        QThreadPool.globalInstance().start(worker)

    @Slot(object)
    def _on_studio_core_ready(self, core) -> None:
        if self._closing or self.studio_workspace is not None:
            core.tools.close()
            return
        if not self._studio_mount_scheduled:
            self._studio_mount_scheduled = True
            QTimer.singleShot(0, self, lambda: self._on_studio_core_ready(core))
            return
        started = monotonic()
        self._studio_mount_scheduled = False
        self._studio_init_started = False
        from ..application.panel_render_service import PanelRenderService
        from .services.preview_task_runner import PreviewTaskRunner
        from .studio_workspace import StudioWorkspace

        tool_settings = QSettings(APP_NAME, "Tools")
        disabled_tools = tool_settings.value("disabled", [])
        if isinstance(disabled_tools, str):
            disabled_tools = [item for item in disabled_tools.split(",") if item]
        core.restore_disabled_tools(disabled_tools or [])
        self._panel_services = {
            "image_source_service": self._image_sources,
            "preview_runner": PreviewTaskRunner(),
            "render_service": PanelRenderService(core.tools),
            "output_service": self._output_service,
        }
        self.destroyed.connect(self._panel_services["preview_runner"].shutdown)
        self._studio_preview_cache = StudioPreviewCache(self._image_sources.resources)
        self.destroyed.connect(self._studio_preview_cache.close)
        from ..application.edit_sessions import EditSessionService
        from .panels.registry import PANEL_TYPES, resolve_tool_id
        self._edit_sessions = EditSessionService(core, self._edit_state_store.drafts,
                                                supported_tools=PANEL_TYPES, resolve_tool=resolve_tool_id)
        studio = StudioWorkspace(self, apply_service=self._apply_service,
                                 core=core, panel_services=self._panel_services,
                                 preview_cache=self._studio_preview_cache,
                                 source_load_request=self._start_studio_source_load,
                                 source_load_cancel=self._cancel_studio_source_load,
                                 browse_source_request=lambda path: self.open_image_path(path, origin="project"),
                                 browse_source_cancel=self._cancel_browse,
                                 raw_display_mode=self._image_display_settings.raw_mode,
                                 non_raw_display_mode=self._image_display_settings.non_raw_mode,
                                 assistant_runner=self._get_assistant_runner(),
                                 edit_sessions=self._edit_sessions,
                                 canvas_preview_mode=self._preload_settings.canvas_preview_mode,
                                 canvas_scale_runner=self._canvas_scale_runner)
        self.studio_workspace = studio
        from ..application.photo_batch import PhotoBatchService
        from .services.photo_batch_runner import PhotoBatchRunner
        self._batch_runner = PhotoBatchRunner(PhotoBatchService(self._edit_sessions, agent=studio.agent), self._draft_runner, self)
        self._batch_runner.item_changed.connect(self._on_batch_photo_changed)
        self._batch_runner.display_ready.connect(self._on_batch_display_ready)
        self._batch_runner.idle.connect(self._on_batch_idle)
        self.destroyed.connect(self._batch_runner.shutdown)
        from ..application.export_store import ExportStore
        from ..application.photo_exports import PhotoExportService
        from .services.photo_export_runner import PhotoExportRunner
        self._export_runner = PhotoExportRunner(PhotoExportService(self._edit_sessions, self._batch_runner.service,
                                                                   ExportStore()), self)
        self._export_runner.idle.connect(self._on_export_idle)
        self._export_runner.changed.connect(self._photo_browser.delivery_changed)
        self.destroyed.connect(self._export_runner.shutdown)
        self._get_assistant_runner().set_batch_services(self._batch_runner, self._capture_batch_context, self._export_runner)
        self._photo_browser.attach(self._edit_sessions, self._export_runner.service, self._image_display_settings)
        self._get_assistant_runner().set_browser(self._browser_command)
        from ..application.photo_calibration import PhotoCalibrationService
        from .services.photo_calibration_runner import PhotoCalibrationRunner
        self._calibration_runner = PhotoCalibrationRunner(PhotoCalibrationService(self._photo_browser.service,
            self._batch_runner.service, self._photo_browser.runner.release_fixed), self,
            before_capture=studio.prepare_batch_capture)
        self._calibration_runner.idle.connect(self._on_calibration_idle)
        self.destroyed.connect(self._calibration_runner.shutdown)
        self._get_assistant_runner().set_calibration_services(self._calibration_runner)
        self._library_runner.attach(self._edit_sessions, self._export_runner.service)
        self._get_assistant_runner().set_library_services(self._library_runner)
        self._export_runner.changed.connect(lambda _identity: self._library_runner.refresh_exports())
        studio.resource_management_requested.connect(lambda tool_id: self._show_settings("tools", tool_id=tool_id))
        studio.model_management_requested.connect(self._show_model_settings)
        studio.toast_requested.connect(self.show_toast)
        studio.apply_window_layout(self._layout_rules)
        self._push_panel_preferences()
        studio.sidebar_preferences_changed.connect(self._on_studio_sidebar_preferences)
        studio.thumbnail_panel.set_default_height(self._layout_rules.filmstrip_height)
        self._connect_photo_info_panel(studio.photo_info_panel)
        if self._current_workspace_index == 1:
            studio.photo_info_panel.set_path(self.manager.current_path)
            studio.photo_info_panel.set_active(True)
        self._sync_photo_info_action()
        from ..application.studio_source_service import StudioSourceService
        from .services.studio_source_runner import StudioSourceRunner
        self._studio_source_runner = StudioSourceRunner(StudioSourceService(core), self._studio_preview_cache, self)
        self._studio_source_runner.finished.connect(self._on_studio_source_finished)
        self._studio_source_runner.preview_ready.connect(self._on_studio_cached_preview)
        self._studio_source_runner.prefetch_failed.connect(self._on_studio_prefetch_failed)
        studio.preview_started.connect(self._studio_source_runner.pause_prefetch)
        studio.preview_finished.connect(self._refresh_studio_prefetch)
        self.destroyed.connect(self._studio_source_runner.shutdown)
        self.tool_sidebar = studio.tool_sidebar
        studio.viewer.set_zoom_changed_callback(self._sync_canvas_toolbar)
        studio.viewer.set_bottom_inset(CANVAS_TOOLBAR_HEIGHT + CANVAS_TOOLBAR_MARGIN)
        studio.thumbnail_panel.collapsed_changed.connect(lambda _collapsed: self._sync_canvas_toolbar())
        studio.thumbnail_panel.set_navigation_actions(self.window_actions.action("previous_image"),
                                                      self.window_actions.action("next_image"))
        studio.thumbnail_panel.rating_filter_requested.connect(self.set_rating_filter)
        studio_thumbs = studio.thumbnail_panel
        self._photo_browser.add_panel(studio_thumbs)
        studio_thumbs.browser_requested.connect(self._open_photo_browser)
        studio_thumbs.set_edit_state_store(self._edit_state_store)
        studio_thumbs.image_selected.connect(self._on_thumbnail_selected)
        studio_thumbs.batch_requested.connect(self._open_photo_batch)
        studio_thumbs.order_changed.connect(self._on_thumbnail_reordered)
        studio_thumbs.building_changed.connect(self._foreground_preparation_changed)
        studio_thumbs.listing_ready.connect(self._on_thumbnail_listing_ready)
        studio_thumbs.apply_rating_filter(self.manager.get_rating_filter())
        self.tool_sidebar.close_requested.connect(self._on_tool_close_requested)
        self.tool_sidebar.reset_requested.connect(self._on_tool_reset_requested)
        self.tool_sidebar.discard_stash_requested.connect(self._on_tool_discard_stash_requested)
        studio.applied.connect(self._on_studio_applied)
        studio.back_requested.connect(self._on_studio_back_requested)

        self.workspace_stack.removeWidget(self._studio_placeholder)
        self._studio_placeholder.deleteLater()
        self._studio_placeholder = None
        self.workspace_stack.insertWidget(1, studio)
        if self._current_workspace_index == 1:
            self.workspace_stack.setCurrentIndex(1)
        self._studio_init_worker = None
        QTimer.singleShot(0, studio_thumbs, lambda: studio_thumbs.load_images(
            self.manager.image_list, self.manager.current_path, version=self.manager.list_version
        ))
        if self.manager.current_path:
            QTimer.singleShot(0, studio_thumbs, lambda: studio_thumbs.select_image(self.manager.current_path))
        if self._current_workspace_index == 1 and self.manager.current_path:
            self._start_studio_source_load(self.manager.current_path)
        pending_tool = self._pending_studio_tool_id
        if pending_tool and not self.manager.current_path:
            self._pending_studio_tool_id = None
        pending_action = self._pending_studio_action
        self._pending_studio_action = None
        studio.action_state_changed.connect(self._on_studio_action_state)
        studio.detail_state_changed.connect(self._on_studio_detail_state)
        studio.canvas_preview_changed.connect(lambda state: self._sync_canvas_toolbar())
        self._update_menu_actions()
        self._on_studio_action_state()
        logger.debug("OPS 工作区挂载 duration_ms=%.1f", (monotonic() - started) * 1000)
        if pending_action:
            pending_action()

    def _cancel_studio_source_load(self):
        if self._studio_source_runner is not None:
            self._studio_source_runner.cancel()

    def _start_studio_source_load(self, path: str) -> None:
        if self._library_path_blocked(path):
            return
        if self.studio_workspace is None or self.studio_workspace._commit_busy:
            return
        if not self._draft_runner.loaded:
            return
        for panel in self._thumbnail_panels():
            panel.set_loading_paused(True)
        self._studio_source_runner.pause_prefetch()
        generation = self.studio_workspace.prepare_image_load(path)
        try:
            initial_workflow = self.studio_workspace.initial_workflow(path)
            preview_workflow = self.studio_workspace.preview_workflow(path)
            preview_key = self.studio_workspace.preview_key(path, workflow=preview_workflow)
        except (CoreOperationError, ValueError) as error:
            self._studio_source_runner.cancel()
            logger.warning("OPS initial color failed: image=%r error=%s", os.path.basename(path), error)
            result = CoreResult.from_exception(error, code=ErrorCode.PARAMETER_INVALID,
                                               message_key="error.workflow.params")
            self.studio_workspace.show_image_load_error(result.error)
            for panel in self._thumbnail_panels():
                panel.set_loading_paused(False)
            return
        self._studio_source_runner.submit(path, self.studio_workspace.cached_session_id(path),
                                            preview_key=preview_key, initial_workflow=initial_workflow,
                                            preview_workflow=preview_workflow)
        snapshot, self._studio_loading_snapshot = self._studio_loading_snapshot, None
        if snapshot is not None and os.path.normcase(snapshot.path) == os.path.normcase(os.path.normpath(path)):
            try:
                matches = self._image_sources.file_stamp(path) == snapshot.stamp
            except OSError:
                matches = False
            if matches and viewer_matches_baseline(preview_workflow,
                    is_raw=self._image_sources.supports_quick_preview(path), raw_display_mode=snapshot.raw_display_mode,
                    non_raw_display_mode=snapshot.non_raw_display_mode):
                self.studio_workspace.show_loading_placeholder(snapshot, generation=generation)

    @Slot(object, object, object)
    def _on_studio_cached_preview(self, request, image, reference_size):
        studio = self.studio_workspace
        if (studio is not None and self._current_workspace_index == 1
                and not request.cancellation.is_set()
                and request.path == os.path.normpath(self.manager.current_path or "")
                and request.preview_key == studio.preview_key(request.path)):
            studio.show_loading_preview(image, reference_size)

    @Slot()
    def _refresh_studio_prefetch(self):
        if self._is_studio_active() and self.studio_workspace.can_prefetch():
            for thumbnails in self._thumbnail_panels():
                thumbnails.set_loading_paused(False)
            panel = self.studio_workspace.thumbnail_panel
            if self.manager.get_rating_filter() != "all" and panel.has_pending_items:
                return
            paths = self._preload_settings.targets(self.manager.get_filtered_images(), self.manager.current_path)
            targets = []
            for path in paths:
                try:
                    targets.append(StudioPreviewTarget(os.path.normpath(path), self.studio_workspace.initial_workflow(path)))
                except (CoreOperationError, ValueError) as error:
                    logger.warning("OPS prefetch color failed: image=%r error=%s", os.path.basename(path), error)
            self._studio_source_runner.prefetch(targets)

    @Slot()
    def _on_studio_prefetch_failed(self):
        self.show_toast(self._tr("studio.prefetch_failed", "Unable to preload an image."), 5000)

    @Slot(object, object, object)
    def _on_studio_source_finished(self, request, result, qimage) -> None:
        studio = self.studio_workspace
        if (studio is None or self._current_workspace_index != 1
                or request.path != os.path.normpath(self.manager.current_path or "")):
            return
        if not result.success:
            studio.show_image_load_error(result.error)
            for panel in self._thumbnail_panels():
                panel.set_loading_paused(False)
            return
        prepared = result.data
        if studio.cached_session_id(request.path) != request.session_id:
            self._start_studio_source_load(request.path)
            return
        current = studio.core.get_session(prepared.project.session_id)
        if not current.success or current.data.state_version != prepared.project.state_version:
            self._start_studio_source_load(request.path)
            return
        try:
            studio.load_prepared_session(request.path, prepared.project.active_source,
                                         prepared.project, qimage=qimage,
                                         preview_key=request.preview_key)
            if studio.session is not None and studio.session.core_id == prepared.project.session_id:
                studio.hold_preview_source(prepared.proxy_lease)
                prepared.proxy_lease = None
        except Exception as error:
            logger.exception("OPS source attachment failed")
            studio.show_image_load_error(error)
            for panel in self._thumbnail_panels():
                panel.set_loading_paused(False)
            return
        finally:
            if studio.cached_session_id(request.path) == prepared.project.session_id:
                prepared.owns_session = False
        pending_tool = self._pending_studio_tool_id
        self._pending_studio_tool_id = None
        if pending_tool:
            studio.switch_tool(pending_tool)
            if studio.core.tools.check_enabled(resolve_tool_id(pending_tool)).success:
                self._active_tool_id = studio.active_tool_id

    def run_studio_action(self, method_name: str, *args, **kwargs) -> None:
        if self.studio_workspace is None:
            self._pending_studio_action = lambda: getattr(self.studio_workspace, method_name)(*args, **kwargs)
            self._start_studio_initialization()
            return
        getattr(self.studio_workspace, method_name)(*args, **kwargs)

    def _capture_batch_context(self):
        if self._batch_runner is None:
            raise ValueError("batch.unavailable")
        if not self._draft_runner.loaded:
            raise ValueError("batch.drafts_loading")
        selection = self.active_thumbnail_panel.selection_snapshot(self.manager.current_path)
        studio = self.studio_workspace
        if (studio.session is not None and selection.active
                and self._edit_sessions.key(studio.session.image_path) == self._edit_sessions.key(selection.active)):
            if not studio.prepare_batch_capture():
                raise ValueError("batch.parameters_pending")
        session = self._edit_sessions.get(selection.active)
        if session is None and selection.active:
            from .services.background_operation import run_background
            core = studio.core
            bound = run_background(self, lambda cancellation: core.bind_source(selection.active,
                is_cancelled=cancellation.is_cancelled))
            if not bound.success:
                raise ValueError(bound.error.message_key)
            try:
                initial = self._edit_sessions.initial_workflow(selection.active,
                    raw_display_mode=self._image_display_settings.raw_mode,
                    non_raw_display_mode=self._image_display_settings.non_raw_mode)
                created = core.create_bound_session(bound.data, initial_workflow=initial)
                if not created.success:
                    raise ValueError(created.error.message_key)
                adopted = self._edit_sessions.adopt(created.data, path=selection.active)
                if not adopted.success:
                    core.close_session(created.data.session_id)
                    raise ValueError(adopted.error.message_key)
                session = adopted.data
            finally:
                core.release_source(bound.data.source_id)
        if session is not None:
            session.refresh()
        return self._batch_runner.service.capture(selection, raw_display_mode=self._image_display_settings.raw_mode,
                                                  non_raw_display_mode=self._image_display_settings.non_raw_mode)

    def _batch_context_current(self, scope):
        selection = self.active_thumbnail_panel.selection_snapshot(self.manager.current_path)
        studio = self.studio_workspace
        if (studio.session is not None and selection.active
                and self._edit_sessions.key(studio.session.image_path) == self._edit_sessions.key(selection.active)
                and not studio.prepare_batch_capture()):
            return False
        return self._batch_runner.service.context_current(scope, selection)

    def _open_photo_batch(self):
        if self._batch_runner is None:
            self._pending_studio_action = self._open_photo_batch
            self._start_studio_initialization()
            return
        if self._batch_dialog is None:
            from .dialogs.photo_batch_dialog import PhotoBatchDialog
            self._batch_dialog = PhotoBatchDialog(self._batch_runner, self._capture_batch_context,
                                                  self._open_batch_photo, self, is_current=self._batch_context_current)
        self._batch_dialog.capture_selection()
        self._batch_dialog.show()
        self._batch_dialog.raise_()
        self._batch_dialog.activateWindow()

    def _open_batch_photo(self, path):
        if self.manager.select_image(path):
            if self._current_workspace_index == 0:
                self.switch_to_studio()
            else:
                self._load_current()
        else:
            self.open_image_path(path, load_siblings=False, origin="batch")
            self.switch_to_studio()

    def _open_photo_export(self):
        if self._export_runner is None:
            self._pending_studio_action = self._open_photo_export
            self._start_studio_initialization()
            return
        if self._export_runner.service.loaded:
            self._export_runner.reload()
        if self._export_dialog is None:
            from .dialogs.photo_export_dialog import PhotoExportDialog
            self._export_dialog = PhotoExportDialog(self._export_runner, self._capture_batch_context, self)
        self._export_dialog.capture_selection()
        self._export_dialog.show()
        self._export_dialog.raise_()
        self._export_dialog.activateWindow()

    def _open_photo_browser(self):
        if self._batch_runner is None:
            self._pending_studio_action = self._open_photo_browser
            self._start_studio_initialization(silent=True)
            return
        if not self.studio_workspace.prepare_batch_capture():
            self.show_toast(self._tr("batch.parameters_pending"))
            return
        self._photo_browser.refresh()
        self._content_stack.setCurrentWidget(self._photo_browser.view)
        self._photo_browser.view.show_grid()

    def _close_photo_browser(self):
        self._content_stack.setCurrentWidget(self.workspace_stack)
        self._photo_browser.view.show_single()

    def _capture_calibration_selection(self):
        if not self._draft_runner.loaded:
            raise ValueError("batch.drafts_loading")
        if self.studio_workspace is None or not self.studio_workspace.prepare_batch_capture():
            raise ValueError("batch.parameters_pending")
        self._photo_browser.refresh()
        return self._photo_browser.service.selection()

    def _open_photo_library(self):
        if self._calibration_runner is None:
            self._pending_studio_action = self._open_photo_library
            self._start_studio_initialization(silent=True)
            return
        if not self.studio_workspace.prepare_batch_capture():
            self.show_toast(self._tr("batch.parameters_pending"), 5000)
            return
        if self._library_dialog is None:
            from .dialogs.photo_library_dialog import PhotoLibraryDialog
            self._library_dialog = PhotoLibraryDialog(self._library_runner, self._tr, self)
            self._library_dialog.photo_requested.connect(lambda path: self.open_image_path(path, load_siblings=False, origin="library"))
        self._library_dialog.refresh()
        self._library_dialog.show()
        self._library_dialog.raise_()
        self._library_dialog.activateWindow()

    def _browse_library_photos(self, identities):
        service = self._library_runner.service
        paths = service.browse_paths()
        if self.manager.publish_images(paths):
            self._load_images_into_panels()
        self._open_photo_browser()

    def _library_path_blocked(self, path):
        runner = getattr(self, "_library_runner", None)
        if runner is None:
            return False
        return runner.path_blocked(path)

    def _on_library_changed(self):
        service = self._library_runner.service
        if service.library_view:
            paths = service.browse_paths()
            if tuple(self.manager.image_list) != paths and paths:
                self.manager.publish_images(paths, preferred_path=self.manager.current_path)
                self._load_images_into_panels()
        self._photo_browser.schedule()
        if (self._library_runner.service.loaded or self._library_runner.service.error) and self._library_deferred_open is not None:
            path, siblings, origin = self._library_deferred_open
            self._library_deferred_open = None
            QTimer.singleShot(0, self, lambda: self.open_image_path(path, load_siblings=siblings, origin=origin))
        elif self._library_refresh_current and self.manager.current_path and not self._library_path_blocked(self.manager.current_path):
            self._library_refresh_current = False
            QTimer.singleShot(0, self, self._load_current)

    def _on_library_evaluated(self):
        # 只有图库视图的照片范围依赖可用性与动态状态；选片状态缓存由浏览服务按交付修订自行失效
        if self._library_runner.service.library_view:
            self._photo_browser.schedule()

    def _on_library_session_released(self, path):
        if self.studio_workspace is not None:
            self.studio_workspace.refresh_external_edit(path)

    def _prepare_library_relocation(self, path, origin="manual"):
        if ((self._batch_runner is not None and self._batch_runner.busy)
                or (self._export_runner is not None and self._export_runner.busy)
                or (self._calibration_runner is not None and self._calibration_runner.busy)):
            raise ValueError("batch.busy")
        studio = self.studio_workspace
        if studio is not None:
            if studio._commit_busy or not studio.prepare_batch_capture():
                raise ValueError("batch.parameters_pending")
            studio.stop_preview()
            studio.cancel_project_operation(force=True, resume=False)
        if self._assistant_runner is not None:
            assistant = self._assistant_runner
            current = (assistant.state.get("conversation") or {}).get("id")
            waiting = [identity for identity in assistant.state.get("busy", []) if origin != "agent" or identity != current]
            if waiting:
                for identity in waiting:
                    assistant.command("stop", identity=identity)
                raise ValueError("library.waiting_ai")
        self._cancel_studio_source_load()
        self._photo_browser.runner.invalidate(path)

    def _lock_library_relocation(self, locked):
        self.window_actions.set_blocked(locked)
        self.workspace_stack.setEnabled(not locked)
        self.thumbnail_panel.setEnabled(not locked)
        if not locked:
            QTimer.singleShot(0, self, self._resume_studio_after_library)

    def _resume_studio_after_library(self):
        # 重定位开始时取消了 OPS 载入；解锁后当前照片仍无会话就重新发起
        current = self.manager.current_path
        if (self._is_studio_active() and current and self.studio_workspace.session is None
                and not self._library_path_blocked(current)):
            self._start_studio_source_load(current)

    def _on_library_relocated(self, old_path, new_path):
        from ..application.photo_library_store import path_key
        self._batch_runner.service.expire_path(old_path)
        self._calibration_runner.service.expire_path(old_path)
        if self._calibration_dialog is not None:
            self._calibration_dialog.preview.clear()
        self._calibration_runner.changed.emit()
        if self._photo_browser.view.pin_snapshot is not None and path_key(self._photo_browser.view.pin_snapshot.path) == path_key(old_path):
            self._photo_browser.view.unpin()
        paths = [new_path if path_key(p) == path_key(old_path) else p for p in self.manager.image_list]
        preferred = new_path if self.manager.current_path and path_key(self.manager.current_path) == path_key(old_path) else self.manager.current_path
        if paths:
            self.manager.publish_images(list(dict.fromkeys(paths)), preferred_path=preferred)
        self._photo_browser.invalidate()
        self._load_images_into_panels()
        self._update_image_path_in_panels(old_path, new_path)
        if self.studio_workspace is not None:
            self.studio_workspace.refresh_external_edit(new_path)

    def _on_library_idle(self):
        if self._library_exit_pending and not self._library_runner.busy:
            self._library_exit_pending = False
            self._library_runner.resume()
            QTimer.singleShot(0, self, self.close)

    def _open_photo_calibration(self):
        if self._calibration_runner is None:
            self._pending_studio_action = self._open_photo_calibration
            self._start_studio_initialization(silent=True)
            return
        if self._calibration_dialog is None:
            from .dialogs.photo_calibration_dialog import PhotoCalibrationDialog
            self._calibration_dialog = PhotoCalibrationDialog(self._calibration_runner, self._batch_runner,
                self._photo_browser, self._capture_calibration_selection, self._show_calibration_batch, self)
        self._calibration_dialog.capture_selection()
        self._calibration_dialog.show()
        self._calibration_dialog.raise_()
        self._calibration_dialog.activateWindow()

    def _show_calibration_batch(self, identity):
        self._open_photo_batch()
        if self._batch_dialog is not None:
            self._batch_dialog.show_batch(identity)

    def _on_calibration_idle(self):
        if self._calibration_exit_pending:
            self._calibration_exit_pending = False
            QTimer.singleShot(0, self, self.close)

    def _on_browser_empty(self):
        self.viewer.clear()
        if self.studio_workspace is not None:
            self.studio_workspace.prepare_image_load(None)
        self._update_menu_actions()

    def _on_browser_idle(self):
        if self._browser_exit_pending and not self._photo_browser.runner.busy:
            self._browser_exit_pending = False
            self.close()

    def _browser_command(self, operation, values):
        if self.studio_workspace is None:
            raise ValueError("batch.unavailable")
        if operation in ("compare_photos", "set_sample_photo", "set_browser_view") and not self.studio_workspace.prepare_batch_capture():
            raise ValueError("batch.parameters_pending")
        result = self._photo_browser.perform(operation, values,
            show=lambda: self._content_stack.setCurrentWidget(self._photo_browser.view),
            single=self._close_photo_browser)
        return result

    def _on_export_idle(self):
        if self._export_exit_pending:
            self._export_exit_pending = False
            QTimer.singleShot(0, self, self.close)

    def _on_batch_photo_changed(self, path):
        studio = self.studio_workspace
        if studio is not None:
            studio.refresh_external_edit(path)
        if path == self.manager.current_path and self._current_workspace_index == 0:
            session = self._edit_sessions.get(path)
            if session is not None:
                self._batch_runner.refresh_display(path, session.core_id)

    def _on_batch_idle(self):
        if self._batch_exit_pending:
            self._batch_exit_pending = False
            QTimer.singleShot(0, self, self.close)

    def _on_batch_display_ready(self, request, result):
        if request[1] != self.manager.current_path or self._current_workspace_index != 0:
            if result.success:
                result.data.close()
            self._batch_runner.cancel_display()
            return
        if not result.success:
            self.show_toast(self._tr(result.error.message_key), 5000)
            if not self.viewer.has_image:
                self.viewer.setText(self._tr(result.error.message_key))
            return
        try:
            frame = result.data.display
            width, height = frame.size
            image = QImage(frame.rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
            self.viewer.set_qimage(image, preserve_zoom=True, reference_size=QSize(*(result.data.full_size or frame.size)))
        finally:
            result.data.close()

    def _on_studio_action_state(self, _state=None) -> None:
        self._refresh_editor_actions()
        self._update_top_bar_title()

    @Slot(str, str)
    def _on_studio_detail_state(self, state, error_key) -> None:
        if self._current_workspace_index == EDITOR and self.canvas_toolbar is not None:
            self.canvas_toolbar.set_detail_state(state, error_key)

    @Slot(object)
    def _on_studio_init_failed(self, error) -> None:
        self._studio_loading_snapshot = None
        self._studio_init_started = False
        self._studio_init_error = error
        logger.exception("OPS initialization failed", exc_info=(type(error), error, error.__traceback__))
        message = self._tr("studio.load_failed", "OPS initialization failed")
        if self._studio_placeholder_label is not None:
            self._studio_placeholder_label.setText(message)
        if self._settings_dialog is not None:
            self._settings_dialog.show_resource_error(message)
        self.show_toast(message, 5000)

    @Slot(int)
    def _on_workspace_index_changed(self, index: int) -> None:
        """切换工作区（0 看图，1 编辑）；切换被拒绝时顶栏回到实际所在的工作区。"""
        self._switch_workspace(index)
        self.top_bar.set_workspace(self._current_workspace_index)
        self._refresh_editor_actions()
        self._update_top_bar_title()

    def _switch_workspace(self, index: int) -> None:
        if self._batch_runner is not None:
            self._batch_runner.cancel_display()
        if index == 1:
            if not self.manager.current_path:
                self.show_toast(self._tr("status.no_image_for_studio", "Please open an image first to edit"))
                return
            if self._current_workspace_index == 0:
                self._studio_loading_snapshot = self.viewer.display_snapshot(self.manager.current_path)
                if self._current_pil_image is not None:
                    self._invalidate_transform_save()
            self._current_workspace_index = 1
            self.info_sidebar.panel.set_active(False)
            if self.studio_workspace is not None:
                self.studio_workspace.photo_info_panel.set_path(self.manager.current_path)
                self.studio_workspace.photo_info_panel.set_active(True)
            self._sync_photo_info_action()
            self.viewer.clear()
            self.workspace_stack.setCurrentIndex(1)
            if self.studio_workspace is None:
                self._start_studio_initialization()
                self._sync_canvas_toolbar()
                return
            self._start_studio_source_load(self.manager.current_path)
        else:
            self._studio_loading_snapshot = None
            if self._studio_source_runner is not None:
                self._studio_source_runner.cancel()
            for panel in self._thumbnail_panels():
                panel.set_loading_paused(False)
            if self.studio_workspace is not None:
                self.studio_workspace.photo_info_panel.set_active(False)
                self.studio_workspace.prepare_image_load(None)
            self._current_workspace_index = 0
            self.workspace_stack.setCurrentIndex(0)
            if self.manager.current_path:
                self.viewer.set_image(self.manager.current_path)
            self._show_viewer_photo_info(self.manager.current_path)
            self.info_sidebar.panel.set_active(True)
            self._sync_photo_info_action()
            if self.viewer.is_fit_mode():
                QTimer.singleShot(0, self.viewer, self.viewer.zoom_fit)
        self._sync_canvas_toolbar()

    @Slot(str)
    def _on_studio_applied(self, new_path: str) -> None:
        """修图工作区保存并应用修改（可能是覆盖原路径或另存的新路径）。"""
        old_path = self.manager.current_path
        if new_path and new_path != old_path:
            self.manager.replace_current_image(new_path)
            if old_path:
                self._update_image_path_in_panels(old_path, new_path)
        self._invalidate_transform_save()
        self._show_viewer_photo_info(new_path)
        self._sync_canvas_toolbar()
        self._update_menu_actions()

        self._on_workspace_index_changed(0)
        self.show_toast(self._tr("status.tool_applied", "Changes applied successfully"))

    @Slot()
    def _on_studio_back_requested(self) -> None:
        """从修图工作区返回看图工作区。"""
        self._on_workspace_index_changed(0)

    def switch_to_studio(self, tool_id: str = "color_adjust") -> None:
        """从任意入口直接切入修图工作区并激活指定工具。"""
        if not self.manager.current_path:
            self.show_toast(self._tr("status.no_image_for_studio", "Please open an image first to edit"))
            return
        tool_id = resolve_tool_id(tool_id)
        if self.studio_workspace is not None:
            available = self.studio_workspace.core.tools.check_enabled(tool_id)
            if not available.success:
                self.show_toast(self._tr(available.error.message_key), 5000)
                return
        self._on_workspace_index_changed(1)
        if self.studio_workspace is None:
            self._pending_studio_tool_id = tool_id
            return
        self.studio_workspace.switch_tool(tool_id)
        self._active_tool_id = self.studio_workspace.active_tool_id
        self._sync_canvas_toolbar()

    def deactivate_tool(self, discard_stash: bool = False) -> None:
        """收起工具侧栏，退出编辑状态并返回看图。"""
        self._end_tool_sampling()
        curr_tool = self._active_tool_id
        panel = self.tool_sidebar.get_current_panel()
        if discard_stash and panel:
            panel.reset()
        if panel:
            panel.cleanup()
        if discard_stash and self.manager.current_path and curr_tool:
            self._edit_state_store.discard(self.manager.current_path, curr_tool)

        self.viewer.uninstall_overlay()
        if self.manager.current_path:
            self.viewer.set_image(self.manager.current_path)

        self.tool_sidebar.hide()
        self._active_tool_id = None
        self._on_studio_back_requested()

    def toggle_tool(self, tool_id: str) -> None:
        """Toggle 切换修图工具：在修图工作区中切换工具，若已在修图区且是同一工具则返回看图。"""
        tool_id = resolve_tool_id(tool_id)
        if self._is_studio_active() and self.studio_workspace.active_tool_id == tool_id:
            self.deactivate_tool()
        else:
            self.switch_to_studio(tool_id)

    def _create_tool_panel(self, tool_id: str) -> BaseToolPanel | None:
        return create_tool_panel(
            tool_id,
            self.manager.current_path,
            parent=self.tool_sidebar,
            edit_state_store=self._edit_state_store,
            services=self._panel_services,
        )

    @Slot(QImage)
    def _on_tool_preview_ready(self, qimage: QImage) -> None:
        if not self._active_tool_id:
            return
        sender_panel = self.sender()
        curr_panel = self.tool_sidebar.get_current_panel()
        if sender_panel is not None and sender_panel is not curr_panel:
            return
        if hasattr(sender_panel, "image_path") and sender_panel.image_path != self.manager.current_path:
            return
        self.viewer.set_preview_image(qimage, preserve_zoom=True)

    def _end_tool_sampling(self):
        self._tool_sampling_epoch += 1
        self.viewer.set_sampling(None)
        handle, self._tool_sampling_resources.sampling = self._tool_sampling_resources.sampling, None
        if handle is not None:
            handle.close()
        panel, self._tool_sampling_panel = self._tool_sampling_panel, None
        if panel is not None:
            panel.cancel_background()
            panel.set_sampling_state("closed")

    def _on_tool_presentation_changed(self, sampling):
        if sampling is None or sampling is not self._tool_sampling_resources.sampling:
            self._end_tool_sampling()

    def _on_tool_sampling_requested(self, panel, active):
        self._end_tool_sampling()
        if not active:
            return
        if (panel is not self.tool_sidebar.get_current_panel() or panel.source_bundle is None
                or panel.image_path != self.manager.current_path or panel.parameters_pending
                or self._is_studio_active()):
            panel.set_sampling_state("closed")
            return
        self._tool_sampling_panel = panel
        panel.set_sampling_state("preparing")
        panel.prepare_sampling(self._tool_sampling_epoch)

    @Slot(object)
    def _on_tool_sampled_preview(self, packet):
        panel = self.sender()
        if (panel is not self._tool_sampling_panel or panel is not self.tool_sidebar.get_current_panel()
                or panel.image_path != self.manager.current_path or self._is_studio_active()
                or packet.sampling is None or packet.sampling.request.epoch != self._tool_sampling_epoch):
            return
        sampling = packet.sampling
        if packet.display is None or sampling.size != packet.display.size:
            self._end_tool_sampling()
            self.show_toast(self._tr("color_adjust.mixer.failed"), 5000)
            return
        if not self._panel_services["render_service"].is_current(sampling.tool_generation):
            self._end_tool_sampling()
            self.show_toast(self._tr("color_adjust.mixer.unavailable"), 5000)
            return
        if not self._image_sources.matches_file(sampling.source_identity):
            self._end_tool_sampling()
            self.show_toast(self._tr("error.source.changed"), 5000)
            return
        frame = packet.display
        width, height = frame.size
        image = QImage(frame.rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        self._tool_sampling_resources.sampling = packet.take_sampling()
        self.viewer.set_preview_image(image, preserve_zoom=True, sampling=self._tool_sampling_resources.sampling)
        panel.set_sampling_state("ready")

    def _on_tool_sampling_picked(self, panel, x, y):
        handle = self._tool_sampling_resources.sampling
        if handle is None or panel is not self._tool_sampling_panel:
            return
        source_current = self._image_sources.matches_file(handle.source_identity)
        if (source_current and panel.image_path == self.manager.current_path
                and handle.request.epoch == self._tool_sampling_epoch
                and self._panel_services["render_service"].is_current(handle.tool_generation)
                and panel.source_bundle is not None and handle.source_identity == panel.source_bundle.identity):
            channel = handle.channel_at(x, y)
            if channel is not None:
                panel.select_sampling_channel(channel)
        self._end_tool_sampling()
        if not source_current:
            self.show_toast(self._tr("error.source.changed"), 5000)

    def _is_studio_active(self) -> bool:
        """True when the Studio workspace owns the shared tool sidebar."""
        return getattr(self, "_current_workspace_index", 0) == 1 and self.studio_workspace is not None

    @Slot()
    def _on_tool_close_requested(self) -> None:
        if self._is_studio_active():
            return  # Studio collapses its own sidebar.
        self.deactivate_tool(discard_stash=False)

    @Slot()
    def _on_tool_reset_requested(self) -> None:
        if self._is_studio_active():
            return  # Studio handles reset.
        panel = self.tool_sidebar.get_current_panel()
        if panel:
            panel.reset()
        if self._active_tool_id and self.manager.current_path:
            self._edit_state_store.discard(self.manager.current_path, self._active_tool_id)
            self.tool_sidebar.set_stash_banner_visible(False)

    @Slot()
    def _on_tool_discard_stash_requested(self) -> None:
        if self._is_studio_active():
            return  # Studio handles its own drafts.
        panel = self.tool_sidebar.get_current_panel()
        if panel:
            panel.reset()
        if self._active_tool_id and self.manager.current_path:
            self._edit_state_store.discard(self.manager.current_path, self._active_tool_id)
            self.tool_sidebar.set_stash_banner_visible(False)

    @Slot()
    def _open_color_adjust(self):
        """打开/切换右侧调色实验室侧栏。"""
        self.toggle_tool("color_adjust")

    @Slot()
    def _open_denoise_dialog(self):
        """保留图片降噪对话框。"""
        if not self._tool_entry_available("ai_enhance", self._open_denoise_dialog):
            return
        selected_paths = self.active_thumbnail_panel.get_selected_paths()
        if not selected_paths:
            selected_paths = [self.manager.current_path] if self.manager.current_path else []
        if not selected_paths:
            return
        dialog = EnhancementDialog(selected_paths, self, output_service=self._output_service,
                               runtime=self.studio_workspace.core.tools, source_service=self._image_sources,
                               manage_models=lambda capability, token: self._show_model_settings(
                                   capability, token, parent=dialog))
        dialog.exec()
        dialog.deleteLater()

    @Slot()
    def _open_ai_enhance(self):
        """打开/切换右侧 AI 增强侧栏。"""
        selected_paths = self.active_thumbnail_panel.get_selected_paths()
        if len(selected_paths) > 1:
            self._open_denoise_dialog()
        else:
            self.toggle_tool("ai_enhance")

    @Slot()
    def _open_beauty(self):
        """打开/切换右侧人像美颜侧栏。"""
        self.toggle_tool("beauty")

    @Slot()
    def _open_id_photo(self):
        """打开/切换左侧的证件照工具。"""
        self.toggle_tool("id_photo")

    @Slot(str)
    def _on_thumbnail_selected(self, path):
        """响应缩略图点击：切换到对应图片并刷新显示"""
        if self._library_runner.path_blocked(path) or self.studio_workspace is not None and self.studio_workspace._commit_busy:
            return
        if self.manager.select_image(path):
            self._load_current()

    @Slot(object)
    def _on_thumbnail_reordered(self, new_paths):
        """处理缩略图顺序手动调整后的同步"""
        panel = self.sender()
        version = panel.list_version if isinstance(panel, ThumbnailPanel) else self.manager.list_version
        if not self.manager.reorder_images(new_paths, expected_version=version):
            self._load_images_into_panels()
            return
        self._cancel_browse()
        self._load_images_into_panels()
        self._sync_canvas_toolbar()
        self._refresh_studio_prefetch()

    @Slot(int)
    def _on_thumbnail_listing_ready(self, version):
        panel = self.sender()
        if (self._closing or not isinstance(panel, ThumbnailPanel)
                or version != self.manager.list_version or not panel.isVisible()):
            return
        if not self.manager.matches_filter(self.manager.current_path):
            if panel.first_matching_path is not None:
                self._on_thumbnail_selected(panel.first_matching_path)
            elif self.manager.image_list:
                self.show_toast(self._tr("status.filter_no_match"), 3000)
        self._refresh_studio_prefetch()

    @Slot()
    def _update_menu_actions(self):
        """根据是否有加载的图片启用或禁用批量操作菜单与侧边栏动作"""
        enabled = self.manager.current_path is not None
        self.window_actions.set_has_image(enabled)
        self._sync_viewer_empty()
        self._sync_canvas_toolbar()
        self._sync_photo_info_action()
        workspace = self.studio_workspace
        self.window_actions.set_tool_check(
            None if workspace is None else lambda tool_id: workspace.core.tools.check_enabled(tool_id).success)
        self._update_top_bar_title()

    @property
    def photo_info_panel(self):
        if self._current_workspace_index == 0:
            return self.info_sidebar.panel
        if self.studio_workspace is not None:
            return self.studio_workspace.photo_info_panel
        return None

    def _show_viewer_photo_info(self, path) -> None:
        """看图工作区的照片信息栏改为显示这张图片；路径不是现存文件时清空。"""
        self.info_sidebar.panel.set_path(path if path and os.path.isfile(path) else None)
        self._restore_info_expanded()

    def _request_viewer_histogram(self) -> None:
        """照片信息栏需要直方图：画布上已经是这张照片的终态画面时交给后台统计，否则等它出来。"""
        path = self.info_sidebar.panel.image_path
        snapshot = self.viewer.display_snapshot(path) if path else None
        if snapshot is None:
            self._histogram_runner.cancel()
            return
        self._histogram_runner.request(path, snapshot.stamp, snapshot.raw_display_mode, snapshot.image,
                                       non_raw_display_mode=snapshot.non_raw_display_mode)

    def _on_viewer_frame_changed(self, loading: bool) -> None:
        """画布开始加载时，原来的直方图不再对应画面；终态画面出来后再统计。快速预览帧不统计。"""
        panel = self.info_sidebar.panel
        if loading:
            self._histogram_runner.cancel()
            panel.clear_histogram()
        else:
            panel.refresh_histogram()

    def _connect_photo_info_panel(self, panel) -> None:
        panel.info_requested.connect(self._info_runner.request)
        panel.reading_stopped.connect(self._info_runner.cancel)
        panel.expanded_changed.connect(self._sync_photo_info_action)
        panel.path_changed.connect(self._sync_photo_info_action)
        self._info_runner.started.connect(panel.set_request)
        self._info_runner.ready.connect(panel.set_result)

    def _set_photo_info_visible(self, visible: bool) -> None:
        panel = self.photo_info_panel
        if panel is not None:
            panel.set_expanded(visible)
            # 看图里停靠着的照片信息栏：用户的选择下次打开时恢复；浮层是临时展开，不记
            if panel is self.info_sidebar.panel and not self._layout_rules.floating_sidebar:
                self._panel_prefs.info_expanded = panel.expanded
        self._sync_photo_info_action()

    def _sync_photo_info_action(self, _expanded=None) -> None:
        if self.top_bar is None:
            return
        panel = self.photo_info_panel
        self.top_bar.set_photo_info_state(panel.expanded if panel is not None else False,
                                          panel is not None and panel.image_path is not None)

    def _tool_entry_available(self, tool_id, entry):
        if self.studio_workspace is None:
            self._pending_studio_action = entry
            self._start_studio_initialization()
            return False
        result = self.studio_workspace.core.tools.check_enabled(tool_id)
        if not result.success:
            self.show_toast(self._tr(result.error.message_key), 5000)
        return result.success

    def _show_model_settings(self, capability="", token="", *, parent=None):
        """面板里的“安装模型…”：打开设置的模型页并定位到所选模型，没有选时列出这一类能力的模型。"""
        self._show_settings("models", model=token, capability=capability, parent=parent)

    def _provide_resource_services(self, dialog):
        """设置窗口进入了工具、模型或运行环境页：编辑内核还没装配时先装配，就绪后把服务交给它。"""
        if self._settings_dialog is not dialog:
            return
        if self.studio_workspace is None:
            previous = self._pending_studio_action

            def provide():
                if previous is not None:
                    previous()
                self._provide_resource_services(dialog)

            self._pending_studio_action = provide
            self._start_studio_initialization(silent=True)
            return
        context_provider = self.studio_workspace.resource_manager_context if self._is_studio_active() else None
        dialog.set_resource_services(self.studio_workspace.core, self._runtime_service, context_provider)

    def _on_tools_changed(self, disabled):
        self._photo_browser.invalidate()
        """设置里启停了工具：记下停用的工具并让两个工作区跟上。"""
        QSettings(APP_NAME, "Tools").setValue("disabled", list(disabled))
        self.studio_workspace.refresh_tool_availability()
        if self._is_studio_active():
            self._active_tool_id = self.studio_workspace.active_tool_id
        elif (self._active_tool_id is not None
                and not self.studio_workspace.core.tools.check_enabled(self._active_tool_id).success):
            self.deactivate_tool(discard_stash=False)
        self._update_menu_actions()

    def request_restart(self):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        app.setProperty("ops_restart", True)
        self.close()

    def _cancel_exit(self):
        from PySide6.QtWidgets import QApplication
        QApplication.instance().setProperty("ops_restart", False)
        self._projects_saved_for_exit = False

    def _cleanup_temp_paths(self) -> None:
        """批量清理本窗口创建的所有临时文件/目录。"""
        import shutil
        for p in list(self._temp_paths):
            if not os.path.exists(p):
                self._temp_paths.discard(p)
                continue
            try:
                if p in self._temp_workspaces:
                    self._temp_workspaces.pop(p).cleanup()
                elif os.path.isfile(p):
                    os.unlink(p)
                elif os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
            except Exception:
                logger.debug("Failed to clean up temp path on exit: %s", p, exc_info=True)
        self._temp_paths.clear()

    def closeEvent(self, event):
        """退出前保存布局状态并清理临时文件。"""
        if self._library_runner.busy:
            self._library_exit_pending = True
            self._library_runner.cancel()
            event.ignore()
            return
        if self._draft_exit_pending or self._cache_exit_pending:
            event.ignore()
            return
        if self._calibration_runner is not None and self._calibration_runner.busy:
            self._calibration_exit_pending = True
            self._calibration_runner.cancel()
            event.ignore()
            return
        if self._photo_browser.runner.busy:
            self._browser_exit_pending = True
            self._photo_browser.runner.cancel()
            event.ignore()
            return
        if self._export_runner is not None and self._export_runner.busy:
            self._export_exit_pending = True
            self._export_runner.cancel_all()
            event.ignore()
            return
        if self._batch_runner is not None and self._batch_runner.busy:
            self._batch_exit_pending = True
            self._batch_runner.cancel()
            event.ignore()
            return
        if self.studio_workspace is not None and self.studio_workspace._commit_busy:
            self._projects_saved_for_exit = False
            event.ignore()
            return
        studio = getattr(self, "studio_workspace", None)
        if studio and studio.saving_projects_for_exit:
            event.ignore()
            return
        if studio:
            studio.assistant_panel.invalidate()
        if studio and studio.has_pending_edits() and not self._projects_saved_for_exit:
            drafts = set(self._edit_state_store.drafts.snapshot())
            unsaved = {session.image_path for session in self._edit_sessions.sessions() if session.has_unsaved_changes()}
            batch_paths = {path for identity in self._batch_runner.service.batches()
                           for path in self._batch_runner.service.paths(identity)}
            choice = ask_choice(
                self, self._tr("studio.unsaved_title", "Unsaved changes"),
                self._tr("batch.exit_message", unsaved=len(unsaved), drafts=len(drafts),
                         batch=len(drafts & batch_paths)),
                (DialogButton("keep", self._tr("studio.keep_editing", "Keep editing"), "text"),
                 DialogButton("discard", self._tr("batch.discard_exit")),
                 DialogButton("save", self._tr("studio.save_exit", "Save and exit"), "primary")),
                default="save", escape="keep")
            if choice == "save":
                if not studio._persist_draft().success:
                    self._cancel_exit()
                    event.ignore()
                    return
                if studio.save_projects_for_exit(self._on_exit_projects_saved):
                    event.ignore()
                    return
            elif choice == "discard":
                self._edit_state_store.drafts.clear_all()
            else:
                self._cancel_exit()
                event.ignore()
                return
            self._projects_saved_for_exit = True
        if not self._drafts_saved_for_exit and not self._draft_exit_discard:
            event.ignore()
            self._draft_exit_pending = True
            self.setEnabled(False)
            self._draft_runner.flush(self._on_exit_drafts_saved)
            return
        if not self._caches_closed_for_exit:
            event.ignore()
            self._draft_runner.shutdown()
            self._library_runner.shutdown()
            if self._calibration_dialog is not None:
                self._calibration_dialog.preview.clear()
            if self._calibration_runner is not None:
                self._calibration_runner.shutdown()
            self._photo_browser.shutdown()
            if self._batch_runner is not None:
                self._batch_runner.shutdown()
            if self._export_runner is not None:
                self._export_runner.shutdown()
            if studio:
                studio.assistant_panel.cleanup()
            if self._assistant_runner is not None:
                self._assistant_runner.begin_shutdown()
            self.shutdown_previews()
            if studio:
                studio.prepare_image_load(None)
            self.setEnabled(False)
            self.show_toast(self._tr("common.closing", "Closing…"), 0)
            self._cache_exit_pending = True
            self._cache_close_worker = _CacheCloseWorker(self._close_source_caches)
            self._cache_close_worker.signals.finished.connect(
                self._on_exit_caches_closed, Qt.ConnectionType.QueuedConnection)
            QThreadPool.globalInstance().start(self._cache_close_worker)
            return
        self.viewer.clear()
        self.viewer.shutdown()
        for panel in self._thumbnail_panels():
            panel.shutdown()
        if studio:
            studio.viewer.shutdown()
        self._invalidate_transform_save()
        self._cleanup_temp_paths()
        super().closeEvent(event)

    def _close_source_caches(self):
        if self._caches_closed_for_exit:
            return
        if self._assistant_runner is not None:
            self._assistant_runner.shutdown()
        self._image_sources.close()
        for runner in (self._thumbnail_cache_runner, self._edit_cache_runner):
            runner.wait_for_idle()
        self._edit_cache.close()
        self._thumbnail_cache.close()

    @Slot(object)
    def _on_exit_caches_closed(self, error):
        self._cache_exit_pending = False
        self._cache_close_worker = None
        if error is not None:
            self.show_toast(self._tr("app.exit_cleanup_failed",
                "Unable to finish closing the cache. Close the window again to retry exiting."), 0)
            return
        self._caches_closed_for_exit = True
        self.close()

    def _on_exit_projects_saved(self, success):
        self._projects_saved_for_exit = success
        if success:
            self.close()
        else:
            self._cancel_exit()

    def _on_drafts_ready(self):
        self._photo_browser.invalidate()
        self._on_draft_saved()
        if self._is_studio_active() and self.manager.current_path:
            self._start_studio_source_load(self.manager.current_path)

    def _on_draft_changed(self, _path):
        self._photo_browser.invalidate(_path)
        self._drafts_saved_for_exit = False
        self._projects_saved_for_exit = False

    def _on_draft_failed(self, error):
        self._draft_error_key = error.message_key
        self._sync_window_notices()

    def _on_draft_saved(self):
        self._draft_error_key = ""
        self._sync_window_notices()

    def _on_exit_drafts_saved(self, result):
        self._draft_exit_pending = False
        self.setEnabled(True)
        if result.success:
            self._drafts_saved_for_exit = True
            self.close()
            return
        choice = ask_choice(
            self, self._tr("studio.error_title", "Editing failed"), self._tr(result.error.message_key),
            (DialogButton("keep", self._tr("studio.keep_editing", "Keep editing"), "text"),
             DialogButton("discard", self._tr("studio.discard_exit", "Discard and exit")),
             DialogButton("retry", self._tr("common.retry", "Retry"), "primary")),
            kind="warning", default="retry", escape="keep")
        if choice == "retry":
            self.close()
        elif choice == "discard":
            self._draft_exit_discard = True
            self.close()
        else:
            self._cancel_exit()

    def shutdown_previews(self):
        self._canvas_scale_runner.shutdown()
        if self._closing:
            return
        self._closing = True
        if self._version_runner is not None:
            self._version_runner.shutdown()
        self._studio_loading_snapshot = None
        if self._studio_init_worker is not None:
            self._studio_init_worker.cancel()
        self._prewarm_timer.stop()
        self._cancel_browse()
        self._browse_runner.shutdown()
        self._info_runner.shutdown()
        self._histogram_runner.shutdown()
        self._thumbnail_cache_runner.shutdown()
        self._edit_cache_runner.shutdown()
        self.info_sidebar.panel.cleanup()
        self.viewer.shutdown()
        for panel in self._thumbnail_panels():
            panel.shutdown()
        if self._studio_source_runner is not None:
            self._studio_source_runner.shutdown()
        if self.studio_workspace is not None:
            self.studio_workspace.shutdown_preview()
            self.studio_workspace.core.tools.close()
        if self._studio_preview_cache is not None:
            self._studio_preview_cache.close()

    @Slot()
    def _clear_all_ratings(self):
        """清除所有评分（持久化）—— 弹窗确认，二次确认避免误删。"""
        from ..core.rating_store import get_rating_store

        store = get_rating_store()
        if not store.has_any():
            show_info(
                self,
                self._tr("dialog.clear_ratings.title", "Clear Ratings"),
                self._tr("dialog.clear_ratings.empty", "No ratings to clear."),
            )
            return
        if confirm_destructive(
            self,
            self._tr("dialog.clear_ratings.title", "Clear Ratings"),
            self._tr(
                "dialog.clear_ratings.confirm",
                "This will permanently delete all saved ratings. Continue?",
            ),
            self._tr("dialog.clear_ratings.action", "Clear Ratings"),
        ):
            count = store.clear_all()
            self._apply_rating_filter_to_panels(self.manager.get_rating_filter())
            for panel in self._thumbnail_panels():
                panel.list_widget.viewport().update()
            self._sync_canvas_toolbar()
            self.show_toast(self._tr("status.ratings_cleared", "Cleared {n} ratings", n=count))

    _clear_ratings = _clear_all_ratings

    @Slot(str)
    def set_rating_filter(self, mode: str) -> None:
        """设置图库星级筛选模式，并同步两处胶片条。"""
        self.manager.set_rating_filter(mode)
        self._photo_browser.service.set_filter({"rating": mode})
        self._photo_browser.refresh()
        if hasattr(self, "thumbnail_panel") and self.thumbnail_panel:
            self._apply_rating_filter_to_panels(mode)

        self._refresh_studio_prefetch()

    def _show_settings(self, page: str = "", *, tool_id=None, model="", capability="", parent=None):
        """打开设置窗口。窗口里每一项改了就生效：它只发出用户的改动，由这里保存并应用。

        page 是打开时所在的页；tool_id、model、capability 是进入工具页或模型页后要定位的条目；
        正在编辑时，安装资源要复核的图片与参数取自编辑工作区。
        """
        from ..application.storage_service import StorageService
        from .services.storage_runner import StorageRunner
        storage_runner = StorageRunner(StorageService(sources=self.manager.image_list), self)
        dialog = SettingsDialog(
            parent or self, page=page, tool_id=tool_id, model=model, capability=capability,
            thumbnail_cache_runner=self._thumbnail_cache_runner, edit_cache_runner=self._edit_cache_runner,
            storage_runner=storage_runner,
            values=SettingsValues(
                language=self.translation_service.get_language() if self.translation_service else "en",
                theme=QSettings(APP_NAME, "Theme").value("current", ThemeManager.THEME_DARK),
                image_display=self._image_display_settings, preload=self._preload_settings,
                memory=self._memory_state, shortcuts=self.window_actions.shortcut_rows(),
                startup_window=self.startup_mode(), check_updates=self._version_runner.state.enabled))
        dialog.language_edited.connect(lambda code: self._set_language(dialog, code))
        dialog.theme_edited.connect(self._set_theme)
        dialog.startup_window_edited.connect(lambda mode: self._set_startup_window(dialog, mode))
        dialog.image_display_edited.connect(lambda value: self._set_image_display(dialog, value))
        dialog.preload_edited.connect(lambda value: self._set_preload(dialog, value))
        dialog.memory_edited.connect(lambda value: self._set_memory(dialog, value))
        dialog.shortcut_edited.connect(lambda key, sequence: self._set_shortcut(dialog, key, sequence))
        dialog.shortcuts_reset_requested.connect(lambda: self._reset_shortcuts(dialog))
        dialog.resource_pages_requested.connect(lambda: self._provide_resource_services(dialog))
        dialog.assistant_services_requested.connect(lambda: dialog.set_assistant_runner(self._get_assistant_runner()))
        dialog.tools_changed.connect(self._on_tools_changed)
        dialog.storage_manage_requested.connect(lambda key: self._manage_storage(dialog, key))
        dialog.update_enabled_edited.connect(self._version_runner.set_enabled)
        dialog.about_page.check_requested.connect(lambda: self._version_runner.check(manual=True))
        dialog.about_page.download_requested.connect(self._version_runner.open_download)
        dialog.set_update_state(self._version_runner.state)
        self._version_runner.changed.connect(dialog.set_update_state)
        previous, self._settings_dialog = self._settings_dialog, dialog
        try:
            dialog.exec()
        finally:
            self._version_runner.changed.disconnect(dialog.set_update_state)
            self._settings_dialog = previous
        restart = dialog.restart_pending
        dialog.deleteLater()
        storage_runner.deleteLater()
        if restart:
            QTimer.singleShot(0, self, self.request_restart)

    def _manage_storage(self, dialog, key):
        if key in ("models", "runtimes"):
            dialog.show_page("models" if key == "models" else "runtime")
        elif key in ("assistant", "attachments", "styles"):
            from .dialogs.storage_page import AssistantStorageDialog
            manager = AssistantStorageDialog(self._get_assistant_runner(), self._tr, key, dialog)
            manager.exec()
            manager.deleteLater()
            dialog.storage_page.refresh()
        elif key in ("library", "exports"):
            dialog.accept()
            QTimer.singleShot(0, self, self._open_photo_library if key == "library" else self._open_photo_export)

    def _get_assistant_runner(self):
        if self._assistant_runner is None:
            from .services.assistant_runner import AssistantRunner
            from ..application.storage_paths import data_root
            directory = data_root() / "assistant"
            self._assistant_runner = AssistantRunner(str(directory), self)
        return self._assistant_runner

    def _set_language(self, dialog, code):
        settings = QSettings(APP_NAME, "Language")
        settings.setValue("current", code)
        settings.sync()
        if self.translation_service:
            self.translation_service.set_language(code)
        dialog.set_shortcuts(self.window_actions.shortcut_rows())

    def _set_startup_window(self, dialog, mode):
        if not self.set_startup_mode(mode):
            dialog.show_page_error("general", self._tr("settings.startup_window.save_failed",
                                                       "Unable to save the startup window setting."))
            dialog.set_startup_window(self.startup_mode())

    def _set_theme(self, code):
        settings = QSettings(APP_NAME, "Theme")
        settings.setValue("current", code)
        settings.sync()
        theme_manager = get_theme_manager()
        if theme_manager is not None:
            theme_manager.apply(code)

    def _set_image_display(self, dialog, value):
        settings = QSettings(APP_NAME, "ImageDisplay")
        value.save(settings)
        if settings.status() != QSettings.Status.NoError:
            dialog.show_page_error("images", self._tr("settings.images.save_failed",
                                                      "Unable to save image display settings."))
            return
        previous, self._image_display_settings = self._image_display_settings, value
        self._photo_browser.settings_changed(value)
        self.viewer.set_raw_display_mode(value.raw_mode)
        self.viewer.set_non_raw_display_mode(value.non_raw_mode)
        if self.studio_workspace is not None:
            self.studio_workspace.set_raw_display_mode(value.raw_mode)
            self.studio_workspace.set_non_raw_display_mode(value.non_raw_mode)
        if ((previous.raw_mode, previous.non_raw_mode) != (value.raw_mode, value.non_raw_mode)
                and self._studio_source_runner is not None):
            self._studio_source_runner.pause_prefetch()
            self._refresh_studio_prefetch()
        theme_manager = get_theme_manager()
        if theme_manager is not None:
            theme_manager.set_canvas_background(value.canvas_background.value)

    def _set_preload(self, dialog, value):
        settings = QSettings(APP_NAME, "OPSPreload")
        value.save(settings)
        if settings.status() != QSettings.Status.NoError:
            if dialog is not None:
                dialog.show_page_error("performance", self._tr("settings.ops.save_failed"))
                dialog.set_preload(self._preload_settings)
            else:
                self.show_toast(self._tr("settings.ops.save_failed"), 5000)
            return
        previous, self._preload_settings = self._preload_settings, value
        if self.studio_workspace is not None and value.canvas_preview_mode != previous.canvas_preview_mode:
            self.studio_workspace.set_canvas_preview_mode(value.canvas_preview_mode)
        self._sync_canvas_toolbar()
        if value.memory_mib != previous.memory_mib:
            self._image_sources.update_memory(preview_cache_bytes=value.memory_mib * 1024 ** 2)
        if value.disk_mib != previous.disk_mib:
            self._edit_cache_runner.set_limit(value.disk_mib * 1024 ** 2)
        if value.thumbnail_mib != previous.thumbnail_mib:
            self._thumbnail_cache_runner.set_limit(value.thumbnail_mib * 1024 ** 2)
        if (value.mode, value.before, value.after, value.memory_mib) == (
                previous.mode, previous.before, previous.after, previous.memory_mib):
            return
        if self._studio_source_runner is not None:
            self._studio_source_runner.pause_prefetch()
        self._refresh_studio_prefetch()

    def _set_canvas_preview_mode(self, mode):
        self._set_preload(None, replace(self._preload_settings, canvas_preview_mode=mode))

    def _set_memory(self, dialog, value):
        """保存内存偏好并立即生效：重新解析预算、向系统登记座位、替换资源池的内存字段，再把生效的结果投影回去。"""
        settings = QSettings(APP_NAME, "Memory")
        value.save(settings)
        if settings.status() != QSettings.Status.NoError:
            dialog.show_page_error("performance", self._tr("settings.memory.save_failed",
                                                           "Unable to save memory settings."))
            dialog.set_memory(self._memory_state)
            return
        self._memory_state = self._memory.apply(value)
        self._image_sources.update_memory(**self._memory_state.policy_fields())
        dialog.set_memory(self._memory_state)
        if self.studio_workspace is not None:
            self.studio_workspace.refresh_memory_budget()

    def _set_shortcut(self, dialog, key, sequence):
        owner = self.window_actions.shortcut_owner(sequence, exclude=key)
        if not owner:
            self.shortcut_manager.set_sequence(key, sequence)
        dialog.set_shortcuts(self.window_actions.shortcut_rows())
        if owner:
            dialog.show_shortcut_conflict(key, sequence, owner)

    def _reset_shortcuts(self, dialog):
        self.shortcut_manager.reset()
        dialog.set_shortcuts(self.window_actions.shortcut_rows())

    # ------------------------------------------------------------------
    # Thumbnail panel synchronization (viewer <-> studio filmstrip)
    # ------------------------------------------------------------------
    def _thumbnail_panels(self) -> list:
        panels = [self.thumbnail_panel]
        studio_thumbs = getattr(getattr(self, "studio_workspace", None), "thumbnail_panel", None)
        if studio_thumbs is not None and studio_thumbs is not self.thumbnail_panel:
            panels.append(studio_thumbs)
        return panels

    @property
    def active_thumbnail_panel(self):
        if self._current_workspace_index == 1 and self.studio_workspace is not None:
            return self.studio_workspace.thumbnail_panel
        return self.thumbnail_panel

    def _load_images_into_panels(self) -> None:
        self._photo_browser.refresh()
        for panel in self._thumbnail_panels():
            panel.load_images(self.manager.image_list, self.manager.current_path,
                              version=self.manager.list_version)
            panel.project_browser()

    def _select_image_in_panels(self, path: str) -> None:
        for panel in self._thumbnail_panels():
            panel.select_image(path)

    def _update_image_path_in_panels(self, old_path: str, new_path: str) -> None:
        self._photo_browser.replace_image_path(old_path, new_path)

    def _apply_rating_filter_to_panels(self, mode: str) -> None:
        for panel in self._thumbnail_panels():
            panel.apply_rating_filter(mode)

    @Slot()
    def _load_current(self):
        if self.manager.current_path and self._library_runner.path_blocked(self.manager.current_path):
            self._library_refresh_current = True
            if self._library_runner.service.loaded:
                self.show_toast(self._tr("library.recovery_required"), 6000)
            return
        self._photo_browser.current_changed(self.manager.current_path)
        if self._batch_runner is not None:
            self._batch_runner.cancel_display()
        self._invalidate_transform_save()
        if not self.manager.current_path:
            self._show_viewer_photo_info(None)
            if self.studio_workspace is not None:
                self.studio_workspace.photo_info_panel.set_path(None)
        if self.manager.current_path:
            if (not self._is_studio_active() and hasattr(self, "tool_sidebar")
                    and self._active_tool_id in ("denoise", "beauty")):
                panel = self.tool_sidebar.get_current_panel()
                if panel is not None:
                    if panel.image_path != self.manager.current_path:
                        panel.set_image_path(self.manager.current_path, force=True)
                    elif panel.source_bundle is not None:
                        panel.set_image_path(self.manager.current_path)
            if self._current_workspace_index == 0:
                session = self._edit_sessions.get(self.manager.current_path) if self._batch_runner is not None else None
                if session is not None and self._batch_runner.service.has_photo(self.manager.current_path):
                    self.viewer.clear()
                    self.viewer.setText(self._tr("studio.image_loading", "Loading image…"))
                    self._batch_runner.refresh_display(self.manager.current_path, session.core_id)
                else:
                    self.viewer.set_image(self.manager.current_path)
            self._sync_canvas_toolbar()
            self._select_image_in_panels(self.manager.current_path)
            self._show_viewer_photo_info(self.manager.current_path)
            if getattr(self, "_current_workspace_index", 0) == 1 and self.studio_workspace is not None:
                self._start_studio_source_load(self.manager.current_path)
        elif (not self._is_studio_active() and hasattr(self, "tool_sidebar")
              and self._active_tool_id in ("denoise", "beauty")):
            panel = self.tool_sidebar.get_current_panel()
            if panel is not None:
                panel.cleanup()
        self._update_menu_actions()

    @Slot()
    def _refresh_all(self):
        self._load_images_into_panels()
        self._load_current()

    @Slot(str, int)
    def _on_rating_changed(self, path: str, value: int):
        """评分变化时刷新底部工具条（仅当变化的是当前图）。"""
        if path == self.manager.current_path:
            self._sync_canvas_toolbar()

    def keyPressEvent(self, event):
        """监听 Esc 键收起侧栏、以及 1-5 / 0 / ` 设置/清除当前图片或选中图片的评分。"""
        if event.key() == Qt.Key.Key_Escape:
            if self._is_studio_active() and self.studio_workspace is not None:
                # Studio: Esc collapses the tool sidebar, stays in the workspace.
                self.studio_workspace._on_tool_sidebar_close()
                event.accept()
                return
            if getattr(self, "_active_tool_id", None):
                self.deactivate_tool(discard_stash=False)
                event.accept()
                return

        from ..core.rating_store import get_rating_store
        from PySide6.QtWidgets import QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QPlainTextEdit

        # 焦点在文本/数值输入控件时，数字键交给控件本身（与调色对话框版保持一致）。
        # 大多数此类控件会自行消费按键，但有可编辑 QComboBox 等不消费的情况，显式避让更稳。
        focused = self.focusWidget()
        if isinstance(focused, (QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QPlainTextEdit)):
            super().keyPressEvent(event)
            return

        key = event.key()
        if event.modifiers() == Qt.KeyboardModifier.NoModifier and not event.isAutoRepeat():
            name = {Qt.Key.Key_P: "photo_keep", Qt.Key.Key_X: "photo_reject", Qt.Key.Key_U: "photo_unmark"}.get(key)
            browsing = focused is not None and (focused is self.active_viewer
                or self.active_thumbnail_panel.isAncestorOf(focused) or self._photo_browser.view.isAncestorOf(focused))
            if name is not None and browsing:
                self.window_actions.action(name).trigger()
                event.accept()
                return
        rating: int | None = None
        if Qt.Key.Key_0 <= key <= Qt.Key.Key_5:
            rating = key - Qt.Key.Key_0
        elif key == Qt.Key.Key_QuoteLeft:  # 反引号 ` = 清除（同 0）
            rating = 0

        if rating is not None:
            # 多选批量评分
            selected = self.active_thumbnail_panel.get_selected_paths()
            if len(selected) > 1:
                try:
                    self._photo_browser.service.rate(selected, rating)
                except ValueError as error:
                    self.show_toast(self._tr(str(error)))
                    event.accept()
                    return
                self.show_toast(
                    self._tr(
                        "status.rating_batch",
                        "Rated {n} images: {stars}",
                        n=len(selected),
                        stars=("★" * rating + "☆" * (5 - rating)) if rating > 0 else self._tr("status.rating_none", "(none)"),
                    )
                )
            elif self.manager.current_path:
                get_rating_store().set_rating(self.manager.current_path, rating)
                # 底部工具条由 _on_rating_changed 统一刷新
            event.accept()
            return

        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        """处理拖入：如果是文件则接受"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.viewer_empty.set_drag_active(True)

    def dragLeaveEvent(self, event):
        self.viewer_empty.set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        """处理松手：加载拖入的图片文件或文件夹"""
        self.viewer_empty.set_drag_active(False)
        urls = event.mimeData().urls()
        if not urls:
            return

        paths = [url.toLocalFile() for url in urls]

        if os.path.isdir(paths[0]):
            self._begin_browse(directory=paths[0], origin="drop")
        elif len(paths) == 1:
            self.open_image_path(paths[0], origin="drop")
        else:
            self._begin_browse(paths=paths, origin="drop")
