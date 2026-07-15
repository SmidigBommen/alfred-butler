"""Optional local English speech-to-text adapter."""

from __future__ import annotations

import gc
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

CONTENT_SUFFIXES = {
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
}


class SpeechBackendError(RuntimeError):
    """Raised when local transcription is unavailable or fails."""


class FasterWhisperTranscriber:
    def __init__(
        self,
        *,
        model_name: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
        temp_dir: Path | None = None,
    ):
        self.model_name = model_name or os.environ.get("ALFRED_STT_MODEL", "small.en")
        self.device = device or os.environ.get("ALFRED_STT_DEVICE", "auto")
        self.compute_type = compute_type or os.environ.get(
            "ALFRED_STT_COMPUTE_TYPE", "int8_float16"
        )
        self.temp_dir = temp_dir
        self._model: Any | None = None
        self._lock = threading.Lock()

    def _load_model(self, *, device: str | None = None, compute_type: str | None = None) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as error:
            raise SpeechBackendError(
                "local speech support is not installed; install Alfred's voice extra"
            ) from error
        self._model = WhisperModel(
            self.model_name,
            device=device or self.device,
            compute_type=compute_type or self.compute_type,
        )
        return self._model

    @staticmethod
    def _run_transcription(model: Any, path: Path) -> tuple[str, Any]:
        segments, info = model.transcribe(
            str(path),
            language="en",
            vad_filter=True,
        )
        return " ".join(segment.text.strip() for segment in segments).strip(), info

    def transcribe(self, audio: bytes, content_type: str) -> dict[str, Any]:
        media_type = content_type.partition(";")[0].strip().lower()
        suffix = CONTENT_SUFFIXES.get(media_type)
        if suffix is None:
            raise ValueError("unsupported audio content type")
        if not audio:
            raise ValueError("audio must not be empty")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="alfred-voice-",
                suffix=suffix,
                dir=self.temp_dir,
                delete=False,
            ) as stream:
                stream.write(audio)
                temporary_path = Path(stream.name)
            with self._lock:
                try:
                    text, info = self._run_transcription(self._load_model(), temporary_path)
                except RuntimeError as error:
                    message = str(error).casefold()
                    gpu_failure = any(
                        marker in message for marker in ("cuda", "cublas", "cudnn", "out of memory")
                    )
                    if not gpu_failure or self.device == "cpu":
                        raise
                    failed_model = self._model
                    self._model = None
                    del failed_model
                    gc.collect()
                    text, info = self._run_transcription(
                        self._load_model(device="cpu", compute_type="int8"),
                        temporary_path,
                    )
            return {
                "text": text,
                "language": "en",
                "duration": float(getattr(info, "duration", 0.0)),
            }
        except (OSError, RuntimeError) as error:
            raise SpeechBackendError(f"local transcription failed: {error}") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
