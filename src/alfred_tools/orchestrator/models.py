"""OpenAI-compatible Responses API adapter for local and remote models."""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from alfred_tools.orchestrator.engine import Message, ModelReply, Tool, ToolCall

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RESPONSE_BYTES = 4_000_000


class ModelBackendError(RuntimeError):
    """Raised when a model provider cannot return a valid response."""


class ModelContextError(ModelBackendError):
    """Raised when a model provider rejects an oversized prompt."""


def _http_error_detail(error: urllib.error.HTTPError, *, max_bytes: int) -> str:
    try:
        data = error.read(max_bytes + 1)
    except OSError:
        return ""
    finally:
        error.close()
    if len(data) > max_bytes:
        return ""
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return data.decode("utf-8", errors="replace")[:2_000]
    if isinstance(value, dict):
        detail = value.get("error")
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            return detail["message"][:2_000]
    return ""


def _is_loopback(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _normalize_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise ValueError("model base URL is invalid") from error
    if not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("model base URL is invalid")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and _is_loopback(parsed.hostname)):
        raise ValueError("model base URL must use HTTPS or loopback HTTP")
    return value


class UrllibJSONTransport:
    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        timeout: float,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        request_headers = {"Content-Type": "application/json", **headers}
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(max_response_bytes + 1)
        except TimeoutError as error:
            raise ModelBackendError(f"model request timed out after {timeout:g} seconds") from error
        except urllib.error.HTTPError as error:
            detail = _http_error_detail(error, max_bytes=max_response_bytes)
            normalized = detail.casefold()
            if "exceeded_context_size_error" in normalized or (
                "exceeds" in normalized and "context size" in normalized
            ):
                raise ModelContextError(
                    "model context is full; use New conversation and try again"
                ) from error
            suffix = f": {detail}" if detail else ""
            raise ModelBackendError(f"model request failed: HTTP {error.code}{suffix}") from error
        except (OSError, urllib.error.URLError) as error:
            raise ModelBackendError(f"model request failed: {error}") from error
        if len(data) > max_response_bytes:
            raise ModelBackendError("model response exceeded the configured size limit")
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelBackendError("model returned invalid JSON") from error
        if not isinstance(value, dict):
            raise ModelBackendError("model response must be a JSON object")
        return value


def _messages_input(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role in {"system", "user"}:
            items.append({"role": message.role, "content": message.content})
        elif message.role == "assistant":
            if message.content:
                items.append({"role": "assistant", "content": message.content})
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
        elif message.role == "tool":
            if message.tool_call_id is None:
                raise ValueError("tool messages require a tool call ID")
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
    return items


def _tool_definition(tool: Tool) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
        "strict": True,
    }


def _parse_reply(response: dict[str, Any]) -> ModelReply:
    output = response.get("output", [])
    if not isinstance(output, list):
        raise ModelBackendError("model response output must be a list")
    texts: list[str] = []
    calls: list[ToolCall] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if all(isinstance(value, str) for value in (call_id, name, arguments)):
                calls.append(ToolCall(call_id, name, arguments))
        if item.get("type") == "message":
            content = item.get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    texts.append(part["text"])
    if not texts and not calls:
        error = response.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            raise ModelBackendError(f"model provider error: {error['message']}")
        raise ModelBackendError("model returned neither text nor tool calls")
    return ModelReply(text="\n".join(texts), tool_calls=tuple(calls))


class ResponsesBackend:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        transport: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ):
        self.base_url = _normalize_base_url(base_url)
        self.model = model.strip()
        if not self.model:
            raise ValueError("model name must not be empty")
        if timeout <= 0 or max_response_bytes < 1:
            raise ValueError("model request bounds must be positive")
        self.api_key = api_key
        self.transport = transport or UrllibJSONTransport()
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def complete(self, messages: tuple[Message, ...], tools: tuple[Tool, ...]) -> ModelReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": _messages_input(messages),
            "store": False,
        }
        if tools:
            payload["tools"] = [_tool_definition(tool) for tool in tools]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = self.transport.post_json(
            f"{self.base_url}/responses",
            payload,
            headers,
            timeout=self.timeout,
            max_response_bytes=self.max_response_bytes,
        )
        return _parse_reply(response)
