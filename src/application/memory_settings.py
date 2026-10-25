"""内存预算的默认值、用户偏好与解析的唯一定义。

软件内存占用上限按物理内存的比例给出；留给系统的内存不随比例变化；座位是向系统登记过、
不核对系统可用内存就放行的登记用量。界面只展示这里解析出的结果，不自行计算预算。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MIB = 1024 ** 2
GIB = 1024 ** 3

# 读不到物理内存时的占用上限
FALLBACK_LIMIT_BYTES = 4 * GIB
# 留给系统的内存：物理内存的十分之一，不少于 0.8 GiB
RESERVE_DIVISOR = 10
RESERVE_FLOOR_BYTES = 4 * GIB // 5
# 座位不超过物理内存的一半
SEAT_DIVISOR = 2
# 界面、运行库这些资源池没有登记的常驻内存，与座位相加后一并向系统登记。
# 实测进入编辑后约 257 MiB（未加载模型），取整并给模型会话留出余量
PROCESS_FOOTPRINT_BYTES = 512 * MIB

# 各类空闲缓存的留存额度
SOURCE_CACHE_BYTES = 512 * MIB
SOURCE_CACHE_ITEMS = 4
PROXY_CACHE_BYTES = 64 * MIB
DISPLAY_CACHE_BYTES = 64 * MIB
PREVIEW_CACHE_BYTES = 64 * MIB
PREVIEW_CACHE_ITEMS = 128


def _stored(settings, name, default):
    try:
        return int(settings.value(name, default))
    except (TypeError, ValueError):
        logger.warning("内存偏好 %s 不是整数，采用默认值 %s", name, default)
        return default


@dataclass(frozen=True)
class MemorySettings:
    """用户的内存偏好：占用上限占物理内存的百分比；为本程序保留的内存（GiB），0 表示不保留。"""

    usage_percent: int = 90
    seat_gib: int = 4

    USAGE_RANGE = (30, 90)

    def __post_init__(self):
        low, high = self.USAGE_RANGE
        if type(self.usage_percent) is not int or not low <= self.usage_percent <= high:
            raise ValueError("内存占用上限的比例无效")
        if type(self.seat_gib) is not int or self.seat_gib < 0:
            raise ValueError("保留的内存必须为非负整数")

    @classmethod
    def load(cls, settings):
        """缺失或不是整数的取默认值，超出范围的取范围内最近的值。"""
        defaults = cls()
        low, high = cls.USAGE_RANGE
        return cls(min(high, max(low, _stored(settings, "usage_percent", defaults.usage_percent))),
                   max(0, _stored(settings, "seat_gib", defaults.seat_gib)))

    def save(self, settings):
        settings.setValue("usage_percent", self.usage_percent)
        settings.setValue("seat_gib", self.seat_gib)
        settings.sync()


@dataclass(frozen=True)
class MemoryBudget:
    """按物理内存解析出的预算（字节）。total_bytes 为 None 表示读不到物理内存。"""

    total_bytes: int | None
    limit_bytes: int
    reserve_bytes: int
    seat_bytes: int

    @property
    def seat_limit_gib(self) -> int:
        """这台机器上座位最多能设多少 GiB。"""
        return 0 if self.total_bytes is None else self.total_bytes // SEAT_DIVISOR // GIB


def resolve(settings, total_bytes):
    """由偏好与物理内存给出占用上限、系统保留量和想要的座位。读不到物理内存时用固定上限，不占座。"""
    if total_bytes is None:
        return MemoryBudget(None, FALLBACK_LIMIT_BYTES, RESERVE_FLOOR_BYTES, 0)
    reserve = max(total_bytes // RESERVE_DIVISOR, RESERVE_FLOOR_BYTES)
    limit = min(total_bytes * settings.usage_percent // 100, total_bytes - reserve)
    return MemoryBudget(total_bytes, limit, reserve,
                        min(settings.seat_gib * GIB, total_bytes // SEAT_DIVISOR, limit))


@dataclass(frozen=True)
class MemoryState:
    """已生效的内存配置：偏好、解析出的预算，以及系统实际批准的座位。"""

    settings: MemorySettings = MemorySettings()
    budget: MemoryBudget = resolve(MemorySettings(), None)
    seat_bytes: int = 0
    # 最近一次登记没有得到系统批准，座位沿用原来的
    seat_refused: bool = False

    def policy_fields(self):
        return {"memory_bytes": self.budget.limit_bytes, "memory_seat_bytes": self.seat_bytes,
                "memory_reserve_bytes": self.budget.reserve_bytes}


class MemoryConfigurator:
    """把内存偏好变成生效的预算：按启动时的物理内存解析，并向系统登记座位。

    register 接收要登记的常驻内存下限（字节，0 为取消登记），返回系统是否批准。
    登记被拒绝时沿用原来的座位。
    """

    def __init__(self, total_bytes, register):
        self._total = total_bytes
        self._register = register
        self._seat = 0

    def apply(self, settings):
        budget = resolve(settings, self._total)
        refused = False
        if budget.seat_bytes != self._seat:
            if self._register(budget.seat_bytes + PROCESS_FOOTPRINT_BYTES if budget.seat_bytes else 0):
                self._seat = budget.seat_bytes
            else:
                refused = True
                logger.warning("系统没有批准登记座位 wanted=%s kept=%s", budget.seat_bytes, self._seat)
        return MemoryState(settings, budget, min(self._seat, budget.limit_bytes), refused)
