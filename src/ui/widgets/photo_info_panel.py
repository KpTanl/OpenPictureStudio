"""照片信息：文件名与拍摄时间、拍摄参数四格、设备、源文件。

``PhotoInfoPanel`` 展示一张照片的信息并表达读取请求，读取由宿主的后台服务完成；
带直方图时，直方图按宿主交来的当前画面统计结果显示，面板只表达“现在需要”与“不再需要”；
``PhotoInfoSidebar`` 是工作区右侧承载它的一栏，随面板的展开状态显示或收起。
"""

import math
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QTextOption
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QTextEdit,
                             QVBoxLayout, QWidget)

from resources.styles.theme_variables import (INFO_SIDEBAR_WIDTH, SIDEBAR_MAX_WIDTH, SIDEBAR_MIN_WIDTH, SPACING_LG,
                                              SPACING_MD, SPACING_SM, SPACING_XS, px)
from ...application.image_info import ImageInfo, ImageInfoQuery, ImageInfoRequest, ImageInfoResult
from ..themed_mixin import ThemedI18nMixin
from .histogram_widget import HistogramWidget
from .notice_bar import NoticeBar


_QUAD = ("shutter", "aperture", "iso", "focal_length")
_GROUPS = (
    ("capture", ("focal_length_35mm", "exposure_bias", "exposure_program", "metering_mode", "white_balance", "flash")),
    ("equipment", ("camera_make", "camera_model", "lens_make", "lens_model")),
    ("file", ("dimensions", "size_bytes", "file_format", "color_info", "path")),
)
# 值很长且没有空格可断的字段：名称独占一行，值在下一行占满整行并可在任意位置换行
_FULL_WIDTH = frozenset(("path",))
_FILE_FIELDS = frozenset(_GROUPS[2][1])
_ENUMS = {
    "exposure_program": {0: "undefined", 1: "manual", 2: "normal", 3: "aperture_priority",
                         4: "shutter_priority", 5: "creative", 6: "action", 7: "portrait", 8: "landscape"},
    "metering_mode": {0: "unknown", 1: "average", 2: "center_weighted", 3: "spot",
                      4: "multi_spot", 5: "pattern", 6: "partial", 255: "other"},
    "white_balance": {0: "auto", 1: "manual"},
    "flash": {False: "not_fired", True: "fired"},
}
_PLACEHOLDER = "—"


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


class _ElidedLabel(QLabel):
    """单行文字，放不下时省略中段。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._full_text = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def full_text(self) -> str:
        return self._full_text

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, self.contentsRect().width()))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_full_text(self._full_text)


class _WrappedText(QTextEdit):
    """可选中复制的只读文字，高度随内容，放不下时在任意位置换行。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setTabChangesFocus(True)
        self.document().setDocumentMargin(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:
        if text != self.toPlainText():
            self.setPlainText(text)
        self._fit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def wheelEvent(self, event) -> None:
        # 自身没有可滚动的内容，滚轮交给所在的滚动区
        event.ignore()

    def _fit(self) -> None:
        self.document().setTextWidth(self.viewport().width())
        self.setFixedHeight(math.ceil(self.document().size().height()))


class _CompressibleLabel(QLabel):
    """空间不足时让文字被裁掉，不把所在的一栏撑宽。"""

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())


def _value_label(parent: QWidget, object_name: str, kind=QLabel) -> QLabel:
    label = kind(parent)
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                  | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return label


def _rule(parent: QWidget, *, vertical: bool = False) -> QFrame:
    line = QFrame(parent)
    line.setObjectName("photoInfoRule")
    if vertical:
        line.setFixedWidth(1)
    else:
        line.setFixedHeight(1)
    return line


