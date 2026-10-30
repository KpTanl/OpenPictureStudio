"""Edit state facade for OpenPictureStudio.

Two tiers remain exposed here:
  1. Per-image draft: uncommitted in-progress edits (delegated to ``DraftStore``).
  2. Last-used params: cross-image parameters remembered from the last apply.

``EditStateStore`` is kept as a compatibility facade so existing UI code keeps
working. New code should talk to ``DraftStore`` (drafts) and the Core API.
"""

from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, Signal

from .draft_store import DraftStore


class EditStateStore(QObject):
    """兼容外观：暂存（Draft）委托给 DraftStore，last-used 仍由本类持有。"""

    stash_changed = Signal(str)        # image_path
    last_used_changed = Signal(str)    # tool_name

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._drafts = DraftStore(on_changed=self.stash_changed.emit)
        self._last_used: Dict[str, Dict[str, Any]] = {}

    @property
    def drafts(self) -> DraftStore:
        """The Draft tier (Phase 1 split). Panels may use this directly."""
        return self._drafts

    # ---- 读取方法 ----
    def get_initial_params(
        self, image_path: str, tool: str, defaults: Dict[str, Any]
    ) -> Dict[str, Any]:
        """打开面板时调用：按优先级返回初始参数。

        优先级：
          1. 该图片当前的未应用暂存（若有）
          2. 该工具上次成功应用的参数（若有）
          3. 默认参数
        """
        draft = self._drafts.get_draft(image_path, tool)
        if draft is not None:
            return draft

        if tool in self._last_used:
            return dict(self._last_used[tool])

        return dict(defaults)

    get_effective_params = get_initial_params

    def has_pending(self, image_path: str) -> bool:
        """检查特定图片是否有未提交的暂存。"""
        return self._drafts.has_pending(image_path)

    def has_tool_stash(self, image_path: str, tool: str) -> bool:
        """检查特定图片的特定工具是否有暂存。"""
        return self._drafts.has_draft(image_path, tool)

    def get_tool_stash(self, image_path: str, tool: str) -> Optional[Dict[str, Any]]:
        """获取特定图片特定工具的暂存数据（副本）。"""
        return self._drafts.get_draft(image_path, tool)

    def get_last_used(self, tool: str) -> Optional[Dict[str, Any]]:
        """获取工具最近一次应用的参数。"""
        if tool in self._last_used:
            return dict(self._last_used[tool])
        return None

    # ---- 写入方法 ----
    def stash(self, image_path: str, tool: str, params: Dict[str, Any]) -> None:
        """面板参数变化时记录暂存（防抖调用）。"""
        self._drafts.set_draft(image_path, tool, params)

    def commit(
        self, old_path: str, new_path: str, tool: str, params: Dict[str, Any]
    ) -> None:
        """应用成功、文件替换后调用：清除暂存并更新 last-used。"""
        self._drafts.discard(old_path, tool)
        self._last_used[tool] = dict(params)
        self.last_used_changed.emit(tool)

    def discard(self, image_path: str, tool: str) -> None:
        """取消或重置时丢弃暂存。"""
        self._drafts.discard(image_path, tool)

    def clear_path(self, path: str) -> None:
        """临时文件清理时同步清除暂存。"""
        self._drafts.clear_path(path)

    def clear_all(self) -> None:
        """清空所有暂存。"""
        self._drafts.clear_all()
