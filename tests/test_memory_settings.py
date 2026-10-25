"""内存偏好与预算解析：占用上限、系统保留量、座位，以及向系统登记座位。"""

import ctypes
import sys
from ctypes import wintypes

import pytest

from src.application.memory_settings import (FALLBACK_LIMIT_BYTES, GIB, PROCESS_FOOTPRINT_BYTES, RESERVE_FLOOR_BYTES,
                                             MemoryConfigurator, MemorySettings, MemoryState, resolve)
from src.application.system_profile import memory_status, set_working_set_minimum


class _Store:
    """设置存储的替身。"""

    def __init__(self, **values):
        self.values = values

    def value(self, name, default=None):
        return self.values.get(name, default)

    def setValue(self, name, value):
        self.values[name] = value

    def sync(self):
        pass


def test_budget_follows_the_usage_percentage_and_keeps_the_system_reserve():
    rows = {4: (3.2, 0.8, 2), 8: (7.2, 0.8, 4), 16: (14.4, 1.6, 4), 32: (28.8, 3.2, 4), 64: (57.6, 6.4, 4)}
    for total, (limit, reserve, seat) in rows.items():
        budget = resolve(MemorySettings(), total * GIB)
        assert budget.total_bytes == total * GIB
        assert budget.limit_bytes == pytest.approx(limit * GIB, abs=1)
        assert budget.reserve_bytes == pytest.approx(reserve * GIB, abs=1)
        assert budget.seat_bytes == seat * GIB and budget.seat_limit_gib == total // 2


def test_default_limit_matches_the_previous_rule_within_one_byte():
    for total in (8 * GIB, 16 * GIB + 12345, 31 * GIB + 813 * 1024 ** 2 + 7):
        assert abs(resolve(MemorySettings(), total).limit_bytes - (total - total // 10)) <= 1


def test_reserve_does_not_change_with_the_percentage_and_seat_never_exceeds_the_limit():
    low = resolve(MemorySettings(usage_percent=30, seat_gib=4), 8 * GIB)
    assert low.reserve_bytes == RESERVE_FLOOR_BYTES
    assert low.limit_bytes == 8 * GIB * 30 // 100 == low.seat_bytes
    wide = resolve(MemorySettings(usage_percent=50, seat_gib=100), 32 * GIB)
    assert wide.reserve_bytes == 32 * GIB // 10 and wide.limit_bytes == wide.seat_bytes == 16 * GIB
    assert resolve(MemorySettings(seat_gib=0), 32 * GIB).seat_bytes == 0


def test_unreadable_physical_memory_uses_the_fallback_without_a_seat():
    budget = resolve(MemorySettings(), None)
    assert (budget.total_bytes, budget.limit_bytes, budget.seat_bytes) == (None, FALLBACK_LIMIT_BYTES, 0)
    assert budget.reserve_bytes == RESERVE_FLOOR_BYTES and budget.seat_limit_gib == 0
    assert MemoryState().budget == budget


def test_preferences_are_validated_clamped_and_saved():
    for invalid in ({"usage_percent": 29}, {"usage_percent": 91}, {"usage_percent": 50.0}, {"seat_gib": -1},
                    {"seat_gib": True}):
        with pytest.raises(ValueError):
            MemorySettings(**invalid)
    assert MemorySettings.load(_Store()) == MemorySettings(90, 4)
    assert MemorySettings.load(_Store(usage_percent="45", seat_gib="8")) == MemorySettings(45, 8)
    assert MemorySettings.load(_Store(usage_percent=5, seat_gib=-3)) == MemorySettings(30, 0)
    assert MemorySettings.load(_Store(usage_percent=500, seat_gib="many")) == MemorySettings(90, 4)
    store = _Store()
    MemorySettings(60, 2).save(store)
    assert store.values == {"usage_percent": 60, "seat_gib": 2} and MemorySettings.load(store) == MemorySettings(60, 2)


def test_configurator_registers_the_seat_and_keeps_the_previous_one_when_refused():
    calls, approve = [], [True]

    def register(size):
        calls.append(size)
        return approve[0]

    memory = MemoryConfigurator(32 * GIB, register)
    state = memory.apply(MemorySettings())
    assert calls == [4 * GIB + PROCESS_FOOTPRINT_BYTES]
    assert (state.seat_bytes, state.seat_refused) == (4 * GIB, False)
    assert state.policy_fields() == {"memory_bytes": state.budget.limit_bytes, "memory_seat_bytes": 4 * GIB,
                                     "memory_reserve_bytes": 32 * GIB // 10}
    # 只改比例、座位不变：不重新登记
    assert memory.apply(MemorySettings(usage_percent=50)).seat_bytes == 4 * GIB and len(calls) == 1
    # 系统不批准：沿用原来的座位，并说明没有批准
    approve[0] = False
    refused = memory.apply(MemorySettings(seat_gib=8))
    assert calls[-1] == 8 * GIB + PROCESS_FOOTPRINT_BYTES
    assert (refused.seat_bytes, refused.seat_refused, refused.settings.seat_gib) == (4 * GIB, True, 8)
    # 取消座位：登记 0
    approve[0] = True
    cleared = memory.apply(MemorySettings(seat_gib=0))
    assert calls[-1] == 0 and (cleared.seat_bytes, cleared.seat_refused) == (0, False)
    assert memory.apply(MemorySettings(seat_gib=0)).seat_bytes == 0 and len(calls) == 3


def test_configurator_follows_the_limit_when_the_seat_is_capped_by_it():
    calls = []
    memory = MemoryConfigurator(8 * GIB, lambda size: calls.append(size) or True)
    assert memory.apply(MemorySettings()).seat_bytes == 4 * GIB
    lowered = memory.apply(MemorySettings(usage_percent=30))
    assert lowered.seat_bytes == lowered.budget.limit_bytes == 8 * GIB * 30 // 100
    assert calls == [4 * GIB + PROCESS_FOOTPRINT_BYTES, lowered.seat_bytes + PROCESS_FOOTPRINT_BYTES]


def test_configurator_without_physical_memory_never_registers():
    calls = []
    state = MemoryConfigurator(None, lambda size: calls.append(size) or True).apply(MemorySettings())
    assert calls == [] and (state.seat_bytes, state.budget.limit_bytes) == (0, FALLBACK_LIMIT_BYTES)


@pytest.mark.skipif(sys.platform != "win32", reason="常驻内存下限是 Windows 的接口")
def test_working_set_minimum_is_restored_or_refused_without_changes():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
                                                  ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessWorkingSetSizeEx.restype = wintypes.BOOL

    def limits():
        low, high, flags = ctypes.c_size_t(), ctypes.c_size_t(), wintypes.DWORD()
        assert kernel.GetProcessWorkingSetSizeEx(kernel.GetCurrentProcess(), ctypes.byref(low), ctypes.byref(high),
                                                 ctypes.byref(flags))
        return low.value, high.value, flags.value

    total = memory_status().total_bytes
    size = 256 * 1024 ** 2
    previous = limits()
    if not set_working_set_minimum(size):
        assert ctypes.get_last_error() == 1314
        assert limits() == previous
        return
    try:
        low, high, flags = limits()
        # 下限是硬的，上限接近物理内存且是软的
        assert low == size and flags & 1 and flags & 8 and high > total // 2
        # 比上限参数还大的登记不了
        assert not set_working_set_minimum(total)
        assert limits()[0] == size
    finally:
        assert set_working_set_minimum(0)
    low, high, flags = limits()
    assert low < 1024 ** 2 and flags & 2 and high > total // 2
