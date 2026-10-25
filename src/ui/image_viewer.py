import os
import logging
from dataclasses import dataclass
from PIL import Image
from PyQt6.QtCore import Qt, QSize, QEvent, QTimer, pyqtSignal, pyqtSlot, QRect, QRectF, QPointF
from PyQt6.QtGui import QBrush, QPixmap, QImage, QTransform, QWheelEvent, QMouseEvent, QPainter
from PyQt6.QtWidgets import QLabel, QSizePolicy, QScrollArea

from .services.image_load_runner import ImageLoadRunner
from ..application.image_load_queue import ImageLoadKey
from ..core.contracts.image_display import (DEFAULT_NON_RAW_DISPLAY_MODE, DEFAULT_RAW_DISPLAY_MODE,
                                            NonRawDisplayMode, RawDisplayMode)
from ..i18n import get_translation_service
from resources.styles.theme_variables import MAX_IMAGE_PIXELS, SPACING_SM, ZOOM_LIMITS, px
from .theme import current_theme_name, get_theme_tokens, to_qcolor


logger = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


@dataclass(frozen=True)
class DisplaySnapshot:
    path: str
    stamp: tuple[int, int, int]
    image: QImage
    reference_size: QSize
    raw_display_mode: RawDisplayMode = DEFAULT_RAW_DISPLAY_MODE
    non_raw_display_mode: NonRawDisplayMode = DEFAULT_NON_RAW_DISPLAY_MODE


# 主题名 -> 棋盘格的一块平铺图；透明的地方露出它，与画布底色区分开
_CHECKER_TILES: dict[str, QPixmap] = {}


def _checker_brush() -> QBrush:
    name = current_theme_name()
    tile = _CHECKER_TILES.get(name)
    if tile is None:
        tokens, cell = get_theme_tokens(), px(SPACING_SM)
        tile = QPixmap(cell * 2, cell * 2)
        tile.fill(to_qcolor(tokens["surface"]))
        painter = QPainter(tile)
        painter.fillRect(0, 0, cell, cell, to_qcolor(tokens["track"]))
        painter.fillRect(cell, cell, cell, cell, to_qcolor(tokens["track"]))
        painter.end()
        _CHECKER_TILES[name] = tile
    return QBrush(tile)


