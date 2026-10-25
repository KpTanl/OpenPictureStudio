"""证件照面板。

``IDPhotoPanel`` 用于参数版本 2：规格、人像位置、背景与输出四组，底部是“查看结果 / 继续调整”。它是画布工具：
调整时画布上是这一步之前的画面，加锁在规格比例上的裁切框与人像参考线；看结果时是做完之后的证件照。
自动对齐用的是宿主经 ``on_step_input()`` 交给它的这一步的输入画面（也就是框底下的那一幅），检测在后台做。
刚进入、还没动过时这个工具还没有进入本次编辑，框按人脸摆好但不记入；点“查看结果”或改了任何一项才记入。

``LegacyIDPhotoPanel`` 只用于打开旧项目里参数版本 1 的节点（整幅缩放到目标尺寸）。
"""

import logging
from typing import Optional

from PIL import Image
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QWidget

from .base_tool_panel import BaseToolPanel
from .canvas_result import ResultViewControl
from .managed_model_panels import ManagedModelPanelMixin
from ...application.tool_feature_readiness import READY
from ...core.tools.id_photo import (BG_PRESETS, ID_PHOTO_PARAMETERS, ID_PHOTO_RESOURCE_FEATURES, LAYOUT_SHEET,
                                    LAYOUT_SINGLE, MANAGED_ID_PHOTO_PARAMETERS, SHEET_SIZE, SIZE_PRESETS,
                                    id_photo_required_resources, normalized_rect, photo_bounds, refit_crop_rect,
                                    sheet_grid)
from ...core.tools.id_photo_alignment import detect_primary_face, portrait_crop_rect
from ..dialogs.color_dialog_utils import pick_color
from ..overlays.id_photo_overlay import IDPhotoOverlay
from ..widgets.apple_button import AppleButton
from ..widgets.apple_menu import AppleDropDown
from ..widgets.apple_spin_box import AppleSpinBox
from ..widgets.chip import ChipGroup
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.color_swatch import ColorSwatch
from ..widgets.segmented_control import AppleSegmentedControl

logger = logging.getLogger(__name__)

_CUSTOM_SIZE = "自定义"
_KEEP, _CUSTOM_COLOR = "keep", "custom"
_SIZE_LABELS = {
    "一寸": ("id_photo.size.one_inch", "1 inch"),
    "二寸": ("id_photo.size.two_inch", "2 inch"),
    "小一寸": ("id_photo.size.small_one_inch", "Small 1 inch"),
    "大一寸": ("id_photo.size.large_one_inch", "Large 1 inch"),
    "护照": ("id_photo.size.passport", "Passport"),
    "签证": ("id_photo.size.visa", "Visa"),
    "社保": ("id_photo.size.social_id", "Social ID"),
    _CUSTOM_SIZE: ("id_photo.size.custom", "Custom"),
}
_BACKGROUND_LABELS = {
    _KEEP: ("id_photo.background.keep", "Original"),
    "白色": ("id_photo.background.white", "White"),
    "蓝色": ("id_photo.background.blue", "Blue"),
    "红色": ("id_photo.background.red", "Red"),
    "浅蓝": ("id_photo.background.light_blue", "Light Blue"),
    "灰色": ("id_photo.background.gray", "Gray"),
    _CUSTOM_COLOR: ("id_photo.background.custom", "Custom"),
}
_FIELDS = {field.name: field for field in MANAGED_ID_PHOTO_PARAMETERS.fields}
_MM_PER_INCH = 25.4
_DPI = 300


def _rgb_image(image: QImage) -> Image.Image:
    """画布上的一帧换成 PIL 的 RGB 图像（复制一份，交给后台）。"""
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    data = bytes(rgb.constBits().asarray(rgb.sizeInBytes()))
    return Image.frombuffer("RGB", (rgb.width(), rgb.height()), data, "raw", "RGB", rgb.bytesPerLine(), 1)


def _background_key(color) -> str:
    if color is None:
        return _KEEP
    return next((name for name, preset in BG_PRESETS.items() if list(preset) == list(color)), _CUSTOM_COLOR)


