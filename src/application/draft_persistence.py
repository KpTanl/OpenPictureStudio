from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import tempfile
from time import monotonic

from .result_types import CoreResult, ErrorCode
from .storage_paths import file_lock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DraftSaveRequest:
    version: int
    drafts: dict[str, dict[str, dict]]
    baseline: dict[str, dict[str, dict]]


class DraftPersistenceService:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return CoreResult.ok(self._read())
        except (OSError, ValueError, TypeError) as error:
            logger.exception("草稿读取失败")
            return CoreResult.from_exception(error, message_key="studio.draft_load_failed")

    def _read(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return self._normalize(data)

    @staticmethod
    def _normalize(data):
        if (not isinstance(data, dict) or any(
                not isinstance(path, str) or not isinstance(tools, dict) or any(
                    not isinstance(tool, str) or not isinstance(params, dict)
                    for tool, params in tools.items()) for path, tools in data.items())):
            raise ValueError("草稿数据格式错误")
        data = {path: dict(tools) for path, tools in data.items()}
        from ..core.tools.color import migrate_legacy_color
        from .color_initialization import color_draft_has_state
        for path, tools in tuple(data.items()):
            if "color_adjust" in tools:
                try:
                    original = tools["color_adjust"]
                    migrated = migrate_legacy_color(original, ui=True)
                    if color_draft_has_state(original):
                        tools["color_adjust"] = migrated
                    else:
                        tools.pop("color_adjust")
                        if not tools:
                            data.pop(path)
                except ValueError as error:
                    logger.warning("调色草稿不符合当前参数契约，保留原条目 path=%s error=%s", path, error)
        return json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")))

    def save(self, request: DraftSaveRequest):
        started = monotonic()
        temp_name = None
        try:
            baseline = self._normalize(request.baseline)
            drafts = self._normalize(request.drafts)
            changed = {path for path in baseline.keys() | drafts.keys()
                       if baseline.get(path) != drafts.get(path)}
            with file_lock(self.path.with_suffix(".lock"), blocking=True):
                current = self._read()
                conflicts = [path for path in changed if current.get(path) not in (
                    baseline.get(path), drafts.get(path))]
                if conflicts:
                    return CoreResult.fail("DRAFT_CONFLICT", "studio.draft_conflict", details={"paths": conflicts})
                for path in changed:
                    if path in drafts:
                        current[path] = drafts[path]
                    else:
                        current.pop(path, None)
                fd, temp_name = tempfile.mkstemp(prefix="drafts-", suffix=".tmp", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(current, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.path)
                temp_name = None
            logger.debug("草稿保存 version=%s duration_ms=%.1f", request.version,
                         (monotonic() - started) * 1000)
            return CoreResult.ok(request.version)
        except (OSError, ValueError, TypeError) as error:
            logger.exception("草稿保存失败 version=%s", request.version)
            return CoreResult.from_exception(error, code=ErrorCode.ARTIFACT_WRITE_FAILED,
                                             message_key="studio.draft_save_failed")
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    logger.exception("草稿临时文件清理失败")
