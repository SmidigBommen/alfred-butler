import json
import subprocess
import unittest

from alfred_tools.orchestrator.lmstudio import LMStudioManager


class RecordingRunner:
    def __init__(self, installed):
        self.installed = installed
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        stdout = json.dumps(self.installed) if command[:3] == ["lms", "ls", "--llm"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")


class LMStudioManagerTests(unittest.TestCase):
    def test_uses_an_already_loaded_model_without_running_the_cli(self):
        runner = RecordingRunner([])
        manager = LMStudioManager(
            base_url="http://127.0.0.1:1234/v1",
            loaded_models=lambda: ["already-loaded"],
            runner=runner,
        )

        self.assertEqual(manager.ensure_model(), "already-loaded")
        self.assertEqual(runner.calls, [])

    def test_loads_the_smallest_installed_tool_model_when_none_is_loaded(self):
        runner = RecordingRunner(
            [
                {"modelKey": "large-tool", "sizeBytes": 20, "trainedForToolUse": True},
                {"modelKey": "small-plain", "sizeBytes": 5, "trainedForToolUse": False},
                {"modelKey": "small-tool", "sizeBytes": 10, "trainedForToolUse": True},
            ]
        )
        manager = LMStudioManager(
            base_url="http://127.0.0.1:1234/v1",
            loaded_models=lambda: [],
            runner=runner,
        )

        self.assertEqual(manager.ensure_model(), "alfred-local")
        load_command = runner.calls[-1][0]
        self.assertEqual(load_command[:3], ["lms", "load", "small-tool"])
        self.assertIn("--gpu", load_command)
        self.assertIn("--ttl", load_command)
        self.assertIn("--yes", load_command)

    def test_configured_model_wins_and_missing_configuration_does_not_download(self):
        runner = RecordingRunner([{"modelKey": "installed", "trainedForToolUse": True}])
        manager = LMStudioManager(
            base_url="http://127.0.0.1:1234/v1",
            configured_model="not-installed",
            loaded_models=lambda: [],
            runner=runner,
        )

        with self.assertRaisesRegex(RuntimeError, "not installed"):
            manager.ensure_model()

        self.assertFalse(any(call[0][1] == "get" for call in runner.calls))


if __name__ == "__main__":
    unittest.main()
