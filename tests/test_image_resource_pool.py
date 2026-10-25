"""图像资源池：内存准入、复用、淘汰与工作内存预留。"""

from concurrent.futures import CancelledError

import pytest
from PIL import Image

from src.application.core_api import CoreAPI
from src.application.image_resources import ImageResourcePolicy, ImageResourcePool
from src.application.image_source_service import ImageSourceService
from src.application.services import ImageEngineAdapter

BUDGET_KEY = "error.image.memory_budget"


def _pool(**overrides):
    values = {"memory_bytes": 1000, "source_cache_bytes": 1000, "proxy_cache_bytes": 1000,
              "display_cache_bytes": 1000}
    values.update(overrides)
    return ImageResourcePool(ImageResourcePolicy(**values))


def _acquire(pool, name, size, *, category="source", calls=None):
    def load(_abandoned):
        if calls is not None:
            calls.append(name)
        return bytearray(size)
    return pool.acquire((name,), category, size, load, len)


def test_estimate_over_total_budget_is_rejected_before_loading():
    """估算超过总预算的资源不可能成功，应立即拒绝且不启动解码。"""
    pool, calls = _pool(), []
    with pytest.raises(MemoryError, match=BUDGET_KEY):
        _acquire(pool, "huge", 2000, calls=calls)
    assert calls == []


def test_same_key_is_loaded_once_and_shared():
    pool, calls = _pool(), []
    with _acquire(pool, "a", 100, calls=calls) as first, _acquire(pool, "a", 100, calls=calls) as second:
        assert first.value is second.value
    assert calls == ["a"]


def test_released_lease_cannot_be_read():
    pool = _pool()
    lease = _acquire(pool, "a", 100)
    lease.close()
    with pytest.raises(RuntimeError):
        lease.value


def test_category_limit_evicts_least_recently_used_idle_entry():
    pool = _pool(source_cache_bytes=100)
    _acquire(pool, "a", 80).close()
    _acquire(pool, "b", 80).close()
    assert pool.borrow_cached(("a",)) is None
    kept = pool.borrow_cached(("b",))
    assert kept is not None
    kept.close()


def test_borrowed_entry_survives_eviction_until_released():
    pool = _pool(source_cache_bytes=100)
    held = _acquire(pool, "a", 80)
    _acquire(pool, "b", 80).close()
    assert len(held.value) == 80
    again = pool.borrow_cached(("a",))
    assert again is not None
    again.close()
    held.close()
    _acquire(pool, "c", 80).close()
    assert pool.borrow_cached(("a",)) is None


def test_borrowed_entries_count_against_the_total_budget():
    """在途借用占着预算时，新资源放不下就拒绝，而不是挤掉正在使用的缓冲。"""
    pool = _pool()
    held = _acquire(pool, "a", 700)
    with pytest.raises(MemoryError, match=BUDGET_KEY):
        _acquire(pool, "b", 700)
    held.close()
    _acquire(pool, "b", 700).close()


def test_loader_error_is_raised_and_not_cached():
    pool, attempts = _pool(), []

    def load(_abandoned):
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("boom")
        return bytearray(10)

    with pytest.raises(ValueError, match="boom"):
        pool.acquire(("a",), "source", 10, load, len)
    pool.acquire(("a",), "source", 10, load, len).close()
    assert len(attempts) == 2


def test_cancelled_request_never_starts_loading():
    pool, calls = _pool(), []
    with pytest.raises(CancelledError):
        pool.acquire(("a",), "source", 10, lambda _abandoned: calls.append(1), len, lambda: True)
    assert calls == []


def test_working_memory_is_reserved_and_released():
    pool = _pool()
    with pool.working_memory(600) as reserved:
        assert reserved == 600
        with pytest.raises(MemoryError, match=BUDGET_KEY):
            with pool.working_memory(600):
                pass
    with pool.working_memory(600) as reserved:
        assert reserved == 600


def test_working_memory_falls_back_to_a_smaller_request():
    pool = _pool()
    with pool.working_memory(600):
        with pool.working_memory(600, fallback_sizes=(300,)) as reserved:
            assert reserved == 300


def test_working_memory_larger_than_budget_is_rejected():
    with pytest.raises(MemoryError, match=BUDGET_KEY):
        with _pool().working_memory(2000):
            pass


def test_closed_pool_rejects_new_requests():
    pool = _pool()
    pool.close()
    with pytest.raises(CancelledError):
        _acquire(pool, "a", 10)


