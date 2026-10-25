"""统一的源图加载、身份识别和预览代理构建。"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional
from concurrent.futures import CancelledError
from PIL import Image

from ..core.contracts.image_display import (DEFAULT_NON_RAW_DISPLAY_MODE, DEFAULT_RAW_DISPLAY_MODE,
                                            NonRawDisplayMode, RawDisplayMode)
from ..core.contracts.result import SourceChangedError
from .color_initialization import display_look

logger = logging.getLogger(__name__)


class _ProxyCacheMiss(CancelledError):
    pass


def path_key(path):
    """同一文件不同写法（大小写、联接目录、映射盘）的等价判断键。

    会访问文件系统，只在后台线程或有缓存处使用；草稿键与浏览列表仍用浏览得到的路径。"""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


@dataclass(frozen=True)
class SourceIdentity:
    path: str
    file_hash: str
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class SourceMetadata:
    orig_size: tuple[int, int]
    source_format: str
    color_pipeline: str


@dataclass(frozen=True)
class SourcePreviewSpec:
    half_size: bool = False
    generation: int = 0


@dataclass
class SourceDisplayImage:
    image: Image.Image
    reference_size: tuple[int, int]

    def close(self):
        self.image.close()


@dataclass
class PanelSnapshot:
    image: Image.Image
    metadata: SourceMetadata

    def close(self):
        self.image.close()


@dataclass
class SourceBundle:
    identity: SourceIdentity
    original: Image.Image
    proxy: Image.Image
    render_source: object = None
    reference_size: tuple[int, int] = (0, 0)
    lease: object = None

    def close(self) -> None:
        self.original.close()
        if self.proxy is not self.original:
            self.proxy.close()
        self.render_source = None
        if self.lease is not None:
            self.lease.close()
            self.lease = None


class ImageSourceService:
    DEFAULT_PROXY_MAX = 1000
    THUMBNAIL_PIPELINE_VERSION = "v2"

    def __init__(self, *, policy=None, engine=None, thumbnail_cache=None, linear_proxy_cache=None):
        from .image_resources import ImageResourcePolicy, ImageResourcePool, ComputeBudget
        policy = policy or ImageResourcePolicy()
        self.compute = ComputeBudget(policy.compute_slots, policy.thread_capacity,
                                     operator_threads=policy.operator_threads)
        self.resources = ImageResourcePool(policy)
        self._engine = engine
        self._thumbnail_cache = thumbnail_cache
        self._linear_proxy_cache = linear_proxy_cache

    @property
    def policy(self):
        """当前生效的资源策略。内存字段可以在运行中由 update_memory() 替换，线程额度启动后不变。"""
        return self.resources.policy

    def update_memory(self, **fields):
        """替换内存上限、座位、保留量或缓存留存额度；已持有的图像与预留保留至释放。"""
        self.resources.update_memory(**fields)

    def close(self):
        self.resources.close()
        self.compute.close()

    @staticmethod
    def _check(cancelled):
        if cancelled():
            raise CancelledError()

    @staticmethod
    def _source_bytes(source):
        return source.pixels.nbytes + (source.alpha.nbytes if source.alpha is not None else 0)

    @staticmethod
    def dimensions(path):
        """不解码读出图片的像素尺寸，供解码前估算内存。"""
        from ..core import raw_loader
        if raw_loader.is_raw_file(path):
            return raw_loader.raw_reference_size(path)
        with Image.open(path) as image:
            return image.size

    def borrow_linear(self, path, *, identity=None, max_side=None, half_size=False,
                      is_cancelled=lambda: False, priority=0, preloading=False):
        generation = self._linear_proxy_cache.generation if self._linear_proxy_cache is not None else None
        if max_side is not None and (type(max_side) is not int or max_side < 1):
            raise ValueError("max_side must be a positive integer")
        identity = identity or self.identity(path, is_cancelled=is_cancelled)
        if not self.matches_file(identity):
            raise SourceChangedError()
        key = self._linear_key(identity, max_side, half_size)
        disk_key = self._proxy_cache_key(identity, max_side, half_size)
        disk_available = disk_key is not None and self._linear_proxy_cache.exists(disk_key)

        def load(cancelled):
            from ..core.photo_engine import load_render_source
            self._check(cancelled)
            if max_side is None:
                with self.compute.borrow(cancelled, priority) as compute:
                    if self._engine is None:
                        source = load_render_source(identity.path, for_preview=half_size,
                                                    threads=compute.threads)
                    else:
                        source = self._engine.load_engine_source(identity.path, for_preview=half_size,
                                                                threads=compute.threads)
                if self.identity(identity.path, is_cancelled=cancelled) != identity:
                    raise SourceChangedError()
            else:
                source = self._linear_proxy_cache.read(disk_key, source_path=identity.path,
                    is_current=lambda: self.matches_file(identity), is_cancelled=cancelled) if disk_available else None
                self._check(cancelled)
                if source is None:
                    from ..core.tools.effects import resize_source
                    with self.borrow_linear(identity.path, identity=identity, is_cancelled=cancelled,
                                            priority=priority, half_size=half_size) as original:
                        full = original.value
                        height, width = full.pixels.shape[:2]
                        ratio = min(1, max_side / max(width, height))
                        if ratio == 1:
                            with self.resources.working_memory(self._source_bytes(full), cancelled):
                                with self.compute.borrow(cancelled, priority, threads=1):
                                    source = replace(full, pixels=full.pixels.copy(),
                                                     alpha=full.alpha.copy() if full.alpha is not None else None)
                        else:
                            size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
                            parallelism = min(3, self.policy.interactive_threads) if priority == 0 else 1
                            channel_bytes = full.pixels[..., 0].nbytes
                            has_alpha = full.alpha is not None
                            output_bytes = size[0] * size[1] * (48 if has_alpha else 24)
                            sizes = tuple((2 * count + int(has_alpha)) * channel_bytes + output_bytes
                                          for count in range(parallelism, 0, -1))
                            with self.resources.working_memory(sizes[0], cancelled,
                                    fallback_sizes=sizes[1:]) as reserved:
                                parallelism -= sizes.index(reserved)
                                with self.compute.borrow(cancelled, priority, role="foreground" if priority == 0 else "operator",
                                        threads=parallelism) as compute:
                                    source = replace(resize_source(full, size, parallelism=min(parallelism, compute.threads)),
                                                     is_proxy=True, orig_size=full.orig_size)
            self._check(cancelled)
            if not self.matches_file(identity):
                raise SourceChangedError()
            source.pixels.setflags(write=False)
            source.content_identity = (identity.file_hash, self._linear_key(identity, None, half_size),
                                       source.source_format, source.color_pipeline)
            if source.alpha is not None:
                source.alpha.setflags(write=False)
            return source

        def estimate():
            if max_side is not None:
                return max_side ** 2 * 16 + 65536 if disk_available else 0
            width, height = self.dimensions(identity.path)
            return width * height * (24 if half_size else 64)
        try:
            lease = self.resources.acquire(key, "source" if max_side is None else "proxy",
                                           estimate, load, self._source_bytes, is_cancelled)
        except _ProxyCacheMiss:
            lease = self.resources.acquire(key, "proxy", estimate, load, self._source_bytes, is_cancelled)
        try:
            self._check(is_cancelled)
            if not self.matches_file(identity):
                raise SourceChangedError()
            self._submit_proxy(key, disk_key, identity, is_cancelled, preloading, generation)
            return lease
        except BaseException:
            lease.close()
            raise

    @staticmethod
    def _linear_key(identity, max_side, half_size):
        key = (identity, "linear-f32-straight-alpha-v1", max_side, "lanczos-premultiplied-v1")
        return key + ("raw-half-v1",) if half_size else key

    def borrow_cached_linear(self, identity):
        if not self.matches_file(identity):
            raise SourceChangedError()
        return self.resources.borrow_cached(self._linear_key(identity, None, False))

    def _proxy_cache_key(self, identity, max_side, half_size=False):
        if self._linear_proxy_cache is None or max_side != 1600 or half_size:
            return None
        from ..core.photo_engine import linear_proxy_decode_identity
        from .linear_proxy_cache import LinearProxyKey
        decode_identity = linear_proxy_decode_identity(identity.path)
        return (LinearProxyKey(identity.file_hash, max_side, decode_identity)
                if decode_identity is not None else None)

    def _submit_proxy(self, key, disk_key, identity, is_cancelled, preloading, generation):
        if disk_key is None:
            return
        lease = self.resources.borrow_cached(key)
        if lease is None:
            return
        if max(lease.value.orig_size) <= disk_key.max_side:
            lease.close()
            return
        try:
            self._linear_proxy_cache.submit(disk_key, lease,
                is_current=lambda: self.matches_file(identity), is_cancelled=is_cancelled,
                preloading=preloading, generation=generation)
        except Exception:
            lease.close()
            logger.exception("编辑缓存提交失败：%s", Path(identity.path).name)

    def borrow_cached_proxy(self, identity, *, max_side=1600, is_cancelled=lambda: False):
        generation = self._linear_proxy_cache.generation if self._linear_proxy_cache is not None else None
        if type(max_side) is not int or max_side < 1:
            raise ValueError("max_side must be a positive integer")
        self._check(is_cancelled)
        if not self.matches_file(identity):
            raise SourceChangedError()
        key = self._linear_key(identity, max_side, False)
        disk_key = self._proxy_cache_key(identity, max_side)
        lease = self.resources.borrow_cached(key)
        if lease is None:
            if disk_key is None or not self._linear_proxy_cache.exists(disk_key):
                return None

            def load(cancelled):
                self._check(cancelled)
                source = self._linear_proxy_cache.read(disk_key, source_path=identity.path,
                    is_current=lambda: self.matches_file(identity), is_cancelled=cancelled)
                self._check(cancelled)
                if not self.matches_file(identity):
                    raise SourceChangedError()
                if source is None:
                    raise _ProxyCacheMiss()
                source.pixels.setflags(write=False)
                source.content_identity = (identity.file_hash, self._linear_key(identity, None, False),
                                           source.source_format, source.color_pipeline)
                if source.alpha is not None:
                    source.alpha.setflags(write=False)
                return source

            try:
                lease = self.resources.acquire(key, "proxy", max_side ** 2 * 16 + 65536,
                                               load, self._source_bytes, is_cancelled)
            except _ProxyCacheMiss:
                return None
        try:
            self._check(is_cancelled)
            if not self.matches_file(identity):
                raise SourceChangedError()
            self._submit_proxy(key, disk_key, identity, is_cancelled, False, generation)
            return lease
        except BaseException:
            lease.close()
            raise

    def borrow_display(self, path, *, for_preview=False, is_cancelled=lambda: False, priority=0):
        from ..core.raw_loader import load_image, is_raw_file
        identity = self.identity(path, is_cancelled=is_cancelled)
        raw_preview = for_preview and is_raw_file(path)
        key = (identity, "viewer-raw-camera-wb-auto-bright-v1" if is_raw_file(path) else "pillow-v1", raw_preview)

        def load(cancelled):
            role = "thumbnail" if raw_preview is False and for_preview and priority >= 2 else "operator"
            with self.compute.borrow(cancelled, priority, role=role,
                                     threads=1 if role == "thumbnail" else self.policy.operator_threads):
                self._check(cancelled)
                image = load_image(path, for_preview=raw_preview)
                try:
                    self._check(cancelled)
                    if not self.matches_file(identity):
                        raise SourceChangedError()
                    return image
                except BaseException:
                    image.close()
                    raise

        def estimate():
            width, height = self.dimensions(path)
            return width * height * 16
        return self.resources.acquire(key, "display", estimate, load,
                                      lambda image: image.width * image.height * len(image.getbands()), is_cancelled)

    @staticmethod
    def file_stamp(path):
        stat = Path(path).stat()
        return stat.st_mtime_ns, stat.st_size, stat.st_ino

    def display_image(self, path, *, size=None, expected_stamp=None, is_cancelled=lambda: False, priority=0):
        from PIL import ImageOps
        if expected_stamp is not None and self.file_stamp(path) != expected_stamp:
            raise SourceChangedError()
        with self.borrow_display(path, for_preview=size is not None, is_cancelled=is_cancelled,
                                 priority=priority) as lease:
            self._check(is_cancelled)
            with self.compute.borrow(is_cancelled, priority,
                    role="thumbnail" if size is not None and priority >= 2 else "operator", threads=1):
                rgba = lease.value.convert("RGBA")
                try:
                    self._check(is_cancelled)
                    result = rgba if size is None else ImageOps.fit(
                        rgba, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
                except BaseException:
                    rgba.close()
                    raise
                if result is not rgba:
                    rgba.close()
        if is_cancelled() or (expected_stamp is not None and self.file_stamp(path) != expected_stamp):
            result.close()
            self._check(is_cancelled)
            raise SourceChangedError()
        return result

    def viewer_image(self, path, *, raw_display_mode=DEFAULT_RAW_DISPLAY_MODE,
                     non_raw_display_mode=DEFAULT_NON_RAW_DISPLAY_MODE, expected_stamp=None,
                     is_cancelled=lambda: False):
        from ..core.raw_loader import is_raw_file
        is_raw = is_raw_file(path)
        mode = RawDisplayMode(raw_display_mode) if is_raw else NonRawDisplayMode(non_raw_display_mode)
        if not is_raw and mode == NonRawDisplayMode.ORIGINAL:
            image = self.display_image(path, expected_stamp=expected_stamp, is_cancelled=is_cancelled)
            return image, image.size
        if expected_stamp is not None and self.file_stamp(path) != expected_stamp:
            raise SourceChangedError()
        identity = self.identity(path, is_cancelled=is_cancelled)
        key = (identity, "viewer-engine-color-v4", mode.value)

        def load(cancelled):
            result = self._camera_display(path, cancelled) if is_raw and mode == RawDisplayMode.CAMERA else None
            if result is None:
                from ..core.photo_engine import render_source, _pil_from
                from ..core.tools.color import COLOR_PARAMETERS
                params = COLOR_PARAMETERS.defaults(COLOR_PARAMETERS.version)
                params["look"] = display_look(is_raw=is_raw, raw_display_mode=raw_display_mode,
                                               non_raw_display_mode=non_raw_display_mode)
                with self.borrow_linear(path, identity=identity, is_cancelled=cancelled) as source_lease:
                    source = source_lease.value
                    with self.resources.working_memory(self._source_bytes(source) * 4, cancelled), \
                            self.compute.borrow(cancelled) as compute:
                        self._check(cancelled)
                        rendered = (render_source(source, params, threads=compute.threads) if self._engine is None
                                    else self._engine.render_source(source, params, threads=compute.threads))
                        self._check(cancelled)
                        image = _pil_from(rendered) if self._engine is None else self._engine.source_to_pil(rendered)
                        result = SourceDisplayImage(image, source.orig_size)
            try:
                self._check(cancelled)
                if not self.matches_file(identity):
                    raise SourceChangedError()
                return result
            except BaseException:
                result.close()
                raise

        with self.resources.acquire(key, "display", 0, load,
                lambda value: value.image.width * value.image.height * len(value.image.getbands()),
                is_cancelled) as lease:
            value = lease.value
            with self.resources.working_memory(value.image.width * value.image.height * 4, is_cancelled), \
                    self.compute.borrow(is_cancelled, threads=1):
                self._check(is_cancelled)
                image = value.image.convert("RGBA")
            reference_size = value.reference_size
        try:
            self._check(is_cancelled)
            if expected_stamp is not None and self.file_stamp(path) != expected_stamp:
                raise SourceChangedError()
            return image, reference_size
        except BaseException:
            image.close()
            raise

    def _camera_display(self, path, is_cancelled):
        from io import BytesIO
        from ..core import raw_loader
        with self.compute.borrow(is_cancelled):
            preview, reason = raw_loader.read_quick_preview(path)
        if preview is None:
            logger.debug("相机预览不可用 file=%s reason=%s", Path(path).name, reason)
            return None
        self._check(is_cancelled)
        try:
            with Image.open(BytesIO(preview.jpeg)) as image:
                reference = preview.mapping.reference_size
                required = len(preview.jpeg) + image.width * image.height * 10 + reference[0] * reference[1] * 16
                with self.resources.working_memory(required, is_cancelled), self.compute.borrow(is_cancelled):
                    self._check(is_cancelled)
                    result = raw_loader.fit_mapped_preview(image, preview.mapping, max_side=max(reference))
        except (OSError, ValueError, SyntaxError) as error:
            logger.warning("相机预览解码失败 file=%s exception=%s", Path(path).name, type(error).__name__)
            return None
        return SourceDisplayImage(result, reference)

    def thumbnail(self, path, size, *, expected_stamp=None, is_cancelled=lambda: False, priority=2):
        from .thumbnail_cache import ThumbnailKey
        stamp = self.file_stamp(path) if expected_stamp is None else expected_stamp
        if self.file_stamp(path) != stamp:
            raise SourceChangedError()
        self._check(is_cancelled)
        key = ThumbnailKey(path, stamp, size, self.THUMBNAIL_PIPELINE_VERSION)
        current = lambda: self.file_stamp(path) == stamp
        cache = self._thumbnail_cache
        if cache is not None:
            cached = None
            try:
                with self.resources.working_memory(size[0] * size[1] * 8, is_cancelled), \
                        self.compute.borrow(is_cancelled, priority, role="thumbnail"):
                    cached = cache.get(key, is_cancelled=is_cancelled, is_current=current)
            except MemoryError:
                logger.warning("缩略图缓存读取内存不足：%s", Path(path).name)
            if cached is not None:
                try:
                    self._check(is_cancelled)
                    if not current():
                        raise SourceChangedError()
                    return cached
                except BaseException:
                    cached.close()
                    raise
        result = self._make_thumbnail(path, size, expected_stamp=stamp,
                                      is_cancelled=is_cancelled, priority=priority)
        try:
            if cache is not None and cache.capacity_ready and cache.is_writer:
                try:
                    with self.resources.working_memory(size[0] * size[1] * 16, is_cancelled):
                        cache.put(key, result, is_cancelled=is_cancelled, is_current=current,
                                  encoding=self.compute.borrow(is_cancelled, priority, role="thumbnail"))
                except MemoryError:
                    logger.warning("缩略图缓存编码内存不足，跳过写入：%s", Path(path).name)
            self._check(is_cancelled)
            if not current():
                raise SourceChangedError()
            return result
        except BaseException:
            result.close()
            raise

    def _make_thumbnail(self, path, size, *, expected_stamp, is_cancelled, priority):
        from PIL import ImageOps
        from ..core import raw_loader
        if expected_stamp is not None and self.file_stamp(path) != expected_stamp:
            raise SourceChangedError()
        self._check(is_cancelled)
        preview = None
        with self.compute.borrow(is_cancelled, priority, role="thumbnail"):
            if raw_loader.is_raw_file(path):
                preview, reason = raw_loader.load_embedded_preview(
                    path, min_short_side=min(size), decode_short_side=max(size))
                if preview is None:
                    logger.debug("内嵌预览不可用 file=%s reason=%s", Path(path).name, reason)
            if preview is not None:
                try:
                    self._check(is_cancelled)
                    with ImageOps.fit(preview.image, size, method=Image.Resampling.LANCZOS,
                                      centering=(0.5, 0.5)) as fitted:
                        result = fitted.convert("RGBA")
                finally:
                    preview.image.close()
        if preview is None:
            return self.display_image(path, size=size, expected_stamp=expected_stamp,
                                      is_cancelled=is_cancelled, priority=priority)
        if is_cancelled() or (expected_stamp is not None and self.file_stamp(path) != expected_stamp):
            result.close()
            self._check(is_cancelled)
            raise SourceChangedError()
        return result

    @staticmethod
    def supports_quick_preview(path):
        from ..core.raw_loader import is_raw_file
        return is_raw_file(path)

    def quick_preview(self, path, *, expected_stamp=None, is_cancelled=lambda: False):
        from io import BytesIO
        from ..core import raw_loader
        self._check(is_cancelled)
        if expected_stamp is not None and self.file_stamp(path) != expected_stamp:
            raise SourceChangedError()
        if not self.supports_quick_preview(path):
            return None
        with self.compute.borrow(is_cancelled, role="quick_preview"):
            preview, reason = raw_loader.read_quick_preview(path)
        if preview is None:
            logger.debug("快速预览不可用 file=%s reason=%s", Path(path).name, reason)
            return None
        self._check(is_cancelled)
        with Image.open(BytesIO(preview.jpeg)) as image:
            limit = raw_loader.QUICK_PREVIEW_MAX_SIDE
            ratio = min(1, limit / max(image.size))
            image.draft("RGB", (max(1, round(image.width * ratio)), max(1, round(image.height * ratio))))
            reference = preview.mapping.reference_size
            canvas_ratio = min(1, limit / max(reference))
            canvas_pixels = max(1, round(reference[0] * canvas_ratio)) * max(1, round(reference[1] * canvas_ratio))
            required = len(preview.jpeg) + image.width * image.height * 6 + canvas_pixels * 16
            with self.resources.working_memory(required, is_cancelled), \
                    self.compute.borrow(is_cancelled, role="quick_preview"):
                self._check(is_cancelled)
                result = raw_loader.fit_mapped_preview(image, preview.mapping)
        try:
            self._check(is_cancelled)
            if expected_stamp is not None and self.file_stamp(path) != expected_stamp:
                raise SourceChangedError()
            return result, reference
        except BaseException:
            result.close()
            raise

    def _display_from(self, source, is_cancelled=lambda: False, priority=0):
        from ..core.photo_engine import _pil_from
        with self.resources.working_memory(source.pixels.nbytes * 4, is_cancelled):
            with self.compute.borrow(is_cancelled, priority, role="foreground" if priority == 0 else "operator",
                    threads=self.policy.interactive_threads if priority == 0 else 1) as compute:
                return _pil_from(source, parallelism=min(compute.threads,
                                 self.policy.interactive_threads if priority == 0 else 1))

    def borrow_panel_snapshot(self, identity, *, image=None, metadata=None, half_size=False,
                              is_cancelled=lambda: False):
        key = (identity, "panel-display-linear-proxy-v2", self.DEFAULT_PROXY_MAX)
        if half_size:
            key += ("raw-half-v1",)

        def load(cancelled):
            self._check(cancelled)
            if image is not None:
                with self.resources.working_memory(self.DEFAULT_PROXY_MAX ** 2 * 8, cancelled):
                    return PanelSnapshot(self.build_proxy(image), metadata)
            with self.borrow_linear(identity.path, identity=identity, max_side=1600,
                                    is_cancelled=cancelled, half_size=half_size) as lease:
                source = lease.value
                original = self._display_from(source, cancelled)
                try:
                    return PanelSnapshot(self.build_proxy(original),
                        SourceMetadata(source.orig_size, source.source_format, source.color_pipeline))
                finally:
                    original.close()

        return self.resources.acquire(key, "display", 0, load,
            lambda value: value.image.width * value.image.height * len(value.image.getbands()), is_cancelled)

    def load_preview(self, path, *, max_side=1600, is_cancelled=lambda: False):
        with self.borrow_linear(path, max_side=max_side, is_cancelled=is_cancelled, priority=2) as lease:
            return self._display_from(lease.value, is_cancelled, priority=2), lease.value.orig_size

    def load_original_display(self, identity, *, is_cancelled=lambda: False):
        with self.borrow_linear(identity.path, identity=identity,
                                is_cancelled=is_cancelled, priority=2) as lease:
            image = self._display_from(lease.value, is_cancelled, priority=2)
        if is_cancelled() or not self.matches_file(identity):
            image.close()
            self._check(is_cancelled)
            raise SourceChangedError()
        return image

    @staticmethod
    def identity(path: str, *, is_cancelled=lambda: False) -> SourceIdentity:
        target = Path(path).resolve()
        before = target.stat()
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                ImageSourceService._check(is_cancelled)
                digest.update(chunk)
        after = target.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise SourceChangedError()
        return SourceIdentity(str(target), digest.hexdigest(), after.st_mtime_ns, after.st_size)

    @staticmethod
    def matches_file(identity: SourceIdentity, *, path=None) -> bool:
        try:
            stat = Path(identity.path).stat()
            if path is not None:
                displayed = Path(path).stat()
                if (stat.st_dev, stat.st_ino) != (displayed.st_dev, displayed.st_ino):
                    return False
                stat = displayed
        except OSError:
            return False
        return (stat.st_mtime_ns, stat.st_size) == (identity.mtime_ns, identity.size)

    @classmethod
    def build_proxy(cls, image: Image.Image, max_side: Optional[int] = None) -> Image.Image:
        limit = cls.DEFAULT_PROXY_MAX if max_side is None else max_side
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("max_side must be a positive integer")
        width, height = image.size
        if max(width, height) <= limit:
            return image.copy()
        scale = limit / max(width, height)
        return image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.BILINEAR,
        )

    def load(self, path: str, max_side: Optional[int] = None, *, proxy_only=False,
             identity=None, half_size=False, is_cancelled=lambda: False) -> SourceBundle:
        if max_side is not None and (
            not isinstance(max_side, int) or isinstance(max_side, bool) or max_side < 1
        ):
            raise ValueError("max_side must be a positive integer")
        identity = identity or self.identity(path, is_cancelled=is_cancelled)
        if proxy_only:
            lease = self.borrow_panel_snapshot(identity, half_size=half_size, is_cancelled=is_cancelled)
            try:
                snapshot = lease.value
                original = snapshot.image.copy()
                return SourceBundle(identity, original, original, snapshot.metadata,
                                    snapshot.metadata.orig_size, lease)
            except BaseException:
                lease.close()
                raise
        lease = self.borrow_linear(path, identity=identity,
                                   is_cancelled=is_cancelled)
        source = lease.value
        original = None
        try:
            original = self._display_from(source, is_cancelled)
            if original.mode not in ("RGB", "RGBA"):
                mode = "RGBA" if "A" in original.getbands() or "transparency" in original.info else "RGB"
                converted = original.convert(mode)
                original.close()
                original = converted
            proxy = self.build_proxy(original, max_side)
            if not self.matches_file(identity):
                proxy.close()
                raise SourceChangedError()
            return SourceBundle(identity, original, proxy, source, source.orig_size, lease)
        except BaseException:
            if original is not None:
                original.close()
            lease.close()
            raise
