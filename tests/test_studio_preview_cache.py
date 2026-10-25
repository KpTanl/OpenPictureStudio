"""编辑预览缓存：帧登记在共享资源池里，占用计入统一上限，留存、让路与失效。"""

import os

from src.application.image_resources import ImageResourcePolicy, ImageResourcePool
from src.application.studio_preview_cache import PreviewFrame, PreviewKey, StudioPreviewCache


def _pool(**overrides):
    values = {"memory_bytes": 1000, "preview_cache_bytes": 400}
    values.update(overrides)
    return ImageResourcePool(ImageResourcePolicy(**values))


def _image(tmp_path, name="a.png"):
    path = tmp_path / name
    path.write_bytes(b"image")
    return str(path)


def _frame(size):
    return PreviewFrame(bytes(size), (size // 4, 1), (size, 4))


def test_frames_are_registered_in_the_shared_pool(tmp_path):
    pool = _pool()
    cache = StudioPreviewCache(pool)
    key = PreviewKey.capture(_image(tmp_path), "v1")
    frame = _frame(100)
    cache.put(key, frame)
    assert cache.contains(key) and cache.peek(key) is frame and cache.get(key) is frame
    assert pool.usage() == {"bytes": 100, "reserved_bytes": 0, "borrowers": 0, "entries": 1}
    assert cache.peek(PreviewKey.capture(key.path, "other")) is None


def test_retention_follows_the_capacity_and_frames_larger_than_it_are_not_kept(tmp_path):
    pool = _pool()
    cache = StudioPreviewCache(pool)
    keys = [PreviewKey.capture(_image(tmp_path, f"{index}.png"), "v") for index in range(4)]
    for key in keys[:3]:
        cache.put(key, _frame(150))
    assert not cache.contains(keys[0]) and cache.contains(keys[1]) and cache.contains(keys[2])
    # 放不进留存额度的帧不进缓存，已有的不动
    cache.put(keys[3], _frame(401))
    assert not cache.contains(keys[3]) and cache.contains(keys[2]) and pool.usage()["bytes"] == 300
    # 调小容量：多出的帧让出
    pool.update_memory(preview_cache_bytes=150)
    assert not cache.contains(keys[1]) and cache.contains(keys[2])


def test_each_image_keeps_only_its_most_recent_variants(tmp_path):
    pool = _pool(memory_bytes=10_000, preview_cache_bytes=10_000)
    cache = StudioPreviewCache(pool)
    path, other = _image(tmp_path), _image(tmp_path, "b.png")
    kept = PreviewKey.capture(other, "kept")
    cache.put(kept, _frame(8))
    variants = [PreviewKey.capture(path, f"v{index}") for index in range(6)]
    for key in variants:
        cache.put(key, _frame(8))
    assert [cache.contains(key) for key in variants] == [False, False, True, True, True, True]
    assert cache.contains(kept) and pool.usage()["entries"] == 5


def test_idle_frames_give_way_and_are_not_cached_when_memory_is_short(tmp_path):
    pool = _pool()
    cache = StudioPreviewCache(pool)
    first = PreviewKey.capture(_image(tmp_path), "v1")
    cache.put(first, _frame(300))
    with pool.working_memory(900):
        # 别的处理需要空间：空闲的预览帧让路
        assert not cache.contains(first)
        # 准入不过：这一帧不进缓存，也不报错
        cache.put(first, _frame(300))
        assert not cache.contains(first)
    cache.put(first, _frame(300))
    assert cache.contains(first)


def test_stale_cancelled_and_closed_writes_are_ignored(tmp_path):
    pool = _pool()
    cache = StudioPreviewCache(pool)
    path = _image(tmp_path)
    key = PreviewKey.capture(path, "v1")
    cache.put(key, _frame(10), is_cancelled=lambda: True)
    assert not cache.contains(key)
    cache.put(key, _frame(10))
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    stale = PreviewKey.capture(path, "v2")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 9_000_000_000))
    cache.put(stale, _frame(10))
    assert not cache.contains(stale) and not stale.matches_file()
    cache.close()
    assert pool.usage()["entries"] == 0 and cache.peek(key) is None and not cache.contains(key)
    cache.put(PreviewKey.capture(path, "v3"), _frame(10))
    assert pool.usage()["entries"] == 0
