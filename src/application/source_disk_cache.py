from dataclasses import asdict, dataclass, fields
from hashlib import sha256
import json
import logging
import math
import os
import re
import struct
import zlib
from enum import Enum
from concurrent.futures import CancelledError

import numpy as np

from ..core.source_input import IntegerSourceInput, SourceInputSpec

logger = logging.getLogger(__name__)
_MAGIC = b"OPSSRC\x00\x01"
_HEADER = struct.Struct("<8sIIQII")
_CHUNK = 1024 * 1024
_CODEC = "lz4-flex-0.14-block-1"


def canonical(value):
    def encode(item):
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, np.generic):
            return item.item()
        raise TypeError("Invalid source cache metadata value")
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, default=encode,
        separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class SourceDiskKey:
    file_hash: str
    spec: SourceInputSpec
    decode_identity: tuple
    half_size: bool = False

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.file_hash) or not self.decode_identity:
            raise ValueError("Invalid source cache identity")

    @property
    def value(self):
        return ["source-input-v1", self.file_hash, asdict(self.spec), list(self.decode_identity), self.half_size, _CODEC]

    @property
    def digest(self):
        return sha256(canonical(self.value)).hexdigest()


class SourceCacheLease:
    def __init__(self, factory):
        self.factory, self.lease = factory, None

    @property
    def value(self):
        if self.lease is None:
            self.lease = self.factory()
        return self.lease.value if self.lease is not None else None

    def close(self):
        if self.lease is not None:
            self.lease.close()
            self.lease = None