class PhotoInfoPanel(QWidget, ThemedI18nMixin):
    info_requested = pyqtSignal(object)
    reading_stopped = pyqtSignal()
    expanded_changed = pyqtSignal(bool)
    path_changed = pyqtSignal()
    metadata_changed = pyqtSignal(object)
    # 带直方图的面板：展开着并且有照片时请求当前画面的直方图，收起、停用或没有照片时通知不再需要
    histogram_requested = pyqtSignal()
    histogram_stopped = pyqtSignal()

    def __init__(self, parent=None, *, read_summary=False, histogram=False):
        super().__init__(parent)
        self._path = None
        self._request = None
        self._result = None
        self._histogram_request = None
        self._read_summary = read_summary
        self._active = False
        self._expanded = False
        self._headings = {}
        self._labels = {}
        self._values = {}
        self._init_themed_i18n()
        self.destroyed.connect(self._cleanup_themed_i18n)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_MD))

        self._histogram = HistogramWidget(self) if histogram else None
        if self._histogram is not None:
            layout.addWidget(self._histogram)

        self._status = QLabel(self)
        self._status.setObjectName("mutedText")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._notice = NoticeBar("warning", parent=self)
        self._notice.action_triggered.connect(lambda: self._request_info(force=True))
        layout.addWidget(self._notice)

        headline = QVBoxLayout()
        headline.setSpacing(px(SPACING_XS))
        self._name = _ElidedLabel(self)
        self._name.setObjectName("photoInfoName")
        headline.addWidget(self._name)
        self._time = _value_label(self, "photoInfoTime")
        self._time.setWordWrap(True)
        headline.addWidget(self._time)
        layout.addLayout(headline)

        for index, (group, names) in enumerate(_GROUPS):
            if index:
                layout.addWidget(_rule(self))
            section = QVBoxLayout()
            section.setSpacing(px(SPACING_SM))
            heading = QLabel(self)
            heading.setObjectName("photoInfoGroup")
            self._headings[group] = heading
            section.addWidget(heading)
            if group == "capture":
                section.addLayout(self._build_quad())
            section.addLayout(self._build_rows(names))
            layout.addLayout(section)
        self.retranslate_ui()
        self.hide()

    def _build_quad(self) -> QHBoxLayout:
        quad = QHBoxLayout()
        quad.setSpacing(0)
        for index, name in enumerate(_QUAD):
            if index:
                quad.addWidget(_rule(self, vertical=True))
            cell = QVBoxLayout()
            cell.setContentsMargins(px(SPACING_SM) if index else 0, 0, px(SPACING_SM), 0)
            cell.setSpacing(0)
            value = _value_label(self, "photoInfoQuadValue", _CompressibleLabel)
            label = _CompressibleLabel(self)
            label.setObjectName("photoInfoQuadLabel")
            label.setBuddy(value)
            self._labels[name], self._values[name] = label, value
            cell.addWidget(value)
            cell.addWidget(label)
            quad.addLayout(cell, 1)
        return quad

    def _build_rows(self, names) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(px(SPACING_LG))
        grid.setVerticalSpacing(px(SPACING_SM))
        grid.setColumnStretch(1, 1)
        row = 0
        for name in names:
            label = QLabel(self)
            label.setObjectName("photoInfoFieldName")
            if name in _FULL_WIDTH:
                value = _WrappedText(self)
                value.setObjectName("photoInfoLongValue")
                grid.addWidget(label, row, 0, 1, 2)
                grid.addWidget(value, row + 1, 0, 1, 2)
                row += 2
            else:
                value = _value_label(self, "photoInfoValue")
                value.setWordWrap(True)
                value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
                grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
                grid.addWidget(value, row, 1)
                row += 1
            label.setBuddy(value)
            self._labels[name], self._values[name] = label, value
        return grid

    @property
    def image_path(self) -> str | None:
        return self._path

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_path(self, path: str | None):
        self._path = path
        self._request = None
        self._result = None
        self.clear_histogram()
        self.metadata_changed.emit(None)
        if path is None:
            self.set_expanded(False)
        self._render()
        self.path_changed.emit()
        self._request_info()
        self.refresh_histogram()

    def set_expanded(self, expanded: bool):
        expanded = bool(expanded and self._path is not None)
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self.setVisible(expanded)
        self.expanded_changed.emit(expanded)
        self._request_info()
        self.refresh_histogram()

    def set_active(self, active: bool):
        if active == self._active:
            return
        self._active = active
        if active:
            self._request_info()
        else:
            self._request = None
            self.reading_stopped.emit()
        self.refresh_histogram()

    @property
    def histogram_wanted(self) -> bool:
        return bool(self._histogram is not None and self._active and self._expanded and self._path)

    def refresh_histogram(self) -> None:
        """按现在的状态重新表达是否需要直方图；宿主在画面换成新的终态后也调用它。"""
        if self._histogram is None:
            return
        if self.histogram_wanted:
            self.histogram_requested.emit()
        else:
            self._histogram_request = None
            self.histogram_stopped.emit()

    def clear_histogram(self) -> None:
        """当前画面不再是直方图所统计的那一帧：清空显示，此前请求的结果不再接收。"""
        self._histogram_request = None
        if self._histogram is not None:
            self._histogram.clear()

    def set_histogram_request(self, request) -> None:
        if self.histogram_wanted and request.path == self._path:
            self._histogram_request = request

    def set_histogram(self, request, histogram: dict) -> None:
        if self._histogram is not None and request == self._histogram_request:
            self._histogram.set_histogram_data(histogram)

    def _request_info(self, *, force=False):
        if not self._active:
            return
        if not self._path or self._read_summary or self._expanded:
            self.info_requested.emit(ImageInfoQuery(self._path, self._expanded, force))
        else:
            self._request = None
            self.reading_stopped.emit()

    def cleanup(self):
        self.set_active(False)
        self._request = None
        self._result = None
        self._cleanup_themed_i18n()

    def set_request(self, request: ImageInfoRequest):
        if not self._active or request.path != self._path:
            return
        self._request = request
        self._result = None
        self.metadata_changed.emit(None)
        self._render()

    def set_result(self, result: ImageInfoResult):
        if not self._active or result.request != self._request:
            return
        self._result = result
        self._render()
        self.metadata_changed.emit(result.info)

    def retranslate_ui(self):
        for name, label in self._headings.items():
            label.setText(self._tr(f"photo_info.group.{name}"))
        for name, label in self._labels.items():
            label.setText(self._tr(f"photo_info.field.{name}"))
        self._render()

    def _field_text(self, name: str, info: ImageInfo) -> tuple[str, bool]:
        """字段的显示文字，以及它是不是照片里实际记录的值（否则文字说明未记录或无法读取）。"""
        if name == "dimensions":
            return f"{info.width} × {info.height}", True
        if name == "size_bytes":
            return _format_size(info.size_bytes), True
        if name == "color_info" and info.kind == "RAW":
            return self._tr("photo_info.raw_color"), True
        if name in _FILE_FIELDS:
            return getattr(info, name), True
        value = getattr(info.photo, name)
        if value is None:
            return self._tr("photo_info.unreadable" if name in info.photo.unreadable
                            else "photo_info.not_recorded"), False
        if name in _ENUMS:
            code = _ENUMS[name].get(value)
            return (self._tr(f"photo_info.{name}.{code}") if code
                    else self._tr("photo_info.unknown_value", value=value)), True
        return value, True

    def _render(self):
        result = self._result
        info = result.info if result is not None else None
        error = result.error if result is not None else None
        loading = bool(self._path) and result is None
        self._status.setText(self._tr("photo_info.loading") if loading else "")
        self._status.setVisible(loading)
        issue_key = error.message_key if error is not None else info.issue_key if info is not None else ""
        retry = error is not None or issue_key == "photo_info.metadata_failed"
        self._notice.set_text(self._tr(issue_key) if issue_key else "")
        self._notice.set_action_text(self._tr("common.retry", "Retry") if retry else "")
        self._notice.setVisible(bool(issue_key))

        path = info.path if info is not None else self._path
        self._name.set_full_text(Path(path).name if path else "")
        self._name.setToolTip(path or "")
        self._name.setAccessibleName(f"{self._tr('photo_info.field.filename')}: {self._name.full_text()}")
        captured_label = self._tr("photo_info.field.captured_at")
        if info is None:
            captured = ""
        else:
            captured, recorded = self._field_text("captured_at", info)
            if not recorded:
                captured = f"{captured_label} {_PLACEHOLDER} {captured}"
        self._time.setText(captured)
        self._time.setVisible(bool(captured))
        self._time.setAccessibleName(f"{captured_label}: {captured}")

        for name, label in self._values.items():
            if info is None:
                text, recorded = (self._path, True) if name == "path" and self._path else (_PLACEHOLDER, True)
            else:
                text, recorded = self._field_text(name, info)
            described = f"{self._labels[name].text()}: {text}"
            # 四格放不下说明文字：缺失时显示占位，未记录还是无法读取写在提示里
            label.setText(_PLACEHOLDER if name in _QUAD and not recorded else text)
            if name == "size_bytes" and info is not None:
                label.setToolTip(self._tr("sidebar.file_bytes", "{count} bytes", count=f"{info.size_bytes:,}"))
            else:
                label.setToolTip(described if name in _QUAD else text)
            label.setAccessibleName(described)


