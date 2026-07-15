"""Provider-independent, bounded tool-calling loop."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Permission = Literal["read", "network_read", "local_write", "external_write", "destructive"]
AUTOMATIC_PERMISSIONS = frozenset({"read", "network_read", "local_write"})


class OrchestrationLimitError(RuntimeError):
    """Raised when a model exceeds a configured orchestration bound."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ModelReply:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


class ModelBackend(Protocol):
    def complete(self, messages: tuple[Message, ...], tools: tuple[Tool, ...]) -> ModelReply: ...


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    permission: Permission
    handler: Callable[[dict[str, Any]], Any] = field(repr=False, compare=False)
    remote_allowed: bool = True


@dataclass(frozen=True, slots=True)
class ToolEvent:
    call_id: str
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class TurnResult:
    answer: str
    messages: tuple[Message, ...]
    tool_events: tuple[ToolEvent, ...]


def _json_result(value: Any, *, max_chars: int) -> str:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(serialized) <= max_chars:
        return serialized
    return json.dumps(
        {
            "error": "tool_output_too_large",
            "message": f"tool output exceeded {max_chars} characters",
        },
        separators=(",", ":"),
    )


def _validate_schema(value: Any, schema: dict[str, Any], path: str = "arguments") -> None:
    expected = schema.get("type")
    valid_type = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)
    if not valid_type:
        raise ValueError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed value")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = [name for name in schema.get("required", []) if name not in value]
        if missing:
            raise ValueError(f"{path} is missing required field: {missing[0]}")
        if schema.get("additionalProperties") is False:
            extra = [name for name in value if name not in properties]
            if extra:
                raise ValueError(f"{path} has unknown field: {extra[0]}")
        for name, item in value.items():
            item_schema = properties.get(name)
            if isinstance(item_schema, dict):
                _validate_schema(item, item_schema, f"{path}.{name}")
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            raise ValueError(f"{path} has too few items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ValueError(f"{path} has too many items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema(item, item_schema, f"{path}[{index}]")
    if isinstance(value, str) and "maxLength" in schema and len(value) > int(schema["maxLength"]):
        raise ValueError(f"{path} is too long")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} is above the maximum")


class Orchestrator:
    def __init__(
        self,
        *,
        model: ModelBackend,
        tools: tuple[Tool, ...],
        system_prompt: str = "You are Alfred, a careful local AI assistant.",
        max_tool_rounds: int = 6,
        max_tool_calls: int = 12,
        max_tool_argument_chars: int = 20_000,
        max_tool_output_chars: int = 50_000,
        approved_permissions: frozenset[Permission] = AUTOMATIC_PERMISSIONS,
    ):
        if (
            max_tool_rounds < 0
            or max_tool_calls < 0
            or max_tool_argument_chars < 100
            or max_tool_output_chars < 100
        ):
            raise ValueError(
                "orchestration bounds must be non-negative and tool output at least 100"
            )
        duplicate_names = len({tool.name for tool in tools}) != len(tools)
        if duplicate_names:
            raise ValueError("tool names must be unique")
        self.model = model
        self.tools = tools
        self.system_prompt = system_prompt.strip()
        self.max_tool_rounds = max_tool_rounds
        self.max_tool_calls = max_tool_calls
        self.max_tool_argument_chars = max_tool_argument_chars
        self.max_tool_output_chars = max_tool_output_chars
        self.approved_permissions = approved_permissions

    def run(self, user_message: str, *, prior_messages: tuple[Message, ...] = ()) -> TurnResult:
        user_message = user_message.strip()
        if not user_message:
            raise ValueError("message must not be empty")
        messages = list(prior_messages)
        if not messages:
            messages.append(Message(role="system", content=self.system_prompt))
        messages.append(Message(role="user", content=user_message))
        events: list[ToolEvent] = []
        tool_count = 0
        tool_rounds = 0
        tools_by_name = {tool.name: tool for tool in self.tools}

        while True:
            reply = self.model.complete(tuple(messages), self.tools)
            messages.append(
                Message(role="assistant", content=reply.text, tool_calls=reply.tool_calls)
            )
            if not reply.tool_calls:
                return TurnResult(reply.text, tuple(messages), tuple(events))
            if tool_rounds >= self.max_tool_rounds:
                raise OrchestrationLimitError("tool round limit exceeded")
            tool_rounds += 1

            for call in reply.tool_calls:
                tool_count += 1
                if tool_count > self.max_tool_calls:
                    raise OrchestrationLimitError("tool call limit exceeded")
                tool = tools_by_name.get(call.name)
                if tool is None:
                    status = "unknown_tool"
                    output = {"error": status, "tool": call.name}
                elif tool.permission not in self.approved_permissions:
                    status = "approval_required"
                    output = {
                        "error": status,
                        "tool": call.name,
                        "permission": tool.permission,
                    }
                else:
                    try:
                        if len(call.arguments) > self.max_tool_argument_chars:
                            raise ValueError("arguments exceeded the configured size limit")
                        arguments = json.loads(call.arguments)
                        if not isinstance(arguments, dict):
                            raise ValueError("arguments must be a JSON object")
                        _validate_schema(arguments, tool.input_schema)
                    except (json.JSONDecodeError, ValueError) as error:
                        status = "invalid_arguments"
                        output = {"error": status, "message": str(error)}
                    else:
                        try:
                            output = tool.handler(arguments)
                            status = "completed"
                        except Exception as error:  # tool failures are data for the model
                            status = "failed"
                            output = {
                                "error": "tool_failed",
                                "tool": call.name,
                                "message": str(error),
                            }
                events.append(ToolEvent(call.id, call.name, status))
                messages.append(
                    Message(
                        role="tool",
                        content=_json_result(output, max_chars=self.max_tool_output_chars),
                        tool_call_id=call.id,
                    )
                )
