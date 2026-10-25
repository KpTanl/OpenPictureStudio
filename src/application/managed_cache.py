from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import stat
import sys
from threading import Event, Lock
from time import time_ns
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagedCacheStatus:
    directory: str
    used_bytes: int
    max_bytes: int
    is_writer: bool
    capacity_ready: bool


class ManagedCache:
    def __init__(self, directory, *, max_bytes, suffix):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("缓存容量必须为正整数")
        if not re.fullmatch(r"\.[a-z0-9]+", suffix):
            raise ValueError("缓存扩展名无效")
        self.directory = Path(directory).absolute()
        self._max_bytes = max_bytes
        self._suffix = suffix
        self._lock = Lock()
        self._state_lock = Lock()
        self._closed = Event()
        self._ready = Event()
        self._owner = None
        self._prepared = False
        self._version = None
        self._files = {}
        self._bytes = 0
        self._reserved = 0
        self._writes = 0
        self._scanned_writes = 0
        self._generation = 0

    @property
    def max_bytes(self):
        return self._max_bytes

    @property
    def generation(self):
        with self._state_lock:
            return self._generation

    @property
    def capacity_ready(self):
        return self._ready.is_set() and not self._closed.is_set()

    @property
    def is_writer(self):
        return self._owner is not None and not self._closed.is_set()

    @staticmethod
    def _plain(path, *, directory=False):
        info = path.lstat()
        linked = stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return not linked and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))

    def _safe_parent(self, target, *, create=False):
        if not self._plain(self.directory, directory=True):
            raise OSError("缓存根目录必须为普通目录")
        for parent in (target.parent.parent, target.parent):
            if create:
                parent.mkdir(exist_ok=True)
            if not self._plain(parent, directory=True):
                raise OSError("缓存目录包含链接")
        if target.exists() or target.is_symlink():
            if not self._plain(target):
                raise OSError("缓存文件包含链接")

    def _target(self, digest, version):
        if not re.fullmatch(r"v\d+", version) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("缓存身份无效")
        return self.directory / version / digest[:2] / f"{digest}{self._suffix}"

    @contextmanager
    def _publication(self):
        with self._lock:
            try:
                yield
            finally:
                if self._closed.is_set():
                    self._release_owner()

    def _acquire_owner(self):
        lock_path = self.directory / "cache.lock"
        if (lock_path.exists() or lock_path.is_symlink()) and not self._plain(lock_path):
            raise OSError("缓存锁文件包含链接")
        stream = lock_path.open("a+b")
        try:
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            logger.warning("缓存写入所有权不可用：%s", self.directory)
            return False
        self._owner = stream
        return True

    def _release_owner(self):
        stream, self._owner = self._owner, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            logger.warning("缓存锁释放失败", exc_info=True)
        finally:
            stream.close()

    def _scan(self):
        files = {}
        pattern = re.compile(r"([0-9a-f]{64})" + re.escape(self._suffix) + r"|\.([0-9a-f]{64})\.[0-9a-f]{32}\.tmp")
        if not self.directory.exists():
            self._files, self._bytes = {}, 0
            return True
        if not self._plain(self.directory, directory=True):
            raise OSError("缓存根目录包含链接")
        for version in self.directory.iterdir():
            if self._closed.is_set():
                return False
            if not re.fullmatch(r"v\d+", version.name) or not self._plain(version, directory=True):
                continue
            for shard in version.iterdir():
                if self._closed.is_set():
                    return False
                if not re.fullmatch(r"[0-9a-f]{2}", shard.name) or not self._plain(shard, directory=True):
                    continue
                for path in shard.iterdir():
                    if self._closed.is_set():
                        return False
                    match = pattern.fullmatch(path.name)
                    if match and (match[1] or match[2])[:2] == shard.name and self._plain(path):
                        info = path.stat()
                        files[path] = (info.st_size, info.st_atime_ns)
        self._files = files
        self._bytes = sum(size for size, _ in files.values())
        return True

    def _delete(self, path):
        try:
            self._safe_parent(path)
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("缓存删除失败：%s", path, exc_info=True)
            return False
        previous = self._files.pop(path, None)
        if previous is not None:
            self._bytes -= previous[0]
        return True

    def _make_room(self, size, protected=None, *, preloading=False):
        if self._bytes + self._reserved + size <= self.max_bytes:
            return True
        if preloading:
            return False
        target = min(self.max_bytes * 9 // 10, self.max_bytes - size)
        for path, _ in sorted(self._files.items(), key=lambda item: item[1][1]):
            if self._closed.is_set():
                return False
            if path != protected:
                self._delete(path)
            if self._bytes + self._reserved <= target:
                return True
        return self._bytes + self._reserved + size <= self.max_bytes

    def prepare(self, version):
        if not re.fullmatch(r"v\d+", version):
            raise ValueError("缓存版本格式无效")
        with self._publication():
            if self._prepared or self._closed.is_set():
                return
            self._prepared, self._version = True, version
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                if not self._plain(self.directory, directory=True):
                    raise OSError("缓存根目录包含链接")
                writer = self._acquire_owner()
                if not self._scan() or not writer:
                    return
                for path in tuple(self._files):
                    if self._closed.is_set():
                        return
                    if path.suffix == ".tmp" or path.parent.parent.name != version:
                        self._delete(path)
                if self._make_room(0):
                    self._ready.set()
                else:
                    logger.warning("缓存容量尚未就绪：%s", self.directory)
            except OSError:
                logger.warning("缓存初始化失败：%s", self.directory, exc_info=True)

    def current(self, generation, is_cancelled=lambda: False, is_current=lambda: True):
        if self._closed.is_set() or is_cancelled() or generation != self.generation:
            return False
        try:
            return is_current()
        except OSError:
            return False

    @contextmanager
    def open_entry(self, digest, version):
        if self._closed.is_set():
            raise FileNotFoundError("缓存已关闭")
        target = self._target(digest, version)
        self._safe_parent(target)
        with target.open("rb") as stream:
            yield stream

    def exists(self, digest, version):
        if self._closed.is_set():
            return False
        try:
            target = self._target(digest, version)
            self._safe_parent(target)
            return self._plain(target)
        except OSError:
            return False

    def touch(self, digest, version, generation, *, is_current=lambda: True,
              is_cancelled=lambda: False):
        if not self.is_writer or not self.capacity_ready:
            return
        with self._publication():
            if not self.current(generation, is_cancelled, is_current):
                return
            target = self._target(digest, version)
            try:
                self._safe_parent(target)
                info = target.stat()
                accessed = time_ns()
                os.utime(target, ns=(accessed, info.st_mtime_ns))
                previous = self._files.get(target, (0, 0))
                self._files[target] = (info.st_size, accessed)
                self._bytes += info.st_size - previous[0]
            except OSError:
                logger.warning("缓存访问记录失败：%s", target, exc_info=True)

    def discard(self, digest, version, generation, *, is_current=lambda: True,
                is_cancelled=lambda: False):
        if not self.is_writer or not self.capacity_ready:
            return
        with self._publication():
            if self.current(generation, is_cancelled, is_current):
                self._delete(self._target(digest, version))

    def write(self, digest, version, size, write_payload, validate_payload, *, generation,
              is_current, is_cancelled=lambda: False, preloading=False):
        if (not self.capacity_ready or not self.is_writer or version != self._version
                or size > self.max_bytes):
            return False
        target = self._target(digest, version)
        temporary = target.with_name(f".{target.stem}.{uuid4().hex}.tmp")
        with self._publication():
            if not self.current(generation, is_cancelled, is_current):
                return False
            reserved = False
            try:
                if self._writes - self._scanned_writes >= 256:
                    if not self._scan():
                        return False
                    self._scanned_writes = self._writes
                self._safe_parent(target, create=True)
                if not self._make_room(size, protected=target, preloading=preloading):
                    return False
                self._reserved += size
                reserved = True
                with temporary.open("xb") as stream:
                    write_payload(stream)
                if temporary.stat().st_size != size:
                    raise OSError("缓存写入长度无效")
                with temporary.open("rb") as stream:
                    validate_payload(stream)
                if not self.current(generation, is_cancelled, is_current):
                    return False
                self._safe_parent(target)
                os.replace(temporary, target)
                self._reserved -= size
                reserved = False
                previous = self._files.get(target, (0, 0))
                self._files[target] = (size, time_ns())
                self._bytes += size - previous[0]
                self._writes += 1
                return True
            except (OSError, ValueError) as error:
                logger.warning("缓存写入失败：%s (%s)", target, str(error))
                return False
            finally:
                if reserved:
                    try:
                        self._safe_parent(temporary)
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("缓存临时文件清理失败：%s", temporary, exc_info=True)
                        try:
                            info = temporary.lstat()
                        except OSError:
                            self._files[temporary] = (size, time_ns())
                            self._bytes += size
                        else:
                            self._files[temporary] = (info.st_size, info.st_atime_ns)
                            self._bytes += info.st_size
                    finally:
                        self._reserved -= size

    def status(self):
        with self._publication():
            if not self.is_writer and not self._closed.is_set():
                try:
                    self._scan()
                except OSError:
                    logger.warning("缓存占用查询失败：%s", self.directory, exc_info=True)
            return ManagedCacheStatus(str(self.directory), self._bytes, self.max_bytes,
                                      self.is_writer, self.capacity_ready)

    def set_limit(self, max_bytes):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("缓存容量必须为正整数")
        with self._publication():
            if self._closed.is_set():
                return
            self._max_bytes = max_bytes
            if self.is_writer:
                self._ready.clear()
                if self._make_room(0):
                    self._ready.set()

    def invalidate(self):
        with self._state_lock:
            self._generation += 1
            return self._generation

    def clear(self, *, invalidate=True):
        if not self.is_writer:
            raise PermissionError("缓存写入所有权不可用")
        if invalidate:
            self.invalidate()
        with self._publication():
            if not self.is_writer:
                raise PermissionError("缓存写入所有权不可用")
            if not self._scan():
                return 0
            removed = 0
            failed = False
            for path in tuple(self._files):
                if self._closed.is_set():
                    return removed
                if self._delete(path):
                    removed += 1
                else:
                    failed = True
            if self._bytes <= self.max_bytes:
                self._ready.set()
            if failed:
                raise OSError("部分缓存未能清除")
            return removed

    def close(self):
        self._closed.set()
        self._ready.clear()
        if self._lock.acquire(blocking=False):
            try:
                self._release_owner()
            finally:
                self._lock.release()
