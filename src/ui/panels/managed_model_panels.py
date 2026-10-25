from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QLabel

from .base_tool_panel import BaseToolPanel
from .beauty_panel import LegacyBeautyPanel
from .bg_remove_panel import LegacyBGRemovePanel
from ...application.tool_feature_readiness import READY, ToolFeatureReadiness
from ...core.tools.managed_models import (BACKGROUND_RESOURCE_FEATURES, BEAUTY_RESOURCE_FEATURES, FACE_LOCATOR_MODEL,
                                         MANAGED_BG_PARAMETERS, MANAGED_BEAUTY_PARAMETERS,
                                         background_required_resources, beauty_required_resources)
from ...core.tools.beauty import BEAUTY_RECOMMENDED_PRESET
from ...core.contracts.beauty import (DETAIL_BEAUTY_ALGORITHM, DETAIL_BEAUTY_PROFILES, detail_beauty_algorithm,
                                      is_current_detail_beauty)
from ..widgets.model_selector import ModelSelector


class ManagedModelPanelMixin:
    """带模型选择的面板：模型能不能用取自工具资源投影，面板只把状态交给模型选择并公布 models_ready。"""

    MODEL_CAPABILITY = ""
    RESOURCE_FEATURES = ()
    # 模型选择所服务的那项功能
    MODEL_FEATURE = ""
    # 可在模型下拉里搭配选用的模型：(模型标识, 这一项的文案键, 参数字段)
    MODEL_PAIRING = None

    def __init__(self, image_path, parent=None, edit_state_store=None, **services):
        from ...application.panel_render_service import PanelRenderService
        from ...application.tool_runtime import ToolRuntime
        from ...application.image_source_service import ImageSourceService
        render = services.get("render_service")
        owned = None
        if render is None:
            sources = services.get("image_source_service") or ImageSourceService()
            services["image_source_service"] = sources
            owned = ToolRuntime(compute_budget=sources.compute, resource_budget=sources.resources,
                                execution_threads=sources.policy.operator_threads)
            render = PanelRenderService(owned)
            services["render_service"] = render
        self.models = render.runtime.models
        self._inference = getattr(render.runtime, "inference", None)
        self._readiness = ToolFeatureReadiness(render.runtime, self.models, self.TOOL_ID,
                                               self.PARAMETER_CONTRACT, self.RESOURCE_FEATURES,
                                               fields=(self.MODEL_PAIRING[2],) if self.MODEL_PAIRING else ())
        super().__init__(image_path, parent, edit_state_store=edit_state_store, **services)
        if owned is not None:
            self.destroyed.connect(owned.close)

    def build_ui(self):
        super().build_ui()
        self.model_selector = ModelSelector(self.models, self.MODEL_CAPABILITY, self,
                                            backend_status=self._inference.status if self._inference else None,
                                            pairing=self.MODEL_PAIRING[:2] if self.MODEL_PAIRING else None)
        self.model_selector.selected.connect(self._model_selected)
        self.model_selector.selection_changed.connect(self._models_selected)
        self.model_selector.manage_requested.connect(self.model_manager_requested)
        self.model_selector.runner.changed.connect(self._models_changed)
        self._place_model_selector()

    def _place_model_selector(self):
        """把模型选择放进面板；默认在最上方。"""
        self._root_layout.insertWidget(0, self.model_selector)

    def _default_pairing(self):
        """新开时搭配模型的默认选择；空串表示不搭配。"""
        return ""

    def _restore_initial_params(self):
        defaults = self._default_params()
        defaults[self.MODEL_CAPABILITY + "_model"] = self.models.default(self.MODEL_CAPABILITY)
        if self.MODEL_PAIRING:
            defaults[self.MODEL_PAIRING[2]] = self._default_pairing()
        params = self._edit_state_store.get_initial_params(self._image_path, self.TOOL_ID, defaults) if self._edit_state_store else defaults
        checked = self.PARAMETER_CONTRACT.normalize(params, self.PARAMETER_CONTRACT.version)
        self._params = checked.data if checked.success else defaults
        if self.TOOL_ID == "bg_remove":
            self._color_target = tuple(self._params["target_color"])
        self._sync_ui_from_params()

    def restore_params(self, params):
        BaseToolPanel.restore_params(self, params)

    def reset(self):
        if hasattr(self, "_cancel_auto"):
            self._cancel_auto()
        field = self.MODEL_CAPABILITY + "_model"
        token = self._params.get(field) or self.models.default(self.MODEL_CAPABILITY)
        self._apply_params({**self._default_params(), **self._model_choice(), field: token})

    def _sync_ui_from_params(self):
        super()._sync_ui_from_params()
        if hasattr(self, "model_selector"):
            self.model_selector.set_token(self._params.get(self.MODEL_CAPABILITY + "_model", ""),
                                          self._params.get(self.MODEL_PAIRING[2], "") if self.MODEL_PAIRING else "")
        self._model_status_changed()

    def _model_choice(self):
        """当前参数里的模型选择（模型及搭配模型），套用推荐或自动参数时保留。"""
        fields = (self.MODEL_CAPABILITY + "_model", *((self.MODEL_PAIRING[2],) if self.MODEL_PAIRING else ()))
        return {field: self._params[field] for field in fields if field in self._params}

    def _models_changed(self):
        """模型或运行环境变化后重新查询。"""
        self._readiness.refresh()
        self._model_status_changed()

    def _model_needed(self):
        """当前参数下是否用得到所选的模型；用不到时不提示它的缺口。"""
        return True

    def _model_status_changed(self):
        states = self._readiness.states(self._params)
        ready = all(states.get(requirement.feature_id) == READY
                    for requirement in self.RESOURCE_REQUIREMENTS(self._params))
        if hasattr(self, "model_selector"):
            state = states.get(self.MODEL_FEATURE, "model_missing") if self._model_needed() else READY
            companion = ""
            if state == "model_missing":
                # 所选模型能用而同一功能还缺别的模型时，说明与安装入口指向缺的那一个
                missing = self._readiness.missing_model(self._params, self.MODEL_FEATURE)
                selected = self._params.get(self.MODEL_CAPABILITY + "_model", "")
                if missing is not None and missing.token and missing.token != selected:
                    companion = missing.token
            self.model_selector.set_readiness(state, companion)
        changed = getattr(self, "models_ready", None) != ready
        self.models_ready = ready
        if changed:
            self.availability_changed.emit()

    def notify_params_changed(self, params, record_stash=True):
        super().notify_params_changed(params, record_stash)
        self._model_status_changed()

    def _model_selected(self, token):
        self._commit_params({**self._params, self.MODEL_CAPABILITY + "_model": token})

    def _models_selected(self, token, paired):
        self._commit_params({**self._params, self.MODEL_CAPABILITY + "_model": token, self.MODEL_PAIRING[2]: paired})


