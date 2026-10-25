from dataclasses import dataclass


@dataclass(frozen=True)
class PreviewTarget:
    mode: str = "full"
    node_id: str | None = None

    def __post_init__(self):
        if (self.mode not in ("full", "after_node") or (self.mode == "full") != (self.node_id is None)
                or self.mode == "after_node" and (not isinstance(self.node_id, str) or not self.node_id)):
            raise ValueError("预览终点无效")

    @classmethod
    def after_node(cls, node_id):
        return cls("after_node", node_id)
