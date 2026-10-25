from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, field
from contextlib import contextmanager
from concurrent.futures import CancelledError
from io import BytesIO
import math

from PIL import Image

from .output_writer import OperationCancellation, OutputWriter
from .services import ExportRequest, ExportService
from ..core.photo_engine import _src_from_pil, load_render_source, render_source, save_render_source, encode_render_source
from ..core.render_codec import validate_output, validate_source
from ..core.image_resize import RESIZE_MODES, ResizeOptions, plan_resize, apply_resize, resize_memory
from ..core.constants import RAW_EXTENSIONS, IMAGE_QUALITY_RANGE
from ..core.output_formats import OUTPUT_FORMATS, output_format, validate_quality
from ..core.contracts.result import SourceChangedError


def source_stamp(path):
    stat = Path(path).stat()
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


@dataclass(frozen=True)
class ImageSourceInfo:
    size: tuple[int, int]
    format: str
    file_size: int
    stamp: tuple[int, int, int]


@dataclass(frozen=True)
class QualityMatch:
    """quality 为 None 时，最低质量的文件大小仍超过目标。"""

    quality: int | None
    file_size: int


@dataclass
class BatchOutputResult:
    succeeded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    def __len__(self):
        return len(self.succeeded)


class ImageOutputService:
    resize_modes = RESIZE_MODES
    quality_range = IMAGE_QUALITY_RANGE
    output_formats = OUTPUT_FORMATS
    output_format = staticmethod(output_format)
    validate_quality = staticmethod(validate_quality)

    def __init__(self, writer=None, *, compute_budget=None, resource_budget=None):
        """resource_budget 是共享的图像资源池；给出时，缩放先按估算的工作内存判断并在执行时准入。"""
        self.writer = writer or OutputWriter()
        self._compute_budget = compute_budget
        self._resource_budget = resource_budget

    @contextmanager
    def _compute(self, cancellation, is_cancelled=None):
        is_cancelled = is_cancelled or cancellation.is_cancelled
        try:
            if is_cancelled():
                raise CancelledError()
            if self._compute_budget is None:
                yield None
            else:
                with self._compute_budget.borrow(is_cancelled, priority=1) as compute:
                    yield compute
        except CancelledError as error:
            raise InterruptedError("error.cancelled") from error

    def save(self, image, path, *, quality=95, overwrite=False, cancellation=None, publication=None):
        cancellation = cancellation or OperationCancellation()
        with self.writer.reserve(path) as reservation:
            with self._compute(cancellation, lambda: cancellation.is_cancelled() or not self.writer.is_current(reservation)):
                return self._save(image, path, quality=quality, overwrite=overwrite,
                                  cancellation=cancellation, publication=publication, reservation=reservation)

    def _save(self, image, path, *, cancellation, reservation, quality=95, overwrite=False, publication=None):
        if cancellation.is_cancelled() or not self.writer.is_current(reservation):
            raise InterruptedError("error.cancelled")
        source = image if hasattr(image, "pixels") else _src_from_pil(image)
        validate_source(source)
        request = ExportRequest(str(path), quality=quality, overwrite=overwrite)
        checked = ExportService.validate_request(request)
        if not checked.success:
            raise ValueError(checked.error.message_key)
        ext, _ = checked.data
        if cancellation.is_cancelled() or not self.writer.is_current(reservation):
            raise InterruptedError("error.cancelled")
        size = (source.pixels.shape[1], source.pixels.shape[0])
        result = self.writer.write(
            reservation, ext=ext, overwrite=overwrite,
            encode=lambda temporary: save_render_source(source, temporary, ext=ext, quality=quality),
            validate=lambda temporary: validate_output(temporary, ext, size=size, has_alpha=source.alpha is not None),
            publication=publication or cancellation.publication,
        )
        if not result.success:
            if result.error.message_key == "error.cancelled":
                raise InterruptedError("error.cancelled")
            raise IOError(result.error.message_key)
        return str(path)

    def convert(self, source_path, target_path, **options):
        cancellation = options.pop("cancellation", None) or OperationCancellation()
        with self.writer.reserve(target_path) as reservation:
            cancelled = lambda: cancellation.is_cancelled() or not self.writer.is_current(reservation)
            with self._compute(cancellation, cancelled) as compute:
                source = load_render_source(str(source_path), for_preview=False,
                                            threads=compute.threads if compute is not None else 1)
                return self._save(source, target_path,
                                  cancellation=cancellation, reservation=reservation, **options)

    @staticmethod
    def resize_options(width, height, mode):
        return ResizeOptions.from_text(width, height, mode)

    def describe_resize(self, size, options):
        """缩放的规划；估算的工作内存超过这台机器的上限时拒绝。"""
        plan = plan_resize(size, options)
        if self._resource_budget is not None and resize_memory(plan) > self._resource_budget.policy.memory_bytes:
            raise ValueError("error.resize.memory")
        return plan

    @contextmanager
    def _resize_memory(self, path, options, cancelled):
        """缩放一张图片期间占住估算的工作内存；没有资源池时不准入。"""
        if self._resource_budget is None:
            yield
            return
        from .image_source_service import ImageSourceService
        plan = plan_resize(tuple(ImageSourceService.dimensions(str(path))), options)
        try:
            with self._resource_budget.working_memory(resize_memory(plan), cancelled):
                yield
        except CancelledError as error:
            raise InterruptedError("error.cancelled") from error

    @staticmethod
    def target_bytes(text):
        try:
            value = float(text)
            if not math.isfinite(value) or value <= 0:
                raise ValueError()
            size = int(value * 1024)
            if size < 1:
                raise ValueError()
            return size
        except (ValueError, OverflowError) as error:
            raise ValueError("compress.status.invalid_target") from error

    def inspect_source(self, path, *, cancellation):
        with self._compute(cancellation) as compute:
            stamp = source_stamp(path)
            suffix = Path(path).suffix.lower()
            if suffix in RAW_EXTENSIONS or suffix in (".tif", ".tiff"):
                source = load_render_source(str(path), for_preview=False,
                                            threads=compute.threads if compute is not None else 1)
                size = (source.pixels.shape[1], source.pixels.shape[0])
                fmt = suffix.lstrip(".").upper()
            else:
                with Image.open(path) as image:
                    size, fmt = image.size, image.format
            if cancellation.is_cancelled():
                raise InterruptedError("error.cancelled")
            if source_stamp(path) != stamp:
                raise SourceChangedError()
            return ImageSourceInfo(size, fmt, stamp[1], stamp)

    @staticmethod
    def _load_resized(path, options, cancelled, expected_stamp, *, threads=1):
        if cancelled():
            raise InterruptedError("error.cancelled")
        stamp = source_stamp(path)
        if expected_stamp is not None and stamp != expected_stamp:
            raise SourceChangedError()
        source = load_render_source(str(path), for_preview=False, threads=threads)
        if cancelled():
            raise InterruptedError("error.cancelled")
        size = (source.pixels.shape[1], source.pixels.shape[0])
        result = apply_resize(source, plan_resize(size, options))
        if cancelled():
            raise InterruptedError("error.cancelled")
        if source_stamp(path) != stamp:
            raise SourceChangedError()
        return result, stamp

    def resize_and_save(self, source_path, target_path, *, options, quality=95,
                        cancellation=None, expected_stamp=None):
        checked = ExportService.validate_request(ExportRequest(str(target_path), quality=quality))
        if not checked.success:
            raise ValueError(checked.error.message_key)
        cancellation = cancellation or OperationCancellation()
        with self.writer.reserve(target_path) as reservation:
            cancelled = lambda: cancellation.is_cancelled() or not self.writer.is_current(reservation)
            with self._resize_memory(source_path, options, cancelled), \
                    self._compute(cancellation, cancelled) as compute:
                source, _ = self._load_resized(source_path, options, cancelled, expected_stamp,
                                             threads=compute.threads if compute is not None else 1)
                return self._save(source, target_path, quality=quality, cancellation=cancellation,
                                  reservation=reservation)

    def match_jpeg_quality(self, source_path, *, options, target_bytes,
                           cancellation, expected_stamp=None):
        if type(target_bytes) is not int or target_bytes < 1:
            raise ValueError("compress.status.invalid_target")
        with self._resize_memory(source_path, options, cancellation.is_cancelled), \
                self._compute(cancellation) as compute:
            source, stamp = self._load_resized(source_path, options, cancellation.is_cancelled, expected_stamp,
                                             threads=compute.threads if compute is not None else 1)
            def encoded_size(quality):
                if cancellation.is_cancelled():
                    raise InterruptedError("error.cancelled")
                with BytesIO() as buffer:
                    encode_render_source(source, buffer, ext=".jpg", quality=quality)
                    return buffer.tell()
            minimum, maximum = self.quality_range
            smallest = encoded_size(minimum)
            best = QualityMatch(None, smallest)
            if smallest <= target_bytes:
                best = QualityMatch(minimum, smallest)
                low, high = minimum + 1, maximum
                while low <= high:
                    quality = (low + high) // 2
                    size = encoded_size(quality)
                    if size <= target_bytes:
                        best = QualityMatch(quality, size)
                        low = quality + 1
                    else:
                        high = quality - 1
            if cancellation.is_cancelled():
                raise InterruptedError("error.cancelled")
            if source_stamp(source_path) != stamp:
                raise SourceChangedError()
            return best

    def render_color(self, source_path, params, target_path, *, source_service=None, cancellation=None, **options):
        from ..core.tools.color import color_engine_contract, migrate_legacy_color
        migrated = migrate_legacy_color(params)
        contract = color_engine_contract(migrated)
        checked = contract.normalize(migrated, contract.version)
        if not checked.success:
            raise ValueError(checked.error.message_key)
        params = checked.data
        cancellation = cancellation or OperationCancellation()
        with self.writer.reserve(target_path) as reservation:
            cancelled = lambda: cancellation.is_cancelled() or not self.writer.is_current(reservation)
            if source_service is None:
                with self._compute(cancellation, cancelled) as compute:
                    source = load_render_source(str(source_path), for_preview=False,
                                                threads=compute.threads if compute is not None else 1)
                    if cancelled():
                        raise InterruptedError("error.cancelled")
                    return self._save(render_source(source, params,
                                      threads=compute.threads if compute is not None else None), target_path,
                                      cancellation=cancellation, reservation=reservation, **options)
            try:
                with source_service.borrow_linear(source_path, is_cancelled=cancelled, priority=1) as lease:
                    with source_service.resources.working_memory(lease.value.pixels.nbytes * 4, cancelled):
                        with source_service.compute.borrow(cancelled, priority=1) as compute:
                            return self._save(render_source(lease.value, params, threads=compute.threads), target_path,
                                              cancellation=cancellation, reservation=reservation, **options)
            except CancelledError as error:
                raise InterruptedError("error.cancelled") from error

    @staticmethod
    def batch(jobs, cancellation):
        jobs = list(jobs)
        result = BatchOutputResult()
        for index, (path, operation) in enumerate(jobs):
            if cancellation.is_cancelled():
                result.cancelled.extend(target for target, _ in jobs[index:])
                break
            try:
                operation()
            except InterruptedError:
                result.cancelled.extend(target for target, _ in jobs[index:])
                break
            except Exception as error:
                result.failed.append((path, str(error)))
            else:
                result.succeeded.append(path)
        return result

    def convert_many(self, jobs, *, quality=95, overwrite=False, cancellation):
        return self.batch([(target, lambda source=source, target=target: self.convert(
            source, target, quality=quality, overwrite=overwrite, cancellation=cancellation))
            for source, target in jobs], cancellation)

    @staticmethod
    def tile_path(directory, prefix, index, extension):
        if (not prefix or prefix in (".", "..") or any(char in prefix for char in '<>:"/\\|?*')
                or prefix.endswith((" ", "."))):
            raise ValueError("error.export.filename")
        extension = extension.lower().lstrip(".")
        extension = {"jpeg": "jpg", "tif": "tiff"}.get(extension, extension)
        if extension not in ("png", "jpg", "webp", "bmp", "tiff"):
            raise ValueError("error.export.format")
        return str(Path(directory) / f"{prefix}_{index}.{extension}")

    def grid(self, source, params, directory, prefix, extension, *, cancellation, publication=None):
        from dataclasses import replace
        from ..core.tools.geometry import GRID_PARAMETERS, checked

        params = checked(GRID_PARAMETERS, params)
        width, height = source.pixels.shape[1], source.pixels.shape[0]
        hs = params["h_lines"] if params["h_lines"] is not None else [i / params["rows"] for i in range(1, params["rows"])]
        vs = params["v_lines"] if params["v_lines"] is not None else [i / params["cols"] for i in range(1, params["cols"])]
        xs = [0] + [round(v * width) for v in vs] + [width]
        ys = [0] + [round(v * height) for v in hs] + [height]
        if any(a >= b for values in (xs, ys) for a, b in zip(values, values[1:])):
            raise ValueError("error.export.grid_size")
        paths = [self.tile_path(directory, prefix, i + 1, extension)
                 for i in range((len(xs) - 1) * (len(ys) - 1))]
        if any(Path(path).exists() for path in paths):
            raise FileExistsError("error.export.exists")
        jobs = []
        for top, bottom in zip(ys, ys[1:]):
            for left, right in zip(xs, xs[1:]):
                tile = replace(source, pixels=source.pixels[top:bottom, left:right],
                               alpha=None if source.alpha is None else source.alpha[top:bottom, left:right],
                               orig_size=(right - left, bottom - top))
                path = paths[len(jobs)]
                jobs.append((path, lambda tile=tile, path=path: self.save(
                    tile, path, cancellation=cancellation, publication=publication)))
        return self.batch(jobs, cancellation)