class BGRemovePanel(ManagedModelPanelMixin, LegacyBGRemovePanel):
    availability_changed = pyqtSignal()
    MODEL_CAPABILITY = "segmentation"
    PARAMETER_CONTRACT = MANAGED_BG_PARAMETERS
    RESOURCE_FEATURES = BACKGROUND_RESOURCE_FEATURES
    MODEL_FEATURE = "ai"
    RESOURCE_REQUIREMENTS = staticmethod(background_required_resources)

    def _place_model_selector(self):
        # 模型只在 AI 抠图时用得到，放在方式一组里
        self._ai_box.layout().addWidget(self.model_selector)

    def _model_needed(self):
        return self._params.get("mode") == "ai"

    def restore_params(self, params):
        LegacyBGRemovePanel.restore_params(self, params)

    def _model_status_changed(self):
        super()._model_status_changed()
        self._project_footer()

    def _blocked_reason(self):
        if not self._model_needed():
            return ""
        state = self._readiness.states(self._params).get(self.MODEL_FEATURE, "model_missing")
        if state == READY:
            return ""
        return self._tr("preview.blocked", group=self._tr("bg_remove.mode.ai"),
                        reason=self._tr("tool_manager.state." + state))

    @staticmethod
    def _default_params():
        return MANAGED_BG_PARAMETERS.defaults(3)


