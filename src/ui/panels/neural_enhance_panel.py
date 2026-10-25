from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_SM, px
from .base_tool_panel import BaseToolPanel
from .manual_preview import ManualPreviewState
from ...application.enhancement_panel_state import EnhancementPanelQuery
from ...core.tools.enhancement import ENHANCE_PARAMETERS, ENHANCE_START_STRENGTHS
from ..widgets.apple_menu import AppleDropDown
from ..widgets.chip import ChipGroup
from ..widgets.collapsible_card import CollapsibleCard
from ..widgets.model_selector import ModelSelector
from ..widgets.parameter_row import ParameterRow, ParameterSpec
from ..widgets.action_footer import ActionFooter
from ..widgets.toggle_section import ToggleSection
from ..services.model_task_runner import ModelTaskRunner

# 三项处理各成一组，自上而下：处理 -> 它的强度参数；处理名同时是所需模型的能力名
_GROUPS = {"super_resolution": (), "denoise": ("luminance", "chroma"), "deblur": ("deblur",)}
_TARGETS = ("2x", "4x", "4k")
_FITS = ("contain", "cover", "pad")
_FIELDS = {field.name: field for field in ENHANCE_PARAMETERS.fields}


def _gigabytes(size: int) -> str:
    return f"{size / 1024 ** 3:.1f}"


