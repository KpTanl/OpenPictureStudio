"""Base overlay interface for spatial interactive tools."""

from PyQt6.QtCore import QObject, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QPainter, QMouseEvent


class ViewerOverlay(QObject):
    """画在 ImageViewer 上的空间交互覆盖层基类。

    为裁剪（Crop）、九宫格（GridCrop）、遮罩（ShapeCrop）、套索（Lasso）提供：
      - paint(painter, image_rect)
      - handle_mouse_press(event, image_rect) -> bool (True 表示拦截消费)
      - handle_mouse_move(event, image_rect) -> bool
      - handle_mouse_release(event, image_rect) -> bool
      - handle_key_press(key) -> bool
      - geometry_changed 信号
      - drag_started、drag_finished 信号：一次拖动的首尾，使用方据此把途中的多次变化记成一步
    """

    geometry_changed = pyqtSignal()
    drag_started = pyqtSignal()
    drag_finished = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._viewer = None

    def set_viewer(self, viewer) -> None:
        if viewer is None:
            self.cancel_interaction()
        self._viewer = viewer

    def is_dragging(self) -> bool:
        """是否正在拖动（按下之后、松开之前）。"""
        return False

    def cancel_interaction(self) -> None:
        """从画布上取下时调用：进行到一半的拖动到此为止。"""

    def update_view(self) -> None:
        if self._viewer is not None and hasattr(self._viewer, "update"):
            self._viewer.update()

    def set_cursor(self, cursor: Qt.CursorShape) -> None:
        if self._viewer is not None and hasattr(self._viewer, "setCursor"):
            self._viewer.setCursor(cursor)

    def paint(self, painter: QPainter, image_rect: QRect) -> None:
        """在绘制完图片后调用，绘制选框、辅助线或蒙版路径。"""
        pass

    def handle_mouse_press(self, event: QMouseEvent, image_rect: QRect) -> bool:
        """鼠标按下事件。返回 True 拦截默认拖拽平移。"""
        return False

    def handle_mouse_move(self, event: QMouseEvent, image_rect: QRect) -> bool:
        """鼠标移动事件。返回 True 拦截默认拖拽平移。"""
        return False

    def handle_mouse_release(self, event: QMouseEvent, image_rect: QRect) -> bool:
        """鼠标释放事件。返回 True 拦截默认拖拽平移。"""
        return False

    def handle_key_press(self, key: int) -> bool:
        """宿主把没有别人处理的按键交给覆盖层。返回 True 表示已处理。"""
        return False
