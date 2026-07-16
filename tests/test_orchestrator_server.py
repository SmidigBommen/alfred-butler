import unittest
from pathlib import Path
from unittest.mock import patch

from alfred_tools.orchestrator.engine import Message, ModelReply, Tool, ToolCall
from alfred_tools.orchestrator.server import (
    SYSTEM_PROMPT,
    ChatService,
    EphemeralSessions,
    configured_audio,
    configured_backends,
    create_server,
)

ROOT = Path(__file__).resolve().parents[1]


class EchoModel:
    def __init__(self):
        self.tool_names = []

    def complete(self, messages, tools):
        self.tool_names.append([tool.name for tool in tools])
        return ModelReply(text=f"reply: {messages[-1].content}")


class OneToolModel:
    def complete(self, messages, tools):
        if messages[-1].role != "tool":
            return ModelReply(
                tool_calls=(
                    ToolCall("research-1", "web_research", '{"queries":["historic ships"]}'),
                )
            )
        return ModelReply(text="Research complete.")


class SessionHistoryModel:
    def __init__(self):
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(messages)
        if messages[-1].role == "tool":
            return ModelReply(text="Games found: https://example.com/games")
        if messages[-1].content == "Find games":
            return ModelReply(
                tool_calls=(ToolCall("research-history", "web_research", '{"queries":[]}'),)
            )
        return ModelReply(text="The previous answer remains available.")


class FakeTranscriber:
    def transcribe(self, audio, content_type):
        return {"text": "hello Alfred", "language": "en", "duration": 1.25}


class FakeSynthesizer:
    def synthesize(self, text):
        return type("Audio", (), {"body": f"cedar:{text}".encode(), "content_type": "audio/wav"})()


