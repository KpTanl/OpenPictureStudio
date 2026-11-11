from dataclasses import dataclass


MIB = 1024 ** 2


@dataclass(frozen=True)
class ImageComputeSettings:
    engine: str = "auto"
    cache: str = "auto"
    cache_mib: int = 2048

    def __post_init__(self):
        if self.engine not in ("auto", "cpu") or self.cache not in ("auto", "off", "custom"):
            raise ValueError("图像计算设置无效")
        if type(self.cache_mib) is not int or not 64 <= self.cache_mib <= 65536:
            raise ValueError("显存缓存额度无效")

    @classmethod
    def load(cls, settings):
        try:
            return cls(str(settings.value("engine", "auto")), str(settings.value("cache", "auto")),
                       int(settings.value("cache_mib", 2048)))
        except (TypeError, ValueError):
            return cls()

    def save(self, settings):
        for name in ("engine", "cache", "cache_mib"):
            settings.setValue(name, getattr(self, name))
        settings.sync()

    def cache_bytes(self, budget):
        if self.cache == "off":
            return 0
        return min(budget // 2, self.cache_mib * MIB if self.cache == "custom" else 2 * 1024 ** 3)
