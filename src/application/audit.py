from __future__ import annotations

import json
import os
import threading
import time
from contextlib import nullcontext
from pathlib import Path


class AuditLog:
    def __init__(self, path: str, *, workspace=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._workspace = workspace

    def append(self, event: str, **fields):
        payload = json.dumps({"time": time.time(), "event": event, **fields},
                             ensure_ascii=False, allow_nan=False) + "\n"
        budget = self._workspace.reserve(len(payload.encode("utf-8"))) if self._workspace else nullcontext()
        with self._lock, budget, self.path.open("a", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