def test_oversized_source_reports_memory_budget_to_the_caller(tmp_path):
    """完整线性源按每像素 64 字节准入；超出总预算时返回内存预算说明而不是内部错误。"""
    path = tmp_path / "large.png"
    Image.new("RGB", (100, 100), "white").save(path)
    engine = ImageEngineAdapter()
    sources = ImageSourceService(engine=engine, policy=ImageResourcePolicy(memory_bytes=100 * 100 * 64 - 1))
    core = CoreAPI(engine=engine, image_source_service=sources, artifact_dir=str(tmp_path / "artifacts"))
    try:
        result = core.open_source(str(path))
        assert not result.success
        assert result.error.code == "DECODE_FAILED"
        assert result.error.message_key == BUDGET_KEY
    finally:
        sources.close()


def test_source_within_budget_opens(tmp_path):
    path = tmp_path / "small.png"
    Image.new("RGB", (100, 100), "white").save(path)
    engine = ImageEngineAdapter()
    sources = ImageSourceService(engine=engine, policy=ImageResourcePolicy(memory_bytes=100 * 100 * 64 * 4))
    core = CoreAPI(engine=engine, image_source_service=sources, artifact_dir=str(tmp_path / "artifacts"))
    try:
        assert core.open_source(str(path)).success
    finally:
        sources.close()


AVAILABLE_KEY = "error.image.memory_available"


class _Probe:
    """可用内存的替身：记录被问了几次。"""

    def __init__(self, available):
        self.available = available
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.available


def _guarded_pool(probe, **overrides):
    """上限 1000、座位 400、保留 100 的池子。"""
    return _pool(memory_bytes=1000, memory_seat_bytes=400, memory_reserve_bytes=100,
                 available_memory=probe, **overrides)


def test_requests_within_the_seat_never_ask_for_available_memory():
    probe = _Probe(0)
    pool = _guarded_pool(probe)
    with pool.working_memory(400) as reserved:
        assert reserved == 400
    _acquire(pool, "a", 300).close()
    assert probe.calls == 0


def test_requests_beyond_the_seat_must_leave_the_reserve_free():
    probe = _Probe(700)
    pool = _guarded_pool(probe)
    # 可用 700，要 600，剩 100，正好留够
    with pool.working_memory(600) as reserved:
        assert reserved == 600
    # 可用 700，要 601，剩不够 100
    with pytest.raises(MemoryError, match=AVAILABLE_KEY):
        with pool.working_memory(601):
            pass
    with pytest.raises(MemoryError, match=AVAILABLE_KEY):
        _acquire(pool, "big", 800)
    # 已登记的用量把合计推过座位时同样核对，哪怕这次请求本身不大
    probe.available = 150
    with pool.working_memory(350):
        with pytest.raises(MemoryError, match=AVAILABLE_KEY):
            with pool.working_memory(100):
                pass
    # 超过上限的仍然报超出预算，与可用内存无关
    probe.available = 10 ** 9
    with pytest.raises(MemoryError, match=BUDGET_KEY):
        with pool.working_memory(1001):
            pass


def test_idle_caches_are_given_up_before_refusing_for_available_memory():
    answers = [0, 700]
    pool = _guarded_pool(lambda: answers.pop(0))
    _acquire(pool, "idle", 300).close()
    held = _acquire(pool, "held", 50)
    assert pool.usage()["entries"] == 2 and answers == [0, 700]
    # 第一次问可用内存不够，让出空闲的缓存后再问一次才够；借用中的不动
    with pool.working_memory(500) as reserved:
        assert reserved == 500 and pool.usage()["entries"] == 1 and answers == []
    held.close()


def test_working_memory_falls_back_when_available_memory_is_short():
    probe = _Probe(650)
    pool = _guarded_pool(probe)
    with pool.working_memory(800, fallback_sizes=(500,)) as reserved:
        assert reserved == 500
    with pytest.raises(MemoryError, match=AVAILABLE_KEY):
        with pool.working_memory(800, fallback_sizes=(700,)):
            pass


def test_releasing_never_fails_for_available_memory():
    probe = _Probe(10 ** 6)
    pool = _guarded_pool(probe)
    lease = _acquire(pool, "a", 900)
    probe.available = 0
    lease.close()
    with pool.working_memory(300):
        pass
    assert pool.usage()["reserved_bytes"] == 0


def test_unreadable_available_memory_does_not_block():
    pool = _guarded_pool(_Probe(None))
    with pool.working_memory(900) as reserved:
        assert reserved == 900


def test_seat_is_rechecked_after_giving_up_idle_caches():
    """让出空闲缓存之后合计回到座位以内的，不再问可用内存，直接放行。"""
    probe = _Probe(0)
    pool = _guarded_pool(probe)
    _acquire(pool, "idle", 300).close()
    with pool.working_memory(350) as reserved:
        assert reserved == 350 and pool.usage()["entries"] == 0
    assert probe.calls == 1


def test_policy_seat_defaults_to_the_limit_and_cannot_exceed_it():
    assert ImageResourcePolicy(memory_bytes=500).memory_seat_bytes == 500
    with pytest.raises(ValueError):
        ImageResourcePolicy(memory_bytes=500, memory_seat_bytes=600)


