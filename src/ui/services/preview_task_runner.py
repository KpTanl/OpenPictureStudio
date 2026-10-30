"""共享的、可取消的 GUI 线程回调任务执行器。"""
from __future__ import annotations

import logging
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock, Event
from typing import Any, Callable, Optional

from PIL import Image
from PySide6.QtCore import QObject, Qt, Signal, Slot

from ...application.image_source_service import ImageSourceService, SourceBundle

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreviewResult:
    job_id: int
    revision: int
    image: Optional[Image.Image]
    error: Optional[BaseException] = None


@dataclass(frozen=True)
class SourceLoadResult:
    job_id: int
    revision: int
    bundle: Optional[SourceBundle]
    error: Optional[BaseException] = None


@dataclass(frozen=True)
class BackgroundResult:
    job_id: int
    revision: int
    value: Any
    error: Optional[BaseException] = None


class PreviewTaskRunner(QObject):
    """运行图像任务；完成回调在创建执行器的 Qt 线程中调用。"""

    _completed = Signal(object)

    def __init__(self, workers: int = 2):
        super().__init__()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ops-preview")
        self._lock = Lock()
        self._next_id = 0
        self._jobs: dict[int, tuple[Future, Callable[[Any], None], Event]] = {}
        self._closed = False
        self._results = {}
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def submit(
        self,
        revision: int,
        image: Image.Image,
        renderer: Callable[..., Image.Image],
        done: Callable[[PreviewResult], None],
        *, cancellable=False,
    ) -> int:
        source = image.copy()
        cancellation = Event()

        def render() -> Image.Image:
            try:
                if cancellation.is_set():
                    raise CancelledError()
                result = renderer(source, cancellation.is_set) if cancellable else renderer(source)
                if not isinstance(result, Image.Image):
                    raise TypeError("Preview renderer must return a PIL Image")
                return result.copy() if result is source else result
            finally:
                source.close()

        try:
            return self._submit(revision, render, PreviewResult, done, source.close, cancellation=cancellation)
        except Exception:
            source.close()
            raise

    def submit_source(
        self,
        revision: int,
        path: str,
        done: Callable[[SourceLoadResult], None],
        max_side: Optional[int] = None,
        source_service=None,
        proxy_only=False,
        identity=None,
        half_size=False,
    ) -> int:
        cancellation = Event()
        service = source_service or ImageSourceService()
        return self._submit(
            revision,
            lambda: service.load(path, max_side, proxy_only=proxy_only, identity=identity, half_size=half_size,
                                 is_cancelled=cancellation.is_set),
            SourceLoadResult,
            done,
            cancellation=cancellation,
        )

    def submit_work(
        self,
        revision: int,
        image: Optional[Image.Image],
        worker: Callable[[Optional[Image.Image]], Any],
        done: Callable[[BackgroundResult], None],
        *, budget=None, cancellable=False,
    ) -> int:
        source = image.copy() if image is not None else None
        cancellation = Event()

        def run() -> Any:
            try:
                if cancellation.is_set():
                    raise CancelledError()
                if budget is None:
                    result = worker(source, cancellation.is_set) if cancellable else worker(source)
                else:
                    with budget.borrow(cancellation.is_set):
                        result = worker(source, cancellation.is_set) if cancellable else worker(source)
                return result.copy() if source is not None and result is source else result
            finally:
                if source is not None:
                    source.close()

        try:
            return self._submit(revision, run, BackgroundResult, done,
                                source.close if source is not None else None, cancellation=cancellation)
        except Exception:
            if source is not None:
                source.close()
            raise

    def _submit(
        self,
        revision: int,
        work: Callable[[], Any],
        result_type: type,
        done: Callable[[Any], None],
        cancelled: Optional[Callable[[], None]] = None,
        cancellation=None,
    ) -> int:
        with self._lock:
            if self._closed:
                raise RuntimeError("Preview task runner is closed")
            self._next_id += 1
            job_id = self._next_id
            future = self._pool.submit(work)
            self._jobs[job_id] = (future, done, cancellation or Event())

        def finish(completed: Future) -> None:
            try:
                value = completed.result()
            except CancelledError as exc:
                if completed.cancelled() and cancelled is not None:
                    cancelled()
                result = result_type(job_id, revision, None, exc)
            except Exception as exc:
                logger.exception("Background image task failed")
                result = result_type(job_id, revision, None, exc)
            else:
                result = result_type(job_id, revision, value)
            with self._lock:
                active = not self._closed and job_id in self._jobs
                if active:
                    self._results[job_id] = result
            if not active:
                self._dispose(result)
                return
            try:
                self._completed.emit(result)
            except RuntimeError:
                with self._lock:
                    self._jobs.pop(job_id, None)
                    queued = self._results.pop(job_id, None)
                if queued is not None:
                    self._dispose(queued)

        future.add_done_callback(finish)
        return job_id

    @Slot(object)
    def _deliver(self, result: Any) -> None:
        with self._lock:
            queued = self._results.pop(result.job_id, None)
            job = self._jobs.pop(result.job_id, None)
        if queued is None:
            return
        if job is None:
            self._dispose(result)
            return
        try:
            job[1](result)
        except Exception:
            logger.exception("Background image task callback failed")
            if isinstance(result, SourceLoadResult):
                self._dispose(result)
        finally:
            if isinstance(result, (PreviewResult, BackgroundResult)):
                self._dispose(result)

    @staticmethod
    def _dispose(result: Any) -> None:
        value = result.bundle if isinstance(result, SourceLoadResult) else (
            result.image if isinstance(result, PreviewResult) else result.value
        )
        close = getattr(value, "close", None)
        if callable(close):
            close()

    def cancel(self, job_id: int) -> None:
        with self._lock:
            job = self._jobs.pop(job_id, None)
            result = self._results.pop(job_id, None)
        if result is not None:
            self._dispose(result)
        if job is not None:
            job[2].set()
            job[0].cancel()

    def cancel_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            results = list(self._results.values())
            self._jobs.clear()
            self._results.clear()
        for result in results:
            self._dispose(result)
        for future, _, cancellation in jobs:
            cancellation.set()
            future.cancel()

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.cancel_all()
        self._pool.shutdown(wait=False, cancel_futures=True)
