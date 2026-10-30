from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event, Lock
import logging

from PySide6.QtCore import QObject, QSize, Qt, Signal, Slot

logger = logging.getLogger(__name__)


@dataclass
class ScaledDisplay:
    images: dict
    memory: object = None
    pixmaps: dict = field(default_factory=dict)

    def close(self):
        self.images.clear()
        self.pixmaps.clear()
        memory, self.memory = self.memory, None
        if memory is not None:
            memory.close()


@dataclass(eq=False)
class _ScaleJob:
    owner: object
    key: tuple
    images: tuple
    input_memory: object
    output_memory: object
    cancellation: Event = field(default_factory=Event)

    def close(self):
        self.images = ()
        for name in ("input_memory", "output_memory"):
            hold = getattr(self, name)
            if hold is not None:
                setattr(self, name, None)
                hold.close()


class CanvasScaleRunner(QObject):
    ready = Signal(object, object, object)
    _completed = Signal(object)

    def __init__(self, resources, parent=None):
        super().__init__(parent)
        self._resources = resources
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-canvas-scale")
        self._active = self._pending = None
        self._closed = False
        self._lock = Lock()
        self._packets = {}
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def submit(self, owner, key, images):
        if self._closed:
            return False
        if self._active is not None and self._active.owner is owner and self._active.key == key and not self._active.cancellation.is_set():
            return True
        if self._pending is not None:
            self._pending.close()
            self._pending = None
        if self._active is not None:
            self._active.cancellation.set()
        input_memory = output_memory = None
        try:
            input_memory = self._resources.try_hold_memory(sum(image.sizeInBytes() * 2 for _, image, _ in images))
            output_memory = self._resources.try_hold_memory(sum(size.width() * size.height() * 12 for _, _, size in images))
        except (MemoryError, CancelledError):
            if input_memory is not None:
                input_memory.close()
            return False
        job = _ScaleJob(owner, key, tuple(images), input_memory, output_memory)
        if self._active is None:
            self._start(job)
        else:
            self._pending = job
        return True

    def _start(self, job):
        self._active = job
        self._pool.submit(self._run, job)

    def _run(self, job):
        packet = None
        images = {}
        image = scaled = None
        try:
            for identity, image, size in job.images:
                if job.cancellation.is_set():
                    raise CancelledError()
                scaled = image.scaled(QSize(size), Qt.AspectRatioMode.IgnoreAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
                if scaled.isNull():
                    raise MemoryError()
                images[identity] = scaled
            if job.cancellation.is_set():
                raise CancelledError()
            packet = ScaledDisplay(images, job.output_memory)
            job.output_memory = None
        except (CancelledError, MemoryError):
            pass
        except Exception:
            logger.exception("画布缩小失败")
        finally:
            if packet is None:
                images.clear()
            image = scaled = None
            job.close()
        with self._lock:
            if self._closed:
                if packet is not None:
                    packet.close()
                return
            self._packets[job] = packet
        try:
            self._completed.emit(job)
        except RuntimeError:
            with self._lock:
                packet = self._packets.pop(job, None)
            if packet is not None:
                packet.close()

    @Slot(object)
    def _deliver(self, job):
        with self._lock:
            packet = self._packets.pop(job, None)
        if packet is not None:
            if not self._closed and not job.cancellation.is_set():
                self.ready.emit(job.owner, job.key, packet)
            else:
                packet.close()
        elif not self._closed and not job.cancellation.is_set():
            self.ready.emit(job.owner, job.key, None)
        if self._active is job:
            self._active = None
        pending, self._pending = self._pending, None
        if pending is not None and not self._closed:
            self._start(pending)

    def cancel(self, owner):
        if self._active is not None and self._active.owner is owner:
            self._active.cancellation.set()
        if self._pending is not None and self._pending.owner is owner:
            self._pending.close()
            self._pending = None

    def shutdown(self):
        with self._lock:
            self._closed = True
            packets, self._packets = self._packets, {}
        for packet in packets.values():
            if packet is not None:
                packet.close()
        if self._active is not None:
            self._active.cancellation.set()
        if self._pending is not None:
            self._pending.close()
            self._pending = None
        self._pool.shutdown(wait=False)
