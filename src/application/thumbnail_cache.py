from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import logging
import os

from PIL import Image

from .managed_cache import ManagedCache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThumbnailKey:
    path: str
    stamp: tuple[int, int, int]
    size: tuple[int, int]
    version: str

    @property
    def digest(self):
        value = (os.path.normcase(os.path.abspath(self.path)), self.stamp, self.size, self.version)
        return sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()


class ThumbnailCache:
    def __init__(self, directory, *, max_bytes=512 * 1024 ** 2):
        self._directory = ManagedCache(directory, max_bytes=max_bytes, suffix=".webp")

    @property
    def directory(self):
        return self._directory.directory

    @property
    def max_bytes(self):
        return self._directory.max_bytes

    @property
    def capacity_ready(self):
        return self._directory.capacity_ready

    @property
    def is_writer(self):
        return self._directory.is_writer

    def prepare(self, version):
        self._directory.prepare(version)

    def status(self):
        return self._directory.status()

    def set_limit(self, max_bytes):
        self._directory.set_limit(max_bytes)

    def get(self, key, *, is_current, is_cancelled=lambda: False):
        generation = self._directory.generation
        if not self._directory.current(generation, is_cancelled, is_current):
            return None
        image = None
        try:
            with self._directory.open_entry(key.digest, key.version) as stream, Image.open(stream) as stored:
                if stored.format != "WEBP" or stored.size != key.size:
                    raise OSError("缩略图缓存格式或尺寸无效")
                image = stored.convert("RGBA")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, SyntaxError):
            logger.warning("缩略图缓存读取失败：%s", key.path, exc_info=True)
            self._directory.discard(key.digest, key.version, generation,
                                    is_current=is_current, is_cancelled=is_cancelled)
            return None
        if not self._directory.current(generation, is_cancelled, is_current):
            image.close()
            return None
        self._directory.touch(key.digest, key.version, generation,
                              is_current=is_current, is_cancelled=is_cancelled)
        if not self._directory.current(generation, is_cancelled, is_current):
            image.close()
            return None
        return image

    def put(self, key, image, *, is_current, is_cancelled=lambda: False, encoding=None):
        generation = self._directory.generation
        if not self.capacity_ready or not self.is_writer:
            return False
        if not self._directory.current(generation, is_cancelled, is_current):
            return False
        try:
            with encoding if encoding is not None else nullcontext(), BytesIO() as buffer:
                image.save(buffer, format="WEBP", quality=88, method=1, exact=True)
                encoded = buffer.getvalue()
        except (OSError, ValueError, KeyError):
            logger.warning("缩略图缓存编码失败：%s", key.path, exc_info=True)
            return False

        def validate(stream):
            with Image.open(stream) as stored:
                if stored.format != "WEBP" or stored.size != key.size:
                    raise OSError("缩略图缓存写入校验失败")
                stored.load()

        return self._directory.write(key.digest, key.version, len(encoded),
            lambda stream: stream.write(encoded), validate, generation=generation,
            is_current=is_current, is_cancelled=is_cancelled)

    def clear(self):
        return self._directory.clear()

    def close(self):
        self._directory.close()
