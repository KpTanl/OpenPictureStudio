from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_LG, SPACING_MD, SPACING_SM, SPACING_XL, SPACING_XS, px
from ...core.local_masks import MASK_LIMIT, LOCAL_UI_FIELDS, EFFECTIVE_RANGES, local_ui_values
from ...core.mask_components import COMPONENT_LIMIT, NODE_COMPONENT_LIMIT, POINT_LIMIT, brush_strokes
from ..base_window import BaseDialog
from ..icons import themed_icon
from ..widgets.apple_button import AppleButton, AppleGhostButton, AppleIconButton, ApplePrimaryButton
from ..widgets.apple_menu import AppleMenu
from ..widgets.color_sliders import WB_SLIDER_DEFS, TONE_SLIDER_DEFS, PRESENCE_SLIDER_DEFS
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.grouped_list import GroupedOptionRow
from ..widgets.notice_bar import NoticeBar
from ..widgets.parameter_row import ParameterRow
from ..widgets.toggle_button import ToggleButton
from ..widgets.tooltip import set_tooltip


# 自动识别的区域与它用的模型能力；“背景”是整张照片减去识别出的主体
RECOGNIZED = {"subject": "segmentation", "background": "segmentation",
              "skin": "face_detection", "eyes": "face_detection", "lips": "face_detection"}
_QUICK_KINDS = ("subject", "background", "all", "brush", "linear")


def area_label(component, tr):
    """区域在列表里的名字：按类型写，画笔另写出有几笔。"""
    kind = component["type"]
    if kind == "brush":
        name = tr("mask.type.erase" if component["op"] == "subtract" else "mask.type.brush")
        return tr("mask.brush_strokes", name=name, count=len(brush_strokes(component)))
    return tr("mask.type." + kind)


def _discard(widget):
    """移出列表的行先隐藏再销毁，销毁前不会叠在新行上。"""
    widget.hide()
    widget.deleteLater()


def _section_title(parent):
    label = QLabel(parent)
    label.setObjectName("maskSectionTitle")
    return label


def _caption(parent):
    label = QLabel(parent)
    label.setObjectName("maskCaption")
    label.setWordWrap(True)
    return label


class MaskModelDialog(BaseDialog):
    """识别主体与人脸用的模型；新识别的区域用这里的选择，已识别好的区域不受影响。"""

    def __init__(self, runtime, manage, refresh, parent=None):
        super().__init__(title_key="mask.models_title", title_default="", min_width=380, min_height=0, parent=parent)
        from ..widgets.model_selector import ModelSelector
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_LG), px(SPACING_XL), px(SPACING_LG))
        layout.setSpacing(px(SPACING_MD))
        self._note = QLabel(self)
        self._note.setObjectName("dialogMessage")
        self._note.setWordWrap(True)
        layout.addWidget(self._note)
        self.selectors, self._labels = {}, {}
        for capability in ("segmentation", "face_detection"):
            label = QLabel(self)
            label.setObjectName("maskSectionTitle")
            selector = ModelSelector(runtime.models, capability, self, backend_status=runtime.inference.status)
            selector.set_token(runtime.models.default(capability))
            selector.manage_requested.connect(manage)
            selector.selected.connect(lambda _token: refresh())
            selector.runner.changed.connect(refresh)
            layout.addWidget(label)
            layout.addWidget(selector)
            self.selectors[capability], self._labels[capability] = selector, label
        footer = QHBoxLayout()
        footer.addStretch(1)
        self._done = ApplePrimaryButton(parent=self)
        self._done.clicked.connect(self.accept)
        footer.addWidget(self._done)
        layout.addLayout(footer)
        self.retranslate_ui()

    def retranslate_ui(self):
        super().retranslate_ui()
        self._note.setText(self._tr("mask.models_note"))
        self._done.setText(self._tr("mask.done"))
        for capability, label in self._labels.items():
            label.setText(self._tr("mask.model." + capability))


