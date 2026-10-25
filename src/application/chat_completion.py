from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from io import BytesIO

from .assistant_models import AssistantConnection, AssistantContext, AssistantModel, context_defaults, endpoint_url
from .assistant_tools import COLOR, parse_proposal
from .assistant_context import AssistantContextBuilder
from .assistant_protocols import build_payload
from .assistant_request import AssistantRequestError, AssistantRequestService
from ..core.contracts.workflow import WorkflowGraph

PARAMETER_RANGES = dict(COLOR.ranges)

__all__ = ["ModelSettings", "PARAMETER_RANGES", "suggest_adjustments"]


@dataclass(frozen=True)
class ModelSettings:
    endpoint: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    send_image: bool = False

    def completion_url(self):
        url = endpoint_url(self.endpoint, "chat_completions")
        if not self.model.strip():
            raise ValueError("AI_MODEL_MISSING")
        return url


def suggest_adjustments(settings, prompt, current, image=None, *, is_cancelled=lambda: False,
                        audit=None, request_id=None):
    from .assistant_models import new_id
    request_id = request_id or new_id()
    settings.completion_url()
    if is_cancelled():
        raise ValueError("AI_CANCELLED")
    attached = bool(settings.send_image and image is not None)
    connection = AssistantConnection(name="API", endpoint=settings.endpoint, api_key=settings.api_key,
                                     authentication="bearer" if settings.api_key else "none",
                                     first_event_timeout=45, idle_timeout=45, total_timeout=45)
    model = AssistantModel(connection_id=connection.id, model=settings.model, name=settings.model,
                           image_input=attached, streaming=False, max_output_tokens=1000)
    photo = AssistantContext("", 0, current, WorkflowGraph())
    preferences = {"context": {**context_defaults(), "image": attached}, "context_start": ""}
    context = AssistantContextBuilder().build([{"id": request_id, "role": "user", "text": prompt, "status": "completed"}],
                                              preferences, model, photo, language="zh-CN", mode="edit",
                                              image_attached=attached)
    images = {}
    if attached:
        preview = image.convert("RGB")
        preview.thumbnail((1024, 1024))
        buffer = BytesIO()
        preview.save(buffer, format="JPEG", quality=85)
        images[request_id] = [buffer.getvalue()]
    payload = build_payload(connection, model, context, mode="edit", images=images)
    if audit:
        audit("agent.request", request_id=request_id, model=model.model,
              endpoint_host=connection.public()["host"], send_image=attached, has_prompt=bool(prompt))

    async def request():
        service = AssistantRequestService()
        task = asyncio.create_task(service.request(connection, payload, lambda _kind, _delta: None))
        try:
            while not task.done():
                if is_cancelled():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise ValueError("AI_CANCELLED")
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
            proposal = parse_proposal((await task)["text"])
            return {"summary": proposal["summary"], "adjustments": proposal["adjustments"].get("color", {})}
        except AssistantRequestError as exc:
            raise ValueError(exc.code) from None
        finally:
            await service.close()

    try:
        result = asyncio.run(request())
    except ValueError as exc:
        if audit:
            audit("agent.result", request_id=request_id, success=False, error=str(exc))
        raise
    if audit:
        audit("agent.result", request_id=request_id, success=True, has_adjustments=bool(result["adjustments"]))
    return result
