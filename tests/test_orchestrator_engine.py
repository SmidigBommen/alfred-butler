import json
import unittest

from alfred_tools.orchestrator.engine import (
    ModelReply,
    OrchestrationLimitError,
    Orchestrator,
    Tool,
    ToolCall,
)


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((messages, tools))
        return self.replies.pop(0)


class OrchestratorTests(unittest.TestCase):
    def test_returns_a_direct_model_answer(self):
        model = ScriptedModel([ModelReply(text="Good evening.")])
        orchestrator = Orchestrator(model=model, tools=(), system_prompt="Be Alfred.")

        result = orchestrator.run("Hello")

        self.assertEqual(result.answer, "Good evening.")
        self.assertEqual(
            [message.role for message in result.messages], ["system", "user", "assistant"]
        )
        self.assertEqual(result.tool_events, ())

    def test_executes_a_registered_tool_and_returns_result_to_model(self):
        call = ToolCall(id="call-1", name="memory_search", arguments='{"query":"tea"}')
        model = ScriptedModel(
            [ModelReply(tool_calls=(call,)), ModelReply(text="You prefer Earl Grey.")]
        )
        tool = Tool(
            name="memory_search",
            description="Search local memory",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            permission="read",
            handler=lambda arguments: {"matches": [arguments["query"]]},
        )
        orchestrator = Orchestrator(model=model, tools=(tool,))

        result = orchestrator.run("What tea do I prefer?")

        self.assertEqual(result.answer, "You prefer Earl Grey.")
        self.assertEqual(result.tool_events[0].name, "memory_search")
        self.assertEqual(result.tool_events[0].status, "completed")
        tool_result = model.requests[1][0][-1]
        self.assertEqual(tool_result.role, "tool")
        self.assertEqual(json.loads(tool_result.content), {"matches": ["tea"]})
        self.assertEqual(tool_result.tool_call_id, "call-1")

    def test_external_write_is_not_run_without_approval(self):
        called = []
        call = ToolCall(id="call-1", name="send_email", arguments="{}")
        model = ScriptedModel(
            [ModelReply(tool_calls=(call,)), ModelReply(text="I need your approval first.")]
        )
        tool = Tool(
            name="send_email",
            description="Send email",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            permission="external_write",
            handler=lambda arguments: called.append(arguments),
        )
        orchestrator = Orchestrator(model=model, tools=(tool,))

        result = orchestrator.run("Send it")

        self.assertEqual(called, [])
        self.assertEqual(result.tool_events[0].status, "approval_required")
        refusal = json.loads(model.requests[1][0][-1].content)
        self.assertEqual(refusal["error"], "approval_required")

    def test_invalid_json_arguments_are_reported_to_the_model(self):
        call = ToolCall(id="bad", name="lookup", arguments="{")
        model = ScriptedModel([ModelReply(tool_calls=(call,)), ModelReply(text="Please rephrase.")])
        tool = Tool(
            name="lookup",
            description="Lookup",
            input_schema={"type": "object"},
            permission="read",
            handler=lambda arguments: arguments,
        )

        result = Orchestrator(model=model, tools=(tool,)).run("Lookup this")

        self.assertEqual(result.tool_events[0].status, "invalid_arguments")
        self.assertEqual(json.loads(model.requests[1][0][-1].content)["error"], "invalid_arguments")

    def test_arguments_must_match_the_registered_schema_before_execution(self):
        called = []
        call = ToolCall(id="bad-schema", name="lookup", arguments='{"query":7,"extra":true}')
        model = ScriptedModel(
            [ModelReply(tool_calls=(call,)), ModelReply(text="That call was invalid.")]
        )
        tool = Tool(
            name="lookup",
            description="Lookup",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            permission="read",
            handler=lambda arguments: called.append(arguments),
        )

        result = Orchestrator(model=model, tools=(tool,)).run("Lookup this")

        self.assertEqual(called, [])
        self.assertEqual(result.tool_events[0].status, "invalid_arguments")

    def test_stops_a_model_that_never_finishes_calling_tools(self):
        calls = [
            ModelReply(tool_calls=(ToolCall(str(index), "lookup", "{}"),)) for index in range(3)
        ]
        model = ScriptedModel(calls)
        tool = Tool(
            name="lookup",
            description="Lookup",
            input_schema={"type": "object"},
            permission="read",
            handler=lambda arguments: {},
        )
        orchestrator = Orchestrator(model=model, tools=(tool,), max_tool_rounds=2)

        with self.assertRaisesRegex(OrchestrationLimitError, "tool round limit"):
            orchestrator.run("Loop")


if __name__ == "__main__":
    unittest.main()