class _IDPhotoControls(BaseToolPanel):
    """证件照面板的控件与交互；模型选择由 IDPhotoPanel 混入。"""

    PARAMETER_CONTRACT = MANAGED_ID_PHOTO_PARAMETERS
    PARAMETER_LABELS = {"custom_width": ("id_photo.param.width", "int"),
                        "custom_height": ("id_photo.param.height", "int")}
    TOOL_ID = "id_photo"
    TOOL_TITLE_KEY = "menu.edit.id_photo"
    TOOL_TITLE_DEFAULT = "ID Photo Maker"

    def __init__(self, image_path: str, parent: Optional[QWidget] = None, edit_state_store=None, **services):
        self._original_image: Optional[Image.Image] = None
        self.overlay = IDPhotoOverlay()
        self.overlay.geometry_changed.connect(self._on_geometry_changed)
        # 这一步的输入画面：宿主在调整状态下交付，自动对齐与没有框时的默认范围都按它算
        self._step_input: Optional[QImage] = None
        self._align_generation = 0
        self._align_job: Optional[int] = None
        self._align_waiting = False
        self._align_commit = False
        self._initial_aligned = False
        self._align_note = ""
        self._custom_color = (255, 255, 255)
        super().__init__(image_path, parent=parent, edit_state_store=edit_state_store, **services)
        self.track_edit_control(self.overlay, started=self.overlay.drag_started,
                                finished=self.overlay.drag_finished, is_active=self.overlay.is_dragging)
        self._restore_initial_params()
        self.load_panel_source(image_path, self._sync_ui_from_params)

    @staticmethod
    def _default_params():
        return MANAGED_ID_PHOTO_PARAMETERS.defaults(MANAGED_ID_PHOTO_PARAMETERS.version)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def build_ui(self) -> None:
        layout = self._root_layout

        self._spec_card = CollapsibleCard("", self)
        self._spec_chips = ChipGroup(4, self)
        self._spec_chips.set_items([(key, "") for key in SIZE_PRESETS])
        self._spec_chips.current_changed.connect(self._on_spec_changed)
        self._spec_card.add_widget(self._spec_chips)
        self._custom_box = QWidget(self)
        custom = QHBoxLayout(self._custom_box)
        custom.setContentsMargins(0, 0, 0, 0)
        self._width_label = QLabel(self._custom_box)
        self._height_label = QLabel(self._custom_box)
        self._width_spin = AppleSpinBox(self._custom_box)
        self._height_spin = AppleSpinBox(self._custom_box)
        for spin, name in ((self._width_spin, "custom_width"), (self._height_spin, "custom_height")):
            spin.setRange(int(_FIELDS[name].minimum), int(_FIELDS[name].maximum))
            spin.valueEdited.connect(self._on_custom_size_edited)
        custom.addWidget(self._width_label)
        custom.addWidget(self._width_spin, 1)
        custom.addWidget(self._height_label)
        custom.addWidget(self._height_spin, 1)
        self._spec_card.add_widget(self._custom_box)
        self._spec_info = QLabel(self)
        self._spec_info.setObjectName("mutedText")
        self._spec_info.setWordWrap(True)
        self._spec_card.add_widget(self._spec_info)
        layout.addWidget(self._spec_card)

        self._position_card = CollapsibleCard("", self)
        self._align_button = AppleButton(parent=self)
        self._align_button.clicked.connect(self._on_align_clicked)
        self._position_card.add_widget(self._align_button)
        self._position_tip = QLabel(self)
        self._position_tip.setObjectName("mutedText")
        self._position_tip.setWordWrap(True)
        self._position_card.add_widget(self._position_tip)
        layout.addWidget(self._position_card)

        self._background_card = CollapsibleCard("", self)
        self._background_chips = ChipGroup(4, self)
        self._background_chips.set_items([(key, "") for key in _BACKGROUND_LABELS])
        self._background_chips.current_changed.connect(self._on_background_changed)
        self._background_card.add_widget(self._background_chips)
        self._swatch_box = QWidget(self)
        swatch = QFormLayout(self._swatch_box)
        swatch.setContentsMargins(0, 0, 0, 0)
        self._swatch_label = QLabel(self._swatch_box)
        self._swatch = ColorSwatch(self._swatch_box)
        self._swatch.clicked.connect(self._pick_custom_color)
        swatch.addRow(self._swatch_label, self._swatch)
        self._background_card.add_widget(self._swatch_box)
        self._background_hint = QLabel(self)
        self._background_hint.setObjectName("mutedText")
        self._background_hint.setWordWrap(True)
        self._background_card.add_widget(self._background_hint)
        layout.addWidget(self._background_card)

        self._output_card = CollapsibleCard("", self)
        self._layout_segments = AppleSegmentedControl(self)
        self._layout_segments.add_segment("", LAYOUT_SINGLE)
        self._layout_segments.add_segment("", LAYOUT_SHEET)
        self._layout_segments.activated.connect(self._on_layout_changed)
        self._output_card.add_widget(self._layout_segments)
        self._output_info = QLabel(self)
        self._output_info.setObjectName("mutedText")
        self._output_info.setWordWrap(True)
        self._output_card.add_widget(self._output_info)
        layout.addWidget(self._output_card)

        layout.addStretch()

        # 宿主支持时固定在底部：查看做完之后的证件照，或回到摆框
        self.result_view = ResultViewControl(self, prepare=self._use_params)
        self.result_view.viewing_changed.connect(lambda _viewing: self._project_texts())
        self.result_view.footer.setVisible(self.external_preview)
        layout.addWidget(self.result_view.footer)
        self.retranslate_ui()

    def take_footer(self):
        self._root_layout.removeWidget(self.result_view.footer)
        return self.result_view.footer

    def _place_model_selector(self):
        # 模型只在铺背景色时用得到，放在背景一组里
        self._background_card.add_widget(self.model_selector)

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        if not hasattr(self, "result_view"):
            return
        self._spec_card.set_title(self._tr("id_photo.spec_group", "Size"))
        for key, (text_key, fallback) in _SIZE_LABELS.items():
            self._spec_chips.set_text(key, self._tr(text_key, fallback))
        self._width_label.setText(self._tr("id_photo.param.width", "Width"))
        self._height_label.setText(self._tr("id_photo.param.height", "Height"))
        self._position_card.set_title(self._tr("id_photo.position_group", "Portrait position"))
        self._align_button.setText(self._tr("id_photo.auto_crop", "Auto Align Portrait"))
        self._background_card.set_title(self._tr("id_photo.bg_group", "Background"))
        for key, (text_key, fallback) in _BACKGROUND_LABELS.items():
            self._background_chips.set_text(key, self._tr(text_key, fallback))
        self._swatch_label.setText(self._tr("id_photo.bg_color_label", "Color"))
        self._swatch.setAccessibleName(self._tr("id_photo.pick_background_color", "Pick background color"))
        self._output_card.set_title(self._tr("id_photo.output_group", "Output"))
        self._layout_segments.set_segment_text(0, self._tr("id_photo.layout.single", "Single photo"))
        self._layout_segments.set_segment_text(1, self._tr("id_photo.layout.sheet", "6-inch sheet"))
        self.overlay.set_labels(self._tr("id_photo.guide_head_top", "Head"), self._tr("id_photo.guide_chin", "Chin"))
        self._project_texts()

    # ------------------------------------------------------------------
    # 参数与投影
    # ------------------------------------------------------------------
    def _size(self):
        return self._params["custom_width"], self._params["custom_height"]

    def _ratio(self):
        width, height = self._size()
        return width / height

    def _input_size(self):
        """框所在那幅画面的大小：有这一步的输入画面时用它，否则用源图的大小。"""
        if self._step_input is not None and not self._step_input.isNull():
            return self._step_input.width(), self._step_input.height()
        width, height = self.source_size
        return (width, height) if width and height else (1000, 1000)

    def _frame(self):
        """画布上框的位置：参数里有就用参数的，没有时是整幅里符合比例的居中最大范围。"""
        rect = self._params["crop_rect"]
        if rect is not None:
            return list(rect)
        size = self._input_size()
        return normalized_rect(photo_bounds(None, size, self._ratio()), size)

    def _sync_ui_from_params(self) -> None:
        params = self._params
        self._spec_chips.set_current(params["preset_size"])
        custom = params["preset_size"] == _CUSTOM_SIZE
        self._custom_box.setVisible(custom)
        self._width_spin.setValue(params["custom_width"])
        self._height_spin.setValue(params["custom_height"])
        key = _background_key(params["background"])
        self._background_chips.set_current(key)
        if key == _CUSTOM_COLOR:
            self._custom_color = tuple(params["background"])
        self._swatch_box.setVisible(params["background"] is not None)
        if params["background"] is not None:
            self._swatch.set_color(params["background"])
        if hasattr(self, "model_selector"):
            self.model_selector.setVisible(self._model_needed())
        self._layout_segments.setCurrentIndex(0 if params["layout"] == LAYOUT_SINGLE else 1)
        self.overlay.set_frame(self._frame(), self._ratio())
        self._project_texts()

    def _project_texts(self) -> None:
        if not hasattr(self, "result_view"):
            return
        width, height = self._size()
        self._spec_info.setText(self._tr(
            "id_photo.spec_info", "{width} × {height} px, about {mm_width} × {mm_height} mm at 300 dpi",
            width=width, height=height, mm_width=round(width / _DPI * _MM_PER_INCH),
            mm_height=round(height / _DPI * _MM_PER_INCH)))
        viewing = self.result_view.viewing
        self._align_button.setEnabled(not viewing and self._align_job is None)
        if viewing:
            tip = self._tr("id_photo.position_result_tip", "To move the frame, click “Keep adjusting” first.")
        elif self._align_job is not None:
            tip = self._tr("id_photo.aligning", "Looking for a face…")
        else:
            tip = self._align_note or self._tr(
                "id_photo.position_tip", "Line the top of the head and the chin up with the two dashed lines. "
                                         "Drag inside the frame to move it; drag a corner to resize.")
        self._position_tip.setText(tip)
        self._background_hint.setText(self._tr(
            "id_photo.background_hint.keep", "Original background: the photo is only cropped and resized.")
            if self._params["background"] is None else self._tr(
            "id_photo.background_hint.color", "The portrait is cut out first, then laid on this color."))
        columns, rows = sheet_grid(self._size())
        if self._params["layout"] == LAYOUT_SHEET:
            output = self._tr("id_photo.layout.sheet_info",
                              "One 6-inch sheet holds {columns} × {rows}, {count} photos in all, for printing.",
                              columns=columns, rows=rows, count=columns * rows)
        elif not columns * rows:
            output = self._tr("id_photo.layout.too_big", "This size doesn't fit on a 6-inch sheet.")
        else:
            output = self._tr("id_photo.layout.single_info", "Exports one {width} × {height} px ID photo.",
                              width=width, height=height)
        self._output_info.setText(output)
        self._project_result()

    def _project_result(self) -> None:
        if not hasattr(self, "result_view"):
            return
        width, height = self._size()
        spec = self._tr(*_SIZE_LABELS[self._params["preset_size"]])
        background = self._tr(*_BACKGROUND_LABELS[_background_key(self._params["background"])])
        if self._params["layout"] == LAYOUT_SHEET:
            columns, rows = sheet_grid(self._size())
            output = self._tr("id_photo.result.sheet", "6-inch sheet {width} × {height} px, {count} photos",
                              width=SHEET_SIZE[0], height=SHEET_SIZE[1], count=columns * rows)
        else:
            output = self._tr("id_photo.result.single", "single {width} × {height} px", width=width, height=height)
        self.result_view.describe(self._tr("id_photo.result.summary", "{spec} · {background} · {output}",
                                           spec=spec, background=background, output=output),
                                  self._blocked_reason())

    def _blocked_reason(self) -> str:
        return ""

    def _model_needed(self) -> bool:
        return self._params.get("background") is not None and bool(self._params.get("ai_matting"))

    def _publish_params(self, params, record_stash):
        super()._publish_params(params, record_stash)
        self._project_texts()

    def restore_params(self, params):
        super().restore_params(params)
        if self._params["crop_rect"] is None:
            # 回到还没有框的状态（重置、撤销到进入之前）：下一幅输入画面到了再按人脸摆一次
            self._initial_aligned = False

    # ------------------------------------------------------------------
    # 用户操作
    # ------------------------------------------------------------------
    def _commit(self, **changes) -> bool:
        """提交用户的一次改动；框还没有记入时一并记入当前画布上的框。"""
        return self._apply_params({**self._params, "crop_rect": self._frame(), **changes})

    def _on_spec_changed(self, key: str) -> None:
        self._cancel_alignment()
        size = SIZE_PRESETS[key] or self._size()
        layout = self._params["layout"]
        if layout == LAYOUT_SHEET and 0 in sheet_grid(size):
            layout = LAYOUT_SINGLE
        self._commit(preset_size=key, custom_width=size[0], custom_height=size[1], layout=layout,
                     crop_rect=refit_crop_rect(self._frame(), self._input_size(), size[0] / size[1]))

    def _on_custom_size_edited(self, _value: int) -> None:
        self._cancel_alignment()
        size = (self._width_spin.value(), self._height_spin.value())
        layout = self._params["layout"]
        if layout == LAYOUT_SHEET and 0 in sheet_grid(size):
            layout = LAYOUT_SINGLE
        self._commit(custom_width=size[0], custom_height=size[1], layout=layout,
                     crop_rect=refit_crop_rect(self._frame(), self._input_size(), size[0] / size[1]))

    def _on_geometry_changed(self) -> None:
        self._align_note = ""
        if self._commit_params({**self._params, "crop_rect": self.overlay.norm_rect}):
            self._project_texts()

    def _on_background_changed(self, key: str) -> None:
        if key == _KEEP:
            self._commit(background=None)
        elif key == _CUSTOM_COLOR:
            self._pick_custom_color()
        else:
            self._commit(background=list(BG_PRESETS[key]))

    def _pick_custom_color(self) -> None:
        current = self._params["background"]
        color = pick_color(self, tuple(current) if current is not None else self._custom_color,
                           self._tr("id_photo.pick_background_color", "Pick background color"))
        if not color.isValid() or not self._commit(background=[color.red(), color.green(), color.blue()]):
            # 没有选：标签回到原来的那一个
            self._background_chips.set_current(_background_key(self._params["background"]))

    def _on_layout_changed(self, index: int) -> None:
        layout = self._layout_segments.currentData()
        if layout == LAYOUT_SHEET and 0 in sheet_grid(self._size()):
            self._layout_segments.setCurrentIndex(0)
            return
        self._commit(layout=layout)

    def _use_params(self) -> None:
        """看结果之前把当前参数与画布上的框记入本次编辑，没改过参数时也记。"""
        self.notify_params_changed({**self._params, "crop_rect": self._frame()})

    def _forget_alignment(self) -> None:
        """回到还没有摆过框的状态：下一幅输入画面到了再按人脸摆一次。"""
        self._cancel_alignment()
        self._align_note = ""
        self._initial_aligned = False

    # ------------------------------------------------------------------
    # 自动对齐
    # ------------------------------------------------------------------
    def on_step_input(self, image: QImage) -> None:
        """宿主在调整状态下交付了这一步的输入画面，也就是画布上框底下的那一幅。"""
        if image is None or image.isNull():
            return
        resized = self._step_input is None or (image.width(), image.height()) != self._input_size()
        self._step_input = image
        if self._align_waiting:
            self._start_alignment(commit=self._align_commit)
        elif self._params["crop_rect"] is None and not self._initial_aligned:
            self._start_alignment(commit=False)
        elif resized and self._params["crop_rect"] is None:
            self.overlay.set_frame(self._frame(), self._ratio())

    def _on_align_clicked(self) -> None:
        if self._step_input is None:
            self._align_waiting, self._align_commit = True, True
            return
        self._start_alignment(commit=True)

    def _cancel_alignment(self) -> None:
        self._align_generation += 1
        self._align_waiting = False
        if self._align_job is not None and self._preview_runner is not None:
            self._preview_runner.cancel(self._align_job)
        self._align_job = None

    def _start_alignment(self, *, commit: bool) -> None:
        """在后台找人脸，再按参考线摆框。commit 为假时只摆在画布上、不记入（刚进入这个工具时）。"""
        self._cancel_alignment()
        self._initial_aligned = True
        generation = self._align_generation
        image = _rgb_image(self._step_input)
        size, ratio, path = image.size, self._ratio(), self._image_path

        def finished(result) -> None:
            if result.revision != self._align_generation or result.job_id != self._align_job:
                return
            self._align_job = None
            if self._image_path != path or self._ratio() != ratio:
                self._project_texts()
                return
            if result.error is not None:
                logger.warning("证件照找人脸失败：%s", result.error)
                face = None
            else:
                face = result.value
            self._align_note = "" if face is not None else self._tr(
                "id_photo.no_face", "No face was found. The frame is placed in the middle.")
            rect = normalized_rect(portrait_crop_rect(size, ratio, face), size)
            if commit:
                if not self._commit(crop_rect=rect):
                    self._project_texts()
            else:
                self._params = {**self._params, "crop_rect": rect}
                self._sync_ui_from_params()

        self._align_job = self._ensure_preview_runner().submit_work(
            generation, image, lambda picture, cancelled: detect_primary_face(picture), finished,
            budget=self._image_source_service.compute, cancellable=True)
        self._project_texts()

    def on_image_path_changed(self, path: str) -> None:
        self._cancel_alignment()
        self._step_input = None
        self._initial_aligned = False
        self._align_note = ""
        self.load_panel_source(path, self._sync_ui_from_params)

    # ------------------------------------------------------------------
    # 独立使用时的渲染
    # ------------------------------------------------------------------
    def apply(self):
        try:
            return self.render_panel_image()
        except Exception as error:
            self._processing_error(error)
            return None

    def _do_update_preview(self):
        self.preview_panel_image()

    def cleanup(self):
        self._cancel_alignment()
        super().cleanup()


