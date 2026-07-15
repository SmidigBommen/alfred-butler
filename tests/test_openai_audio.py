import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from alfred_tools.orchestrator.openai_audio import (
    AudioResponse,
    FFmpegAudioNormalizer,
    NormalizedAudio,
    OpenAISpeechSynthesizer,
    OpenAITranscriber,
)


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, body, headers, *, timeout, max_response_bytes):
        self.calls.append((url, body, headers, timeout, max_response_bytes))
        return self.responses.pop(0)


class RecordingNormalizer:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def normalize(self, audio, content_type):
        self.calls.append((audio, content_type))
        return self.result


class OpenAIAudioTests(unittest.TestCase):
    def test_normalizer_uses_fixed_pcm_wav_settings_logs_metadata_and_cleans_up(self):
        command = []
        temporary_paths = []

        def run(arguments, **options):
            command.extend(arguments)
            temporary_paths.extend((Path(arguments[7]), Path(arguments[-1])))
            Path(arguments[-1]).write_bytes(
                b"RIFF" + (b"\0" * 4) + b"WAVE" + (b"normalized" * 1_000)
            )
            self.assertTrue(options["capture_output"])
            self.assertFalse(options["check"])
            self.assertGreater(options["timeout"], 0)
            return SimpleNamespace(returncode=0, stderr=b"")

        with tempfile.TemporaryDirectory() as directory:
            normalizer = FFmpegAudioNormalizer(
                ffmpeg_binary="/usr/bin/ffmpeg",
                runner=run,
                temp_dir=Path(directory),
            )
            with self.assertLogs("alfred.audio", level="DEBUG") as logs:
                result = normalizer.normalize(b"private-webm-bytes", "audio/webm; codecs=opus")

        self.assertEqual(result.content_type, "audio/wav")
        self.assertIn("-ac", command)
        self.assertIn("1", command)
        self.assertIn("-ar", command)
        self.assertIn("16000", command)
        self.assertIn("pcm_s16le", command)
        self.assertTrue(all(not path.exists() for path in temporary_paths))
        combined_logs = " ".join(logs.output)
        self.assertIn("mime=audio/webm", combined_logs)
        self.assertIn("input_bytes=18", combined_logs)
        self.assertNotIn("private-webm-bytes", combined_logs)

    def test_normalizer_rejects_undecodable_recording_without_calling_openai(self):
        transport = RecordingTransport([])

        def reject(*args, **kwargs):
            return SimpleNamespace(returncode=1, stderr=b"invalid media")

        transcriber = OpenAITranscriber(
            api_key="secret",
            transport=transport,
            normalizer=FFmpegAudioNormalizer(
                ffmpeg_binary="/usr/bin/ffmpeg",
                runner=reject,
            ),
        )

        with self.assertRaisesRegex(ValueError, "hold push-to-talk"):
            transcriber.transcribe(b"broken-webm", "audio/webm")

        self.assertEqual(transport.calls, [])

    def test_transcription_uses_bounded_multipart_english_request(self):
        transport = RecordingTransport(
            [AudioResponse(b'{"text":"Hello Alfred."}', "application/json")]
        )
        transcriber = OpenAITranscriber(
            api_key="test-secret",
            transport=transport,
            boundary_factory=lambda: "test-boundary",
            normalizer=RecordingNormalizer(NormalizedAudio(b"RIFF-normalized-audio", "audio/wav")),
        )

        result = transcriber.transcribe(b"webm-audio", "audio/webm")

        self.assertEqual(result, {"text": "Hello Alfred.", "language": "en"})
        url, body, headers, timeout, max_bytes = transport.calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/audio/transcriptions")
        self.assertEqual(headers["Authorization"], "Bearer test-secret")
        self.assertEqual(headers["Content-Type"], "multipart/form-data; boundary=test-boundary")
        self.assertIn(b'name="model"\r\n\r\ngpt-4o-mini-transcribe', body)
        self.assertIn(b'name="language"\r\n\r\nen', body)
        self.assertIn(b'filename="recording.wav"', body)
        self.assertIn(b"Content-Type: audio/wav", body)
        self.assertIn(b"RIFF-normalized-audio", body)
        self.assertNotIn(b"webm-audio", body)
        self.assertNotIn(b"test-secret", body)
        self.assertGreater(timeout, 0)
        self.assertGreater(max_bytes, 0)

    def test_speech_generation_requests_wav_and_returns_bounded_audio(self):
        transport = RecordingTransport([AudioResponse(b"RIFF-audio", "audio/wav")])
        synthesizer = OpenAISpeechSynthesizer(api_key="test-secret", transport=transport)

        result = synthesizer.synthesize("Good evening.")

        self.assertEqual(result.body, b"RIFF-audio")
        self.assertEqual(result.content_type, "audio/wav")
        url, body, headers, _, _ = transport.calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/audio/speech")
        self.assertEqual(headers["Authorization"], "Bearer test-secret")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(
            json.loads(body),
            {
                "model": "gpt-4o-mini-tts",
                "voice": "cedar",
                "input": "Good evening.",
                "instructions": (
                    "Speak in a warm, composed masculine voice with a natural modern British "
                    "English accent, medium-low pitch, measured pace, and understated delivery. "
                    "Avoid an exaggerated theatrical accent."
                ),
                "response_format": "wav",
            },
        )

    def test_rejects_unsupported_audio_before_network(self):
        transport = RecordingTransport([])
        transcriber = OpenAITranscriber(api_key="secret", transport=transport)

        with self.assertRaisesRegex(ValueError, "content type"):
            transcriber.transcribe(b"audio", "application/octet-stream")

        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