def test_system_policy_follows_physical_memory_and_has_no_seat_until_registered():
    from src.application.image_resources import system_resource_policy
    from src.application.memory_settings import FALLBACK_LIMIT_BYTES, MemorySettings, resolve
    from src.application.system_profile import available_memory, memory_status
    status = memory_status()
    policy = system_resource_policy()
    assert policy.memory_seat_bytes == 0 and policy.available_memory is available_memory
    if status is None:
        assert policy.memory_bytes == FALLBACK_LIMIT_BYTES
    else:
        total, available = status.total_bytes, status.available_bytes
        budget = resolve(MemorySettings(), total)
        assert status.used_bytes == total - available >= 0 and 0 < available <= total
        assert (policy.memory_bytes, policy.memory_reserve_bytes) == (budget.limit_bytes, budget.reserve_bytes)
        assert available_memory() > 0


def test_lowering_the_limit_never_breaks_release_and_applies_to_new_requests():
    """运行中调低上限：已持有的照常释放，收尾不报预算错误；新增申请按新上限判断。"""
    pool = _pool()
    held = _acquire(pool, "held", 600)
    _acquire(pool, "idle", 200).close()
    with pool.working_memory(100):
        pool.update_memory(memory_bytes=300, memory_seat_bytes=300)
        # 空闲的缓存立即让出，借用中的保留
        assert pool.usage() == {"bytes": 600, "reserved_bytes": 100, "borrowers": 1, "entries": 1}
        with pytest.raises(MemoryError, match=BUDGET_KEY):
            with pool.working_memory(50):
                pass
        # 估算为零的新申请同样要过准入
        with pytest.raises(MemoryError, match=BUDGET_KEY):
            pool.hold_memory(0)
    held.close()
    assert pool.usage()["bytes"] == 0
    with pool.working_memory(300) as reserved:
        assert reserved == 300
    pool.update_memory(memory_bytes=1000, memory_seat_bytes=1000)
    _acquire(pool, "again", 900).close()


def test_changing_the_seat_takes_effect_for_the_next_request():
    probe = _Probe(0)
    pool = _guarded_pool(probe)
    with pytest.raises(MemoryError, match=AVAILABLE_KEY):
        with pool.working_memory(500):
            pass
    pool.update_memory(memory_seat_bytes=500)
    with pool.working_memory(500) as reserved:
        assert reserved == 500
    pool.update_memory(memory_seat_bytes=0)
    with pytest.raises(MemoryError, match=AVAILABLE_KEY):
        with pool.working_memory(1):
            pass


def test_only_memory_fields_can_be_replaced_at_runtime():
    pool = _pool()
    with pytest.raises(ValueError):
        pool.update_memory(compute_slots=4)
    with pytest.raises(ValueError):
        pool.update_memory(memory_bytes=100, memory_seat_bytes=200)
    assert pool.policy.memory_bytes == 1000


def test_stored_cache_items_count_against_the_budget_and_their_category():
    """已经算好的缓存项登记进池：计入总用量，受分类的字节数与条数约束，放不下时不登记也不等待。"""
    pool = _pool(preview_cache_bytes=250, preview_cache_items=2)
    assert pool.store(("p", 1), "preview", "one", 100) and pool.store(("p", 2), "preview", "two", 100)
    assert pool.usage()["bytes"] == 200 and pool.contains(("p", 1))
    # 字节数超出留存额度：最久没用的让出
    assert pool.store(("p", 3), "preview", "three", 100)
    assert not pool.contains(("p", 1)) and pool.contains(("p", 2)) and pool.contains(("p", 3))
    # 条数超出：同样让出最久没用的
    assert pool.store(("p", 4), "preview", "four", 10)
    assert not pool.contains(("p", 2)) and pool.usage()["bytes"] == 110
    with pool.borrow_cached(("p", 3)) as lease:
        assert lease.value == "three"
        pool.discard(("p", 3))
        assert pool.contains(("p", 3))
    pool.discard(("p", 3))
    assert not pool.contains(("p", 3))
    # 别的申请需要空间时，空闲的缓存项让路
    with pool.working_memory(995):
        assert pool.usage()["entries"] == 0
        assert not pool.store(("p", 5), "preview", "five", 10)
    pool.update_memory(preview_cache_bytes=5)
    assert pool.store(("p", 6), "preview", "six", 10) is False and pool.usage()["bytes"] == 0


def test_memory_errors_keep_the_reason_for_the_user():
    from src.core.contracts.result import memory_error_key
    assert memory_error_key(MemoryError(AVAILABLE_KEY)) == AVAILABLE_KEY
    assert memory_error_key(MemoryError(BUDGET_KEY)) == BUDGET_KEY
    assert memory_error_key(MemoryError("Unable to allocate 4.00 GiB")) == BUDGET_KEY
