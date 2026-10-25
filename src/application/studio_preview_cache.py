from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from threading import Event, Lock


def preview_variant(workflow, tool_generation):
    return f"{workflow.workflow_hash()}:{tool_generation}"


@dataclass(frozen=True)
class PreviewKey:
    path: str
    mtime_ns: int
    file_size: int
    variant: str

    @classmethod
    def capture(cls, path, variant):
        path = os.path.normcase(os.path.abspath(path))
        stat = os.stat(path)
        return cls(path, stat.st_mtime_ns, stat.st_size, variant)

    def matches_file(self):
        try:
            return self == self.capture(self.path, self.variant)
        except OSError:
            return False


@dataclass(frozen=True)
class PreviewFrame:
    rgba: bytes | memoryview
    size: tuple[int, int]
    reference_size: tuple[int, int]
    full_size: tuple[int, int] | None = None
    effect_status: tuple = ()
    input_size: tuple[int, int] | None = None
    input_is_proxy: bool | None = None


@dataclass(frozen=True)
class CanvasPreviewState:
    mode: str = "auto"
    kind: str = "loading"
    size: tuple[int, int] | None = None
    input_size: tuple[int, int] | None = None
    input_is_proxy: bool | None = None


class StudioPreviewCache:
    """编辑预览帧的缓存。帧登记在共享资源池的 preview 分类里：占用计入统一的内存上限，存放、计数与淘汰由资源池执行。

    这里保留键、“文件变了就失效”的判断，以及同一张图片最多留几种效果的规则。
    """

    SOURCE_VARIANTS = 4
    _NAMESPACE = "studio-preview"

    def __init__(self, pool):
        self._pool = pool
        # 登记过的键，按最近使用排列；只用来执行每张图片的效果数上限，资源池淘汰掉的在下次写入时清出
        self._keys = OrderedDict()
        self._lock = Lock()
        self._closed = Event()

    def _name(self, key):
        return self._NAMESPACE, key

    def peek(self, key):
        if self._closed.is_set():
            return None
        lease = self._pool.borrow_cached(self._name(key))
        if lease is None:
            return None
        with lease:
            frame = lease.value
        with self._lock:
            if key in self._keys:
                self._keys.move_to_end(key)
        return frame

    def get(self, key):
        return self.peek(key)

    def contains(self, key):
        return not self._closed.is_set() and self._pool.contains(self._name(key))

    def put(self, key, frame, *, is_cancelled=lambda: False):
        """写入一帧。放不进留存额度或内存准入不过时不进缓存，预览照常显示，下次回到这个状态重新渲染。"""
        if self._closed.is_set() or is_cancelled() or not key.matches_file():
            return
        size = len(frame.rgba)
        if size > self._pool.policy.preview_cache_bytes or not self._pool.store(self._name(key), "preview", frame, size):
            return
        with self._lock:
            if self._closed.is_set():
                self._pool.discard(self._name(key))
                return
            self._keys[key] = None
            self._keys.move_to_end(key)
            variants = 0
            for known in reversed(tuple(self._keys)):
                if not self._pool.contains(self._name(known)):
                    del self._keys[known]
                elif known.path == key.path:
                    variants += 1
                    if variants > self.SOURCE_VARIANTS:
                        self._pool.discard(self._name(known))
                        del self._keys[known]

    def close(self):
        with self._lock:
            self._closed.set()
            for key in self._keys:
                self._pool.discard(self._name(key))
            self._keys.clear()
