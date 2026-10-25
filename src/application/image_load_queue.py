from collections import deque
from dataclasses import dataclass, field
from threading import Event
from typing import NamedTuple

from ..core.contracts.image_display import (DEFAULT_NON_RAW_DISPLAY_MODE, DEFAULT_RAW_DISPLAY_MODE,
                                            NonRawDisplayMode, RawDisplayMode)


class ImageLoadKey(NamedTuple):
    path: str
    stamp: tuple[int, int, int]
    size: tuple[int, int] | None
    stage: str = "final"
    raw_display_mode: RawDisplayMode = DEFAULT_RAW_DISPLAY_MODE
    non_raw_display_mode: NonRawDisplayMode = DEFAULT_NON_RAW_DISPLAY_MODE


@dataclass
class ImageLoadRequest:
    request_id: int
    key: tuple
    cancellation: Event = field(default_factory=Event)


class ImageLoadQueue:
    def __init__(self, slots=1, pending_limit=1):
        self.slots, self.pending_limit = slots, pending_limit
        self.active = {}
        self.pending = deque()
        self.generation = 0
        self.closed = False

    def replace(self, keys):
        if self.closed:
            return []
        keys = list(dict.fromkeys(keys))[:self.slots + self.pending_limit]
        for request in self.active.values():
            if request.key not in keys:
                request.cancellation.set()
        for request in self.pending:
            request.cancellation.set()
        self.pending.clear()
        existing = {request.key for request in self.active.values() if not request.cancellation.is_set()}
        for key in keys:
            if key not in existing and len(self.pending) < self.pending_limit + self.slots - len(self.active):
                self.generation += 1
                self.pending.append(ImageLoadRequest(self.generation, key))
        return self._take()

    def _take(self):
        ready = []
        while self.pending and len(self.active) < self.slots and not self.closed:
            request = self.pending.popleft()
            self.active[request.request_id] = request
            ready.append(request)
        return ready

    def finish(self, request):
        self.active.pop(request.request_id, None)
        return self._take()

    def close(self):
        self.replace([])
        self.closed = True
