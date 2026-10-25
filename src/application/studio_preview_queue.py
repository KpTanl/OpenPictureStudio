from dataclasses import dataclass, field, replace
from threading import Event
from time import monotonic

from ..core.mixer_sampling import MixerSamplingRequest
from ..core.contracts.preview import PreviewTarget
from .image_source_service import SourceIdentity, SourcePreviewSpec


@dataclass(frozen=True)
class StudioPreviewContext:
    session_id: str
    source_identity: SourceIdentity
    tool_generation: int
    editing_tool: str | None
    source_preview: SourcePreviewSpec = field(default_factory=SourcePreviewSpec)
    sampling_epoch: int | None = None
    preview_target: PreviewTarget = field(default_factory=PreviewTarget)
    gesture_id: str | None = None
    geometry_generation: int = 0
    interaction_id: str | None = None
    preview_mode: str = "auto"


@dataclass(frozen=True)
class StudioPreviewRequest:
    generation: int
    context: StudioPreviewContext
    session: object
    preview_key: object
    reference_size: tuple[int, int]
    context_cancellation: Event = field(default_factory=Event, compare=False)
    cancellation: Event = field(default_factory=Event, compare=False)
    requested_at: float = field(default_factory=monotonic, compare=False)
    sampling_request: MixerSamplingRequest | None = None
    keep_full_display: bool = False
    temporary_workflow: object = None

    def is_cancelled(self):
        return self.cancellation.is_set() or self.context_cancellation.is_set()


class StudioPreviewQueue:
    def __init__(self):
        self.active = None
        self.pending = None
        self.latest = None
        self._context_cancellation = Event()

    def submit(self, request):
        if self.latest is not None and request.context != self.latest.context:
            self.cancel()
        if self.pending is not None:
            self.pending.cancellation.set()
        request = replace(request, context_cancellation=self._context_cancellation)
        self.pending = self.latest = request
        return request

    def take(self):
        if self.active is not None or self.pending is None:
            return None
        self.active, self.pending = self.pending, None
        return self.active

    def finish(self, request):
        if self.active is request:
            self.active = None

    def accepts(self, request):
        return (self.latest is not None and request.context == self.latest.context
                and not request.is_cancelled())

    def cancel(self):
        for request in (self.active, self.pending, self.latest):
            if request is not None:
                request.cancellation.set()
        self._context_cancellation.set()
        self._context_cancellation = Event()
        self.pending = self.latest = None
