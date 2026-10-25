from __future__ import annotations

import os
import logging
from bisect import bisect_left
from collections import OrderedDict, deque
from time import perf_counter

from PyQt6.QtCore import Qt, QSize, QEvent, QRectF, QTimer, pyqtSignal, pyqtSlot, QMimeData, QSettings, QItemSelectionModel
from PyQt6.QtGui import QAction, QPixmap, QIcon, QColor, QDrag, QWheelEvent, QFont, QPainter, QPen, QKeySequence
from PyQt6.QtWidgets import (
    QWidget, QListWidget, QListWidgetItem,
    QLabel, QHBoxLayout, QSplitter, QStyledItemDelegate, QSizePolicy
)
from ...utils.app_info import APP_NAME
from ..services.image_load_runner import ImageLoadRunner
from ...application.image_load_queue import ImageLoadKey
from ...core.rating_store import get_rating_store, matches_rating
from ...i18n import get_translation_service
from ..theme import get_theme_tokens, to_qcolor
from .action_button import ActionIconButton
from .apple_menu import AppleDropDown
from .apple_button import AppleCompactButton
from .rating_filters import RATING_FILTERS
from .tooltip import set_tooltip
from resources.styles.theme_variables import (
    CANVAS_COLORS, CONTROL_HEIGHT_COMPACT, FILMSTRIP_HEIGHT, SPACING_MD, SPACING_SM, SPACING_XS,
    THUMBNAIL_CACHE_SIZE, px,
)

# 每个格子在缩略图之外的留白：样式表里选中描边 2 像素、内边距 1 像素，四周共 6 像素，再留 2 像素间隙
THUMB_GRID_HORIZONTAL_PADDING = 8
THUMB_GRID_VERTICAL_PADDING = 8
# 胶片条高度里缩略图以外的部分：上下边距、格子留白、3 像素的位置条与 1 像素间隙
PANEL_VERTICAL_MARGIN = 6
PANEL_CHROME = 2 * PANEL_VERTICAL_MARGIN + THUMB_GRID_VERTICAL_PADDING + 4
DEFAULT_THUMB_WIDTH = FILMSTRIP_HEIGHT - PANEL_CHROME
MIN_THUMB_WIDTH = 40
MAX_THUMB_WIDTH = 240
LOAD_THUMBNAIL_SIZE = QSize(MAX_THUMB_WIDTH, MAX_THUMB_WIDTH)
THUMB_ZOOM_STEP = 8
SAVE_DELAY_MS = 300
# 评分角标底的不透明度
BADGE_OPACITY = 0.85
MAX_CACHE_SIZE = THUMBNAIL_CACHE_SIZE
LIST_BATCH_SIZE = 128
LIST_BATCH_SECONDS = 0.004


logger = logging.getLogger(__name__)


class _LRUCache:
    def __init__(self, max_size=MAX_CACHE_SIZE):
        self._data: OrderedDict = OrderedDict()
        self._max_size = max_size

    def get(self, key):
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return None

    def put(self, key, value):
        if key in self._data:
            self._data.move_to_end(key)
        else:
            self._data[key] = value
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def clear(self):
        self._data.clear()