class IDPhotoPanel(ManagedModelPanelMixin, _IDPhotoControls):
    """证件照（参数版本 2）：带抠图模型的选择，模型能不能用取自工具资源投影。"""

    availability_changed = pyqtSignal()
    MODEL_CAPABILITY = "segmentation"
    RESOURCE_FEATURES = ID_PHOTO_RESOURCE_FEATURES
    MODEL_FEATURE = "ai"
    RESOURCE_REQUIREMENTS = staticmethod(id_photo_required_resources)

    # 下面几项在继承顺序上会被混入类的同名方法盖住，在这里接回面板自己的实现
    def restore_params(self, params):
        _IDPhotoControls.restore_params(self, params)

    def _model_needed(self):
        return _IDPhotoControls._model_needed(self)

    def _place_model_selector(self):
        _IDPhotoControls._place_model_selector(self)

    def reset(self):
        self._forget_alignment()
        ManagedModelPanelMixin.reset(self)

    def _model_status_changed(self):
        super()._model_status_changed()
        self._project_result()

    def _blocked_reason(self) -> str:
        if not self._model_needed():
            return ""
        state = self._readiness.states(self._params).get(self.MODEL_FEATURE, "model_missing")
        if state == READY:
            return ""
        return self._tr("preview.blocked", group=self._tr("id_photo.bg_group"),
                        reason=self._tr("tool_manager.state." + state))