class MaskPanel(QWidget):
    """调色的蒙版页：上面是蒙版列表，选中的蒙版下面依次是它的区域与调整。

    区域按保存顺序合成，“添加”并进来，“减去”挖掉；调整只属于蒙版本身。面板只展示与发出请求，
    参数由调色面板经 ``set_state()`` 投影。
    """

    selected = pyqtSignal(object)
    create_requested = pyqtSignal(object)
    component_selected = pyqtSignal(object)
    edit_requested = pyqtSignal(str, object)
    range_requested = pyqtSignal(bool)
    upgrade_requested = pyqtSignal()
    generation_cancel_requested = pyqtSignal()

    def __init__(self, tr, track_edit, finish_edit, parent=None):
        super().__init__(parent)
        self._tr, self._masks, self._selected, self._component = tr, [], None, None
        self._component_mode, self._editable, self._busy = False, False, None
        self._signature, self._rows = None, {}
        self._component_signature, self._component_rows = None, {}
        self._parameter_rows = {}
        self._model_dialog, self._model_states, self._runtime = None, {}, None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        self.notice = NoticeBar(parent=self)
        self.notice.action_triggered.connect(self.upgrade_requested.emit)
        layout.addWidget(self.notice)

        head = QHBoxLayout()
        self._list_title = _section_title(self)
        self._new = AppleButton(parent=self)
        self._new.setIcon(themed_icon("plus"))
        self._new.clicked.connect(lambda: self._area_menu(self._new, "add", None))
        head.addWidget(self._list_title, 1)
        head.addWidget(self._new)
        layout.addLayout(head)
        self._list = QVBoxLayout()
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(0)
        layout.addLayout(self._list)
        self._pending = QWidget(self)
        pending = QHBoxLayout(self._pending)
        pending.setContentsMargins(px(SPACING_MD), 0, 0, 0)
        self._pending_label = _caption(self._pending)
        self._pending_cancel = AppleGhostButton(parent=self._pending)
        self._pending_cancel.clicked.connect(self.generation_cancel_requested.emit)
        pending.addWidget(self._pending_label, 1)
        pending.addWidget(self._pending_cancel)
        layout.addWidget(self._pending)

        self._empty = QWidget(self)
        empty = QVBoxLayout(self._empty)
        empty.setContentsMargins(0, px(SPACING_LG), 0, px(SPACING_SM))
        empty.setSpacing(px(SPACING_SM))
        self._empty_title = QLabel(self._empty)
        self._empty_title.setObjectName("emptyStateTitle")
        self._empty_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._empty_text = QLabel(self._empty)
        self._empty_text.setObjectName("emptyStateText")
        self._empty_text.setWordWrap(True)
        self._empty_text.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(self._empty_title)
        empty.addWidget(self._empty_text)
        quick = QGridLayout()
        quick.setSpacing(px(SPACING_SM))
        self._quick = {}
        for index, kind in enumerate((*_QUICK_KINDS, "more")):
            button = AppleButton(parent=self._empty)
            if kind == "more":
                button.clicked.connect(lambda _checked=False, b=button: self._area_menu(b, "add", None))
            else:
                button.clicked.connect(lambda _checked=False, k=kind: self._request(k, "add", None))
            quick.addWidget(button, index // 2, index % 2)
            self._quick[kind] = button
        empty.addLayout(quick)
        layout.addWidget(self._empty)

        self._detail = QWidget(self)
        detail = QVBoxLayout(self._detail)
        detail.setContentsMargins(0, px(SPACING_SM), 0, 0)
        detail.setSpacing(px(SPACING_SM))
        title_row = QHBoxLayout()
        self._title = QLabel(self._detail)
        self._title.setObjectName("maskDetailTitle")
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        self._rename = QLineEdit(self._detail)
        self._rename.setMaxLength(80)
        self._rename.hide()
        self._rename.editingFinished.connect(self._finish_rename)
        self._rename.installEventFilter(self)
        self._range = ToggleButton(icon_name="eye", parent=self._detail)
        self._range.clicked.connect(self.range_requested.emit)
        title_row.addWidget(self._title, 1)
        title_row.addWidget(self._rename, 1)
        title_row.addWidget(self._range)
        detail.addLayout(title_row)

        self._areas = QWidget(self._detail)
        areas = QVBoxLayout(self._areas)
        areas.setContentsMargins(0, 0, 0, 0)
        areas.setSpacing(px(SPACING_XS))
        areas_head = QHBoxLayout()
        self._areas_title = _section_title(self._areas)
        self._areas_order = _caption(self._areas)
        self._areas_order.setWordWrap(False)
        areas_head.addWidget(self._areas_title)
        areas_head.addStretch(1)
        areas_head.addWidget(self._areas_order)
        areas.addLayout(areas_head)
        self._component_list = QVBoxLayout()
        self._component_list.setContentsMargins(0, 0, 0, 0)
        self._component_list.setSpacing(0)
        areas.addLayout(self._component_list)
        self._busy_row = QWidget(self._areas)
        busy = QHBoxLayout(self._busy_row)
        busy.setContentsMargins(px(SPACING_MD), 0, 0, 0)
        self._busy_label = _caption(self._busy_row)
        self._busy_cancel = AppleGhostButton(parent=self._busy_row)
        self._busy_cancel.clicked.connect(self.generation_cancel_requested.emit)
        busy.addWidget(self._busy_label, 1)
        busy.addWidget(self._busy_cancel)
        areas.addWidget(self._busy_row)
        self._areas_empty = _caption(self._areas)
        areas.addWidget(self._areas_empty)
        actions = QHBoxLayout()
        self._add, self._subtract = AppleButton(parent=self._areas), AppleButton(parent=self._areas)
        self._add.setIcon(themed_icon("plus"))
        self._subtract.setIcon(themed_icon("minus"))
        self._add.clicked.connect(lambda: self._area_menu(self._add, "add", self._selected))
        self._subtract.clicked.connect(lambda: self._area_menu(self._subtract, "subtract", self._selected))
        actions.addWidget(self._add, 1)
        actions.addWidget(self._subtract, 1)
        areas.addLayout(actions)
        detail.addWidget(self._areas)

        adjust_head = QHBoxLayout()
        adjust_head.setContentsMargins(0, px(SPACING_SM), 0, 0)
        self._adjust_title = _section_title(self._detail)
        self._reset = AppleGhostButton(parent=self._detail)
        self._reset.clicked.connect(lambda: self.edit_requested.emit("reset", None))
        adjust_head.addWidget(self._adjust_title, 1)
        adjust_head.addWidget(self._reset)
        detail.addLayout(adjust_head)
        self._groups = []
        for key, specs in (("tone", WB_SLIDER_DEFS + TONE_SLIDER_DEFS), ("presence", PRESENCE_SLIDER_DEFS)):
            card = CollapsibleCard("", self._detail, is_expanded=True)
            rows = QVBoxLayout()
            rows.setSpacing(0)
            for original in specs:
                spec = replace(original, default=0, value_format="int_signed" if original.key == "temperature" else original.value_format)
                row = ParameterRow(spec, "", self._detail)
                row.value_edited.connect(lambda value, k=spec.key: self.edit_requested.emit("parameter", (k, value)))
                row.typing_finished.connect(finish_edit)
                track_edit(row.slider, started=row.slider.sliderPressed, finished=row.slider.sliderReleased,
                           is_active=row.slider.isSliderDown)
                self._parameter_rows[spec.key] = row
                rows.addWidget(row)
            card.add_layout(rows)
            self._groups.append((key, card))
            detail.addWidget(card)
        self._clipping = NoticeBar(parent=self._detail)
        self._clipping.hide()
        detail.addWidget(self._clipping)
        layout.addWidget(self._detail)
        layout.addStretch(1)
        self.retranslate_ui()

    # ── 识别模型 ──────────────────────────────────────────────────────
    def configure_models(self, runtime, manage):
        if self._model_dialog is not None or runtime.models is None:
            return
        self._runtime = runtime
        self._model_dialog = MaskModelDialog(runtime, manage, self._refresh_models, self)
        self._refresh_models()

    def _refresh_models(self):
        if self._model_dialog is None:
            return
        for capability, selector in self._model_dialog.selectors.items():
            try:
                model, _paths = self._runtime.models.describe(selector.token())
                state = "ready" if self._runtime.models.snapshot(model.key).usable else "model_missing"
                if state == "ready" and not self._runtime.models.runtime_available(model):
                    state = "runtime_missing"
            except (KeyError, ValueError):
                state = "model_missing"
            selector.set_readiness(state)
            self._model_states[capability] = state

    def model_token(self, kind):
        if self._model_dialog is None:
            return ""
        return self._model_dialog.selectors[RECOGNIZED.get(kind, "segmentation")].token()

    def _show_models(self):
        if self._model_dialog is not None:
            self._refresh_models()
            self._model_dialog.exec()

    def _model_ready(self, kind):
        return self._model_states.get(RECOGNIZED[kind], "ready") == "ready"

    def _ask_for_model(self, kind):
        from ..widgets.message_dialog import DialogButton, ask_choice
        capability = RECOGNIZED[kind]
        choice = ask_choice(self, self._tr("mask.model_missing_title"),
                            self._tr("mask.model_missing", type=self._tr("mask.type." + kind)),
                            (DialogButton("cancel", self._tr("common.cancel")),
                             DialogButton("manage", self._tr("mask.open_models"), "primary")),
                            default="manage", escape="cancel")
        if choice == "manage" and self._model_dialog is not None:
            self._model_dialog.selectors[capability].open_manager()

    # ── 新建与添加区域 ───────────────────────────────────────────────
    def _request(self, kind, operation, parent):
        if kind in RECOGNIZED and self._model_dialog is not None and not self._model_ready(kind):
            self._ask_for_model(kind)
            return
        self.create_requested.emit({"kind": kind, "op": operation, "parent": parent})

    def _area_menu(self, button, operation, parent):
        menu = AppleMenu(self)
        if not self._component_mode:
            for kind in ("linear", "radial"):
                menu.addAction(self._tr("mask.type." + kind), lambda k=kind: self._request(k, operation, parent))
            menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
            return
        recognized = ["subject", *(["background"] if parent is None else []), "skin", "eyes", "lips"]
        manual = [*(["all"] if operation == "add" else []), "brush", "linear", "radial"]
        for header, kinds in (("mask.menu.recognized", recognized), ("mask.menu.manual", manual)):
            menu.addAction(self._tr(header)).setEnabled(False)
            for kind in kinds:
                label = self._tr("mask.type.erase" if kind == "brush" and operation == "subtract" else "mask.type." + kind)
                if kind in RECOGNIZED and self._model_dialog is not None and not self._model_ready(kind):
                    label = self._tr("mask.needs_model", type=label)
                menu.addAction(label, lambda k=kind: self._request(k, operation, parent))
        if self._model_dialog is not None:
            menu.addSeparator()
            menu.addAction(self._tr("mask.models"), self._show_models)
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    # ── 蒙版与区域的菜单 ─────────────────────────────────────────────
    def _mask_menu(self, identity, button):
        self.selected.emit(identity)
        mask = next(item for item in self._masks if item["id"] == identity)
        menu = AppleMenu(self)
        menu.addAction(self._tr("mask.rename"), self._begin_rename)
        duplicate = menu.addAction(self._tr("mask.duplicate"), lambda: self.edit_requested.emit("duplicate", None))
        components = [item for parent in self._masks for item in parent.get("components", ())]
        points = sum(len(stroke["points"]) for item in components if item["type"] == "brush" for stroke in brush_strokes(item))
        own = mask.get("components", ())
        duplicate.setEnabled(len(self._masks) < MASK_LIMIT and len(components) + len(own) <= NODE_COMPONENT_LIMIT
                             and points + sum(len(stroke["points"]) for item in own if item["type"] == "brush"
                                              for stroke in brush_strokes(item)) <= POINT_LIMIT)
        if mask.get("type") == "radial":
            action = menu.addAction(self._tr("mask.invert"), lambda: self.edit_requested.emit("invert", None))
            action.setCheckable(True)
            action.setChecked(mask.get("invert", False))
        reset = menu.addAction(self._tr("mask.reset_adjustments"), lambda: self.edit_requested.emit("reset", None))
        reset.setEnabled(any(mask.get("params", {}).values()))
        menu.addSeparator()
        menu.addAction(self._tr("mask.delete_mask"), lambda: self.edit_requested.emit("delete", None))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _component_menu(self, identity, button):
        self.component_selected.emit(identity)
        mask = next(item for item in self._masks if item["id"] == self._selected)
        index = next(i for i, item in enumerate(mask["components"]) if item["id"] == identity)
        component = mask["components"][index]
        menu = AppleMenu(self)
        if component["type"] == "brush":
            menu.addAction(self._tr("mask.continue_painting"), lambda: self.edit_requested.emit("component_continue", identity))
        menu.addAction(self._tr("mask.component.to_subtract" if component["op"] == "add" else "mask.component.to_add"),
                       lambda: self.edit_requested.emit("component_op", identity))
        if component["type"] == "radial":
            action = menu.addAction(self._tr("mask.invert"), lambda: self.edit_requested.emit("component_invert", identity))
            action.setCheckable(True)
            action.setChecked(component.get("invert", False))
        for operation, enabled in (("up", index > 0), ("down", index + 1 < len(mask["components"]))):
            action = menu.addAction(self._tr("mask.component." + operation), lambda op=operation: self.edit_requested.emit("component_" + op, identity))
            action.setEnabled(enabled)
        menu.addSeparator()
        menu.addAction(self._tr("mask.delete_area"), lambda: self.edit_requested.emit("component_delete", identity))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    # ── 重命名 ───────────────────────────────────────────────────────
    def _begin_rename(self):
        current = next((item for item in self._masks if item["id"] == self._selected), None)
        if current is None or not self._editable:
            return
        self._title.hide()
        self._rename.setText(current["name"])
        self._rename.show()
        self._rename.setFocus()
        self._rename.selectAll()

    def _finish_rename(self):
        if self._rename.isHidden():
            return
        current = next((item for item in self._masks if item["id"] == self._selected), None)
        name = self._rename.text().strip()
        self._rename.hide()
        self._title.show()
        if current is not None and name and name != current["name"]:
            self.edit_requested.emit("rename", name)

    def _cancel_rename(self):
        self._rename.hide()
        self._title.show()

    # ── 投影 ─────────────────────────────────────────────────────────
    def set_state(self, params, selected, *, ready, message="", upgrade=False, show_range=False, component=None, busy=None):
        """busy 是正在识别的区域：(类型, 所属蒙版)，所属蒙版为 None 时识别完新建一个蒙版。"""
        self._masks, self._selected, self._component, self._busy = params.get("masks") or [], selected, component, busy
        self._component_mode = params.get("process_version") == 4
        self._editable = editable = params.get("process_version") in (3, 4)
        self.notice.setVisible(bool(message))
        self.notice.set_text(message)
        self.notice.set_action_text(self._tr("mask.upgrade") if upgrade else "")
        self.notice.action_button.setEnabled(upgrade and ready)
        self._new.setEnabled(ready and editable and len(self._masks) < MASK_LIMIT and busy is None)
        set_tooltip(self._new, self._tr("mask.limit") if len(self._masks) >= MASK_LIMIT else "")
        self._list_title.setVisible(bool(self._masks) or busy is not None)
        self._new.setVisible(bool(self._masks) or busy is not None or not editable)
        self._sync_mask_rows(ready, editable)
        pending = busy is not None and busy[1] is None
        self._pending.setVisible(pending)
        if pending:
            self._pending_label.setText(self._tr("mask.recognizing", type=self._tr("mask.type." + busy[0])))
        self._empty.setVisible(not self._masks and not pending and editable)
        for kind, button in self._quick.items():
            button.setVisible(self._component_mode or kind in ("linear", "more"))
            button.setEnabled(ready and busy is None)
        current = next((item for item in self._masks if item.get("id") == selected), None)
        self._detail.setVisible(current is not None and editable)
        self._detail.setEnabled(ready)
        if current is None or not editable:
            self._cancel_rename()
            self._clipping.hide()
            return
        if not self._rename.isVisible():
            self._title.setText(current["name"])
        self._range.setEnabled(ready)
        self._range.setChecked(show_range)
        for key, value in local_ui_values(current).items():
            self._parameter_rows[key].set_value(value)
        self._reset.setEnabled(any(current.get("params", {}).values()))
        self._areas.setVisible(self._component_mode)
        if self._component_mode:
            self._sync_component_rows(current, busy)
        self._clipping.hide()

    def _sync_mask_rows(self, ready, editable):
        signature = editable, tuple((item.get("id"), item.get("name"), item.get("enabled", True)) for item in self._masks)

        def identity_of(mask, index):
            return mask.get("id") if editable else "legacy:" + str(index)
        if signature != self._signature:
            while self._list.count():
                _discard(self._list.takeAt(0).widget())
            self._rows = {}
            for index, mask in enumerate(self._masks):
                identity = identity_of(mask, index)
                host = QWidget(self)
                bar = QHBoxLayout(host)
                bar.setContentsMargins(0, 0, 0, 0)
                bar.setSpacing(px(SPACING_XS))
                row = GroupedOptionRow(index, identity, str(mask.get("name") or index + 1), parent=host)
                row.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                row.installEventFilter(self)
                row.clicked.connect(lambda _index, item=identity, widget=row: (self.selected.emit(item), widget.setFocus()))
                row.double_clicked.connect(lambda _index, item=identity: (self.selected.emit(item), self._begin_rename()))
                enabled = mask.get("enabled", True)
                toggle = AppleIconButton(themed_icon("eye" if enabled else "eye-off"), parent=host)
                set_tooltip(toggle, self._tr("mask.turn_off" if enabled else "mask.turn_on"))
                toggle.clicked.connect(lambda _checked=False, item=identity, value=not enabled:
                                       self.edit_requested.emit("enabled", (item, value)))
                more = AppleIconButton(themed_icon("dots"), parent=host)
                more.clicked.connect(lambda _checked=False, item=identity, btn=more: self._mask_menu(item, btn))
                bar.addWidget(row, 1)
                bar.addWidget(toggle)
                bar.addWidget(more)
                self._list.addWidget(host)
                self._rows[identity] = (row, toggle, more)
            self._signature = signature
        for index, mask in enumerate(self._masks):
            row, toggle, more = self._rows[identity_of(mask, index)]
            row.set_checked(row.key == self._selected)
            count = len(mask.get("components", ()))
            row.set_subtitle(self._tr("mask.disabled") if not mask.get("enabled", True)
                             else self._tr("mask.area_count", count=count) if self._component_mode and count
                             else self._tr("mask.area_none") if self._component_mode else "")
            for widget in (row, toggle, more):
                widget.setEnabled(ready and editable)

    def _sync_component_rows(self, mask, busy):
        items = mask.get("components", [])
        signature = mask["id"], tuple((item["id"], item["op"], item["enabled"]) for item in items)
        if signature != self._component_signature:
            while self._component_list.count():
                _discard(self._component_list.takeAt(0).widget())
            self._component_rows = {}
            for index, item in enumerate(items):
                host = QWidget(self._areas)
                bar = QHBoxLayout(host)
                bar.setContentsMargins(0, 0, 0, 0)
                bar.setSpacing(px(SPACING_XS))
                row = GroupedOptionRow(index, item["id"], "", parent=host, icon="plus" if item["op"] == "add" else "minus")
                row.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                row.installEventFilter(self)
                row.clicked.connect(lambda _index, identity=item["id"], widget=row: (self.component_selected.emit(identity), widget.setFocus()))
                toggle = AppleIconButton(themed_icon("eye" if item["enabled"] else "eye-off"), parent=host)
                set_tooltip(toggle, self._tr("mask.area_off" if item["enabled"] else "mask.area_on"))
                toggle.clicked.connect(lambda _checked=False, identity=item["id"], value=not item["enabled"]:
                                       self.edit_requested.emit("component_enabled", (identity, value)))
                more = AppleIconButton(themed_icon("dots"), parent=host)
                more.clicked.connect(lambda _checked=False, identity=item["id"], button=more: self._component_menu(identity, button))
                bar.addWidget(row, 1)
                bar.addWidget(toggle)
                bar.addWidget(more)
                self._component_list.addWidget(host)
                self._component_rows[item["id"]] = row
            self._component_signature = signature
        for item in items:
            row = self._component_rows[item["id"]]
            row.set_title(area_label(item, self._tr))
            row.set_checked(item["id"] == self._component)
            row.set_subtitle("" if item["enabled"] else self._tr("mask.disabled"))
            set_tooltip(row, self._tr("mask.op_tip." + item["op"]))
        here = busy is not None and busy[1] == mask["id"]
        self._busy_row.setVisible(here)
        if here:
            self._busy_label.setText(self._tr("mask.recognizing", type=self._tr("mask.type." + busy[0])))
        self._areas_empty.setVisible(not items and not here)
        can_add = len(items) < COMPONENT_LIMIT and busy is None
        self._add.setEnabled(can_add)
        self._subtract.setEnabled(can_add and bool(items))

    # ── 键盘 ─────────────────────────────────────────────────────────
    def eventFilter(self, watched, event):
        if watched is self._rename:
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self._cancel_rename()
                return True
            return super().eventFilter(watched, event)
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            watched.set_focused(event.type() == QEvent.Type.FocusIn)
        if event.type() == QEvent.Type.KeyPress:
            rows, signal = [widgets[0] for widgets in self._rows.values()], self.selected
            component_row = watched in self._component_rows.values()
            if component_row:
                rows, signal = list(self._component_rows.values()), self.component_selected
            if watched in rows:
                index = rows.index(watched)
                if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                    index = max(0, min(len(rows) - 1, index + (1 if event.key() == Qt.Key.Key_Down else -1)))
                    rows[index].setFocus()
                    signal.emit(rows[index].key)
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                    signal.emit(watched.key)
                    return True
                if event.key() == Qt.Key.Key_F2 and not component_row:
                    signal.emit(watched.key)
                    self._begin_rename()
                    return True
        return super().eventFilter(watched, event)

    def focused_list(self):
        """键盘焦点所在的列表：“mask”“area”或 None；Delete 删除的就是这一层的选中项。"""
        if any(row.hasFocus() for row, _toggle, _more in self._rows.values()):
            return "mask"
        if any(row.hasFocus() for row in self._component_rows.values()):
            return "area"
        return None

    def set_clipping(self, keys):
        labels = []
        for key in keys:
            name = self._tr(self._parameter_rows[LOCAL_UI_FIELDS[key]].spec.label_key)
            low, high = EFFECTIVE_RANGES[key]
            bounds = f"{low:+.2f}…{high:+.2f} EV" if key == "exposure" else f"{low:g}…{high:g}×" if key in ("contrast", "saturation") else f"{low:+g}…{high:+g}"
            labels.append(name + " (" + bounds + ")")
        self._clipping.set_text(self._tr("mask.clipped", parameters=self._tr("history.joiner", ", ").join(labels)))
        self._clipping.setVisible(bool(keys))

    def retranslate_ui(self):
        self._list_title.setText(self._tr("mask.title"))
        self._new.setText(self._tr("mask.new"))
        self._empty_title.setText(self._tr("mask.empty_title"))
        self._empty_text.setText(self._tr("mask.empty"))
        for kind, button in self._quick.items():
            button.setText(self._tr("mask.more" if kind == "more" else "mask.type." + kind))
        self._pending_cancel.setText(self._tr("common.cancel"))
        self._busy_cancel.setText(self._tr("common.cancel"))
        self._range.setText(self._tr("mask.range"))
        set_tooltip(self._range, self._tr("mask.range_tip"))
        self._rename.setPlaceholderText(self._tr("mask.name"))
        self._areas_title.setText(self._tr("mask.areas"))
        self._areas_order.setText(self._tr("mask.areas_order"))
        self._areas_empty.setText(self._tr("mask.areas_empty"))
        self._add.setText(self._tr("mask.area.add"))
        self._subtract.setText(self._tr("mask.area.subtract"))
        self._adjust_title.setText(self._tr("mask.adjustments"))
        self._reset.setText(self._tr("mask.reset"))
        for key, card in self._groups:
            card.set_title(self._tr("color_adjust.group." + key))
        for row in self._parameter_rows.values():
            row.set_label(self._tr(row.spec.label_key))
        self._signature = self._component_signature = None
