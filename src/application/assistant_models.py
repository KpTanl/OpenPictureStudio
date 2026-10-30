from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit
from .assistant_images import MIN_REGION_SIDE, image_capability, image_sizing_rule

MODES = ("agent", "plan")
PROTOCOLS = ("chat_completions", "responses")
MAX_REPLY_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_CONVERSATION_ATTACHMENT_BYTES = 512 * 1024 * 1024
MAX_QUEUED_REQUESTS = 16
# 发送给模型的照片：来源 → (长边像素, JPEG 质量)。
IMAGE_SOURCES = {"current": (1536, 90), "original": (2048, 92)}
LOOP_MAX_ROUNDS = 5


def new_id() -> str:
    return uuid.uuid4().hex


def builtin_reasoning(provider: str, model: str) -> dict:
    if provider == "deepseek" and model in ("deepseek-flash", "deepseek-v4-pro"):
        return {"reasoning": "deepseek", "efforts": ("none", "low", "high", "max"),
                "tool_calling": True, "capability_source": "builtin"}
    if provider == "deepseek" and model == "deepseek-v4-flash-vision-exp":
        # 实验性视觉模型，按第三方整理的官方资料：接受 base64 图片。
        return {"image_input": True, "capability_source": "builtin"}
    return {}


def endpoint_url(endpoint: str, protocol: str, *, models: bool = False) -> str:
    try:
        parts = urlsplit(endpoint.strip().rstrip("/"))
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError("AI_ENDPOINT_INVALID") from None
    if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            or parts.scheme not in ("https", "http")):
        raise ValueError("AI_ENDPOINT_INVALID")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("AI_HTTPS_REQUIRED")
    if protocol not in PROTOCOLS:
        raise ValueError("AI_CONFIGURATION_INVALID")
    path = parts.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/models", "/decisions"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    path += "/models" if models else "/responses" if protocol == "responses" else "/chat/completions"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


@dataclass(frozen=True)
class AssistantConnection:
    id: str = field(default_factory=new_id)
    name: str = ""
    endpoint: str = ""
    protocol: str = "chat_completions"
    provider: str = "generic"
    authentication: str = "bearer"
    api_key: str = field(default="", repr=False)
    key_header: str = "api-key"
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    proxy: str = field(default="", repr=False)
    connect_timeout: int = 10
    first_event_timeout: int = 120
    idle_timeout: int = 45
    total_timeout: int = 300
    revision: int = 1

    def validate(self) -> None:
        endpoint_url(self.endpoint, self.protocol)
        if not self.name.strip() or self.authentication not in ("bearer", "header", "none"):
            raise ValueError("AI_CONFIGURATION_INVALID")
        if self.provider not in ("generic", "openai", "deepseek"):
            raise ValueError("AI_CONFIGURATION_INVALID")
        if any(type(value) is not int or not 1 <= value <= 3600 for value in (
                self.connect_timeout, self.first_event_timeout, self.idle_timeout, self.total_timeout)):
            raise ValueError("AI_CONFIGURATION_INVALID")
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", self.key_header):
            raise ValueError("AI_HEADERS_INVALID")
        if not isinstance(self.headers, dict):
            raise ValueError("AI_HEADERS_INVALID")
        for key, value in self.headers.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                    or not isinstance(value, str) or "\r" in value or "\n" in value
                    or key.lower() in ("host", "content-length", "content-type", "authorization", self.key_header.lower())):
                raise ValueError("AI_HEADERS_INVALID")
        if "\r" in self.api_key or "\n" in self.api_key:
            raise ValueError("AI_HEADERS_INVALID")
        if self.proxy:
            try:
                proxy = urlsplit(self.proxy)
                if proxy.port is not None and not 1 <= proxy.port <= 65535:
                    raise ValueError()
            except ValueError:
                raise ValueError("AI_PROXY_INVALID") from None
            if proxy.scheme not in ("http", "https") or not proxy.hostname or proxy.query or proxy.fragment:
                raise ValueError("AI_PROXY_INVALID")

    def request_headers(self) -> dict[str, str]:
        headers = {**self.headers, "Content-Type": "application/json"}
        if self.authentication != "none" and not self.api_key:
            raise ValueError("AI_KEY_MISSING")
        if self.authentication == "bearer":
            headers["Authorization"] = "Bearer " + self.api_key
        elif self.authentication == "header":
            headers[self.key_header] = self.api_key
        return headers

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "protocol": self.protocol, "provider": self.provider,
                "host": urlsplit(self.endpoint).hostname or "", "revision": self.revision}