class BeautyPanel(ManagedModelPanelMixin, LegacyBeautyPanel):
    availability_changed = pyqtSignal()
    MODEL_CAPABILITY = "face_detection"
    PARAMETER_CONTRACT = MANAGED_BEAUTY_PARAMETERS
    RESOURCE_FEATURES = BEAUTY_RESOURCE_FEATURES
    MODEL_FEATURE = "effect"
    RESOURCE_REQUIREMENTS = staticmethod(beauty_required_resources)
    # 模型下拉里多一项“多人脸”：选了人脸定位模型即开启多人脸检测
    MODEL_PAIRING = (FACE_LOCATOR_MODEL, "beauty.model.multi_face", "face_locator_model")
    PARAMETER_LABELS = {**LegacyBeautyPanel.PARAMETER_LABELS, "face_locator_model": ("beauty.param.multi_face", "")}

    def __init__(self, *args, **kwargs):
        self._detail_beauty = True
        self._detail_algorithm = DETAIL_BEAUTY_ALGORITHM
        self._beauty_status = None
        self._multi_face_supported = True
        super().__init__(*args, **kwargs)

    def _default_pairing(self):
        from ...application.managed_tool_modules import default_face_locator
        return default_face_locator(self.models)

    def _place_model_selector(self):
        super()._place_model_selector()
        self._multi_face_hint = QLabel(self)
        self._multi_face_hint.setObjectName("mutedText")
        self._multi_face_hint.setWordWrap(True)
        self._root_layout.insertWidget(self._root_layout.indexOf(self.model_selector) + 1, self._multi_face_hint)

    def set_beauty_reference(self, reference):
        detail = reference is None or reference.implementation_id == "beauty.detail"
        algorithm = (detail_beauty_algorithm(reference.implementation_version) if reference is not None
                     else DETAIL_BEAUTY_ALGORITHM) if detail else None
        if detail != self._detail_beauty or algorithm != self._detail_algorithm:
            self._cancel_auto()
            self._beauty_status = None
        self._detail_beauty = detail
        self._detail_algorithm = algorithm
        # 多人脸要求计算配方已是最新；更早的旧版效果需先升级
        self._multi_face_supported = reference is None or is_current_detail_beauty(
            reference.implementation_id, reference.implementation_version)
        self.model_selector.set_pairing_enabled(self._multi_face_supported)
        # 所需模型随这条记录的效果版本与人脸定位模型的选择而定
        self._readiness.set_implementation(reference.implementation_version if detail and reference is not None else "")
        self._model_status_changed()

    def set_beauty_status(self, status):
        self._beauty_status = status
        self._project_engine()

    def _cancel_auto(self):
        super()._cancel_auto()
        if self._beauty_status == "processing":
            self._beauty_status = None

    @staticmethod
    def _default_params():
        return MANAGED_BEAUTY_PARAMETERS.defaults(3)

    def _on_recommended_preset(self):
        if self._detail_beauty:
            self._start_detail_analysis(preset=True)
            return
        self._cancel_auto()
        self._apply_params({**BEAUTY_RECOMMENDED_PRESET, **self._model_choice()})

    def _on_auto_ready(self, request_id, params):
        from ...application.beauty_analysis import BeautyAnalysisNotice
        if request_id != self._auto_request_id:
            return
        if not self._auto_guard():
            self.set_beauty_status(None)
            return
        if isinstance(params, BeautyAnalysisNotice):
            self.set_beauty_status(params.status)
            self.request_status.emit(self._tr("beauty.detail." + params.status))
            return
        self.set_beauty_status(None)
        self._apply_params({**params, **self._model_choice()}, publication=self._auto_publication)

    def _on_auto_beauty(self):
        if self._detail_beauty:
            self._start_detail_analysis(preset=False)
        else:
            super()._on_auto_beauty()

    def _start_detail_analysis(self, *, preset):
        import copy
        from ...application.beauty_analysis import beauty_analysis_outcome
        if self.parameters_pending or self.source_bundle is None:
            return
        self._cancel_auto()
        request_id = self._auto_request_id
        settings = copy.deepcopy(self._params)
        if self.beauty_analysis_provider is not None:
            job = self.beauty_analysis_provider(settings, preset)
            self._auto_guard = job.is_current
            self._auto_publication = job
        else:
            service, sources, identity = self._ensure_render_service(), self._image_source_service, self.source_bundle.identity
            reference = self._bound_tool_reference
            job = lambda cancelled: service.analyze_full(identity, self.TOOL_ID, settings, source_service=sources,
                                                         is_cancelled=cancelled, preset=preset, reference=reference)
            self._auto_guard = lambda: True
            self._auto_publication = None
        self.set_beauty_status("processing")
        self.submit_background(self.source_bundle.proxy, lambda image, cancelled: beauty_analysis_outcome(job, cancelled),
            lambda params: self._on_auto_ready(request_id, params), self._on_auto_error, cancellable=True)

    def _on_auto_error(self, error):
        from ...application.result_types import CoreOperationError
        if isinstance(error, CoreOperationError) and error.error.code == "CANCELLED":
            self.set_beauty_status(None)
            return
        state = error.error.message_key.removeprefix("beauty.detail.") if isinstance(error, CoreOperationError) else "failed"
        self.set_beauty_status(state if state in ("no_face", "insufficient_landmarks", "face_too_small") else "failed")
        super()._on_auto_error(error)

    def _model_status_changed(self):
        super()._model_status_changed()
        self._project_engine()

    def _project_engine(self):
        super()._project_engine()
        if hasattr(self, "_multi_face_hint"):
            key = ("beauty.multi_face.legacy" if not self._multi_face_supported
                   else "beauty.multi_face.on" if self._params.get("face_locator_model") else "beauty.multi_face.off")
            self._multi_face_hint.setText(self._tr(key))

    def _models_changed(self):
        self._cancel_auto()
        super()._models_changed()

    def _engine_state(self):
        # 模型用不了时由模型选择说明原因并给出安装入口，这里只把参数置灰
        if self._readiness.states(self._params).get(self.MODEL_FEATURE) != READY:
            return "pending", "", "", frozenset(self._parameter_rows)
        if not self._detail_beauty:
            return "success", self._tr("beauty.engine.ready"), "", frozenset()
        calculation = DETAIL_BEAUTY_PROFILES[self._detail_algorithm][1]
        description = self._tr("beauty.detail.description" if calculation == 1
                               else f"beauty.detail.description_v{calculation}")
        status = self._beauty_status
        if isinstance(status, str):
            return ("pending" if status == "processing" else "warning",
                    self._tr("beauty.detail." + status), description, frozenset())
        if status is not None:
            if status.processed:
                text = self._tr("beauty.detail.processed", count=status.processed, skipped=status.skipped)
                if status.spots_skipped:
                    text += " · " + self._tr("beauty.detail.spots_skipped", count=status.spots_skipped)
                return "success", text, description, frozenset()
            return "warning", self._tr("beauty.detail." + status.status), description, frozenset()
        return "success", self._tr("beauty.detail.ready"), description, frozenset()