class _ThumbnailDelegate(QStyledItemDelegate):
    """缩略图的叠加层：有未应用暂存的在右上角画圆点，已评分的在右下角画星数。

    取值在绘制时读取当前主题。
    """

    STAR = "★"

    def __init__(self, parent=None, edit_state_store=None):
        super().__init__(parent)
        self._edit_state_store = edit_state_store
        self._browser = None

    def set_browser(self, service):
        self._browser = service

    def paint(self, painter: QPainter, option, index) -> None:
        super().paint(painter, option, index)

        path = index.data(Qt.ItemDataRole.UserRole)
        if not path:
            return

        rect = option.rect
        tokens = get_theme_tokens()
        surface = to_qcolor(tokens["surface"])
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.parent().currentIndex() == index:
            painter.setPen(QPen(to_qcolor(tokens["accent"]), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(rect).adjusted(1, 1, -1, -1), 3, 3)

        edited = (self._browser.photo_state(path)["edited"] if self._browser is not None
                  else self._edit_state_store is not None and self._edit_state_store.has_pending(path))
        if edited:
            dot_r = 4
            painter.setPen(surface)
            painter.setBrush(to_qcolor(tokens["warning"]))
            painter.drawEllipse(rect.right() - dot_r * 2 - 5, rect.top() + 5, dot_r * 2, dot_r * 2)

        mark = self._browser.metadata.get_mark(path) if self._browser is not None else "unmarked"
        if mark != "unmarked":
            painter.setPen(to_qcolor(tokens["accent"] if mark == "keep" else tokens["warning"]))
            painter.drawText(rect.adjusted(5, 2, -5, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                             "✓" if mark == "keep" else "×")

        rating = get_rating_store().get_rating(path)
        if rating > 0:
            # 字号随缩略图宽度变化
            font = QFont(painter.font())
            font.setPointSize(max(7, min(12, rect.width() // 14)))
            font.setBold(True)
            painter.setFont(font)
            text = self.STAR * rating
            fm = painter.fontMetrics()
            tw, th = fm.horizontalAdvance(text), fm.height()
            pad_x, pad_y = 4, 1
            x = rect.right() - tw - pad_x - 4
            y = rect.bottom() - 4
            badge = QColor(surface)
            badge.setAlphaF(BADGE_OPACITY)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(badge)
            painter.drawRoundedRect(QRectF(x - pad_x, y - th - pad_y, tw + 2 * pad_x, th + 2 * pad_y), 3, 3)
            painter.setPen(to_qcolor(tokens["rating"]))
            painter.drawText(x, y - fm.descent(), text)
        painter.restore()


class _ReorderListWidget(QListWidget):
    """自定义列表组件：在 IconMode 下实现严格顺序的拖拽重排。
    
    Qt 默认的 IconMode + InternalMove 会将 item 放到鼠标释放的 2D 坐标上，
    导致 item 间出现间隙或位置混乱。本类完全接管 dropEvent，
    根据释放位置计算目标索引，通过 takeItem + insertItem 保证严格顺序。
    """
    reordered = pyqtSignal()
    thumb_zoom_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_start_row = -1

    def startDrag(self, supportedActions):
        """手动创建拖拽操作。
        
        不调用 super().startDrag()，因为 Qt 默认实现会在 drag.exec()
        返回 MoveAction 后自动删除源 item，与我们 dropEvent 中的
        takeItem + insertItem 冲突，导致 item 被双重删除而消失。
        """
        if not self.dragEnabled():
            return
        self._drag_start_row = self.currentRow()
        item = self.currentItem()
        if item is None:
            return

        drag = QDrag(self)
        mime = self.mimeData(self.selectedItems())
        if mime is None:
            mime = QMimeData()

        drag.setMimeData(mime)
        if not item.icon().isNull():
            pixmap = item.icon().pixmap(self.iconSize())
            drag.setPixmap(pixmap)
            drag.setHotSpot(pixmap.rect().center())

        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event):
        if event.source() is self:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.source() is self:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if not self.dragEnabled():
            event.ignore()
            return
        source_row = self._drag_start_row
        self._drag_start_row = -1

        if source_row < 0 or source_row >= self.count():
            event.ignore()
            return

        pos = event.position().toPoint()
        target_row = self._calc_insert_index(pos)

        if target_row == source_row or target_row == source_row + 1:
            event.acceptProposedAction()
            return

        item = self.takeItem(source_row)
        if item is None:
            event.ignore()
            return

        if source_row < target_row:
            target_row -= 1

        self.insertItem(target_row, item)
        self.setCurrentItem(item)
        self.reordered.emit()
        event.acceptProposedAction()

    def _calc_insert_index(self, pos):
        hit = self.itemAt(pos)
        if hit:
            row = self.row(hit)
            rect = self.visualItemRect(hit)
            if pos.x() > rect.center().x():
                return row + 1
            return row

        for i in range(self.count()):
            rect = self.visualItemRect(self.item(i))
            if pos.x() < rect.center().x():
                return i
        return self.count()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.SelectAll) and not self.dragEnabled():
            event.ignore()
            return
        super().keyPressEvent(event)

    def selectAll(self):
        if self.dragEnabled():
            super().selectAll()

    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = 1 if event.angleDelta().y() > 0 else -1
            self.thumb_zoom_requested.emit(delta)
            event.accept()
        else:
            super().wheelEvent(event)


class ThumbnailPanel(QWidget):
    image_selected = pyqtSignal(str)
    order_changed = pyqtSignal(list)
    building_changed = pyqtSignal(bool)
    listing_ready = pyqtSignal(int)
    collapsed_changed = pyqtSignal(bool)
    rating_filter_requested = pyqtSignal(str)  # 用户在筛选下拉里选了一项
    batch_requested = pyqtSignal()
    browser_requested = pyqtSignal()
    selection_changed = pyqtSignal()
    mark_requested = pyqtSignal(str)
    rating_requested = pyqtSignal(int)

    def __init__(self, parent=None, *, image_source_service):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        settings = QSettings(APP_NAME, "ThumbnailPanel")
        saved = settings.value("thumbSize", DEFAULT_THUMB_WIDTH, type=int)
        self._thumb_width = max(MIN_THUMB_WIDTH, min(MAX_THUMB_WIDTH, saved))
        # 用户拖过或滚轮调过之后以用户的选择为准，否则跟随窗口尺寸给出的默认高度
        self._thumb_chosen = settings.value("thumbSize") is not None
        self._collapsed = False

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(px(SPACING_MD), PANEL_VERTICAL_MARGIN, px(SPACING_MD), PANEL_VERTICAL_MARGIN)
        self._layout.setSpacing(px(SPACING_SM))

        self._cache = _LRUCache()
        self._image_sources = image_source_service
        self._load_runner = ImageLoadRunner(self._image_sources, thumbnail=True, parent=self)
        self._load_runner.ready.connect(self._on_thumbnail_ready)
        self.destroyed.connect(self._load_runner.shutdown)
        self._viewport_timer = QTimer(self)
        self._viewport_timer.setSingleShot(True)
        self._viewport_timer.timeout.connect(self._refresh_requests)
        self._filtered_paths = []
        self._loading_paused = False
        self._wanted_keys = {}
        self._icon_keys = {}
        self._generation = 0
        self._path_to_item: dict[str, QListWidgetItem] = {}
        self._all_paths: tuple[str, ...] = ()
        self._path_indices = {}
        self._list_version = 0
        self._built_indices = []
        self._visible_indices = []
        self._priority_indices = deque()
        self._filter_pending = deque()
        self._build_cursor = 0
        self._select_pending = None
        self._restored_selection_paths = set()
        self._building_notified = False
        self._completed_generation = None
        self._work_started = perf_counter()
        self._build_timer = QTimer(self)
        self._build_timer.setSingleShot(True)
        self._build_timer.timeout.connect(self._build_batch)
        self._closed = False
        self._rating_filter: str = "all"
        self._filter_revision = 0
        self._browser = None
        self._browser_runner = None
        self._browser_results = None
        self._browser_channel = "filmstrip:" + str(id(self))

        self._placeholder_icon = self._make_placeholder()
        self._error_icon = self._make_error_icon()

        position = QHBoxLayout()
        position.setSpacing(px(SPACING_XS))
        self._position_current = QLabel(self)
        self._position_current.setObjectName("filmstripCurrent")
        self._position_total = QLabel(self)
        self._position_total.setObjectName("filmstripTotal")
        position.addWidget(self._position_current)
        position.addWidget(self._position_total)
        self._layout.addLayout(position)
        self._navigation_buttons: list[ActionIconButton] = []

        self.list_widget = _ReorderListWidget()
        self.list_widget.installEventFilter(self)
        self.list_widget.viewport().installEventFilter(self)
        self.list_widget.horizontalScrollBar().valueChanged.connect(self._schedule_requests)
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setFlow(QListWidget.Flow.LeftToRight)
        self.list_widget.setWrapping(False)
        self._apply_thumb_size()
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Fixed)
        self.list_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list_widget.horizontalScrollBar().setObjectName("thumbnailHorizontalScrollBar")
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.setHorizontalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)

        self.list_widget.setDragEnabled(True)
        self.list_widget.setAcceptDrops(True)
        self.list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.reordered.connect(self._on_reordered)
        self.list_widget.thumb_zoom_requested.connect(self._on_thumb_zoom)

        svc = get_translation_service()
        if svc:
            svc.add_observer(self._on_language_changed)

        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_widget.currentItemChanged.connect(lambda *_: self._update_position())
        self._layout.addWidget(self.list_widget, 1)

        self._filter_dropdown = AppleDropDown(self)
        self._filter_dropdown.setFixedHeight(CONTROL_HEIGHT_COMPACT)
        self._filter_dropdown.activated.connect(
            lambda index: self.rating_filter_requested.emit(self._filter_dropdown.itemData(index)))
        self._layout.addWidget(self._filter_dropdown)
        self._batch_button = AppleCompactButton(self._tr("batch.title", "Batch Adjustments…"), self)
        self._batch_button.clicked.connect(self.batch_requested)
        self._layout.addWidget(self._batch_button)
        self._browser_button = AppleCompactButton(self._tr("browser.title", "Photo Selection"), self)
        self._browser_button.clicked.connect(self.browser_requested)
        self._layout.addWidget(self._browser_button)
        self._populate_filters()
        self._update_position()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._save_thumb_width)
        self._watched_splitter = None
        self._set_height_limits()

        # 安装评分星标 delegate + 订阅 rating_changed 触发重绘
        self._rating_delegate = _ThumbnailDelegate(self.list_widget)
        self.list_widget.setItemDelegate(self._rating_delegate)
        get_rating_store().rating_changed.connect(self._on_rating_changed)
        self._edit_state_store = None

        if settings.value("collapsed", False, type=bool):
            self._collapsed = True
            self._set_height_limits()

    # ---- 翻页、收起与高度 ----
    def set_navigation_actions(self, previous: QAction, following: QAction) -> None:
        """在缩略图两端放上一张、下一张的按钮；动作由调用方提供。"""
        if self._navigation_buttons:
            return
        before = ActionIconButton(previous, "chevron-left", self, size=CONTROL_HEIGHT_COMPACT)
        after = ActionIconButton(following, "chevron-right", self, size=CONTROL_HEIGHT_COMPACT)
        self._layout.insertWidget(0, before)
        self._layout.insertWidget(self._layout.indexOf(self._filter_dropdown), after)
        self._navigation_buttons = [before, after]

    def set_collapsed(self, collapsed: bool) -> None:
        """收起时高度为零，列表与选中状态保留；展开后回到当前这一张。"""
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._set_height_limits()
        # 分隔条记着这一格原来的高度，高度上限变成零也不会自己把它让给画布，必须明确交还
        self._request_height(self._panel_height(self._thumb_width))
        if not collapsed:
            QTimer.singleShot(0, self._scroll_to_current)
        QSettings(APP_NAME, "ThumbnailPanel").setValue("collapsed", collapsed)
        self.collapsed_changed.emit(collapsed)

    def _scroll_to_current(self) -> None:
        item = self.list_widget.currentItem()
        if item is not None and not self._closed:
            self.list_widget.scrollToItem(item)

    def set_default_height(self, height: int) -> None:
        """窗口尺寸给出的默认高度；用户自己选过大小之后不再跟随。"""
        width = max(MIN_THUMB_WIDTH, min(MAX_THUMB_WIDTH, int(height) - PANEL_CHROME))
        if self._thumb_chosen or width == self._thumb_width:
            return
        self._thumb_width = width
        if not self._collapsed:
            self._request_height(self._panel_height(width))

    def toggle_collapse(self) -> None:
        self.set_collapsed(not self._collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def showEvent(self, event) -> None:
        super().showEvent(event)
        splitter = self.parentWidget()
        if isinstance(splitter, QSplitter) and splitter is not self._watched_splitter:
            self._watched_splitter = splitter
            splitter.splitterMoved.connect(self._on_splitter_moved)
        QTimer.singleShot(0, lambda: self._request_height(self._panel_height(self._thumb_width)))
        self._schedule_requests()
        self._queue_build()

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), 0 if self._collapsed else self._panel_height(self._thumb_width))

    def resizeEvent(self, event) -> None:
        """缩略图跟着实际高度变大变小，始终完整显示。"""
        super().resizeEvent(event)
        if not self._collapsed:
            self._apply_thumb_size(self._fitted_width())
            self._schedule_requests()

    def _fitted_width(self) -> int:
        return max(MIN_THUMB_WIDTH, min(MAX_THUMB_WIDTH, self.height() - PANEL_CHROME))

    def _on_splitter_moved(self, *_args) -> None:
        """用户拖动了分隔条：把当前高度对应的大小记为新的选择。"""
        if not self._collapsed and self._fitted_width() != self._thumb_width:
            self._thumb_width = self._fitted_width()
            self._thumb_chosen = True
            self._save_timer.start()
            QTimer.singleShot(0, self._scroll_to_current)

    def _set_height_limits(self) -> None:
        if self._collapsed:
            self.setFixedHeight(0)
        else:
            self.setMinimumHeight(self._panel_height(MIN_THUMB_WIDTH))
            self.setMaximumHeight(self._panel_height(MAX_THUMB_WIDTH))
        self.updateGeometry()

    def _request_height(self, height: int) -> None:
        """向所在的分隔条要一个高度；不在分隔条里时直接调整自己。收起时高度限制为零，要到的就是零。"""
        if self._closed:
            return
        height = max(self.minimumHeight(), min(self.maximumHeight(), int(height)))
        splitter = self.parentWidget()
        if isinstance(splitter, QSplitter) and splitter.count() == 2:
            index = splitter.indexOf(self)
            sizes = splitter.sizes()
            total = sum(sizes)
            if total > height:
                sizes[index] = height
                sizes[1 - index] = total - height
                splitter.setSizes(sizes)
        else:
            self.resize(self.width(), height)

    def _save_thumb_width(self) -> None:
        QSettings(APP_NAME, "ThumbnailPanel").setValue("thumbSize", self._thumb_width)

    def hideEvent(self, event):
        self._build_timer.stop()
        self._load_runner.replace([])
        if self._browser_runner is not None:
            self._clear_browser_images("")
            self._browser_runner.forget(self._browser_channel)
        super().hideEvent(event)
        self._notify_building()

    def eventFilter(self, obj, event) -> bool:
        if (obj is self.list_widget and self._browser is not None and event.type() == QEvent.Type.KeyPress
                and event.modifiers() == Qt.KeyboardModifier.NoModifier and not event.isAutoRepeat()):
            value = {Qt.Key.Key_P: "keep", Qt.Key.Key_X: "reject", Qt.Key.Key_U: "unmarked"}.get(event.key())
            if value is not None:
                self.mark_requested.emit(value)
                return True
            if Qt.Key.Key_0 <= event.key() <= Qt.Key.Key_5 or event.key() == Qt.Key.Key_QuoteLeft:
                self.rating_requested.emit(0 if event.key() == Qt.Key.Key_QuoteLeft else event.key() - Qt.Key.Key_0)
                return True
        if obj is self.list_widget.viewport() and event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide):
            self._schedule_requests()
            QTimer.singleShot(0, self._queue_build)
        return super().eventFilter(obj, event)

    @staticmethod
    def _panel_height(thumb_width: int) -> int:
        return thumb_width + PANEL_CHROME

    def set_edit_state_store(self, store) -> None:
        """接入编辑暂存状态仓库，订阅暂存变化并同步角标。"""
        self._edit_state_store = store
        self._rating_delegate._edit_state_store = store
        if store:
            store.stash_changed.connect(self._on_stash_changed)

    def _on_stash_changed(self, _path: str) -> None:
        self.list_widget.viewport().update()

    def set_browser(self, service, runner):
        self._browser, self._browser_runner = service, runner
        self._rating_delegate.set_browser(service)
        runner.ready.connect(self._browser_image_ready)
        runner.clearing.connect(self._clear_browser_images)
        self.project_browser()

    def project_browser(self):
        if self._browser is None:
            return
        results = set(self._browser.results)
        self._rating_filter = self._browser.filters.rating
        self._filter_revision = self._browser.revision
        index = self._filter_dropdown.findData(self._rating_filter)
        self._filter_dropdown.setCurrentIndex(index)
        if results != self._browser_results:
            self._browser_results = results
            self._update_filter_visibility()
        selected = {self._browser.key(path) for path in self._browser.selected}
        blocked = self.list_widget.blockSignals(True)
        try:
            for path, item in self._path_to_item.items():
                item.setSelected(self._browser.key(path) in selected)
        finally:
            self.list_widget.blockSignals(blocked)
        self._restored_selection_paths = set(self._browser.selected) - self._path_to_item.keys()
        self.list_widget.viewport().update()
        self._schedule_requests()

    def _browser_image_ready(self, path):
        if path in self._path_to_item:
            self._schedule_requests()

    def _clear_browser_images(self, path):
        for target, item in self._path_to_item.items():
            if not path or self._browser.key(path) == self._browser.key(target):
                item.setIcon(self._placeholder_icon)
                self._icon_keys.pop(target, None)

    def _tr(self, key: str, default: str, **kwargs) -> str:
        svc = get_translation_service()
        if svc:
            return svc.translate(key, default=default, **kwargs)
        return default.format(**kwargs) if kwargs else default

    def _update_position(self) -> None:
        """位置：当前这一张在可见照片里的序号与总数；列表还在准备时显示进度。"""
        total = len(self._all_paths)
        if self.has_pending_items:
            self._position_current.setText("")
            self._position_total.setText(self._tr(
                "thumbnail.preparing_count", "Preparing {count}/{total} images…",
                count=self.list_widget.count(), total=total))
            return
        if self._browser is not None or self._rating_filter and self._rating_filter != "all":
            total = len(self._filtered_paths)
        item = self.list_widget.currentItem()
        index = self._path_indices.get(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None
        place = bisect_left(self._visible_indices, index) if index is not None else len(self._visible_indices)
        shown = place < len(self._visible_indices) and self._visible_indices[place] == index
        self._position_current.setText(str(place + 1) if shown else "–")
        self._position_total.setText(f"/ {total}")

    def _populate_filters(self) -> None:
        current = self._filter_dropdown.currentIndex()
        self._filter_dropdown.clear()
        for mode, key, default in RATING_FILTERS:
            self._filter_dropdown.addItem(self._tr(key, default), mode)
        self._filter_dropdown.setCurrentIndex(max(0, current))
        set_tooltip(self._filter_dropdown, self._tr("thumbnail.filter_tip", "Choose which photos to show by rating"))

    def apply_rating_filter(self, mode: str):
        self._filter_revision += 1
        self._rating_filter = mode
        index = self._filter_dropdown.findData(mode)
        if index >= 0:
            self._filter_dropdown.setCurrentIndex(index)
        self._update_filter_visibility()

    def _matches_rating_filter(self, path: str) -> bool:
        if self._browser is not None:
            return path in (self._browser_results or ())
        if not self._rating_filter or self._rating_filter == "all":
            return True
        return matches_rating(get_rating_store().get_rating(path), self._rating_filter)

    def _update_filter_visibility(self):
        self._generation += 1
        self._completed_generation = None
        self._filter_pending = deque(self._built_indices)
        self._filtered_paths = []
        self._visible_indices = []
        self._queue_build()
        self._update_position()

    def _apply_item_filter(self, index, item):
        path = self._all_paths[index]
        matches = self._matches_rating_filter(path)
        item.setHidden(not matches)
        position = bisect_left(self._visible_indices, index)
        present = position < len(self._visible_indices) and self._visible_indices[position] == index
        if matches and not present:
            self._visible_indices.insert(position, index)
            self._filtered_paths.insert(position, path)
        elif not matches and present:
            self._visible_indices.pop(position)
            self._filtered_paths.pop(position)

    def retranslate_ui(self):
        self._browser_button.setText(self._tr("browser.title", "Photo Selection"))
        self._batch_button.setText(self._tr("batch.title", "Batch Adjustments…"))
        self._populate_filters()
        self._update_position()

    def _on_language_changed(self, lang_code: str):
        self.retranslate_ui()

    def _apply_thumb_size(self, w=None):
        w = self._thumb_width if w is None else w
        if self.list_widget.iconSize().width() == w:
            return
        self.list_widget.setIconSize(QSize(w, w))
        self.list_widget.setGridSize(QSize(
            w + THUMB_GRID_HORIZONTAL_PADDING, w + THUMB_GRID_VERTICAL_PADDING,
        ))

    def _on_thumb_zoom(self, delta):
        """Ctrl 加滚轮调整胶片条的高度，缩略图随高度变化。"""
        width = max(MIN_THUMB_WIDTH, min(MAX_THUMB_WIDTH, self._thumb_width + delta * THUMB_ZOOM_STEP))
        if width != self._thumb_width:
            self._thumb_width = width
            self._thumb_chosen = True
            self._request_height(self._panel_height(width))
            self._save_timer.start()
            QTimer.singleShot(0, self._scroll_to_current)

    @property
    def list_version(self):
        return self._list_version

    @property
    def has_pending_items(self):
        return len(self._built_indices) < len(self._all_paths) or bool(self._filter_pending)

    @property
    def is_building(self):
        return self.has_pending_items and self.isVisible()

    @property
    def first_matching_path(self):
        return self._filtered_paths[0] if self._filtered_paths else None

    def _notify_building(self):
        busy = self.is_building and not self._closed
        if busy != self._building_notified:
            self._building_notified = busy
            self.building_changed.emit(busy)

    def _queue_build(self):
        if self._closed:
            return
        self.list_widget.setDragEnabled(not self.has_pending_items)
        self.list_widget.setAcceptDrops(not self.has_pending_items)
        if self.is_building:
            self._build_timer.start(0)
        self._notify_building()
        if not self.has_pending_items and self._completed_generation != self._generation:
            self._completed_generation = self._generation
            self._update_position()
            logger.debug("胶片条列表 generation=%s count=%s elapsed_ms=%.1f",
                         self._generation, len(self._all_paths), (perf_counter() - self._work_started) * 1000)
            self.listing_ready.emit(self._list_version)

    def load_images(self, image_paths: list[str], current_path: str = None, *, version=None):
        if self._closed:
            return
        paths = tuple(image_paths)
        if version is not None and version == self._list_version and paths == self._all_paths:
            if current_path:
                self.select_image(current_path)
            return
        self._build_timer.stop()
        selected = set(self.get_selected_paths()) | self._restored_selection_paths
        self._generation += 1
        self._list_version = self._list_version + 1 if version is None else version
        self._completed_generation = None
        self._work_started = perf_counter()
        self._load_runner.replace([])
        self._wanted_keys.clear()
        self._icon_keys.clear()
        self._path_to_item.clear()
        self._all_paths = paths
        self._path_indices = {path: index for index, path in enumerate(paths)}
        self._restored_selection_paths = selected.intersection(self._path_indices)
        self._built_indices = []
        self._visible_indices = []
        self._filtered_paths = []
        self._filter_pending.clear()
        self._priority_indices.clear()
        self._build_cursor = 0
        blocked = self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self.list_widget.blockSignals(blocked)
        self._select_pending = current_path
        if current_path in self._path_indices:
            self._prioritize_path(current_path)
        self._update_position()
        self._queue_build()

    def _prioritize_path(self, path):
        index = self._path_indices[path]
        self._priority_indices = deque([index] + [
            nearby for nearby in range(max(0, index - 64), min(len(self._all_paths), index + 65))
            if nearby != index and self._all_paths[nearby] not in self._path_to_item])

    def _build_batch(self):
        if self._closed or not self.is_building:
            self._notify_building()
            return
        generation = self._generation
        started = perf_counter()
        cell = max(1, self.list_widget.gridSize().width())
        first = self.list_widget.horizontalScrollBar().value() // cell
        anchor = self._path_to_item.get(self._filtered_paths[first]) if first < len(self._filtered_paths) else None
        anchor_left = self.list_widget.visualItemRect(anchor).left() if anchor is not None else 0
        blocked = self.list_widget.blockSignals(True)
        try:
            for _ in range(LIST_BATCH_SIZE):
                if generation != self._generation or self._closed:
                    return
                if self._filter_pending:
                    index = self._filter_pending.popleft()
                    self._apply_item_filter(index, self._path_to_item[self._all_paths[index]])
                else:
                    index = None
                    while self._priority_indices:
                        candidate = self._priority_indices.popleft()
                        if self._all_paths[candidate] not in self._path_to_item:
                            index = candidate
                            break
                    if index is None:
                        while self._build_cursor < len(self._all_paths):
                            candidate = self._build_cursor
                            self._build_cursor += 1
                            if self._all_paths[candidate] not in self._path_to_item:
                                index = candidate
                                break
                    if index is None:
                        break
                    path = self._all_paths[index]
                    item = QListWidgetItem()
                    item.setData(Qt.ItemDataRole.UserRole, path)
                    item.setToolTip(os.path.basename(path))
                    item.setIcon(self._placeholder_icon)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                                  Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
                    position = bisect_left(self._built_indices, index)
                    self._built_indices.insert(position, index)
                    self.list_widget.insertItem(position, item)
                    self._path_to_item[path] = item
                    self._apply_item_filter(index, item)
                    if path in self._restored_selection_paths:
                        item.setSelected(True)
                        self._restored_selection_paths.discard(path)
                if perf_counter() - started >= LIST_BATCH_SECONDS:
                    break
        finally:
            self.list_widget.blockSignals(blocked)
        self.list_widget.doItemsLayout()
        if anchor is not None:
            scroll = self.list_widget.horizontalScrollBar()
            scroll.setValue(scroll.value() + self.list_widget.visualItemRect(anchor).left() - anchor_left)
        if self._select_pending in self._path_to_item:
            path, self._select_pending = self._select_pending, None
            item = self._path_to_item[path]
            blocked = self.list_widget.blockSignals(True)
            self.list_widget.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
            item.setSelected(True)
            self.list_widget.scrollToItem(item)
            self.list_widget.blockSignals(blocked)
        self._update_position()
        self._schedule_requests()
        self._queue_build()

    def select_image(self, path: str):
        item = self._path_to_item.get(path)
        if not item:
            if path in self._path_indices:
                self._select_pending = path
                self._prioritize_path(path)
                self._queue_build()
            return
        self._select_pending = None
        if self._browser is not None:
            blocked = self.list_widget.blockSignals(True)
            self.list_widget.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
            self.list_widget.blockSignals(blocked)
            self.project_browser()
            self.list_widget.scrollToItem(item)
            return
        if self.list_widget.currentItem() is item and item.isSelected():
            return
        self._restored_selection_paths.clear()
        self.list_widget.blockSignals(True)
        self.list_widget.clearSelection()
        item.setSelected(True)
        self.list_widget.setCurrentItem(item)
        self.list_widget.scrollToItem(item)
        self.list_widget.blockSignals(False)
        self._update_position()
        self._schedule_requests()

    def update_image_path(self, old_path: str, new_path: str, *, version=None):
        if old_path not in self._path_indices:
            return
        index = self._path_indices.pop(old_path)
        paths = list(self._all_paths)
        paths[index] = new_path
        self._all_paths = tuple(paths)
        self._path_indices[new_path] = index
        if old_path in self._restored_selection_paths:
            self._restored_selection_paths.remove(old_path)
            self._restored_selection_paths.add(new_path)
        if version is not None:
            self._list_version = version
        item = self._path_to_item.pop(old_path, None)
        if item is None:
            if self._select_pending == old_path:
                self._select_pending = new_path
            self._queue_build()
            return
        item.setData(Qt.ItemDataRole.UserRole, new_path)
        item.setToolTip(os.path.basename(new_path))
        self._path_to_item[new_path] = item
        item.setIcon(self._placeholder_icon)
        self._icon_keys.pop(old_path, None)
        self._icon_keys.pop(new_path, None)
        self._update_filter_visibility()
        self.select_image(new_path)

    def get_all_paths(self) -> list[str]:
        return list(self._all_paths)

    def get_selected_paths(self) -> list[str]:
        if self._browser is not None:
            return list(self._browser.selected)
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.list_widget.selectedItems()
        ]

    def selection_snapshot(self, active_path=None):
        if self._browser is not None:
            return self._browser.selection()
        from ...application.photo_batch import PhotoSelection
        key = lambda path: os.path.normcase(os.path.abspath(path))
        selected = {key(path) for path in set(self.get_selected_paths()) | self._restored_selection_paths}
        seen, paths = set(), []
        for path in self._all_paths:
            identity = key(path)
            if identity not in seen and self._matches_rating_filter(path):
                seen.add(identity)
                paths.append(path)
        filtered = tuple(paths)
        return PhotoSelection(tuple(path for path in filtered if key(path) in selected), filtered,
                              active_path, self._list_version, self._filter_revision)

    def _schedule_requests(self, *_):
        if self._closed:
            return
        self._viewport_timer.start(0)

    def set_loading_paused(self, paused):
        self._loading_paused = paused
        if paused:
            self._viewport_timer.stop()
            self._load_runner.replace([])
            if self._browser_runner is not None:
                self._browser_runner.forget(self._browser_channel)
        else:
            self._schedule_requests()

    def _refresh_requests(self):
        if self._closed or self._loading_paused:
            return
        paths = []
        if self.isVisible() and self.list_widget.isVisible() and self._filtered_paths:
            cell = max(1, self.list_widget.gridSize().width())
            first = self.list_widget.horizontalScrollBar().value() // cell
            count = self.list_widget.viewport().width() // cell + 2
            start, end = max(0, first - 4), min(len(self._filtered_paths), first + count + 4)
            indices = list(range(start, end))
            current = self.list_widget.currentItem()
            selected = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
            indices.sort(key=lambda index: (self._filtered_paths[index] != selected,
                                            not first <= index < first + count, abs(index - first)))
            limit = self._image_sources.policy.thumbnail_pending + self._image_sources.policy.thumbnail_slots
            paths = [self._filtered_paths[index] for index in indices[:limit]]
        rendered = set()
        if self._browser_runner is not None and self._browser.sessions is not None:
            # 编辑结果就是源画面的照片沿用源缩略图，其余取编辑结果
            ordered = [path for path in paths if not self._browser.shows_source(path)]
            rendered = set(ordered)
            self._browser_runner.request(self._browser_channel, ordered, maximum=MAX_THUMB_WIDTH, square=True)
            for path in paths:
                item = self._path_to_item.get(path)
                if item is None:
                    continue
                details = [os.path.basename(path), self._tr("browser.mark." + self._browser.metadata.get_mark(path), "")]
                if path in rendered:
                    image = self._browser_runner.image(path, maximum=MAX_THUMB_WIDTH, square=True)
                    if path not in self._icon_keys or self._icon_keys[path] is not image:
                        item.setIcon(QIcon(image.pixmap) if image is not None and image.pixmap is not None
                                     else self._error_icon if image is not None and image.error else self._placeholder_icon)
                    self._icon_keys[path] = image
                    if image is None or image.error:
                        details.append(self._tr(image.error if image is not None else "browser.preparing", ""))
                item.setToolTip("\n".join(details))
        wanted = {}
        pending = []
        for path in paths:
            item = self._path_to_item.get(path)
            if item is None or path in rendered:
                continue
            try:
                key = ImageLoadKey(path, self._image_sources.file_stamp(path), (MAX_THUMB_WIDTH, MAX_THUMB_WIDTH))
            except OSError:
                item.setIcon(self._error_icon)
                continue
            wanted[path] = key
            if self._icon_keys.get(path) == key:
                continue
            cached = self._cache.get(key)
            if cached is not None:
                item.setIcon(QIcon(cached))
                self._icon_keys[path] = key
            else:
                item.setIcon(self._placeholder_icon)
                pending.append(key)
        for path in set(self._icon_keys) - set(wanted) - rendered:
            item = self._path_to_item.get(path)
            if item is not None:
                item.setIcon(self._placeholder_icon)
            self._icon_keys.pop(path, None)
        self._wanted_keys = wanted
        self._load_runner.replace(pending)

    def _on_thumbnail_ready(self, request, qimage, error):
        path = request.key.path
        if self._wanted_keys.get(path) != request.key:
            return
        try:
            if self._image_sources.file_stamp(path) != request.key.stamp:
                self._schedule_requests()
                return
        except OSError:
            qimage = None
        item = self._path_to_item.get(path)
        if item is None:
            return
        if qimage is None:
            item.setIcon(self._error_icon)
        else:
            pixmap = QPixmap.fromImage(qimage)
            self._cache.put(request.key, pixmap)
            item.setIcon(QIcon(pixmap))
        self._icon_keys[path] = request.key
        self._schedule_requests()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._build_timer.stop()
        self._viewport_timer.stop()
        self._load_runner.shutdown()
        if self._browser_runner is not None:
            self._browser_runner.forget(self._browser_channel)
        self._cache.clear()
        self._priority_indices.clear()
        self._filter_pending.clear()
        self._all_paths = ()
        self._path_indices.clear()
        self._restored_selection_paths.clear()
        self._filtered_paths.clear()
        self._visible_indices.clear()
        self._built_indices.clear()
        self._path_to_item.clear()
        self._notify_building()

    def _on_item_clicked(self, item):
        if self._browser is not None:
            from PyQt6.QtWidgets import QApplication
            if QApplication.keyboardModifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
                return
        path = item.data(Qt.ItemDataRole.UserRole)
        self.image_selected.emit(path)

    def _on_selection_changed(self):
        self._restored_selection_paths.clear()
        if self._browser is not None:
            self._browser.select([item.data(Qt.ItemDataRole.UserRole) for item in self.list_widget.selectedItems()
                                  if not item.isHidden()])
            self.selection_changed.emit()

    @pyqtSlot(str, int)
    def _on_rating_changed(self, path: str, value: int):
        """评分变化时仅重绘那一项，若有筛选模式则动态调整隐藏状态。"""
        if self._browser is not None:
            self.list_widget.viewport().update()
            return
        self._filter_revision += 1
        item = self._path_to_item.get(path)
        if item is None:
            return
        idx = self.list_widget.indexFromItem(item)
        if idx.isValid():
            self.list_widget.update(idx)
        if self._rating_filter and self._rating_filter != "all":
            self._completed_generation = None
            self._filter_pending.append(self._path_indices[path])
            self._queue_build()

    @pyqtSlot()
    def _on_reordered(self):
        """拖拽重排完成后，重建 path → item 映射并通知外部"""
        if self.has_pending_items:
            return
        self._path_to_item.clear()
        paths = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            path = item.data(Qt.ItemDataRole.UserRole)
            self._path_to_item[path] = item
            paths.append(path)
        self._all_paths = tuple(paths)
        self._path_indices = {path: index for index, path in enumerate(paths)}
        self._built_indices = list(range(len(paths)))
        self._update_filter_visibility()
        self.order_changed.emit(self.get_all_paths())

    @staticmethod
    def _make_placeholder():
        pm = QPixmap(LOAD_THUMBNAIL_SIZE)
        pm.fill(QColor(*CANVAS_COLORS["placeholder_thumb"]))
        return QIcon(pm)

    @staticmethod
    def _make_error_icon():
        pm = QPixmap(LOAD_THUMBNAIL_SIZE)
        pm.fill(QColor(*CANVAS_COLORS["error_thumb"]))
        return QIcon(pm)