class AIEnhancePanel(BaseToolPanel):
    """画质增强：先打开要做的处理并选模型，再点底部的“渲染预览”。

    面板只表达用户的选择并显示状态快照：哪项处理在做、各目标尺寸能否执行、模型是否就绪都取自
    ``EnhancementPanelQuery``；开关是参数的另一种呈现，打开时写入起始值，关闭时写回不处理的值。

    预览任务归宿主。面板发出 ``preview_requested``、``cancel_requested``，任务是否在跑由宿主经
    ``set_preview_state()`` 报告：processing、updated、failed、stopped，并带回发起时的 ``preview_ticket``。
    参数或图片一变凭据就换，过期的报告不处理；面板自己做宿主时（批量窗口）直接置位。
    """

    PARAMETER_CONTRACT = ENHANCE_PARAMETERS
    PARAMETER_LABELS = {key: ("enhance." + key, "percent") for keys in _GROUPS.values() for key in keys}
    TOOL_ID = "ai_enhance"
    TOOL_TITLE_KEY = "menu.edit.ai_enhance"
    TOOL_TITLE_DEFAULT = "Image Enhancement"
    MANUAL_PREVIEW = True
    preview_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    availability_changed = pyqtSignal()

    def __init__(self, image_path, parent=None, edit_state_store=None, **services):
        from ...application.panel_render_service import PanelRenderService
        from ...application.tool_runtime import ToolRuntime
        render = services.get("render_service")
        self._owned_runtime = None
        if render is None:
            from ...application.image_source_service import ImageSourceService
            sources = services.get("image_source_service") or ImageSourceService()
            services["image_source_service"] = sources
            self._owned_runtime = ToolRuntime(compute_budget=sources.compute, resource_budget=sources.resources,
                                              execution_threads=sources.policy.operator_threads)
            render = PanelRenderService(self._owned_runtime)
            services["render_service"] = render
        self.models = render.runtime.models
        self._inference = getattr(render.runtime, "inference", None)
        self._query = EnhancementPanelQuery(render.runtime, self.models)
        self._state = None
        self._preview = ManualPreviewState()
        self._params = self._default_model_params()
        super().__init__(image_path, parent, edit_state_store=edit_state_store, **services)
        self._sync_ui_from_params()
        self.destroyed.connect(self._cleanup_themed_i18n)
        if self._owned_runtime is not None:
            self.destroyed.connect(self._owned_runtime.close)
        self.load_panel_source(image_path)

    @property
    def parameters(self):
        return dict(self._params)

    @staticmethod
    def _default_params():
        return ENHANCE_PARAMETERS.defaults(3)

    def _default_model_params(self):
        params = self._default_params()
        for cap in ("super_resolution", "denoise", "deblur"):
            params[cap + "_model"] = self.models.default(cap)
        return params

    def build_ui(self):
        self._sections, self._selectors, self._details, self._rows = {}, {}, {}, {}
        self._step_select = QLabel(self)
        self._step_select.setObjectName("stepTitle")
        self._root_layout.addWidget(self._step_select)

        for group, keys in _GROUPS.items():
            section = ToggleSection(parent=self)
            section.toggled_by_user.connect(lambda checked, name=group: self._toggle(name, checked))
            selector = ModelSelector(self.models, group, section,
                                     backend_status=self._inference.status if self._inference else None)
            selector.selected.connect(lambda token, name=group: self._change(name + "_model", token))
            selector.manage_requested.connect(self.model_manager_requested)
            # 模型就绪后才显示这一组的选项
            detail = QWidget(section)
            detail_layout = QVBoxLayout(detail)
            detail_layout.setContentsMargins(0, 0, 0, 0)
            detail_layout.setSpacing(px(SPACING_SM))
            for widget in (selector, detail):
                section.add_widget(widget)
            self._root_layout.addWidget(section)
            self._sections[group], self._selectors[group], self._details[group] = section, selector, detail
            for key in keys:
                field = _FIELDS[key]
                label_key, value_format = self.PARAMETER_LABELS[key]
                spec = ParameterSpec(key, label_key, key, field.minimum, field.maximum, field.default, value_format)
                row = ParameterRow(spec, "", detail)
                slider = row.slider
                self.track_edit_control(slider, started=slider.sliderPressed, finished=slider.sliderReleased,
                                        is_active=slider.isSliderDown)
                row.typing_finished.connect(self.end_edit_group)
                row.value_edited.connect(lambda value, name=key: self._change(name, value))
                detail_layout.addWidget(row)
                self._rows[key] = row
        # 拖动结束后再按参数收起已经归零的一组
        self.edit_group_finished.connect(self._project)

        # 放大：倍数、尺寸变化，选 4K 时才需要的构图
        upscale = self._details["super_resolution"].layout()
        self._target_chips = ChipGroup(len(_TARGETS), self)
        self._target_chips.set_items([(value, "") for value in _TARGETS])
        self._target_chips.current_changed.connect(lambda value: self._change("target", value))
        upscale.addWidget(self._target_chips)
        # 因为内存不够而做不了的目标：写出各自要多少、这台电脑最多给多少
        self._memory_info = QLabel(self)
        self._memory_info.setObjectName("statusWarning")
        self._memory_info.setWordWrap(True)
        upscale.addWidget(self._memory_info)
        self._output_info = QLabel(self)
        self._output_info.setObjectName("mutedText")
        self._output_info.setWordWrap(True)
        upscale.addWidget(self._output_info)
        self._fit_row = QWidget(self)
        fit_layout = QHBoxLayout(self._fit_row)
        fit_layout.setContentsMargins(0, 0, 0, 0)
        fit_layout.setSpacing(px(SPACING_SM))
        self._fit_label = QLabel(self._fit_row)
        self._fit_combo = AppleDropDown(self._fit_row)
        for value in _FITS:
            self._fit_combo.addItem("", value)
        self._fit_combo.activated.connect(lambda index: self._change("fit", self._fit_combo.itemData(index)))
        fit_layout.addWidget(self._fit_label)
        fit_layout.addWidget(self._fit_combo, 1)
        upscale.addWidget(self._fit_row)

        self._info_card = CollapsibleCard("", self, is_expanded=False)
        self._backend_info = QLabel(self)
        self._backend_info.setObjectName("mutedText")
        self._backend_info.setWordWrap(True)
        self._info_card.add_widget(self._backend_info)
        self._color_info = QLabel(self)
        self._color_info.setObjectName("mutedText")
        self._color_info.setWordWrap(True)
        self._info_card.add_widget(self._color_info)
        self._root_layout.addWidget(self._info_card)
        self._root_layout.addStretch()

        # 第二步：说明将要做什么，点了才渲染。宿主支持时固定在底部，不随上面的内容滚动
        self._footer = ActionFooter(self)
        self._footer.primary_clicked.connect(self._request_preview)
        self._footer.cancel_clicked.connect(self._cancel_processing)
        self._root_layout.addWidget(self._footer)
        self._runner = ModelTaskRunner(self.models, self)
        self._runner.changed.connect(self._availability)
        self.retranslate_ui()

    def take_footer(self):
        self._root_layout.removeWidget(self._footer)
        return self._footer

    def retranslate_ui(self):
        self._step_select.setText(self._tr("enhance.step.select"))
        self._footer.set_title(self._tr("enhance.step.render"))
        for group, section in self._sections.items():
            section.set_title(self._tr("enhance.group." + group))
            self._selectors[group].combo.setAccessibleName(self._tr("enhance." + group + "_model"))
            self._selectors[group].combo.setToolTip(self._tr("enhance." + group + "_model"))
        for value in _TARGETS:
            self._target_chips.set_text(value, self._tr("enhance.target." + value))
        self._fit_label.setText(self._tr("enhance.fit"))
        for index, value in enumerate(_FITS):
            self._fit_combo.setItemText(index, self._tr("enhance.fit." + value))
        for key, row in self._rows.items():
            row.set_label(self._tr("enhance." + key))
        self._info_card.set_title(self._tr("enhance.info"))
        self._color_info.setText(self._tr("enhance.color_range"))
        self._project()

    def _change(self, key, value):
        if value is not None:
            self._edit({**self._params, key: value})

    def _toggle(self, group, checked):
        """开关是参数的另一种呈现：打开写入起始值，关闭写回不处理的值。"""
        params = dict(self._params)
        if group == "super_resolution":
            choice = next((item.target for item in self._state.targets if not item.error and item.enlarges), None)
            params["target"] = choice if checked and choice else "original"
        else:
            for key, value in ENHANCE_START_STRENGTHS[group].items():
                params[key] = value if checked else 0
        self._edit(params)

    def _edit(self, params):
        if self._commit_params(params):
            self.cancel_preview()
            self.cancel_requested.emit()
        self._sync_ui_from_params()

    def notify_params_changed(self, params, record_stash=True):
        checked = self.PARAMETER_CONTRACT.normalize(params, 3)
        if not checked.success:
            self.request_status.emit(self._tr(checked.error.message_key))
            return
        self._params = checked.data
        self._invalidate_preview()
        self._publish_params(self._params, record_stash)

    def restore_params(self, params):
        self._invalidate_preview()
        super().restore_params(params)

    @property
    def preview_ticket(self):
        """当前这份参数与图片对应的预览凭据；宿主报告状态时带回，过期的报告不处理。"""
        return self._preview.ticket

    def _invalidate_preview(self):
        self._preview.invalidate()

    def _availability(self):
        """模型或运行环境变化后重新查询。"""
        self._query.refresh()
        self._project()

    def on_memory_budget_changed(self):
        """各目标尺寸能否执行与内存说明随新的上限重新投影；参数、展开状态与已有的预览不动。"""
        self._project()

    def _project(self):
        """把参数与状态快照投影到界面：开关、每组的内容、底部的说明与按钮。"""
        state = self._state = self._query.state(self._params, self.source_size)
        changed = getattr(self, "models_ready", None) != state.ready
        self.models_ready = state.ready
        width, height = state.source_size
        for group, keys in _GROUPS.items():
            feature, section = state.feature(group), self._sections[group]
            # 先定是否保持展开，再回填开关：拖到零的那一刻不能把正在拖的滑块收起来
            section.set_content_held(any(self._rows[key].slider.isSliderDown() for key in keys))
            section.set_checked(feature.active)
            section.set_description(self._tr("enhance.desc." + group))
            self._selectors[group].set_readiness(feature.readiness)
            self._details[group].setVisible(feature.ready)

        # 放大：这张图片做不了的目标置灰并写明原因；一个都做不了时整项不能打开
        upscale, current = self._sections["super_resolution"], state.current_target
        upscale.set_switch_enabled(state.can_enlarge or current is not None)
        if state.targets and not state.can_enlarge and current is None:
            upscale.set_description(self._tr("enhance.upscale.unavailable", width=width, height=height))
        for outcome in state.targets:
            reason = self._tr(outcome.error) if outcome.error else self._tr("enhance.target.not_larger")
            self._target_chips.set_item_enabled(
                outcome.target, (not outcome.error and outcome.enlarges) or outcome.target == state.target, reason)
        blocked = state.over_memory
        self._memory_info.setVisible(bool(blocked) and state.memory_limit is not None)
        if blocked and state.memory_limit is not None:
            needs = self._tr("enhance.summary.separator").join(
                self._tr("enhance.memory.need", target=self._tr("enhance.target." + item.target),
                         size=_gigabytes(item.memory_bytes)) for item in blocked)
            self._memory_info.setText(self._tr("enhance.memory.over", needs=needs,
                                               limit=_gigabytes(state.memory_limit)))
        if current is None:
            self._output_info.clear()
        elif current.error:
            self._output_info.setText(self._tr(current.error))
        else:
            self._output_info.setText(self._tr(
                "enhance.size_change" if current.enlarges else "enhance.size_shrink",
                width=width, height=height, out_width=current.output_size[0], out_height=current.output_size[1]))
        self._output_info.setVisible(bool(self._output_info.text()))
        self._fit_row.setVisible(state.target == "4k")

        self._project_footer(state)
        self._update_backend_info()
        if changed:
            self.availability_changed.emit()

    def _project_footer(self, state):
        steps = []
        current = state.current_target
        if current is not None and (current.error or current.enlarges):
            steps.append(self._tr("enhance.summary.upscale", target=self._tr("enhance.target." + state.target)))
        elif current is not None:
            steps.append(self._tr("enhance.summary.shrink", width=current.output_size[0], height=current.output_size[1]))
        if state.feature("denoise").active:
            steps.append(self._tr("enhance.summary.denoise"))
        if state.feature("deblur").active:
            steps.append(self._tr("enhance.summary.deblur", value=self._params["deblur"]))
        steps = self._tr("enhance.summary.separator").join(steps)
        blocked, reason = state.blocked_feature, ""
        if state.size_error:
            reason = self._tr(state.size_error)
        elif blocked is not None:
            reason = self._tr("preview.blocked", group=self._tr("enhance.group." + blocked.feature_id),
                              reason=self._tr("tool_manager.state." + blocked.readiness))
        self._footer.show_preview(steps=steps, preview_state=self._preview.state,
                                idle="" if state.active else self._tr("enhance.summary.none"),
                                blocked=reason, ready=self.source_bundle is not None)

    def _update_backend_info(self):
        if self._inference is None:
            self._backend_info.clear()
            return
        status = self._inference.status()
        text = self._tr("inference.backend", name=self._tr("inference.backend." + status.backend))
        self._backend_info.setText(text + (" " + self._tr(status.reason) if status.reason else ""))

    def _sync_ui_from_params(self):
        if self._params["target"] in _TARGETS:
            self._target_chips.set_current(self._params["target"])
        self._fit_combo.setCurrentIndex(self._fit_combo.findData(self._params["fit"]))
        for key, row in self._rows.items():
            row.set_value(self._params[key])
        for group, selector in self._selectors.items():
            if selector.token() != self._params[group + "_model"]:
                selector.set_token(self._params[group + "_model"])
        self._project()

    def update_preview(self):
        if hasattr(self, "_footer"):
            self._project()

    def _request_preview(self):
        if self.external_preview:
            # 任务由宿主开始，开始后由它报告 processing
            self.preview_requested.emit()
        else:
            self.set_preview_state("processing")
            self.preview_panel_image()

    def _emit_panel_preview(self, image):
        super()._emit_panel_preview(image)
        self.set_preview_state("updated")

    def _cancel_processing(self):
        if self.external_preview:
            # 任务停下后由宿主报告 stopped，在此之前显示“正在取消”
            self.set_preview_state("cancelling")
            self.cancel_requested.emit()
        else:
            self.cancel_preview()
            self.set_preview_state("cancelled")

    def apply(self):
        try:
            return self.render_panel_image()
        except Exception as error:
            self._processing_error(error)
            return None

    def reset(self):
        self._apply_params(self._default_model_params())

    def on_image_path_changed(self, path):
        self._invalidate_preview()
        self.load_panel_source(path)

    def cleanup(self):
        super().cleanup()

    def close_panel(self):
        self.cleanup()
        self._runner.close()
        for selector in self._selectors.values():
            selector.close_subscription()
        self._cleanup_themed_i18n()
        if self._owned_runtime is not None:
            self._owned_runtime.close()

    def _processing_error(self, error):
        self.set_preview_state("failed")
        super()._processing_error(error)

    def set_preview_state(self, state, ticket=None):
        """报告预览任务的状态。ticket 是发起时的凭据，与当前凭据不同的报告不处理。

        stopped 表示任务没有完成就停了：是用户取消的显示“已取消”，否则回到尚未预览。
        """
        if self._preview.report(state, ticket):
            self._project()