class ImageViewer(QLabel):
    """
    自定义图片查看组件，支持缩放、平移拖拽、适应窗口以及异步加载。
    图像与覆盖层使用同一显示矩形，绘制范围限制在当前视口。
    """
    loading_changed = pyqtSignal(bool)
    load_status_changed = pyqtSignal(str, bool)
    presentation_changed = pyqtSignal(object)
    view_changed = pyqtSignal()

    def __init__(self, parent=None, *, image_source_service=None, raw_display_mode=DEFAULT_RAW_DISPLAY_MODE,
                 non_raw_display_mode=DEFAULT_NON_RAW_DISPLAY_MODE, canvas_scale_runner=None):
        super().__init__(parent)
        self._zoom = 1.0
        self._pixmap: QPixmap | None = None
        self._bitmap_key = None
        self._companion = None
        self._scale_runner = canvas_scale_runner
        self._scale_deferred = False
        self._scale_owner = object()
        self._scaled_display = None
        self._scale_retained = []
        retained = self._scale_retained
        self.destroyed.connect(lambda: [packet.close() for packet in retained])
        self._scaled_pixmaps = {}
        self._scaled_key = self._scale_requested_key = self._scale_failed_key = None
        self._scale_timer = QTimer(self)
        self._scale_timer.setSingleShot(True)
        self._scale_timer.setInterval(120)
        self._scale_timer.timeout.connect(self._prepare_scale)
        self.view_changed.connect(self._queue_scale)
        if canvas_scale_runner is not None:
            canvas_scale_runner.ready.connect(self._on_scale_ready)
            owner = self._scale_owner
            self.destroyed.connect(lambda: canvas_scale_runner.cancel(owner))
        self._sampling = None
        self._display_size = QSize()
        self._drag_start = None
        self._drag_pos = None
        self._min_zoom = ZOOM_LIMITS["canvas_default"]["min"]
        self._max_zoom = ZOOM_LIMITS["canvas_default"]["max"]
        self._zoom_changed_callback = None
        self._screen_window = None
        self._fit_mode = True
        self._bottom_inset = 0
        self._image_sources = image_source_service
        self._load_runner = None
        if image_source_service is not None:
            self._load_runner = ImageLoadRunner(image_source_service, parent=self)
            self._load_runner.display_ready.connect(self._on_image_loaded)
            self._load_runner.quick_ready.connect(self._on_quick_loaded)
            self.destroyed.connect(self._load_runner.shutdown)
        self._load_key = None
        self._quick_key = None
        self._display_key = None
        self._loading = False
        self._raw_display_mode = RawDisplayMode(raw_display_mode)
        self._non_raw_display_mode = NonRawDisplayMode(non_raw_display_mode)
        self._terminal_memory_error = False
        self._quick_finished = False
        self._quick_displayed = False
        self._overlay = None
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._refit_viewport)
        self._wheel_timer = QTimer(self)
        self._wheel_timer.setSingleShot(True)
        self._wheel_timer.setInterval(16)
        self._wheel_timer.timeout.connect(self._apply_wheel_zoom)
        self._wheel_zoom = None
        self._wheel_anchor = None

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setObjectName("imageViewerLabel")

    def set_zoom_changed_callback(self, callback):
        """设置缩放比例改变时的回调函数"""
        self._zoom_changed_callback = callback

    @property
    def is_loading(self):
        return self._loading

    def _set_loading(self, loading):
        if self._loading != loading:
            self._loading = loading
            self.loading_changed.emit(loading)

    def clear(self):
        self._reset_scaled_display()
        self._companion = None
        self._replace_sampling(None)
        self._set_loading(False)
        self._load_key = None
        self._quick_key = None
        self._display_key = None
        self._terminal_memory_error = False
        self._quick_finished = False
        self._quick_displayed = False
        self.load_status_changed.emit("", False)
        if self._load_runner is not None:
            self._load_runner.replace([])
        self._fit_timer.stop()
        self._cancel_wheel_zoom()
        self._pixmap = None
        self._display_size = QSize()
        self.set_overlay(None)
        super().clear()
        self.setMinimumSize(0, 0)
        self.update()

    def set_image(self, path: str):
        self._replace_sampling(None)
        if self._load_runner is None:
            raise RuntimeError("文件加载需要注入源图服务")
        path = os.path.normpath(path)
        self._cancel_wheel_zoom()
        self._load_key = None
        self._quick_key = None
        self._display_key = None
        self._terminal_memory_error = False
        self._quick_finished = False
        self._quick_displayed = False
        self.load_status_changed.emit("", False)
        if self._load_runner is not None:
            self._load_runner.replace([])
        try:
            self._load_key = ImageLoadKey(path, self._image_sources.file_stamp(path), None,
                                          raw_display_mode=self._raw_display_mode,
                                          non_raw_display_mode=self._non_raw_display_mode)
        except OSError:
            self._show_load_error("error.source.not_found")
            return
        keys = [self._load_key]
        if self._image_sources.supports_quick_preview(path):
            self._quick_key = self._load_key._replace(stage="quick")
            keys.append(self._quick_key)
        self._set_loading(True)
        self._load_runner.replace(keys)

    def set_raw_display_mode(self, mode):
        mode = RawDisplayMode(mode)
        if mode == self._raw_display_mode:
            return
        self._raw_display_mode = mode
        if self._load_key is not None and self._image_sources.supports_quick_preview(self._load_key.path):
            self.set_image(self._load_key.path)

    def set_non_raw_display_mode(self, mode):
        mode = NonRawDisplayMode(mode)
        if mode == self._non_raw_display_mode:
            return
        self._non_raw_display_mode = mode
        if self._load_key is not None and not self._image_sources.supports_quick_preview(self._load_key.path):
            self.set_image(self._load_key.path)

    def retry_load(self):
        if self._load_key is not None:
            self.set_image(self._load_key.path)

    def _show_load_error(self, key, *, retry=False):
        service = get_translation_service()
        load_key = self._load_key
        self.clear()
        if retry:
            self._load_key = load_key
        self.setText(service.translate(key) if service else key)
        self.adjustSize()
        self.load_status_changed.emit(key, retry)

    def _on_image_loaded(self, request, qimage, error, reference_size):
        if request.key != self._load_key:
            return
        try:
            if self._image_sources.file_stamp(request.key.path) != request.key.stamp:
                self._show_load_error("error.source.changed")
                return
        except OSError:
            self._show_load_error("error.source.not_found")
            return
        if qimage is None:
            if isinstance(error, MemoryError):
                self._terminal_memory_error = True
                if self._quick_key is None or self._quick_finished:
                    self._finish_memory_error()
            else:
                self._show_load_error("error.source.decode_failed")
            return
        self._display_key = request.key
        self._quick_key = None
        self._load_runner.replace([])
        self._replace_loaded_image(qimage, reference_size)
        self._set_loading(False)
        self.load_status_changed.emit("", False)

    def _on_quick_loaded(self, request, qimage, reference_size):
        if not self.is_loading or request.key != self._quick_key:
            return
        self._quick_finished = True
        try:
            if self._image_sources.file_stamp(request.key.path) != request.key.stamp:
                self._show_load_error("error.source.changed")
                return
        except OSError:
            self._show_load_error("error.source.not_found")
            return
        if qimage is not None and not qimage.isNull() and reference_size is not None:
            self._replace_loaded_image(qimage, reference_size)
            self._quick_displayed = True
        if self._terminal_memory_error:
            self._finish_memory_error()

    def _finish_memory_error(self):
        if self._quick_displayed:
            self._display_key = None
            self._quick_key = None
            self._load_runner.replace([])
            self._set_loading(False)
            self.load_status_changed.emit("viewer.memory_camera_preview", True)
        else:
            self._show_load_error("error.image.memory_budget", retry=True)

    def _replace_loaded_image(self, qimage, reference_size):
        self._replace_sampling(None)
        scroll = self._scroll_area()
        position = ((scroll.horizontalScrollBar().value(), scroll.verticalScrollBar().value())
                    if scroll is not None else None)
        self.setText("")
        self._pixmap = QPixmap.fromImage(qimage)
        self._bitmap_key = qimage.cacheKey()
        self._display_size = QSize(*reference_size) if reference_size is not None else self._pixmap.size()
        if self._fit_mode:
            self._fit_to_window()
        else:
            self._update_display()
            if position is not None:
                scroll.horizontalScrollBar().setValue(position[0])
                scroll.verticalScrollBar().setValue(position[1])

    @property
    def has_image(self):
        return self._pixmap is not None and not self._pixmap.isNull()

    def display_snapshot(self, path: str) -> DisplaySnapshot | None:
        if (self.is_loading or not self.has_image or self._display_key is None
                or self._display_size.isEmpty()
                or os.path.normcase(os.path.normpath(path)) != os.path.normcase(self._display_key.path)):
            return None
        try:
            if self._image_sources.file_stamp(path) != self._display_key.stamp:
                return None
        except OSError:
            return None
        return DisplaySnapshot(self._display_key.path, self._display_key.stamp,
                               self._pixmap.toImage(), QSize(self._display_size), self._display_key.raw_display_mode,
                               self._display_key.non_raw_display_mode)

    def shutdown(self):
        self._reset_scaled_display()
        self._companion = None
        self._pixmap = None
        self._load_key = None
        self._quick_key = None
        self._display_key = None
        self._set_loading(False)
        self._fit_timer.stop()
        self._cancel_wheel_zoom()
        if self._load_runner is not None:
            self._load_runner.shutdown()

    def set_sampling(self, sampling):
        self._sampling = sampling

    def _replace_sampling(self, sampling):
        self.set_sampling(sampling)
        self.presentation_changed.emit(sampling)

    def set_pixmap(self, pixmap: QPixmap, preserve_zoom: bool = False, reference_size: QSize | None = None,
                   sampling=None):
        """直接设置 QPixmap 显示，跳过文件加载（用于内存中的即时刷新）。

        preserve_zoom=True：保持当前缩放/平移，用于连续刷新同一张图（如滑块拖拽预览）。
        reference_size：100% 显示时的像素边界；代理图按原比例放入该边界。
        """
        self._replace_sampling(sampling)
        self._set_loading(False)
        self._load_key = None
        self._quick_key = None
        self._display_key = None
        self.load_status_changed.emit("", False)
        if not preserve_zoom:
            self._cancel_wheel_zoom()
        if self._load_runner is not None:
            self._load_runner.replace([])
        self._pixmap = pixmap
        self._bitmap_key = pixmap.cacheKey()
        self.setText("")
        self._display_size = (
            pixmap.size().scaled(reference_size, Qt.AspectRatioMode.KeepAspectRatio)
            if reference_size is not None else pixmap.size()
        )
        if preserve_zoom and not self._fit_mode:
            self._update_display()
        else:
            self._fit_mode = True
            self._fit_to_window()

    def set_preview_image(self, qimage: QImage, preserve_zoom: bool = True, reference_size: QSize | None = None,
                          sampling=None) -> None:
        """从内存中的 QImage 直接设置预览图，用于面板实时刷新。"""
        if qimage is None or qimage.isNull():
            return
        pixmap = QPixmap.fromImage(qimage)
        if pixmap.isNull():
            raise MemoryError()
        self.set_pixmap(pixmap, preserve_zoom=preserve_zoom, reference_size=reference_size, sampling=sampling)
        self._bitmap_key = qimage.cacheKey()
        self._queue_scale()

    def set_qimage(self, qimage: QImage, preserve_zoom: bool = False, reference_size: QSize | None = None,
                   sampling=None) -> None:
        """从内存中的 QImage 直接设置并显示图像。"""
        self.set_preview_image(qimage, preserve_zoom=preserve_zoom, reference_size=reference_size, sampling=sampling)

    def replace_display_image(self, image: QImage) -> None:
        if image is not None and not image.isNull() and self._pixmap is not None:
            self.replace_display_pixmap(QPixmap.fromImage(image))
            self._bitmap_key = image.cacheKey()
            self._queue_scale()

    def replace_display_pixmap(self, pixmap: QPixmap) -> None:
        if pixmap is not None and not pixmap.isNull() and self._pixmap is not None:
            self._pixmap = pixmap
            self._bitmap_key = pixmap.cacheKey()
            self._queue_scale()
            self.update()

    def set_comparison_image(self, image, reference_size=None):
        self._companion = ((image, tuple(reference_size or (image.width(), image.height())))
                           if image is not None and not image.isNull() else None)
        self._queue_scale()

    def set_display_pair(self, image, reference_size, companion=None, companion_reference=None, *, sampling=None,
                         preserve_zoom=True):
        self._scale_deferred = True
        try:
            self.set_qimage(image, preserve_zoom=preserve_zoom, reference_size=reference_size, sampling=sampling)
            self.set_comparison_image(companion, companion_reference)
        finally:
            self._scale_deferred = False
        self._queue_scale()

    def _scale_spec(self):
        if self._scale_runner is None or not self.has_image:
            return None, ()
        dpr = self.devicePixelRatioF()
        sources = [(self._bitmap_key, None, (self._display_size.width(), self._display_size.height()),
                    (self._pixmap.width(), self._pixmap.height()))]
        if self._companion is not None:
            image, reference = self._companion
            if image.cacheKey() != self._bitmap_key:
                sources.append((image.cacheKey(), image, reference, (image.width(), image.height())))
        entries = []
        for identity, image, reference, size in sources:
            target = QSize(max(1, round(reference[0] * self._zoom * dpr)),
                           max(1, round(reference[1] * self._zoom * dpr)))
            if max(target.width() / size[0], target.height() / size[1]) < 0.5:
                entries.append((identity, image, target))
        key = (tuple(sorted((identity, size.width(), size.height()) for identity, _, size in entries)), dpr)
        return (key if entries else None), entries

    def _reset_scaled_display(self):
        self._scale_timer.stop()
        if self._scale_runner is not None:
            self._scale_runner.cancel(self._scale_owner)
        self._scale_requested_key = None
        self._scaled_key = None
        self._scaled_pixmaps.clear()
        self._scale_retained.clear()
        display, self._scaled_display = self._scaled_display, None
        if display is not None:
            display.close()

    def _queue_scale(self):
        if self._scale_deferred:
            return
        key, _ = self._scale_spec()
        if key is None:
            self._reset_scaled_display()
            self.update()
            return
        if key == self._scaled_key and key is not None:
            return
        if key != self._scale_requested_key:
            self._reset_scaled_display()
        if key is not None and key != self._scale_failed_key and key != self._scale_requested_key:
            self._scale_timer.start()
        self.update()

    def _prepare_scale(self):
        key, entries = self._scale_spec()
        if key is None or key == self._scaled_key or not self.isVisible():
            return
        images = tuple((identity, self._pixmap.toImage() if image is None else image, size)
                       for identity, image, size in entries)
        self._scale_requested_key = key
        if not self._scale_runner.submit(self._scale_owner, key, images):
            self._scale_requested_key = None
            self._scale_failed_key = key

    def _on_scale_ready(self, owner, key, packet):
        if owner is not self._scale_owner:
            return
        current, _ = self._scale_spec()
        if current != key or not self.isVisible():
            if packet is not None:
                packet.close()
            return
        self._scale_requested_key = None
        if packet is None:
            self._scale_failed_key = key
            return
        try:
            pixmaps = {}
            for identity, image in packet.images.items():
                pixmap = QPixmap.fromImage(image)
                if pixmap.isNull():
                    raise MemoryError()
                pixmap.setDevicePixelRatio(key[1])
                pixmaps[identity] = pixmap
        except MemoryError:
            packet.close()
            self._scale_failed_key = key
            return
        old, self._scaled_display = self._scaled_display, packet
        self._scaled_key = key
        packet.pixmaps.update(pixmaps)
        self._scaled_pixmaps = packet.pixmaps
        self._scale_retained[:] = [packet]
        if old is not None:
            old.close()
        self.update()

    def set_overlay(self, overlay) -> None:
        """安装或卸载覆盖层。"""
        if overlay is not None:
            self.install_overlay(overlay)
        else:
            self.uninstall_overlay()

    def install_overlay(self, overlay) -> None:
        """安装交互覆盖层（如裁剪、套索等）。"""
        if self._overlay is overlay:
            return
        self.uninstall_overlay()
        self._overlay = overlay
        if hasattr(overlay, "set_viewer"):
            overlay.set_viewer(self)
        self.update()

    def uninstall_overlay(self) -> None:
        """卸载交互覆盖层。"""
        if hasattr(self._overlay, "set_viewer"):
            self._overlay.set_viewer(None)
        self._overlay = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def set_bottom_inset(self, height: int) -> None:
        """设置下沿被浮动工具条占用的高度。适应窗口时图像完整显示在它上方，其余倍率下不受影响。"""
        height = max(0, int(height))
        if height == self._bottom_inset:
            return
        self._bottom_inset = height
        if self._fit_mode and self._pixmap:
            self._fit_to_window()

    def _fit_inset(self) -> int:
        return self._bottom_inset if self._fit_mode else 0

    def _widget_size(self) -> QSize:
        """控件尺寸：适应窗口时在图像下方多留出保留高度，使图像在可见区域内居中。"""
        size = self._scaled_size()
        return QSize(size.width(), size.height() + self._fit_inset())

    def get_image_display_rect(self) -> QRect:
        """图像与覆盖层共用的控件坐标矩形。"""
        if not self._pixmap:
            return self.rect()
        size = self._scaled_size()
        x = max(0, (self.width() - size.width()) // 2)
        y = max(0, (self.height() - self._fit_inset() - size.height()) // 2)
        return QRect(x, y, size.width(), size.height())

    def display_color_at(self, x: float, y: float) -> tuple[int, int, int] | None:
        """当前显示图像在归一化坐标处的颜色；没有图像或坐标越界时返回 None。"""
        if not self.has_image or not (0 <= x < 1 and 0 <= y < 1):
            return None
        image = self._pixmap.toImage()
        color = image.pixelColor(min(image.width() - 1, int(x * image.width())),
                                 min(image.height() - 1, int(y * image.height())))
        return color.red(), color.green(), color.blue()

    def _scaled_size(self):
        return QSize(max(1, int(self._display_size.width() * self._zoom)),
                     max(1, int(self._display_size.height() * self._zoom)))

    def sizeHint(self):
        return self._widget_size() if self._pixmap else super().sizeHint()

    def rotate_display(self, angle: int):
        """使用 QTransform 即时旋转显示图片（不触发文件 I/O）。
        angle 遵循 Qt 惯例：正值逆时针，负值顺时针。
        """
        if self.is_loading or not self._pixmap:
            return
        self._replace_sampling(None)
        transform = QTransform()
        transform.rotate(angle)
        self._cancel_wheel_zoom()
        self._pixmap = self._pixmap.transformed(
            transform, Qt.TransformationMode.FastTransformation
        )
        self._bitmap_key = self._pixmap.cacheKey()
        self._display_size = transform.mapRect(QRect(0, 0, self._display_size.width(), self._display_size.height())).size()
        if self._fit_mode:
            self._fit_to_window()
        else:
            self._update_display()

    def flip_display(self, horizontal: bool = True):
        """使用 QTransform 即时翻转显示图片。"""
        if self.is_loading or not self._pixmap:
            return
        self._replace_sampling(None)
        transform = QTransform()
        if horizontal:
            transform.scale(-1, 1)
        else:
            transform.scale(1, -1)
        self._cancel_wheel_zoom()
        self._pixmap = self._pixmap.transformed(
            transform, Qt.TransformationMode.FastTransformation
        )
        self._bitmap_key = self._pixmap.cacheKey()
        if self._fit_mode:
            self._fit_to_window()
        else:
            self._update_display()

    def _fit_to_window(self):
        if not self._pixmap:
            return
        margin = 8
        viewport = self.parentWidget()
        if viewport:
            avail_w = viewport.width() - margin * 2
            avail_h = viewport.height() - margin * 2 - self._bottom_inset
        else:
            avail_w = self.width() - margin * 2
            avail_h = self.height() - margin * 2 - self._bottom_inset

        if avail_w <= 30 or avail_h <= 30:
            return

        scale_w = avail_w / self._display_size.width()
        scale_h = avail_h / self._display_size.height()
        self._zoom = min(scale_w, scale_h)
        self._update_display()

    def showEvent(self, event):
        super().showEvent(event)
        self._queue_scale()
        window = self.window().windowHandle()
        if window is not None and window is not self._screen_window:
            if self._screen_window is not None:
                self._screen_window.screenChanged.disconnect(self._on_screen_changed)
            self._screen_window = window
            window.screenChanged.connect(self._on_screen_changed)
        viewport = self.parentWidget()
        if viewport is not None:
            viewport.installEventFilter(self)
        self._fit_timer.start(0)

    def hideEvent(self, event):
        self._reset_scaled_display()
        self._fit_timer.stop()
        self._cancel_wheel_zoom()
        viewport = self.parentWidget()
        if viewport is not None:
            viewport.removeEventFilter(self)
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            if self._fit_mode:
                self._fit_timer.start(0)
        return super().eventFilter(watched, event)

    def event(self, event):
        result = super().event(event)
        if event.type() == getattr(QEvent.Type, "DevicePixelRatioChange", None):
            self.view_changed.emit()
        return result

    def _on_screen_changed(self, screen):
        QTimer.singleShot(0, self.view_changed.emit)

    def _refit_viewport(self):
        if self.isVisible() and self._fit_mode and self._pixmap:
            self._fit_to_window()

    def _update_display(self):
        if not self._pixmap:
            return
        size = self._widget_size()
        self.setMinimumSize(size)
        self.resize(size)
        self.updateGeometry()
        self.update()
        if self._zoom_changed_callback:
            self._zoom_changed_callback()
        self.view_changed.emit()

    def _scroll_area(self):
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                return parent
            parent = parent.parentWidget()
        return None

    @property
    def space_pan_active(self):
        return getattr(self, "_space_pan", False)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_pan = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_pan = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._space_pan = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().focusOutEvent(event)

    def view_state(self):
        if self._pixmap is None:
            return None
        scroll = self._scroll_area()
        rect = self.get_image_display_rect()
        point = self.mapFrom(scroll.viewport(), scroll.viewport().rect().center()) if scroll is not None else rect.center()
        return self._fit_mode, self._zoom, ((point.x() - rect.x()) / max(1, rect.width()),
                                           (point.y() - rect.y()) / max(1, rect.height()))

    def restore_view_state(self, state):
        if state is None or self._pixmap is None:
            return
        fit, zoom, center = state
        if fit:
            self.zoom_fit()
            return
        self._set_zoom(zoom)
        scroll = self._scroll_area()
        if scroll is not None:
            rect = self.get_image_display_rect()
            point = self.mapFrom(scroll.viewport(), scroll.viewport().rect().center())
            scroll.horizontalScrollBar().setValue(scroll.horizontalScrollBar().value() + round(rect.x() + center[0] * rect.width() - point.x()))
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().value() + round(rect.y() + center[1] * rect.height() - point.y()))

    def _set_zoom(self, zoom, anchor=None):
        if not self._pixmap:
            return
        scroll = self._scroll_area()
        position = None
        if scroll is not None:
            viewport = scroll.viewport()
            anchor = anchor if anchor is not None else viewport.rect().center()
            local = self.mapFrom(viewport, anchor)
            rect = self.get_image_display_rect()
            position = QPointF((local.x() - rect.x()) / rect.width(),
                               (local.y() - rect.y()) / rect.height())
        self._fit_mode = False
        self._zoom = zoom
        self._update_display()
        if position is not None:
            rect = self.get_image_display_rect()
            local = self.mapFrom(scroll.viewport(), anchor)
            horizontal = scroll.horizontalScrollBar()
            vertical = scroll.verticalScrollBar()
            horizontal.setValue(horizontal.value() + round(rect.x() + position.x() * rect.width() - local.x()))
            vertical.setValue(vertical.value() + round(rect.y() + position.y() * rect.height() - local.y()))

    def _cancel_wheel_zoom(self):
        self._wheel_timer.stop()
        self._wheel_zoom = None
        self._wheel_anchor = None

    def _apply_wheel_zoom(self):
        zoom, anchor = self._wheel_zoom, self._wheel_anchor
        self._wheel_zoom = self._wheel_anchor = None
        if zoom is not None:
            self._set_zoom(zoom, anchor)

    @pyqtSlot()
    def zoom_in(self, factor=1.25):
        """放大"""
        if not self._pixmap:
            return
        self._cancel_wheel_zoom()
        self._set_zoom(min(self._zoom * factor, self._max_zoom))

    @pyqtSlot()
    def zoom_out(self, factor=0.8):
        """缩小"""
        if not self._pixmap:
            return
        self._cancel_wheel_zoom()
        self._set_zoom(max(self._zoom * factor, self._min_zoom))

    @pyqtSlot()
    def zoom_fit(self):
        """自适应窗口模式"""
        self._cancel_wheel_zoom()
        self._fit_mode = True
        self._fit_to_window()

    @pyqtSlot(int)
    def set_zoom_percent(self, percent: int):
        """设置特定的缩放百分比 (5% - 500%)"""
        if not self._pixmap:
            return
        percent = max(5, min(500, percent))
        new_zoom = percent / 100.0
        self._cancel_wheel_zoom()
        
        # 如果比例没有变化且不在自适应模式，则跳过重绘
        if abs(self._zoom - new_zoom) < 0.001 and not self._fit_mode:
            return
            
        self._set_zoom(new_zoom)

    def get_zoom_percent(self) -> int:
        """获取当前缩放百分比"""
        return int(self._zoom * 100)

    def is_fit_mode(self) -> bool:
        """是否处于适应窗口模式"""
        return self._fit_mode

    def wheelEvent(self, event: QWheelEvent):
        """鼠标滚轮事件：Ctrl + 滚轮执行缩放"""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier and self._pixmap:
            delta = event.angleDelta().y()
            if not delta:
                event.accept()
                return
            zoom = self._wheel_zoom if self._wheel_zoom is not None else self._zoom
            self._wheel_zoom = max(self._min_zoom, min(self._max_zoom,
                zoom * 1.25 ** (delta / 120)))
            scroll = self._scroll_area()
            self._wheel_anchor = (self.mapTo(scroll.viewport(), event.position().toPoint())
                                  if scroll is not None else None)
            if not self._wheel_timer.isActive():
                self._wheel_timer.start()
            event.accept()
        else:
            super().wheelEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._pixmap:
            return
        painter = QPainter(self)
        painter.setClipRegion(event.region())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.get_image_display_rect()
        visible = rect.intersected(event.rect())
        if not visible.isEmpty():
            scale_x = self._pixmap.width() / rect.width()
            scale_y = self._pixmap.height() / rect.height()
            source = QRectF((visible.x() - rect.x()) * scale_x,
                            (visible.y() - rect.y()) * scale_y,
                            visible.width() * scale_x, visible.height() * scale_y)
            if self._pixmap.hasAlphaChannel():
                # 棋盘格跟着照片的左上角走，平移时不会相对照片滑动
                painter.setBrushOrigin(rect.topLeft())
                painter.fillRect(visible, _checker_brush())
            scaled = self._scaled_pixmaps.get(self._bitmap_key)
            if scaled is not None:
                painter.drawPixmap(QPointF(rect.topLeft()), scaled)
            else:
                painter.drawPixmap(QRectF(visible), self._pixmap, source)
        if self._overlay is not None:
            self._overlay.paint(painter, rect)

    def mousePressEvent(self, event: QMouseEvent):
        """鼠标按下：优先询问覆盖层，否则左键准备拖动图片"""
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if getattr(self, "_overlay", None) is not None and not self.space_pan_active:
            if self._overlay.handle_mouse_press(event, self.get_image_display_rect()):
                event.accept()
                return

        if event.button() == Qt.MouseButton.LeftButton and self._pixmap:
            self._drag_start = event.globalPosition().toPoint()
            self._drag_pos = event.globalPosition().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent):
        """鼠标移动：优先询问覆盖层，否则实现图片平移"""
        if getattr(self, "_overlay", None) is not None and not self.space_pan_active:
            if self._overlay.handle_mouse_move(event, self.get_image_display_rect()):
                event.accept()
                return

        if self._drag_start is not None and self._pixmap:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self._drag_pos = event.globalPosition().toPoint()
            scroll_area = self._scroll_area()
                
            if scroll_area:
                h_bar = scroll_area.horizontalScrollBar()
                v_bar = scroll_area.verticalScrollBar()
                h_bar.setValue(h_bar.value() - delta.x())
                v_bar.setValue(v_bar.value() - delta.y())

    def mouseReleaseEvent(self, event: QMouseEvent):
        """鼠标释放：优先询问覆盖层，否则停止拖动"""
        if getattr(self, "_overlay", None) is not None:
            if self._overlay.handle_mouse_release(event, self.get_image_display_rect()):
                event.accept()
                return

        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
