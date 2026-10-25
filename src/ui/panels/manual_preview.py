"""手动预览的面板共用的凭据与状态。

预览任务归宿主：面板发出请求，宿主在任务排队、完成、中断时报告状态，并带回发起时的凭据。
参数或图片一变凭据就换，过期的报告不处理。
"""

from __future__ import annotations


class ManualPreviewState:
    def __init__(self):
        self.ticket = 0
        self.state = "not_previewed"

    def invalidate(self) -> None:
        """参数或图片变了：换一张凭据，回到尚未预览。"""
        self.ticket += 1
        self.state = "not_previewed"

    def report(self, state: str, ticket=None) -> bool:
        """记下宿主报告的状态，返回界面是否需要更新。ticket 与当前凭据不同的报告不处理。

        stopped 表示任务没有完成就停了：是用户取消的记为“已取消”，否则回到尚未预览。
        """
        if ticket is not None and ticket != self.ticket:
            return False
        if state == "stopped":
            if self.state == "cancelling":
                state = "cancelled"
            elif self.state == "processing":
                state = "not_previewed"
            else:
                return False
        self.state = state
        return True
