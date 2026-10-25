"""共享结果与错误契约。

Every Core API call returns a ``CoreResult``. Business branching must use
``CoreError.code`` / ``message_key``, never exception text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class ErrorCode(str, Enum):
    """Stable, machine-readable error codes."""

    INVALID_INPUT = "INVALID_INPUT"
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    DECODE_FAILED = "DECODE_FAILED"
    WORKFLOW_INVALID = "WORKFLOW_INVALID"
    NODE_NOT_REGISTERED = "NODE_NOT_REGISTERED"
    PORT_TYPE_MISMATCH = "PORT_TYPE_MISMATCH"
    CYCLE_DETECTED = "CYCLE_DETECTED"
    PARAMETER_INVALID = "PARAMETER_INVALID"
    ARTIFACT_WRITE_FAILED = "ARTIFACT_WRITE_FAILED"
    MODEL_LOAD_FAILED = "MODEL_LOAD_FAILED"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    REVISION_NOT_FOUND = "REVISION_NOT_FOUND"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    PROJECT_CHANGED = "PROJECT_CHANGED"
    STALE_CANDIDATE = "STALE_CANDIDATE"
    AUDIT_WRITE_FAILED = "AUDIT_WRITE_FAILED"
    PLUGIN_CRASH = "PLUGIN_CRASH"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    MODEL_NOT_INSTALLED = "MODEL_NOT_INSTALLED"
    TOOL_MISSING = "TOOL_MISSING"
    TOOL_DISABLED = "TOOL_DISABLED"
    TOOL_INCOMPATIBLE = "TOOL_INCOMPATIBLE"
    TOOL_RESOURCE_MISSING = "TOOL_RESOURCE_MISSING"
    TOOL_LEGACY_UNPINNED = "TOOL_LEGACY_UNPINNED"


@dataclass(frozen=True)
class CoreError:
    """Structured error payload. UI translates ``message_key``; it never parses
    exception strings to decide a branch."""

    code: str
    message_key: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    cause_id: str | None = None

    def __post_init__(self) -> None:
        # Accept both the enum and its string value.
        if isinstance(self.code, ErrorCode):
            object.__setattr__(self, "code", self.code.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message_key": self.message_key,
            "details": dict(self.details),
            "retryable": self.retryable,
            "cause_id": self.cause_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CoreError":
        return cls(
            code=str(data.get("code", ErrorCode.INTERNAL_ERROR.value)),
            message_key=str(data.get("message_key", "")),
            details=dict(data.get("details", {})),
            retryable=bool(data.get("retryable", False)),
            cause_id=data.get("cause_id"),
        )


@dataclass
class CoreResult(Generic[T]):
    """Uniform result envelope for all Core API calls.

    Invariant: success ⇒ ``error is None``; failure ⇒ ``data is None``.
    """

    success: bool
    data: T | None = None
    error: CoreError | None = None

    def __post_init__(self) -> None:
        if self.success and self.error is not None:
            raise ValueError("CoreResult invariant violated: success with error")
        if not self.success and self.data is not None:
            raise ValueError("CoreResult invariant violated: failure with data")

    @classmethod
    def ok(cls, data: T = None) -> "CoreResult[T]":  # type: ignore[assignment]
        return cls(success=True, data=data)

    @classmethod
    def fail(
        cls,
        code: str | ErrorCode,
        message_key: str = "",
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        cause_id: str | None = None,
    ) -> "CoreResult[T]":
        return cls(
            success=False,
            error=CoreError(
                code=code,
                message_key=message_key,
                details=dict(details or {}),
                retryable=retryable,
                cause_id=cause_id,
            ),
        )

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        code: str | ErrorCode = ErrorCode.INTERNAL_ERROR,
        message_key: str = "error.internal",
        retryable: bool = False,
    ) -> "CoreResult[T]":
        if isinstance(exc, CoreOperationError):
            return cls(success=False, error=exc.error)
        if isinstance(exc, SourceChangedError):
            return cls.fail(ErrorCode.SOURCE_CHANGED, SourceChangedError.MESSAGE_KEY)
        return cls.fail(
            code,
            message_key,
            details={"exception": type(exc).__name__, "message": str(exc)},
            retryable=retryable,
        )

    def unwrap(self) -> T:
        if not self.success or self.data is None:
            raise RuntimeError(
                f"CoreResult failed: {self.error.code if self.error else 'unknown'}"
            )
        return self.data

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error.to_dict() if self.error else None,
        }


MEMORY_BUDGET_KEY = "error.image.memory_budget"
MEMORY_AVAILABLE_KEY = "error.image.memory_available"


def memory_error_key(error) -> str:
    """内存不足时给用户看的文案键：资源池说明了是当前可用内存不足的照此返回，其余按超出预算处理。"""
    return MEMORY_AVAILABLE_KEY if str(error) == MEMORY_AVAILABLE_KEY else MEMORY_BUDGET_KEY


class CoreOperationError(RuntimeError):
    def __init__(self, error: CoreError):
        self.error = error
        super().__init__(error.message_key or error.code)


class SourceChangedError(ValueError):
    """源文件在读取或处理期间被替换、修改或删除后重建。

    属于 ValueError，既有的参数类错误处理仍能接住；经 ``CoreResult.from_exception``
    转换时归为 ``SOURCE_CHANGED``，不会被兜底分支当成解码失败。
    """

    MESSAGE_KEY = "error.source.changed"

    def __init__(self):
        super().__init__(self.MESSAGE_KEY)
