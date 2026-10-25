from __future__ import annotations

import copy
from concurrent.futures import CancelledError
from contextlib import ExitStack
from dataclasses import dataclass
from threading import Event

import numpy as np
from PyQt6.QtGui import QImage

from resources.styles.semantic_colors import OVERLAY_COLORS, color_channels
from ...core.local_color import Fields
from ...core.local_masks import LOCAL_KEYS, mask_weight, prepare_masks


@dataclass
class MaskRangeFrame:
    token: object
    image: QImage | None
    clipped: tuple[str, ...]
    hold: object = None

    def take(self):
        image, hold = self.image, self.hold
        self.image, self.hold = None, None
        return image, hold

    def close(self):
        hold, self.hold = self.hold, None
        self.image = None
        if hold is not None:
            hold.close()


class MaskRangeRunner:
    def __init__(self, runner, sources, ready, failed, *, assets=None):
        self._runner, self._sources = runner, sources
        self._ready, self._failed = ready, failed
        self._active, self._pending = None, None
        self._epoch, self._closed = 0, False
        self._assets = assets

    def request(self, token, params, identity, reference_size, show_range):
        self._epoch += 1
        self._pending = (self._epoch, token, copy.deepcopy(params), identity, reference_size, show_range)
        if self._active is not None:
            self._active.set()
        else:
            self._start()

    def cancel(self):
        self._epoch += 1
        self._pending = None
        if self._active is not None:
            self._active.set()

    def close(self):
        self._closed = True
        self.cancel()

    def _start(self):
        if self._pending is None or self._closed:
            return
        epoch, token, params, identity, reference_size, show_range = self._pending
        self._pending = None
        cancellation = self._active = Event()
        color = color_channels(OVERLAY_COLORS["mask"])
        sources = self._sources

        def worker(_image, runner_cancelled):
            cancelled = lambda: cancellation.is_set() or runner_cancelled()
            if cancelled():
                raise CancelledError()
            width, height = reference_size
            scale = min(1, 1600 / max(width, height))
            width, height = max(1, int(width * scale + .5)), max(1, int(height * scale + .5))
            settings = params
            clipped = set()
            estimate = width * height * 12 + width * min(64, height) * 4 * 40
            hold, image = None, None
            prepared = ExitStack()
            try:
                if settings.get("process_version") == 4:
                    from ...core.mask_components import prepare_components
                    active_ids = {mask.identity for mask in prepare_components(settings.get("masks"))}
                    needed = active_ids | ({identity} if show_range else set())
                    all_masks = prepared.enter_context(self._assets.prepare_size(settings, (height, width), reference_size,
                        cancelled, active_only=False, identities=needed))
                    active = tuple(mask for mask in all_masks if mask.identity in active_ids)
                else:
                    all_masks = prepare_masks(settings.get("masks"), active_only=False)
                    active = prepare_masks(settings.get("masks"))
                selected = next((mask for mask in all_masks if mask.identity == identity), None)
                fields = Fields(settings, active, (height, width), reference_size, cancelled)
                if show_range and selected is not None:
                    hold = sources.resources.hold_memory(width * height * 4, cancelled)
                with sources.resources.working_memory(estimate, cancelled), sources.compute.borrow(cancelled, threads=1):
                    rgba = np.empty((height, width, 4), dtype=np.uint8) if show_range and selected is not None else None
                    for start in range(0, height, 64):
                        if cancelled():
                            raise CancelledError()
                        stop = min(start + 64, height)
                        fields.values(LOCAL_KEYS, start, stop, clipped=clipped)
                        if rgba is not None:
                            weights = mask_weight(selected, (height, width), start, stop, reference_size=reference_size)
                            rgba[start:stop, :, :3] = color[:3]
                            rgba[start:stop, :, 3] = np.rint(weights * np.float32(color[3] * 255)).astype(np.uint8)
                    if rgba is not None:
                        image = QImage(rgba.data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
                        if image.isNull():
                            raise MemoryError()
                    if cancelled():
                        raise CancelledError()
                    return MaskRangeFrame(token, image, tuple(key for key in LOCAL_KEYS if key in clipped), hold)
            except BaseException:
                if hold is not None:
                    hold.close()
                raise
            finally:
                prepared.close()

        def finished(result):
            self._active = None
            if not self._closed and epoch == self._epoch and not cancellation.is_set():
                if result.error is not None:
                    if not isinstance(result.error, CancelledError):
                        self._failed(result.error)
                elif result.value is not None:
                    self._ready(result.value)
            self._start()

        self._runner.submit_work(epoch, None, worker, finished, cancellable=True)
