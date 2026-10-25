from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from threading import Event
from time import perf_counter

from ..core.image_listing import directory_images, existing_images
from .result_types import CoreResult, ErrorCode
from .image_source_service import path_key


@dataclass(frozen=True)
class BrowseRequest:
    request_id: int
    list_version: int
    selection_revision: int
    paths: tuple[str, ...] = ()
    directory: str | None = None
    append: bool = True
    target: str | None = None
    origin: str = "files"
    cancellation: Event = field(default_factory=Event, compare=False)


@dataclass(frozen=True)
class BrowseListing:
    paths: tuple[str, ...]
    preferred_path: str | None
    elapsed_ms: float
    path_keys: tuple[tuple[str, str], ...] = ()


class BrowseService:
    def prepare(self, request):
        started = perf_counter()
        cancelled = request.cancellation.is_set
        try:
            if request.directory is not None:
                paths = directory_images(request.directory, is_cancelled=cancelled)
                preferred = request.target or (paths[0] if paths else None)
                if request.target and request.target not in paths:
                    if not existing_images((request.target,), is_cancelled=cancelled):
                        return CoreResult.fail(ErrorCode.SOURCE_NOT_FOUND, "error.source.not_found")
                    paths.append(request.target)
                    paths.sort()
            else:
                paths = existing_images(request.paths, is_cancelled=cancelled)
                preferred = paths[0] if paths else None
                if not request.append:
                    paths.sort()
            if cancelled():
                raise CancelledError()
            keys = []
            for path in paths:
                if cancelled():
                    raise CancelledError()
                keys.append((path, path_key(path)))
            return CoreResult.ok(BrowseListing(tuple(paths), preferred, (perf_counter() - started) * 1000, tuple(keys)))
        except CancelledError:
            return CoreResult.fail(ErrorCode.CANCELLED, "error.cancelled")
        except Exception as error:
            return CoreResult.from_exception(error, message_key="browse.failed")
