from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
import os
import re
import struct
from threading import Condition, Event, Thread, current_thread
import zlib

import numpy as np

from .managed_cache import ManagedCache

logger = logging.getLogger(__name__)

_MAGIC = b"OPSLIN\x00\x01"
_HEADER = struct.Struct("<8sIIQI")
_MAX_METADATA = 32768
_BLOCK = 1024 * 1024


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class LinearProxyKey:
    file_hash: str
    max_side: int
    decode_identity: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.file_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", self.file_hash):
            raise ValueError("源内容身份无效")
        if type(self.max_side) is not int or not 1 <= self.max_side <= 1600:
            raise ValueError("代理尺寸无效")
        if (type(self.decode_identity) is not tuple or not self.decode_identity
                or any(type(value) is not str or not value or len(value) > 256
                       for value in self.decode_identity)):
            raise ValueError("解码身份无效")

    @property
    def value(self):
        return [self.file_hash, "linear-f32-straight-alpha-v1", self.max_side,
                "lanczos-premultiplied-v1", list(self.decode_identity)]

    @property
    def digest(self):
        return sha256(_canonical(self.value)).hexdigest()


@dataclass
class _WriteRequest:
    key: LinearProxyKey
    lease: object
    subscribers: dict
    preloading: bool
    generation: int
    size: int


