"""一个工具各项功能的资源就绪状态，取自工具资源投影；面板据此显示，不另写就绪判断。"""

from __future__ import annotations

from ..core.contracts.tool_resources import ToolParameterRequest
from .tool_resource_service import ResourceContext, ToolResourceService

READY = "ready"


class ToolFeatureReadiness:
    """只在界面线程使用。就绪只与各项功能所选的模型有关，按选择缓存；模型或运行环境变化后调用 refresh()。"""

    def __init__(self, tools, models, tool_id, contract, features, *, fields=()):
        """fields：除功能声明里的模型选择外，还会改变所需模型的参数（如美颜的人脸定位模型）。"""
        self._resources = ToolResourceService(tools, models)
        self._tool_id = tool_id
        self._contract = contract
        self._fields = tuple(dict.fromkeys((*(model.selection_field for feature in features
                                              for model in feature.models if model.selection_field), *fields)))
        self._implementation_version = ""
        self._tokens = None
        self._states: dict[str, str] = {}
        self._features = {}

    def refresh(self) -> None:
        self._tokens = None

    def set_implementation(self, implementation_version: str) -> None:
        """当前记录的精确实现版本；所需模型随版本而定的工具据此判断，空串表示新建。"""
        if implementation_version != self._implementation_version:
            self._implementation_version = implementation_version
            self._tokens = None

    def states(self, params) -> dict[str, str]:
        """功能标识到状态：ready，或工具资源投影给出的缺口（model_missing、runtime_missing 等）。"""
        tokens = tuple(params.get(field, "") for field in self._fields)
        if tokens != self._tokens:
            # 按开始调整后会用到的模型判断，参数全为 0 时也能提前说明缺口
            from .tool_modules import analysis_settings
            request = ToolParameterRequest(self._tool_id, analysis_settings(self._tool_id, dict(params)),
                                           self._contract.version, self._implementation_version)
            overview = self._resources.overview(ResourceContext(None, requests=(request,)))
            row = next((item for item in overview.rows if item.tool_id == self._tool_id), None)
            self._features = {feature.feature_id: feature for feature in row.features} if row else {}
            self._states = {feature_id: feature.state for feature_id, feature in self._features.items()}
            self._tokens = tokens
        return self._states

    def missing_model(self, params, feature_id):
        """该功能里第一个还不能用的模型（工具资源投影的资源项），都能用时为 None。"""
        self.states(params)
        feature = self._features.get(feature_id)
        return next((resource for resource in feature.resources if resource.kind == "model" and not resource.usable),
                    None) if feature is not None else None
