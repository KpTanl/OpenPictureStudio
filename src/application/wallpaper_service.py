from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import CancelledError
from contextlib import contextmanager
from pathlib import Path

from ..core.contracts.wallpaper import WallpaperPosition
from ..core.render_codec import validate_output
from .result_types import CoreError, CoreOperationError, CoreResult, ErrorCode

logger = logging.getLogger(__name__)


class WallpaperService:
    def __init__(self, directory, *, image_sources, writer, desktop_factory):
        self._directory = Path(directory).resolve()
        self._image_sources = image_sources
        self._writer = writer
        self._desktop_factory = desktop_factory

    @contextmanager
    def _storage(self):
        import msvcrt

        self._directory.mkdir(parents=True, exist_ok=True)
        with (self._directory / "wallpaper.lock").open("a+b") as stream:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise CoreOperationError(CoreError(
                    ErrorCode.PERMISSION_DENIED, "wallpaper.error.busy", retryable=True
                )) from error
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    @staticmethod
    def _path_key(path):
        return os.path.normcase(str(Path(path).resolve()))

    def _target(self, wallpapers, image_path):
        active = {self._path_key(path) for _, path in wallpapers if path}
        active.add(self._path_key(image_path))
        for index in range(max(2, len(wallpapers) + 2)):
            target = self._directory / f"wallpaper-{index}.bmp"
            if self._path_key(target) not in active:
                return target
        raise RuntimeError("没有可用的壁纸文件位置")

    def apply(self, image_path, position, *, cancellation):
        if sys.platform != "win32":
            return CoreResult.fail(ErrorCode.INVALID_INPUT, "wallpaper.error.unsupported")
        try:
            if isinstance(position, bool) or not isinstance(position, int):
                raise ValueError()
            position = WallpaperPosition(position)
        except (TypeError, ValueError):
            return CoreResult.fail(ErrorCode.PARAMETER_INVALID, "wallpaper.error.invalid_style")

        message_key = "wallpaper.error.prepare"
        try:
            if cancellation.is_cancelled():
                raise CancelledError()
            try:
                stamp = self._image_sources.file_stamp(image_path)
            except FileNotFoundError as error:
                raise CoreOperationError(CoreError(
                    ErrorCode.SOURCE_NOT_FOUND, "error.source.not_found"
                )) from error
            with self._storage():
                with self._image_sources.borrow_display(
                    image_path, is_cancelled=cancellation.is_cancelled, priority=1
                ) as lease:
                    message_key = "wallpaper.error.connect"
                    with self._desktop_factory() as desktop:
                        message_key = "wallpaper.error.apply"
                        wallpapers = desktop.get_wallpapers()
                        previous_position = desktop.get_position()
                        target = self._target(wallpapers, image_path)
                        message_key = "wallpaper.error.prepare"
                        with self._image_sources.resources.working_memory(
                            lease.value.width * lease.value.height * 8,
                            cancellation.is_cancelled,
                        ), self._image_sources.compute.borrow(cancellation.is_cancelled, priority=1):
                            with lease.value.convert("RGB") as image, self._writer.reserve(target) as reservation:
                                result = self._writer.write(
                                    reservation, ext=".bmp", overwrite=True,
                                    encode=lambda temporary: image.save(temporary, "BMP"),
                                    validate=lambda temporary: validate_output(
                                        temporary, ".bmp", size=image.size, has_alpha=False
                                    ),
                                    publication=cancellation.publication,
                                )
                        if not result.success:
                            return result
                        message_key = "wallpaper.error.apply"
                        if self._image_sources.file_stamp(image_path) != stamp:
                            return CoreResult.fail(ErrorCode.SOURCE_CHANGED, "error.source.changed")
                        with cancellation.publication() as valid:
                            if not valid:
                                return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
                            cancellation.mark_published()
                        try:
                            desktop.set_position(position)
                            desktop.set_wallpaper(target)
                        except Exception as error:
                            logger.exception("Windows 壁纸设置失败：%s", image_path)
                            restored = self._restore(desktop, wallpapers, previous_position)
                            return CoreResult.from_exception(
                                error, message_key=("wallpaper.error.apply" if restored
                                                    else "wallpaper.error.restore")
                            )
                        return CoreResult.ok(str(target))
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except CoreOperationError as error:
            logger.exception("壁纸操作未能执行：%s", image_path)
            return CoreResult.from_exception(error)
        except Exception as error:
            logger.exception("壁纸处理失败：%s", image_path)
            return CoreResult.from_exception(error, message_key=message_key)

    @staticmethod
    def _restore(desktop, wallpapers, position):
        restored = True
        for monitor, path in wallpapers:
            try:
                desktop.set_wallpaper(path, monitor)
            except Exception:
                logger.exception("Windows 原壁纸恢复失败：%s", monitor)
                restored = False
        try:
            desktop.set_position(position)
        except Exception:
            logger.exception("Windows 原壁纸样式恢复失败")
            restored = False
        return restored
