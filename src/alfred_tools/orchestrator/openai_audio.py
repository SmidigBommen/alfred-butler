"""Bounded OpenAI request adapters for explicit remote speech input and output."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alfred_tools.orchestrator.speech import SpeechBackendError

OPENAI_AUDIO_BASE_URL = "https://api.openai.com/v1/audio"
DEFAULT_TIMEOUT = 120.0
MAX_TRANSCRIPT_BYTES = 1_000_000
MAX_SPEECH_BYTES = 25_000_000
MAX_SPEECH_INPUT_CHARS = 4_096
MAX_AUDIO_INPUT_BYTES = 12_000_000
MAX_NORMALIZED_AUDIO_BYTES = 10_000_000
MIN_NORMALIZED_AUDIO_BYTES = 8_000
NORMALIZATION_TIMEOUT = 30.0
NORMALIZED_AUDIO_SECONDS = 300
OPENAI_SPEECH_VOICE = "cedar"
OPENAI_SPEECH_INSTRUCTIONS = (
    "Speak in a warm, composed masculine voice with a natural modern British English accent, "
    "medium-low pitch, measured pace, and understated delivery. Avoid an exaggerated theatrical "
    "accent."
)
AUDIO_EXTENSIONS = {
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/webm": "webm",
}
LOGGER = logging.getLogger("alfred.audio")


@dataclass(frozen=True, slots=True)
class AudioResponse:
    body: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class NormalizedAudio:
    body: bytes
    content_type: str


class FFmpegAudioNormalizer:
    """Decode browser media into a bounded, predictable PCM WAV upload."""

    def __init__(
        self,
        *,
        ffmpeg_binary: str | None = None,
        runner: Callable[..., Any] | None = None,
        temp_dir: Path | None = None,
    ):
        self.ffmpeg_binary = ffmpeg_binary
        self.runner = runner or subprocess.run
        self.temp_dir = temp_dir

    def normalize(self, audio: bytes, content_type: str) -> NormalizedAudio:
        media_type = content_type.partition(";")[0].strip().lower()
        extension = AUDIO_EXTENSIONS.get(media_type)
        if extension is None:
            raise ValueError("unsupported audio content type")
        if not 1 <= len(audio) <= MAX_AUDIO_INPUT_BYTES:
            raise ValueError("audio size is invalid")
        ffmpeg = self.ffmpeg_binary or shutil.which("ffmpeg")
        if not ffmpeg:
            raise SpeechBackendError("ffmpeg is required for remote microphone transcription")

        LOGGER.debug("normalizing audio mime=%s input_bytes=%d", media_type, len(audio))
        with tempfile.TemporaryDirectory(prefix="alfred-audio-", dir=self.temp_dir) as directory:
            source = Path(directory) / f"recording.{extension}"
            output = Path(directory) / "recording.wav"
            source.write_bytes(audio)
            try:
                result = self.runner(
                    [
                        ffmpeg,
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(source),
                        "-t",
                        str(NORMALIZED_AUDIO_SECONDS),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-c:a",
                        "pcm_s16le",
                        str(output),
                    ],
                    capture_output=True,
                    check=False,
                    timeout=NORMALIZATION_TIMEOUT,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                LOGGER.debug("audio decoder execution failed error=%s", type(error).__name__)
                raise ValueError(_invalid_recording_message()) from error

            stderr = result.stderr.decode("utf-8", errors="replace")[-500:].strip()
            if result.returncode != 0:
                LOGGER.debug(
                    "audio decoder rejected recording exit_code=%d detail=%r",
                    result.returncode,
                    stderr,
                )
                raise ValueError(_invalid_recording_message())
            try:
                normalized = output.read_bytes()
            except OSError as error:
                LOGGER.debug("audio decoder produced no readable output")
                raise ValueError(_invalid_recording_message()) from error

        if (
            not MIN_NORMALIZED_AUDIO_BYTES <= len(normalized) <= MAX_NORMALIZED_AUDIO_BYTES
            or normalized[:4] != b"RIFF"
            or normalized[8:12] != b"WAVE"
        ):
            LOGGER.debug("audio normalization rejected output_bytes=%d", len(normalized))
            raise ValueError(_invalid_recording_message())
        LOGGER.debug("audio normalized output_mime=audio/wav output_bytes=%d", len(normalized))
        return NormalizedAudio(normalized, "audio/wav")


def _invalid_recording_message() -> str:
    return "Recording was too short or could not be decoded; hold push-to-talk a little longer."


class UrllibAudioTransport:
    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        *,
        timeout: float,
        max_response_bytes: int,
    ) -> AudioResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = response.read(max_response_bytes + 1)
                content_type = response.headers.get("Content-Type", "").partition(";")[0].lower()
        except urllib.error.HTTPError as error:
            detail = error.read(16_384)
            message = _remote_error_message(detail) or f"HTTP {error.code}"
            raise SpeechBackendError(f"OpenAI audio request failed: {message}") from error
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise SpeechBackendError(f"could not reach OpenAI audio API: {error}") from error
        if len(result) > max_response_bytes:
            raise SpeechBackendError("OpenAI audio response exceeded the configured size limit")
        return AudioResponse(result, content_type)


def _remote_error_message(body: bytes) -> str | None:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("error"), dict):
        return None
    message = value["error"].get("message")
    return str(message)[:500] if message else None


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    ).encode()


def _multipart_audio(boundary: str, audio: bytes, content_type: str, extension: str) -> bytes:
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="recording.{extension}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    return header + audio + b"\r\n"


class OpenAITranscriber:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o-mini-transcribe",
        transport: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        boundary_factory: Callable[[], str] | None = None,
        normalizer: Any | None = None,
    ):
        if not api_key or not model.strip():
            raise ValueError("OpenAI API key and transcription model are required")
        self.api_key = api_key
        self.model = model.strip()
        self.transport = transport or UrllibAudioTransport()
        self.timeout = timeout
        self.boundary_factory = boundary_factory or (lambda: uuid.uuid4().hex)
        self.normalizer = normalizer or FFmpegAudioNormalizer()

    def transcribe(self, audio: bytes, content_type: str) -> dict[str, Any]:
        normalized = self.normalizer.normalize(audio, content_type)
        media_type = normalized.content_type
        extension = AUDIO_EXTENSIONS.get(media_type)
        if extension is None:  # pragma: no cover - protects the injected adapter contract
            raise ValueError("audio normalizer returned an unsupported content type")
        boundary = self.boundary_factory()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", boundary):
            raise ValueError("multipart boundary is invalid")
        body = b"".join(
            (
                _multipart_field(boundary, "model", self.model),
                _multipart_field(boundary, "language", "en"),
                _multipart_field(boundary, "response_format", "json"),
                _multipart_audio(boundary, normalized.body, media_type, extension),
                f"--{boundary}--\r\n".encode(),
            )
        )
        response = self.transport.post(
            f"{OPENAI_AUDIO_BASE_URL}/transcriptions",
            body,
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            timeout=self.timeout,
            max_response_bytes=MAX_TRANSCRIPT_BYTES,
        )
        try:
            value = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SpeechBackendError("OpenAI returned an invalid transcription response") from error
        text = value.get("text") if isinstance(value, dict) else None
        if not isinstance(text, str):
            raise SpeechBackendError("OpenAI transcription response did not contain text")
        return {"text": text.strip(), "language": "en"}


class OpenAISpeechSynthesizer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o-mini-tts",
        transport: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        if not api_key or not model.strip():
            raise ValueError("OpenAI API key and speech model are required")
        self.api_key = api_key
        self.model = model.strip()
        self.transport = transport or UrllibAudioTransport()
        self.timeout = timeout

    def synthesize(self, text: str) -> AudioResponse:
        text = text.strip()
        if not text or len(text) > MAX_SPEECH_INPUT_CHARS:
            raise ValueError(
                f"speech text must contain between 1 and {MAX_SPEECH_INPUT_CHARS} characters"
            )
        body = json.dumps(
            {
                "model": self.model,
                "voice": OPENAI_SPEECH_VOICE,
                "input": text,
                "instructions": OPENAI_SPEECH_INSTRUCTIONS,
                "response_format": "wav",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        response = self.transport.post(
            f"{OPENAI_AUDIO_BASE_URL}/speech",
            body,
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
            max_response_bytes=MAX_SPEECH_BYTES,
        )
        if not response.content_type.startswith("audio/"):
            raise SpeechBackendError("OpenAI speech response was not audio")
        return response