class SourceDiskCodec:
    def __init__(self, cache):
        self.cache = cache
        self.compute = self.resources = None
        self.enabled = True

    def bind(self, compute, resources):
        self.compute, self.resources = compute, resources

    def _header(self, stream, key):
        header = stream.read(_HEADER.size)
        if len(header) != _HEADER.size:
            raise ValueError("Truncated source cache header")
        magic, meta_size, index_size, data_size, metadata_crc, data_crc = _HEADER.unpack(header)
        if (magic != _MAGIC or not 0 < meta_size <= 65536 or index_size > 16384 * 16
                or index_size % 16 or not 0 < data_size <= key.spec.nbytes
                or os.fstat(stream.fileno()).st_size != _HEADER.size + meta_size + index_size + data_size):
            raise ValueError("Invalid source cache size")
        encoded = stream.read(meta_size)
        if zlib.crc32(encoded) != metadata_crc:
            raise ValueError("Source cache metadata CRC mismatch")
        meta = json.loads(encoded)
        if (type(meta) is not dict or set(meta) != {"key", "codec", "matrix", "raw_meta"}
                or canonical(meta) != encoded or canonical(meta["key"]) != canonical(key.value)
                or meta["codec"] not in ("raw", _CODEC)
                or (meta["codec"] == "raw") != (index_size == 0)
                or index_size == 0 and data_size != key.spec.nbytes):
            raise ValueError("Source cache identity mismatch")
        matrix = meta["matrix"]
        if (type(matrix) is not list or len(matrix) != (9 if key.spec.transfer == "xyz-d65" else 0)
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in matrix)):
            raise ValueError("Invalid source cache matrix")
        if matrix:
            from ..core.dcp import xyz_d65_to_srgb_matrix
            if matrix != [float(v) for row in xyz_d65_to_srgb_matrix() for v in row]:
                raise ValueError("Source cache conversion changed")
        if key.spec.transfer == "xyz-d65":
            from ..core.photo_engine import RawDecodeMeta, _raw_postprocess_kwargs
            if type(meta["raw_meta"]) is not dict or set(meta["raw_meta"]) != {item.name for item in fields(RawDecodeMeta)}:
                raise ValueError("Source cache RAW metadata mismatch")
            reference = meta["raw_meta"]["reference_size"]
            if type(reference) is not list or len(reference) != 2 or any(type(v) is not int or v < 1 for v in reference):
                raise ValueError("Source cache RAW reference mismatch")
            size = tuple((v + 1) // 2 for v in reference) if key.half_size else tuple(reference)
            if size != key.spec.size:
                raise ValueError("Source cache RAW reference mismatch")
            if canonical(meta["raw_meta"]["postprocess_kwargs"]) != canonical(_raw_postprocess_kwargs(key.half_size)):
                raise ValueError("Source cache RAW parameters changed")
        elif meta["raw_meta"] is not None:
            raise ValueError("Unexpected RAW metadata")
        return meta, index_size, data_size, data_crc

    def peek(self, key, *, is_current, is_cancelled):
        directory = self.cache._directory
        generation = directory.generation
        if not self.enabled or not directory.current(generation, is_cancelled, is_current):
            return None
        import ops_accel
        if not hasattr(ops_accel, "source_decompress_blocks"):
            return None
        try:
            with directory.open_entry(key.digest, self.cache.VERSION) as stream:
                _, indices, size, _ = self._header(stream, key)
            return generation, indices + size + 65536
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            directory.discard(key.digest, self.cache.VERSION, generation, is_current=is_current, is_cancelled=is_cancelled)
            return None

    def read(self, key, generation, *, is_current, is_cancelled):
        directory = self.cache._directory
        valid = lambda: self.enabled and directory.current(generation, is_cancelled, is_current)
        if not valid():
            return None
        try:
            with directory.open_entry(key.digest, self.cache.VERSION) as stream:
                meta, index_size, size, expected_crc = self._header(stream, key)
                blocks = np.empty((index_size // 16, 4), "<u4") if index_size else None
                data = np.empty(size, np.uint8)
                crc = 0
                for array in (blocks, data):
                    if array is None:
                        continue
                    view = memoryview(array).cast("B")
                    for start in range(0, len(view), _CHUNK):
                        if not valid():
                            return None
                        part = view[start:start + _CHUNK]
                        if stream.readinto(part) != len(part):
                            raise ValueError("Truncated source cache payload")
                        crc = zlib.crc32(part, crc)
                    array.setflags(write=False)
                if crc != expected_crc:
                    raise ValueError("Source cache payload CRC mismatch")
            if blocks is not None:
                validate_index(data, blocks, key.spec.nbytes)
            raw_meta = meta["raw_meta"]
            if raw_meta is not None:
                from ..core.photo_engine import RawDecodeMeta, rawpy
                for name in ("raw_pattern_shape", "reference_size", "effective_whitebalance"):
                    raw_meta[name] = tuple(raw_meta[name])
                raw_meta["postprocess_kwargs"]["gamma"] = tuple(raw_meta["postprocess_kwargs"]["gamma"])
                raw_meta["postprocess_kwargs"]["output_color"] = rawpy.ColorSpace(raw_meta["postprocess_kwargs"]["output_color"])
                raw_meta = RawDecodeMeta(**raw_meta)
            if not valid():
                return None
            directory.touch(key.digest, self.cache.VERSION, generation, is_current=is_current, is_cancelled=is_cancelled, blocking=False)
            logger.debug("磁盘源缓存命中 source=%s codec=%s stored_bytes=%s raw_bytes=%s", key.file_hash[:12], meta["codec"],
                index_size + size, key.spec.nbytes)
            return IntegerSourceInput(key.spec, data, raw_meta, tuple(meta["matrix"]), blocks)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            logger.warning("源缓存读取失败：%s", key.digest, exc_info=True)
            directory.discard(key.digest, self.cache.VERSION, generation, is_current=is_current, is_cancelled=is_cancelled)
            return None

    def write(self, request):
        directory = self.cache._directory
        cancelled = lambda: self.cache._closed.is_set() or not self.enabled or not self.cache._request_current(request)
        current = lambda: directory.current(request.generation, cancelled)
        if not current() or self.resources is None or not self.cache.capacity_ready or not self.cache.is_writer:
            return
        if directory.exists(request.key.digest, self.cache.VERSION):
            directory.touch(request.key.digest, self.cache.VERSION, request.generation, is_cancelled=cancelled)
            return
        source = request.lease.value
        if source is None or source.spec != request.key.spec or source.blocks is not None:
            return
        raw = source.data
        if raw.nbytes != source.spec.nbytes or raw.flags.writeable or not raw.flags.c_contiguous:
            return
        reservation = None
        def admit():
            nonlocal reservation
            reservation = self.resources.try_working_memory((raw.nbytes * 2 if source.spec.transfer != "xyz-d65" else 0) + _CHUNK)
            if reservation is None:
                raise MemoryError("Source cache build budget is unavailable")
            return True
        try:
            with self.compute.borrow(cancelled, priority=2, admit=admit) as allocation:
                data, blocks, codec = raw, None, "raw"
                if source.spec.transfer != "xyz-d65":
                    import ops_accel
                    block_size = max(65536, min(262144, ((raw.nbytes + 1023) // 1024 + 4095) // 4096 * 4096))
                    packed, index = ops_accel.source_compress_blocks(raw, block_size,
                        threads=allocation.threads, cancelled=cancelled)
                    if packed.nbytes + index.nbytes < raw.nbytes * .9:
                        data, blocks, codec = packed, index, _CODEC
                    else:
                        packed = index = None
                retained = reservation.split((data.nbytes + blocks.nbytes if blocks is not None else 0) + _CHUNK)
                reservation.close()
                reservation = retained
                meta = canonical({"key": request.key.value, "codec": codec, "matrix": list(source.matrix),
                    "raw_meta": asdict(source.raw_meta) if source.raw_meta is not None else None})
                if len(meta) > 65536:
                    return
                views = ([memoryview(blocks).cast("B")] if blocks is not None else []) + [memoryview(data).cast("B")]
                crc = 0
                for view in views:
                    for start in range(0, len(view), _CHUNK):
                        if cancelled():
                            raise CancelledError()
                        crc = zlib.crc32(view[start:start + _CHUNK], crc)
            index_size = blocks.nbytes if blocks is not None else 0
            header = _HEADER.pack(_MAGIC, len(meta), index_size, data.nbytes, zlib.crc32(meta), crc)
            def write(stream):
                stream.write(header)
                stream.write(meta)
                for view in views:
                    for start in range(0, len(view), _CHUNK):
                        if not current():
                            raise OSError("Source cache build cancelled")
                        stream.write(view[start:start + _CHUNK])
                stream.flush()
                os.fsync(stream.fileno())
            def validate(stream):
                _, index_count, size, expected = self._header(stream, request.key)
                actual, remaining = 0, index_count + size
                while remaining:
                    if not current():
                        raise OSError("Source cache validation cancelled")
                    part = stream.read(min(_CHUNK, remaining))
                    if not part:
                        raise ValueError("Source cache payload is incomplete")
                    actual = zlib.crc32(part, actual)
                    remaining -= len(part)
                if actual != expected:
                    raise ValueError("Source cache CRC mismatch")
            if directory.write(request.key.digest, self.cache.VERSION, len(header) + len(meta) + index_size + data.nbytes,
                    write, validate, generation=request.generation, is_current=lambda: True,
                    is_cancelled=cancelled, preloading=request.preloading):
                logger.debug("磁盘源缓存发布 source=%s codec=%s stored_bytes=%s raw_bytes=%s", request.key.file_hash[:12],
                    codec, index_size + data.nbytes, raw.nbytes)
        finally:
            if reservation is not None:
                reservation.close()


def validate_index(data, blocks, expected):
    if not 0 < len(blocks) <= 16384:
        raise ValueError("Source block count is invalid")
    incoming = outgoing = 0
    block_size = int(blocks[0, 3])
    if not 1 <= block_size <= 1048576:
        raise ValueError("Source block size is invalid")
    for number, values in enumerate(blocks):
        offset, size, start, count = map(int, values)
        if (offset != incoming or start != outgoing or not 0 < size <= data.nbytes - offset
                or not 0 < count <= block_size or number + 1 < len(blocks) and count != block_size):
            raise ValueError("Source block index is invalid")
        incoming += (size + 3) // 4 * 4
        outgoing += count
    if incoming != data.nbytes or outgoing != expected:
        raise ValueError("Source block extent mismatch")