class LegacyIDPhotoPanel(BaseToolPanel):
    """证件照（参数版本 1）：整幅缩放到目标尺寸，有透明时铺背景色。只用于打开旧项目里的这类节点。"""

    PARAMETER_CONTRACT = ID_PHOTO_PARAMETERS
    TOOL_ID = "id_photo"
    TOOL_TITLE_KEY = "menu.edit.id_photo"
    TOOL_TITLE_DEFAULT = "ID Photo Maker"

    def __init__(
        self,
        image_path: str,
        parent: Optional[QWidget] = None,
        edit_state_store=None,
        **services,
    ):
        self._original_image: Optional[Image.Image] = None
        self._generation = 0
        self._displayed_generation = -1

        super().__init__(image_path, parent=parent, edit_state_store=edit_state_store, **services)

        self._load_source(image_path)
        self._restore_initial_params()
        self.update_preview()

    @staticmethod
    def _default_params():
        return ID_PHOTO_PARAMETERS.defaults(ID_PHOTO_PARAMETERS.version)

    def _load_source(self, path):
        self.load_panel_source(path, None)

    def _restore_initial_params(self) -> None:
        defaults = self._default_params()
        params = defaults
        if self._edit_state_store and self._image_path:
            params = self._edit_state_store.get_initial_params(
                self._image_path, self.TOOL_ID, defaults
            )
        checked = ID_PHOTO_PARAMETERS.normalize(params)
        if not checked.success:
            self.request_status.emit(self._tr(checked.error.message_key, "Invalid photo parameters"))
        self._params = checked.data if checked.success else defaults
        self._sync_ui_from_params()

    def build_ui(self) -> None:
        layout = self._root_layout

        # 卡片 1: 证件照尺寸规格
        card_size = CollapsibleCard(self._tr("id_photo.spec_group", "Photo Specification"), self)
        form_size = QFormLayout()

        self._size_combo = AppleDropDown(self)
        for name in SIZE_PRESETS:
            self._size_combo.addItem(name, name)
        self._size_combo.activated.connect(self._on_size_preset_changed)
        form_size.addRow(QLabel(self._tr("id_photo.preset_label", "Preset:"), self), self._size_combo)

        # 自定义宽高
        wh_row = QWidget(self)
        wh_l = QHBoxLayout(wh_row)
        wh_l.setContentsMargins(0, 0, 0, 0)
        wh_l.setSpacing(6)

        self._w_spin = AppleSpinBox(self)
        width_field = next(field for field in ID_PHOTO_PARAMETERS.fields if field.name == "custom_width")
        self._w_spin.setRange(width_field.minimum, width_field.maximum)
        self._w_spin.setValue(width_field.default)
        self._w_spin.valueEdited.connect(self._on_custom_wh_changed)
        wh_l.addWidget(QLabel("W:", self))
        wh_l.addWidget(self._w_spin)

        self._h_spin = AppleSpinBox(self)
        height_field = next(field for field in ID_PHOTO_PARAMETERS.fields if field.name == "custom_height")
        self._h_spin.setRange(height_field.minimum, height_field.maximum)
        self._h_spin.setValue(height_field.default)
        self._h_spin.valueEdited.connect(self._on_custom_wh_changed)
        wh_l.addWidget(QLabel("H:", self))
        wh_l.addWidget(self._h_spin)
        form_size.addRow(QLabel(self._tr("id_photo.custom_size", "Size (px):"), self), wh_row)

        card_size.setContentLayout(form_size)
        layout.addWidget(card_size)

        # 卡片 2: 背景色设置
        card_bg = CollapsibleCard(self._tr("id_photo.bg_group", "Background Color"), self)
        form_bg = QFormLayout()

        self._bg_combo = AppleDropDown(self)
        for name in BG_PRESETS:
            self._bg_combo.addItem(name, name)
        self._bg_combo.addItem(self._tr("id_photo.bg_none", "Keep Original"), "原图")
        self._bg_combo.activated.connect(self._on_bg_preset_changed)
        form_bg.addRow(QLabel(self._tr("id_photo.bg_color_label", "Background:"), self), self._bg_combo)

        card_bg.setContentLayout(form_bg)
        layout.addWidget(card_bg)

        layout.addStretch()

    def _on_size_preset_changed(self, index: int) -> None:
        name = self._size_combo.itemData(index)
        params = {**self._params, "preset_size": name}
        preset = SIZE_PRESETS.get(name)
        if preset is not None:
            params["custom_width"], params["custom_height"] = preset
        self._apply_params(params)

    def _on_custom_wh_changed(self) -> None:
        params = {**self._params, "custom_width": self._w_spin.value(), "custom_height": self._h_spin.value()}
        if self._commit_params(params):
            self.update_preview()

    def _on_bg_preset_changed(self, index: int) -> None:
        if self._commit_params({**self._params, "bg_preset": self._bg_combo.itemData(index)}):
            self.update_preview()

    def _sync_ui_from_params(self) -> None:
        preset_name = self._params["preset_size"]
        for i in range(self._size_combo.count()):
            if self._size_combo.itemData(i) == preset_name:
                self._size_combo.setCurrentIndex(i)
                break

        w = self._params["custom_width"]
        h = self._params["custom_height"]
        self._w_spin.setValue(w)
        self._h_spin.setValue(h)

        bg_name = self._params.get("bg_preset", "白色")
        for i in range(self._bg_combo.count()):
            if self._bg_combo.itemData(i) == bg_name:
                self._bg_combo.setCurrentIndex(i)
                break

    def apply(self):
        try:
            return self.render_panel_image()
        except Exception as error:
            self._processing_error(error)
            return None

    def reset(self) -> None:
        self._apply_params(self._default_params())

    def on_image_path_changed(self, path: str) -> None:
        self._load_source(path)
        self._restore_initial_params()
        self.update_preview()

    def _do_update_preview(self):
        self.preview_panel_image()

    def cleanup(self):
        super().cleanup()