class OrchestratorServerTests(unittest.TestCase):
    def setUp(self):
        self.service = ChatService(
            backends={"local": EchoModel(), "remote": EchoModel()},
            tools=(),
            sessions=EphemeralSessions(),
            transcribers={"local": FakeTranscriber(), "openai": FakeTranscriber()},
            synthesizers={"openai": FakeSynthesizer()},
        )

    def test_serves_the_text_interface_and_ephemeral_chat_api(self):
        body = (ROOT / "src/alfred_tools/orchestrator/static/index.html").read_text()
        self.assertIn("Shift+CapsLock", body)
        result = self.service.chat(
            {"session_id": "session-one", "backend": "local", "message": "Good evening"}
        )
        self.assertEqual(result["answer"], "reply: Good evening")
        self.assertEqual(result["answer_blocks"][0]["type"], "paragraph")
        self.assertEqual(result["session_id"], "session-one")

    def test_system_prompt_uses_the_requested_terse_practical_butler_persona(self):
        self.assertIn("tersely by default", SYSTEM_PROMPT)
        self.assertIn("British butler", SYSTEM_PROMPT)
        self.assertIn("practical applications", SYSTEM_PROMPT)
        self.assertIn("concrete next actions", SYSTEM_PROMPT)
        self.assertIn("when the user asks for detail", SYSTEM_PROMPT)
        self.assertIn("without theatrical affectation", SYSTEM_PROMPT)

    def test_does_not_allow_a_session_to_switch_from_local_to_remote(self):
        self.service.chat({"session_id": "private", "backend": "local", "message": "local context"})
        with self.assertRaisesRegex(ValueError, "cannot switch"):
            self.service.chat({"session_id": "private", "backend": "remote", "message": "send it"})

    def test_transcribes_bounded_audio_without_persisting_it(self):
        with self.assertLogs("alfred.audio", level="DEBUG") as logs:
            result = self.service.transcribe("local", b"fake-webm", "audio/webm")
        self.assertEqual(result["text"], "hello Alfred")
        diagnostic = " ".join(logs.output)
        self.assertIn("provider=local", diagnostic)
        self.assertIn("input_bytes=9", diagnostic)
        self.assertIn("output_chars=12", diagnostic)
        self.assertNotIn("hello Alfred", diagnostic)
        self.assertNotIn("fake-webm", diagnostic)

    def test_remote_audio_requires_an_explicit_available_provider(self):
        config = self.service.config()
        self.assertEqual(config["voice_input_providers"], ["local", "openai"])
        self.assertEqual(config["voice_output_providers"], ["browser", "openai"])

        result = self.service.transcribe("openai", b"fake-webm", "audio/webm")
        speech = self.service.synthesize("openai", "Good evening")
        self.assertEqual(result["text"], "hello Alfred")
        self.assertEqual(speech.body, b"cedar:Good evening")

        with self.assertRaisesRegex(ValueError, "voice input provider"):
            self.service.transcribe("missing", b"fake-webm", "audio/webm")

    def test_openai_audio_is_available_only_when_the_server_has_an_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            transcribers, synthesizers = configured_audio(local_enabled=False)
        self.assertEqual(transcribers, {})
        self.assertEqual(synthesizers, {})

        environment = {
            "OPENAI_API_KEY": "server-secret",
            "ALFRED_OPENAI_TRANSCRIBE_MODEL": "custom-stt",
            "ALFRED_OPENAI_TTS_MODEL": "custom-tts",
        }
        with patch.dict("os.environ", environment, clear=True):
            transcribers, synthesizers = configured_audio(local_enabled=False)

        self.assertEqual(transcribers["openai"].model, "custom-stt")
        self.assertEqual(synthesizers["openai"].model, "custom-tts")
        self.assertNotIn("server-secret", str(self.service.config()))

    def test_local_model_has_a_longer_configurable_timeout_than_openai(self):
        environment = {
            "OPENAI_API_KEY": "server-secret",
            "ALFRED_OPENAI_MODEL": "remote-model",
        }
        with patch.dict("os.environ", environment, clear=True):
            backends = configured_backends()

        self.assertEqual(backends["local"].timeout, 600.0)
        self.assertEqual(backends["remote"].timeout, 120.0)

        environment["ALFRED_LMSTUDIO_TIMEOUT"] = "900"
        with patch.dict("os.environ", environment, clear=True):
            configured = configured_backends()
        self.assertEqual(configured["local"].timeout, 900.0)

        environment["ALFRED_LMSTUDIO_TIMEOUT"] = "invalid"
        with (
            patch.dict("os.environ", environment, clear=True),
            self.assertRaisesRegex(ValueError, "ALFRED_LMSTUDIO_TIMEOUT"),
        ):
            configured_backends()

    def test_remote_models_are_not_offered_private_memory_tools(self):
        remote = EchoModel()
        tools = tuple(
            Tool(
                name=name,
                description=name,
                input_schema={"type": "object"},
                permission="read",
                handler=lambda arguments: arguments,
                remote_allowed=not name.startswith("memory_"),
            )
            for name in ("memory_search", "memory_capture", "web_search")
        )
        service = ChatService(backends={"remote": remote}, tools=tools)

        service.chat({"session_id": "remote-only", "backend": "remote", "message": "Hello"})

        self.assertEqual(remote.tool_names[0], ["web_search"])

    def test_chat_response_exposes_only_the_safe_tool_event_summary(self):
        tool = Tool(
            name="web_research",
            description="Research",
            input_schema={"type": "object"},
            permission="network_read",
            handler=lambda arguments: {"text": "full evidence", "sources": ["source"]},
            trace_builder=lambda arguments, output, status: {
                "queries": arguments["queries"],
                "source_count": len(output["sources"]),
            },
        )
        service = ChatService(backends={"local": OneToolModel()}, tools=(tool,))

        result = service.chat(
            {"session_id": "trace-test", "backend": "local", "message": "Find ships"}
        )

        self.assertEqual(result["tools"][0]["name"], "web_research")
        self.assertEqual(result["tools"][0]["status"], "completed")
        self.assertEqual(
            result["tools"][0]["summary"],
            {"queries": ["historic ships"], "source_count": 1},
        )
        self.assertNotIn("full evidence", str(result["tools"]))

    def test_session_history_discards_raw_tool_evidence_after_the_turn(self):
        model = SessionHistoryModel()
        tool = Tool(
            name="web_research",
            description="Research",
            input_schema={"type": "object"},
            permission="network_read",
            handler=lambda arguments: {"text": "raw fetched evidence"},
        )
        service = ChatService(backends={"local": model}, tools=(tool,))

        service.chat({"session_id": "compact", "backend": "local", "message": "Find games"})
        service.chat({"session_id": "compact", "backend": "local", "message": "And next week?"})

        later_request = model.requests[-1]
        self.assertNotIn("raw fetched evidence", str(later_request))
        self.assertNotIn("tool", [message.role for message in later_request])
        self.assertIn("Games found: https://example.com/games", str(later_request))

    def test_session_history_drops_oldest_complete_turns_to_fit_budget(self):
        sessions = EphemeralSessions(max_history_chars=220)
        sessions.history("bounded", "local")
        messages = [Message("system", "Be Alfred.")]
        for number in range(5):
            messages.extend(
                (
                    Message("user", f"question-{number}-" + "q" * 40),
                    Message("assistant", f"answer-{number}-" + "a" * 40),
                )
            )

        sessions.save("bounded", "local", tuple(messages))
        history = sessions.history("bounded", "local")

        serialized = " ".join(message.content for message in history)
        self.assertNotIn("question-0", serialized)
        self.assertIn("question-4", serialized)
        self.assertLessEqual(sum(len(message.content) for message in history), 220)

    def test_rejects_non_loopback_binding(self):
        service = ChatService(backends={"local": EchoModel()}, tools=())
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_server("0.0.0.0", 8123, service)

    def test_interface_implements_the_requested_push_to_talk_chord(self):
        page = (ROOT / "src/alfred_tools/orchestrator/static/index.html").read_text()
        script = (ROOT / "src/alfred_tools/orchestrator/static/app.js").read_text()
        self.assertIn('event.code === "CapsLock" && event.shiftKey', script)
        self.assertIn("MediaRecorder", script)
        self.assertIn("recordingRequested", script)
        self.assertIn("Hold push-to-talk a little longer", script)
        self.assertIn("first response can take several minutes", script)
        self.assertIn("transcriptionProvider.value", script)
        self.assertIn('fetch("/api/speech"', script)
        self.assertIn("OpenAI · cedar · British", script)
        self.assertIn("JSON.stringify({provider: speechOutput.value, text})", script)
        self.assertNotIn('voice: "marin"', script)
        self.assertIn('document.createElement("table")', script)
        self.assertIn("answer_blocks", script)
        self.assertIn("Activity & sources", script)
        self.assertIn("result.tools", script)
        self.assertIn('id="new-session"', page)
        self.assertIn('newSession.addEventListener("click"', script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("localStorage", script)


if __name__ == "__main__":
    unittest.main()
