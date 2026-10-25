from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from contextlib import asynccontextmanager

import httpx

from .assistant_models import AssistantConnection, MAX_REPLY_BYTES, endpoint_url
from .assistant_protocols import response_result


@asynccontextmanager
async def _deadline(seconds):
    task = asyncio.current_task()
    expired = False
    def expire():
        nonlocal expired
        expired = True
        task.cancel()
    handle = asyncio.get_running_loop().call_later(seconds, expire)
    try:
        yield
    except asyncio.CancelledError:
        if expired:
            raise TimeoutError() from None
        raise
    finally:
        handle.cancel()


@dataclass
class AssistantRequestError(Exception):
    code: str
    status: int = 0
    parameter: str = ""
    request_id: str = ""

    def public(self) -> dict:
        return {"code": self.code, "status": self.status, "parameter": self.parameter,
                "request_id": self.request_id}


class AssistantRequestService:
    def __init__(self):
        self._clients = {}
        self._users = {}
        self.slots = asyncio.Semaphore(2)

    async def _client(self, connection: AssistantConnection):
        key = (connection.id, connection.revision)
        if key not in self._clients:
            self._clients[key] = httpx.AsyncClient(
                proxy=connection.proxy or None, follow_redirects=False, trust_env=False,
                timeout=httpx.Timeout(connection.first_event_timeout, connect=connection.connect_timeout,
                                      write=connection.connect_timeout, pool=connection.connect_timeout),
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2))
        self._users[key] = self._users.get(key, 0) + 1
        return self._clients[key]

    async def _release(self, connection):
        key = (connection.id, connection.revision)
        self._users[key] -= 1
        for cached in tuple(self._clients):
            if len(self._clients) <= 4:
                break
            if self._users.get(cached, 0) == 0:
                client = self._clients.pop(cached)
                self._users.pop(cached, None)
                await client.aclose()

    @staticmethod
    async def _check_response(response) -> None:
        if 200 <= response.status_code < 300:
            return
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > 8192:
                break
        parameter = ""
        try:
            error = json.loads(raw).get("error", {})
            value = error.get("param", "") if isinstance(error, dict) else ""
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                parameter = value
        except (ValueError, TypeError, AttributeError):
            pass
        request_id = response.headers.get("x-request-id", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{0,120}", request_id):
            request_id = ""
        raise AssistantRequestError("AI_HTTP_" + str(response.status_code), response.status_code,
                                    parameter, request_id)

    async def request(self, connection: AssistantConnection, payload: dict, emit, *, decisions=False) -> dict:
        connection.validate()
        headers = connection.request_headers()
        client = await self._client(connection)
        url = endpoint_url(connection.endpoint, connection.protocol)
        if decisions:
            url = endpoint_url(connection.endpoint, "responses").removesuffix("/responses") + "/decisions"
        started = time.monotonic()
        try:
            async with _deadline(connection.total_timeout):
                async with client.stream("POST", url, headers=headers,
                                         content=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")) as response:
                    await self._check_response(response)
                    if decisions or not payload["stream"]:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > MAX_REPLY_BYTES:
                                raise ValueError("AI_RESPONSE_INVALID")
                        envelope = json.loads(raw)
                        if decisions:
                            if not isinstance(envelope, dict):
                                raise ValueError("AI_RESPONSE_INVALID")
                            return envelope
                        return response_result(envelope, connection.protocol)
                    if "text/event-stream" not in response.headers.get("content-type", "").lower():
                        raise ValueError("AI_STREAM_UNSUPPORTED")
                    return await self._stream(response, connection, emit, started)
        except (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError):
            raise AssistantRequestError("AI_TIMEOUT") from None
        except httpx.HTTPError:
            raise AssistantRequestError("AI_CONNECTION_FAILED") from None
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise AssistantRequestError("AI_RESPONSE_INVALID") from None
        finally:
            await self._release(connection)

    async def _stream(self, response, connection, emit, started) -> dict:
        text, summary, usage = "", "", {}
        total, data = 0, []
        finished, first = False, True
        first_deadline = started + connection.first_event_timeout
        iterator = response.aiter_lines().__aiter__()
        finish_reason = None
        tool_calls, reasoning = {}, ""
        while True:
            timeout = max(0.001, first_deadline - time.monotonic()) if first else connection.idle_timeout
            try:
                line = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
            except StopAsyncIteration:
                break
            total += len(line.encode("utf-8"))
            if total > MAX_REPLY_BYTES:
                raise ValueError("AI_RESPONSE_INVALID")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            if line or not data:
                continue
            chunk, data = "\n".join(data), []
            if chunk == "[DONE]":
                finished = True
                break
            first = False
            event = json.loads(chunk)
            if event.get("error"):
                raise ValueError("AI_REQUEST_FAILED")
            if connection.protocol == "responses":
                kind = event.get("type", "")
                if kind == "response.output_text.delta":
                    delta = event.get("delta", "")
                    text += delta
                    emit("text", delta)
                elif kind == "response.reasoning_summary_text.delta":
                    delta = event.get("delta", "")
                    summary += delta
                    emit("summary", delta)
                elif kind == "response.completed":
                    result = response_result(event["response"], connection.protocol)
                    if not text:
                        emit("text", result["text"])
                    return result
                elif kind in ("response.failed", "response.incomplete", "error"):
                    raise ValueError("AI_RESPONSE_INCOMPLETE")
            else:
                usage = event.get("usage") or usage
                choices = event.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {})
                if delta.get("function_call"):
                    raise ValueError("AI_ACTION_UNSUPPORTED")
                for part in delta.get("tool_calls") or ():
                    index = part.get("index")
                    if type(index) is not int or not 0 <= index < 32:
                        raise ValueError("AI_RESPONSE_INVALID")
                    call = tool_calls.setdefault(index, {"id": "", "type": "function",
                                                        "function": {"name": "", "arguments": ""}})
                    if part.get("id"):
                        call["id"] += part["id"]
                    if part.get("type") and part["type"] != "function":
                        raise ValueError("AI_ACTION_UNSUPPORTED")
                    for key in ("name", "arguments"):
                        value = (part.get("function") or {}).get(key)
                        if value is not None:
                            if not isinstance(value, str):
                                raise ValueError("AI_RESPONSE_INVALID")
                            call["function"][key] += value
                if delta.get("reasoning_content"):
                    if not isinstance(delta["reasoning_content"], str):
                        raise ValueError("AI_RESPONSE_INVALID")
                    reasoning += delta["reasoning_content"]
                if delta.get("refusal"):
                    raise ValueError("AI_REFUSED")
                part = delta.get("content") or ""
                if not isinstance(part, str):
                    raise ValueError("AI_RESPONSE_INVALID")
                if part:
                    text += part
                    emit("text", part)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
        if connection.protocol == "responses" or not finished or finish_reason not in ("stop", "tool_calls"):
            raise ValueError("AI_RESPONSE_INCOMPLETE")
        message = {"role": "assistant", "content": text,
                   "tool_calls": [tool_calls[index] for index in sorted(tool_calls)]}
        if reasoning:
            message["reasoning_content"] = reasoning
        return response_result({"choices": [{"finish_reason": finish_reason, "message": message}],
                                "usage": usage}, connection.protocol)

    async def models(self, connection: AssistantConnection) -> list[dict]:
        connection.validate()
        client = await self._client(connection)
        try:
            async with _deadline(connection.total_timeout):
                async with client.stream("GET", endpoint_url(connection.endpoint, connection.protocol, models=True),
                                         headers=connection.request_headers()) as response:
                    await self._check_response(response)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_REPLY_BYTES:
                            raise ValueError("AI_RESPONSE_INVALID")
                    models = json.loads(raw)["data"]
                    if not isinstance(models, list):
                        raise ValueError("AI_RESPONSE_INVALID")
                    return [item for item in models if isinstance(item, dict) and isinstance(item.get("id"), str)]
        except (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError):
            raise AssistantRequestError("AI_TIMEOUT") from None
        except httpx.HTTPError:
            raise AssistantRequestError("AI_CONNECTION_FAILED") from None
        except (KeyError, TypeError, json.JSONDecodeError):
            raise AssistantRequestError("AI_RESPONSE_INVALID") from None
        finally:
            await self._release(connection)

    async def close(self) -> None:
        await asyncio.gather(*(client.aclose() for client in self._clients.values()))
        self._clients.clear()