@dataclass(frozen=True)
class AssistantModel:
    id: str = field(default_factory=new_id)
    connection_id: str = ""
    model: str = ""
    name: str = ""
    image_input: bool = False
    image_effective_long_edge: int = 0
    image_detail: str = ""
    tool_calling: bool = False
    structured_output: str = "none"
    streaming: bool = True
    stream_usage: bool = False
    reasoning: str = "none"
    efforts: tuple[str, ...] = ()
    reasoning_field: str = "reasoning_effort"
    reasoning_summary: bool = False
    default_effort: str = ""
    context_window: int = 200_000
    max_output_tokens: int = 128_000
    reasoning_reserve: int = 0
    output_field: str = "max_tokens"
    temperature: float | None = None
    top_p: float | None = None
    sampling_supported: bool = True
    extra_fields: dict = field(default_factory=dict, repr=False)
    capability_source: str = "manual"

    @property
    def effective_image_long_edge(self):
        return image_capability(self)["effective_long_edge"]

    @classmethod
    def from_dict(cls, values: dict) -> "AssistantModel":
        values = dict(values)
        values["efforts"] = tuple(values.get("efforts", ()))
        return cls(**values)

    def validate(self) -> None:
        if not self.model.strip() or not self.connection_id:
            raise ValueError("AI_MODEL_MISSING")
        if type(self.tool_calling) is not bool:
            raise ValueError("AI_CONFIGURATION_INVALID")
        if (type(self.image_effective_long_edge) is not int or not 0 <= self.image_effective_long_edge <= 65535
                or self.image_effective_long_edge and self.image_effective_long_edge < MIN_REGION_SIDE
                or self.image_detail not in ("", "auto", "low", "high", "original")):
            raise ValueError("AI_CONFIGURATION_INVALID")
        image_sizing_rule(self)
        if self.structured_output not in ("none", "json", "json_schema") or self.reasoning not in (
                "none", "effort", "deepseek", "custom"):
            raise ValueError("AI_CONFIGURATION_INVALID")
        if (len(set(self.efforts)) != len(self.efforts) or len(self.efforts) > 12
                or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) for value in self.efforts)
                or self.default_effort and self.default_effort not in self.efforts):
            raise ValueError("AI_EFFORT_INVALID")
        if self.reasoning == "deepseek" and set(self.efforts) - {"none", "low", "high", "max"}:
            raise ValueError("AI_EFFORT_INVALID")
        if (self.reasoning == "deepseek" and self.default_effort != "none"
                and self.top_p is not None and self.top_p < 0.95):
            raise ValueError("AI_TOP_P_INVALID")
        if self.output_field not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("AI_CONFIGURATION_INVALID")
        if (type(self.max_output_tokens) is not int or not 16 <= self.max_output_tokens <= 1_000_000
                or type(self.context_window) is not int or not 0 <= self.context_window <= 10_000_000
                or type(self.reasoning_reserve) is not int or not 0 <= self.reasoning_reserve <= 1_000_000):
            raise ValueError("AI_CONFIGURATION_INVALID")
        if self.context_window and self.max_output_tokens + self.reasoning_reserve >= self.context_window:
            raise ValueError("AI_CONTEXT_BUDGET")
        for value, upper in ((self.temperature, 2), (self.top_p, 1)):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= upper):
                raise ValueError("AI_CONFIGURATION_INVALID")
        if (not isinstance(self.extra_fields, dict) or set(self.extra_fields) & {
                "model", "messages", "input", "instructions", "tools", "tool_choice", "stream", "stream_options",
                "store", "previous_response_id", "conversation", "include", "metadata", "reasoning", "thinking",
                "reasoning_effort", "text", "response_format", "max_tokens", "max_completion_tokens",
                "max_output_tokens", "temperature", "top_p"}):
            raise ValueError("AI_FIELDS_INVALID")
        try:
            json.dumps(self.extra_fields, allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError("AI_FIELDS_INVALID") from None
        if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", self.reasoning_field)
                or self.reasoning_field.split(".")[0] in {"model", "messages", "input", "tools", "tool_choice", "instructions",
                    "store", "stream", "conversation", "previous_response_id", "metadata", "include", "response_format", "text",
                    "temperature", "top_p", "max_tokens", "max_completion_tokens", "max_output_tokens"}):
            raise ValueError("AI_FIELDS_INVALID")

    def public(self) -> dict:
        values = asdict(self)
        values.pop("extra_fields")
        return values


@dataclass(frozen=True)
class AssistantContext:
    session_id: str
    state_version: int
    color: Mapping[str, Any]
    workflow: Any
    object_key: str = ""
    photo_name: str = ""
    document_id: str = ""
    source_hash: str = ""
    revision_id: str = ""
    width: int = 0
    height: int = 0
    beauty: Mapping[str, Any] = field(default_factory=dict)
    # AI 可调工具 → 不可用的原因键；空字符串表示可用。调色始终可用，不列出。
    tools: Mapping[str, str] = field(default_factory=dict)

    def identity(self) -> dict:
        return {"object_key": self.object_key, "photo_name": self.photo_name, "document_id": self.document_id,
                "source_hash": self.source_hash, "revision_id": self.revision_id, "session_id": self.session_id,
                "state_version": self.state_version, "workflow_hash": self.workflow.workflow_hash(),
                "width": self.width, "height": self.height}


def context_matches(photo: AssistantContext, session) -> bool:
    return (session.state_version == photo.state_version
            and session.current_revision.revision_id == photo.revision_id
            and session.draft_workflow.workflow_hash() == photo.workflow.workflow_hash())


class AssistantHost(Protocol):
    def assistant_context(self) -> AssistantContext | None: ...
    def assistant_candidate(self, context: AssistantContext, adjustments: Mapping[str, Mapping[str, Any]]): ...
    def assistant_candidate_builder(self, context: AssistantContext): ...
    def assistant_revision_labels(self): ...
    def stop_preview(self) -> None: ...
    def assistant_binding(self) -> tuple[str, str]: ...
    def assistant_history_impact(self, affected) -> str: ...


def context_defaults() -> dict:
    return {"history": True, "color": True, "workflow": False, "image": True,
            "image_source": "current", "analysis": True, "requirements_enabled": True, "requirements": "",
            "summary_enabled": True}