class LinearProxyCache:
    VERSION = "v1"
    PENDING_WRITES = 8
    PENDING_BYTES = 64 * 1024 ** 2

    def __init__(self, directory, *, max_bytes=2048 * 1024 ** 2):
        self._directory = ManagedCache(directory, max_bytes=max_bytes, suffix=".opslin")
        self._condition = Condition()
        self._closed = Event()
        self._pending = OrderedDict()
        self._pending_bytes = 0
        self._active = None
        self._version = self.VERSION
        self._writer = Thread(target=self._write_loop, name="ops-linear-cache")
        self._writer.start()

    @property
    def directory(self):
        return self._directory.directory

    @property
    def max_bytes(self):
        return self._directory.max_bytes

    @property
    def capacity_ready(self):
        return not self._closed.is_set() and self._directory.capacity_ready

    @property
    def is_writer(self):
        return not self._closed.is_set() and self._directory.is_writer

    @property
    def generation(self):
        return self._directory.generation

    def prepare(self, version=VERSION):
        if version != self.VERSION:
            raise ValueError("线性缓存格式版本无效")
        self._directory.prepare(version)

    def status(self):
        return self._directory.status()

    def set_limit(self, max_bytes):
        self._directory.set_limit(max_bytes)

    def exists(self, key):
        return not self._closed.is_set() and self._directory.exists(key.digest, self._version)

    @staticmethod
    def _metadata(source, key):
        return {
            "key": key.value,
            "shape": list(source.pixels.shape),
            "has_alpha": source.alpha is not None,
            "orig_size": list(source.orig_size),
            "source_format": source.source_format,
            "color_pipeline": source.color_pipeline,
            "colorspace": source.colorspace,
            "white_point": source.white_point,
            "gamut": source.gamut,
            "note": source.note,
            "dcp_has_tone_curve": source.dcp_has_tone_curve,
            "is_proxy": source.is_proxy,
        }

    @staticmethod
    def _validate_metadata(metadata, key):
        fields = {"key", "shape", "has_alpha", "orig_size", "source_format", "color_pipeline",
                  "colorspace", "white_point", "gamut", "note", "dcp_has_tone_curve", "is_proxy"}
        if (type(metadata) is not dict or set(metadata) != fields
                or _canonical(metadata["key"]) != _canonical(key.value)):
            raise ValueError("线性缓存元数据或身份无效")
        shape, original = metadata["shape"], metadata["orig_size"]
        if (type(shape) is not list or len(shape) != 3 or shape[2] != 3
                or any(type(value) is not int or not 1 <= value <= key.max_side for value in shape[:2])
                or type(shape[2]) is not int):
            raise ValueError("线性缓存像素形状无效")
        if (type(original) is not list or len(original) != 2
                or any(type(value) is not int or not 1 <= value <= 2 ** 31 - 1 for value in original)
                or max(original) <= key.max_side):
            raise ValueError("线性缓存参考尺寸无效")
        ratio = key.max_side / max(original)
        expected = [max(1, round(original[1] * ratio)), max(1, round(original[0] * ratio)), 3]
        if shape != expected:
            raise ValueError("线性缓存尺寸合同无效")
        if (type(metadata["has_alpha"]) is not bool or type(metadata["is_proxy"]) is not bool
                or not metadata["is_proxy"] or type(metadata["dcp_has_tone_curve"]) is not bool):
            raise ValueError("线性缓存状态无效")
        if metadata["source_format"] not in ("raw", "srgb8", "srgb16"):
            raise ValueError("线性缓存源格式无效")
        if metadata["color_pipeline"] not in ("libraw", "srgb-direct"):
            raise ValueError("线性缓存色彩管线无效")
        if (metadata["colorspace"] != "linear_srgb" or metadata["white_point"] != "D65"
                or metadata["gamut"] != "srgb" or type(metadata["note"]) is not str
                or len(metadata["note"]) > 4096):
            raise ValueError("线性缓存色彩合同无效")
        return shape[0] * shape[1] * (16 if metadata["has_alpha"] else 12)

    @classmethod
    def _read_header(cls, stream, key):
        header = stream.read(_HEADER.size)
        if len(header) != _HEADER.size:
            raise ValueError("线性缓存文件头不完整")
        magic, version, metadata_size, payload_size, crc = _HEADER.unpack(header)
        if magic != _MAGIC or version != 1 or not 0 < metadata_size <= _MAX_METADATA:
            raise ValueError("线性缓存文件头无效")
        if os.fstat(stream.fileno()).st_size != _HEADER.size + metadata_size + payload_size:
            raise ValueError("线性缓存文件长度无效")
        encoded = stream.read(metadata_size)
        if len(encoded) != metadata_size:
            raise ValueError("线性缓存元数据不完整")
        metadata = json.loads(encoded)
        if _canonical(metadata) != encoded or cls._validate_metadata(metadata, key) != payload_size:
            raise ValueError("线性缓存元数据合同无效")
        return metadata, zlib.crc32(encoded), crc, payload_size

    def read(self, key, *, source_path, is_current, is_cancelled=lambda: False):
        generation = self.generation
        if self._closed.is_set() or not self._directory.current(generation, is_cancelled, is_current):
            return None
        pixels = alpha = array = view = block = source = None
        try:
            with self._directory.open_entry(key.digest, self._version) as stream:
                metadata, crc, expected_crc, _ = self._read_header(stream, key)
                shape = tuple(metadata["shape"])
                pixels = np.empty(shape, dtype="<f4")
                alpha = np.empty(shape[:2], dtype="<f4") if metadata["has_alpha"] else None
                for array in (pixels, alpha):
                    if array is None:
                        continue
                    view = memoryview(array).cast("B")
                    for start in range(0, len(view), _BLOCK):
                        if self._closed.is_set() or not self._directory.current(generation, is_cancelled, is_current):
                            return None
                        block = view[start:start + _BLOCK]
                        if stream.readinto(block) != len(block):
                            raise ValueError("线性缓存载荷不完整")
                        crc = zlib.crc32(block, crc)
                if crc != expected_crc:
                    raise ValueError("线性缓存校验失败")
            if self._closed.is_set() or not self._directory.current(generation, is_cancelled, is_current):
                return None
            pixels.setflags(write=False)
            if alpha is not None:
                alpha.setflags(write=False)
            from ..core.photo_engine import RenderSource
            source = RenderSource(pixels=pixels, alpha=alpha, source_path=os.path.normpath(source_path),
                source_format=metadata["source_format"], is_proxy=metadata["is_proxy"],
                orig_size=tuple(metadata["orig_size"]), color_pipeline=metadata["color_pipeline"],
                colorspace=metadata["colorspace"], white_point=metadata["white_point"],
                gamut=metadata["gamut"], note=metadata["note"],
                dcp_has_tone_curve=metadata["dcp_has_tone_curve"])
            self._directory.touch(key.digest, self._version, generation,
                                  is_current=is_current, is_cancelled=is_cancelled)
            if self._closed.is_set() or not self._directory.current(generation, is_cancelled, is_current):
                return None
            return source
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            logger.warning("编辑缓存读取失败：%s", key.digest, exc_info=True)
            self._directory.discard(key.digest, self._version, generation,
                                    is_current=is_current, is_cancelled=is_cancelled)
            return None
        finally:
            pixels = alpha = array = view = block = source = None

    @classmethod
    def _source_size(cls, source, key):
        metadata = cls._metadata(source, key)
        size = cls._validate_metadata(metadata, key)
        for array in (source.pixels, source.alpha):
            if array is None:
                continue
            if (array.dtype != np.dtype("<f4") or not array.flags.c_contiguous
                    or array.flags.writeable):
                raise ValueError("缓存写入要求只读连续 float32 缓冲")
        if source.alpha is not None and source.alpha.shape != source.pixels.shape[:2]:
            raise ValueError("缓存 Alpha 形状无效")
        if len(_canonical(metadata)) > _MAX_METADATA:
            raise ValueError("缓存元数据超过上限")
        return size

    def submit(self, key, lease, *, is_current, is_cancelled=lambda: False,
               preloading=False, generation=None):
        discarded = []
        accepted = False
        try:
            generation = self.generation if generation is None else generation
            if (not self.capacity_ready or not self.is_writer
                    or not self._directory.current(generation, is_cancelled, is_current)):
                return False
            size = self._source_size(lease.value, key)
            if size > self.PENDING_BYTES:
                return False
            request = _WriteRequest(key, lease, {bool(preloading): (is_current, is_cancelled)},
                                    bool(preloading), generation, size)
            with self._condition:
                if (self._closed.is_set()
                        or not self._directory.current(generation, is_cancelled, is_current)):
                    return False
                previous = self._pending.pop(key, None)
                if previous is not None:
                    self._pending_bytes -= previous.size
                    if (preloading and not previous.preloading
                            and self._request_current(previous, preloading=False)):
                        self._pending[key] = previous
                        self._pending_bytes += previous.size
                        return True
                    if self._request_current(previous):
                        request.preloading = request.preloading and previous.preloading
                        request.subscribers = {**previous.subscribers, **request.subscribers}
                    discarded.append(previous.lease)
                if (self._active is not None and self._active.key == key
                        and self._active.generation == generation):
                    if self._request_current(self._active):
                        request.preloading = request.preloading and self._active.preloading
                        request.subscribers = {**self._active.subscribers, **request.subscribers}
                        self._active.subscribers = request.subscribers.copy()
                        if not request.preloading:
                            self._active.preloading = False
                self._pending[key] = request
                self._pending_bytes += size
                accepted = True
                while len(self._pending) > self.PENDING_WRITES or self._pending_bytes > self.PENDING_BYTES:
                    victim = next((item for item, value in self._pending.items() if value.preloading),
                                  next(iter(self._pending)))
                    removed = self._pending.pop(victim)
                    self._pending_bytes -= removed.size
                    discarded.append(removed.lease)
                self._condition.notify()
            return True
        except (OSError, ValueError, TypeError, AttributeError) as error:
            logger.warning("编辑缓存跳过写入：%s (%s)", key.digest, str(error))
            return False
        finally:
            if not accepted:
                discarded.append(lease)
            for item in discarded:
                item.close()

    def _write_loop(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closed.is_set())
                if self._closed.is_set():
                    return
                _, request = self._pending.popitem(last=False)
                self._pending_bytes -= request.size
                self._active = request
            try:
                self._write(request)
            except Exception as error:
                logger.warning("编辑缓存写入失败：%s (%s)", request.key.digest, str(error))
            finally:
                with self._condition:
                    self._active = None
                request.lease.close()

    def _write(self, request):
        cancelled = self._closed.is_set
        current = lambda: self._request_current(request)
        if not self._directory.current(request.generation, cancelled, current):
            return
        if self._directory.exists(request.key.digest, self._version):
            self._directory.touch(request.key.digest, self._version, request.generation,
                                  is_current=current, is_cancelled=cancelled)
            return
        source = request.lease.value
        metadata = _canonical(self._metadata(source, request.key))
        views = [memoryview(source.pixels).cast("B")]
        if source.alpha is not None:
            views.append(memoryview(source.alpha).cast("B"))
        crc = zlib.crc32(metadata)
        for view in views:
            for start in range(0, len(view), _BLOCK):
                if not self._directory.current(request.generation, cancelled, current):
                    return
                crc = zlib.crc32(view[start:start + _BLOCK], crc)
        header = _HEADER.pack(_MAGIC, 1, len(metadata), request.size, crc)

        def write(stream):
            stream.write(header)
            stream.write(metadata)
            for view in views:
                for start in range(0, len(view), _BLOCK):
                    if not self._directory.current(request.generation, cancelled, current):
                        raise OSError("编辑缓存写入已取消")
                    stream.write(view[start:start + _BLOCK])
            stream.flush()
            os.fsync(stream.fileno())

        def validate(stream):
            _, stored_crc, expected_crc, remaining = self._read_header(stream, request.key)
            buffer = bytearray(4096)
            while remaining:
                if not self._directory.current(request.generation, cancelled, current):
                    raise OSError("编辑缓存校验已取消")
                count = min(len(buffer), remaining)
                view = memoryview(buffer)[:count]
                if stream.readinto(view) != count:
                    raise ValueError("编辑缓存写入载荷不完整")
                stored_crc = zlib.crc32(view, stored_crc)
                remaining -= count
            if stored_crc != expected_crc:
                raise ValueError("编辑缓存写入校验失败")

        self._directory.write(request.key.digest, self._version, len(header) + len(metadata) + request.size,
            write, validate, generation=request.generation, is_current=current,
            is_cancelled=cancelled, preloading=request.preloading)

    def _request_current(self, request, *, preloading=None):
        with self._condition:
            subscribers = tuple(value for kind, value in request.subscribers.items()
                                if preloading is None or kind == preloading)
        for current, cancelled in subscribers:
            try:
                if not cancelled() and current():
                    return True
            except OSError:
                continue
        return False

    def clear(self):
        if not self.is_writer:
            raise PermissionError("编辑缓存写入所有权不可用")
        with self._condition:
            self._directory.invalidate()
            discarded = tuple(self._pending.values())
            self._pending.clear()
            self._pending_bytes = 0
        for request in discarded:
            request.lease.close()
        return self._directory.clear(invalidate=False)

    def close(self):
        with self._condition:
            self._closed.set()
            self._directory.invalidate()
            discarded = tuple(self._pending.values())
            self._pending.clear()
            self._pending_bytes = 0
            self._condition.notify_all()
        for request in discarded:
            request.lease.close()
        if current_thread() is not self._writer:
            self._writer.join()
        self._directory.close()
