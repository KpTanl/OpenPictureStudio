"""画质增强面板的状态查询：把尺寸规划、活动处理和资源就绪汇总成面板要显示的一份快照。

规则各有出处：各目标尺寸能否执行取自 ``target_outcomes()``（放大所需内存与工具运行时的内存上限比较），哪几项处理在做取自
``enhancement_required_resources()``，模型与运行库是否就绪取自工具资源投影。这里只做汇总。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.tools.enhancement import (ENHANCE_PARAMETERS, ENHANCE_RESOURCE_FEATURES, TargetOutcome,
                                      enhancement_required_resources, target_outcomes)
from .tool_feature_readiness import READY, ToolFeatureReadiness

TOOL_ID = "ai_enhance"


@dataclass(frozen=True)
class FeatureStatus:
    """一项处理的状态。readiness 为 ready，或工具资源投影给出的缺口（model_missing、runtime_missing 等）。"""
    feature_id: str
    active: bool
    readiness: str

    @property
    def ready(self) -> bool:
        return self.readiness == READY


@dataclass(frozen=True)
class EnhancementPanelState:
    """memory_limit 是这台机器的工作内存上限（字节），没有资源预算时为 None。"""

    source_size: tuple[int, int]
    target: str
    targets: tuple[TargetOutcome, ...]
    features: tuple[FeatureStatus, ...]
    memory_limit: int | None = None

    @property
    def over_memory(self) -> tuple[TargetOutcome, ...]:
        """因为内存不够而做不了的放大目标。"""
        return tuple(item for item in self.targets if item.error == "error.enhance.upscale_source")

    def feature(self, feature_id: str) -> FeatureStatus:
        return next(item for item in self.features if item.feature_id == feature_id)

    @property
    def current_target(self) -> TargetOutcome | None:
        """当前选中的目标；选的是原尺寸或还不知道源图尺寸时没有。"""
        return next((item for item in self.targets if item.target == self.target), None)

    @property
    def can_enlarge(self) -> bool:
        """这张图片至少有一个目标能放大。"""
        return any(not item.error and item.enlarges for item in self.targets)

    @property
    def active(self) -> bool:
        return any(item.active for item in self.features)

    @property
    def size_error(self) -> str:
        current = self.current_target
        return current.error if current is not None else ""

    @property
    def blocked_feature(self) -> FeatureStatus | None:
        """第一项正在做但资源没有就绪的处理。"""
        return next((item for item in self.features if item.active and not item.ready), None)

    @property
    def ready(self) -> bool:
        return self.blocked_feature is None and not self.size_error


class EnhancementPanelQuery:
    """只在界面线程使用。模型或运行环境变化后调用 refresh()。"""

    def __init__(self, tools, models):
        self._readiness = ToolFeatureReadiness(tools, models, TOOL_ID, ENHANCE_PARAMETERS, ENHANCE_RESOURCE_FEATURES)
        self._models = models
        self._tools = tools

    def refresh(self) -> None:
        self._readiness.refresh()

    def state(self, params, source_size) -> EnhancementPanelState:
        active = {item.feature_id for item in enhancement_required_resources(params)}
        readiness = self._readiness.states(params)
        features = tuple(FeatureStatus(feature.feature_id, feature.feature_id in active,
                                       readiness.get(feature.feature_id, "model_missing"))
                         for feature in ENHANCE_RESOURCE_FEATURES)
        # 内存上限可以在运行中调整，每份快照取一次，目标规划与上限保持一致
        memory_limit = self._tools.memory_limit
        targets = ()
        if min(source_size) > 0:
            models = {}
            try:
                models["super_resolution"] = self._models.describe(params["super_resolution_model"])[0]
            except (KeyError, ValueError, FileNotFoundError):
                pass
            targets = target_outcomes(tuple(source_size), params, models, memory_limit)
        return EnhancementPanelState(tuple(source_size), params["target"], targets, features, memory_limit)
