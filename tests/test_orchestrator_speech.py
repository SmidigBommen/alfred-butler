import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alfred_tools.orchestrator.speech import FasterWhisperTranscriber


class Segment:
    text = " Hello Alfred. "


class Info:
    language = "en"
    duration = 2.5


class FakeWhisperModel:
    instances = []

    def __init__(self, model, *, device, compute_type):
        self.settings = (model, device, compute_type)
        self.calls = []
        self.__class__.instances.append(self)

    def transcribe(self, path, *, language, vad_filter):
        self.calls.append((path, language, vad_filter))
        self.path_existed_during_call = Path(path).is_file()
        return iter([Segment()]), Info()


class OOMWhisperModel(FakeWhisperModel):
    def transcribe(self, path, *, language, vad_filter):
        if self.settings[1] != "cpu":
            raise RuntimeError("CUDA failed with error out of memory")
        return super().transcribe(path, language=language, vad_filter=vad_filter)


class MissingCUDAWhisperModel(FakeWhisperModel):
    def transcribe(self, path, *, language, vad_filter):
        if self.settings[1] != "cpu":
            raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")
        return super().transcribe(path, language=language, vad_filter=vad_filter)


class FasterWhisperTranscriberTests(unittest.TestCase):
    def test_defaults_to_a_compact_quantized_english_model(self):
        transcriber = FasterWhisperTranscriber()

        self.assertEqual(transcriber.model_name, "small.en")
        self.assertEqual(transcriber.compute_type, "int8_float16")

    def test_transcribes_english_and_deletes_the_temporary_audio(self):
        module = type(sys)("faster_whisper")
        module.WhisperModel = FakeWhisperModel
        FakeWhisperModel.instances.clear()
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(sys.modules, {"faster_whisper": module}):
                transcriber = FasterWhisperTranscriber(temp_dir=Path(directory))
                result = transcriber.transcribe(b"audio", "audio/webm")
            remaining = list(Path(directory).iterdir())

        model = FakeWhisperModel.instances[0]
        self.assertTrue(model.path_existed_during_call)
        self.assertEqual(model.calls[0][1:], ("en", True))
        self.assertEqual(result["text"], "Hello Alfred.")
        self.assertEqual(remaining, [])

    def test_retries_on_cpu_when_the_chat_model_has_filled_gpu_memory(self):
        module = type(sys)("faster_whisper")
        module.WhisperModel = OOMWhisperModel
        OOMWhisperModel.instances.clear()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(sys.modules, {"faster_whisper": module}),
        ):
            result = FasterWhisperTranscriber(temp_dir=Path(directory)).transcribe(
                b"audio", "audio/webm"
            )

        self.assertEqual(result["text"], "Hello Alfred.")
        self.assertEqual(
            [instance.settings[1:] for instance in OOMWhisperModel.instances],
            [("auto", "int8_float16"), ("cpu", "int8")],
        )

    def test_retries_on_cpu_when_the_cuda_runtime_is_unavailable(self):
        module = type(sys)("faster_whisper")
        module.WhisperModel = MissingCUDAWhisperModel
        MissingCUDAWhisperModel.instances.clear()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(sys.modules, {"faster_whisper": module}),
        ):
            result = FasterWhisperTranscriber(temp_dir=Path(directory)).transcribe(
                b"audio", "audio/webm"
            )

        self.assertEqual(result["text"], "Hello Alfred.")
        self.assertEqual(MissingCUDAWhisperModel.instances[-1].settings[1:], ("cpu", "int8"))


if __name__ == "__main__":
    unittest.main()
