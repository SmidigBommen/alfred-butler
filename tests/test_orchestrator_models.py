import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from alfred_tools.orchestrator.engine import Message, Tool, ToolCall
from alfred_tools.orchestrator.models import (
    ModelBackendError,
    ModelContextError,
    ResponsesBackend,
    UrllibJSONTransport,
)


class RecordingTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post_json(self, url, payload, headers, *, timeout, max_response_bytes):
        self.calls.append((url, payload, headers, timeout, max_response_bytes))
        return self.response


class ResponsesBackendTests(unittest.TestCase):
    def test_context_overflow_returns_a_clear_recovery_error(self):
        payload = {
            "error": {
                "message": (
                    "Engine protocol predict request returned 400: "
                    "exceeded_context_size_error; request exceeds available context size"
                )
            }
        }
        response = urllib.error.HTTPError(
            "http://127.0.0.1:1234/v1/responses",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(json.dumps(payload).encode()),
        )
        with (
            patch("urllib.request.urlopen", side_effect=response),
            self.assertRaisesRegex(ModelContextError, "context is full.*New conversation"),
        ):
            UrllibJSONTransport().post_json(
                "http://127.0.0.1:1234/v1/responses",
                {},
                {},
                timeout=42,
                max_response_bytes=1_000,
            )

    def test_timeout_error_reports_the_configured_deadline(self):
        with (
            patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")),
            self.assertRaisesRegex(ModelBackendError, "timed out after 42 seconds"),
        ):
            UrllibJSONTransport().post_json(
                "http://127.0.0.1:1234/v1/responses",
                {},
                {},
                timeout=42,
                max_response_bytes=1_000,
            )

    def test_sends_non_stored_responses_request_and_parses_text(self):
        transport = RecordingTransport(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "At your service."}],
                    }
                ]
            }
        )
        backend = ResponsesBackend(
            base_url="https://api.openai.com/v1",
            model="configured-model",
            api_key="secret",
            transport=transport,
        )

        reply = backend.complete((Message("user", "Hello"),), ())

        self.assertEqual(reply.text, "At your service.")
        url, payload, headers, timeout, max_bytes = transport.calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/responses")
        self.assertEqual(payload["model"], "configured-model")
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["input"], [{"role": "user", "content": "Hello"}])
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertGreater(timeout, 0)
        self.assertGreater(max_bytes, 0)

    def test_translates_tool_history_and_parses_function_call(self):
        transport = RecordingTransport(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "next-call",
                        "name": "memory_search",
                        "arguments": '{"query":"coffee"}',
                    }
                ]
            }
        )
        backend = ResponsesBackend(
            base_url="http://127.0.0.1:1234/v1",
            model="local-model",
            transport=transport,
        )
        prior_call = ToolCall("prior-call", "memory_search", '{"query":"tea"}')
        messages = (
            Message("system", "Be Alfred"),
            Message("user", "Remember?"),
            Message("assistant", tool_calls=(prior_call,)),
            Message("tool", '{"results":[]}', tool_call_id="prior-call"),
        )
        tool = Tool(
            name="memory_search",
            description="Search memory",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            permission="read",
            handler=lambda arguments: arguments,
        )

        reply = backend.complete(messages, (tool,))

        self.assertEqual(reply.tool_calls[0].id, "next-call")
        payload = transport.calls[0][1]
        self.assertNotIn("Authorization", transport.calls[0][2])
        self.assertEqual(payload["input"][2]["type"], "function_call")
        self.assertEqual(payload["input"][3]["type"], "function_call_output")
        self.assertEqual(payload["tools"][0]["name"], "memory_search")
        self.assertIs(payload["tools"][0]["strict"], True)

    def test_rejects_insecure_remote_provider_url(self):
        with self.assertRaisesRegex(ValueError, "HTTPS or loopback"):
            ResponsesBackend(base_url="http://example.com/v1", model="model")

    def test_requires_a_model_name(self):
        with self.assertRaisesRegex(ValueError, "model"):
            ResponsesBackend(base_url="http://127.0.0.1:1234/v1", model="")


if __name__ == "__main__":
    unittest.main()