class PhotoInfoSidebar(QFrame):
    """工作区右侧的照片信息栏：标题加可滚动的照片信息，随面板的展开状态显示或收起。

    read_summary 为真时，收起状态下仍读取尺寸等摘要，供宿主在别处显示；histogram 为真时顶部带直方图。
    """

    def __init__(self, translate: Callable[[str, str], str], parent: Optional[QWidget] = None, *,
                 read_summary: bool = True, histogram: bool = False):
        super().__init__(parent)
        self._translate = translate
        self.setObjectName("infoSidebar")
        # 宽度由所在的分隔条或浮层决定，可在这个范围内拖动
        self.setMinimumWidth(SIDEBAR_MIN_WIDTH)
        self.setMaximumWidth(SIDEBAR_MAX_WIDTH)
        margin = px(SPACING_LG)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._title = QLabel(self)
        self._title.setObjectName("infoSidebarTitle")
        self._title.setContentsMargins(margin, margin, margin, px(SPACING_SM))
        layout.addWidget(self._title)

        scroll = QScrollArea(self)
        scroll.setObjectName("infoSidebarScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        content.setObjectName("infoSidebarContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(margin, px(SPACING_SM), margin, margin)
        self.panel = PhotoInfoPanel(content, read_summary=read_summary, histogram=histogram)
        content_layout.addWidget(self.panel)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        self.panel.expanded_changed.connect(self.setVisible)
        self.retranslate_ui()
        self.hide()

    def sizeHint(self) -> QSize:
        return QSize(INFO_SIDEBAR_WIDTH, super().sizeHint().height())

    def retranslate_ui(self) -> None:
        self._title.setText(self._translate("photo_info.title", "Photo Info"))
