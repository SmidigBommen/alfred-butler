import unittest
from pathlib import Path
from unittest.mock import patch

from alfred_tools.orchestrator.engine import ModelReply, Tool
from alfred_tools.orchestrator.server import (
    ChatService,
    EphemeralSessions,
    configured_audio,
    create_server,
)

ROOT = Path(__file__).resolve().parents[1]


class EchoModel:
    def __init__(self):
        self.tool_names = []

    def complete(self, messages, tools):
        self.tool_names.append([tool.name for tool in tools])
        return ModelReply(text=f"reply: {messages[-1].content}")


class FakeTranscriber:
    def transcribe(self, audio, content_type):
        return {"text": "hello Alfred", "language": "en", "duration": 1.25}


class FakeSynthesizer:
    def synthesize(self, text, *, voice):
        return type(
            "Audio", (), {"body": f"{voice}:{text}".encode(), "content_type": "audio/wav"}
        )()


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
        self.assertEqual(result["session_id"], "session-one")

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
        speech = self.service.synthesize("openai", "Good evening", voice="marin")
        self.assertEqual(result["text"], "hello Alfred")
        self.assertEqual(speech.body, b"marin:Good evening")

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

    def test_rejects_non_loopback_binding(self):
        service = ChatService(backends={"local": EchoModel()}, tools=())
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_server("0.0.0.0", 8123, service)

    def test_interface_implements_the_requested_push_to_talk_chord(self):
        script = (ROOT / "src/alfred_tools/orchestrator/static/app.js").read_text()
        self.assertIn('event.code === "CapsLock" && event.shiftKey', script)
        self.assertIn("MediaRecorder", script)
        self.assertIn("recordingRequested", script)
        self.assertIn("Hold push-to-talk a little longer", script)
        self.assertIn("transcriptionProvider.value", script)
        self.assertIn('fetch("/api/speech"', script)
        self.assertNotIn("localStorage", script)


if __name__ == "__main__":
    unittest.main()
