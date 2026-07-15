import tempfile
import unittest
from pathlib import Path

from alfred_tools.install_skill import install_skill

ROOT = Path(__file__).resolve().parents[1]
WEB_SKILL = ROOT / "skills" / "alfred-search-web"
MEMORY_SKILL = ROOT / "skills" / "alfred-personal-memory"
WEATHER_SKILL = ROOT / "skills" / "alfred-weather"


class SkillInstallerTests(unittest.TestCase):
    def test_installs_skill_and_agent_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "skills" / "alfred-search-web"

            install_skill(WEB_SKILL, destination)

            self.assertEqual(
                (destination / "SKILL.md").read_text(), (WEB_SKILL / "SKILL.md").read_text()
            )
            self.assertEqual(
                (destination / "agents" / "openai.yaml").read_text(),
                (WEB_SKILL / "agents" / "openai.yaml").read_text(),
            )

    def test_rejects_a_source_without_skill_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "not-a-skill"
            source.mkdir()

            with self.assertRaisesRegex(ValueError, "SKILL.md"):
                install_skill(source, Path(directory) / "destination")


class AgentIntegrationContractTests(unittest.TestCase):
    def test_skill_is_complete_and_implicitly_triggerable(self):
        instructions = (WEB_SKILL / "SKILL.md").read_text()
        metadata = (WEB_SKILL / "agents" / "openai.yaml").read_text()

        self.assertNotIn("TODO", instructions)
        self.assertIn("alfred-web-search", instructions)
        self.assertIn("alfred-web-fetch", instructions)
        self.assertIn("alfred-web-research", instructions)
        self.assertIn("allow_implicit_invocation: true", metadata)

    def test_personal_memory_skill_automatically_captures_locally_with_privacy_gates(self):
        instructions = (MEMORY_SKILL / "SKILL.md").read_text()
        metadata = (MEMORY_SKILL / "agents" / "openai.yaml").read_text()

        self.assertNotIn("TODO", instructions)
        self.assertIn("alfred-notes search", instructions)
        self.assertIn("alfred-notes capture", instructions)
        self.assertIn("--automatic", instructions)
        self.assertIn("without using the internet", instructions.lower())
        self.assertIn("sensitive", instructions.lower())
        self.assertIn("allow_implicit_invocation: true", metadata)

    def test_weather_skill_uses_the_dedicated_attributed_forecast_command(self):
        instructions = (WEATHER_SKILL / "SKILL.md").read_text()
        metadata = (WEATHER_SKILL / "agents" / "openai.yaml").read_text()

        self.assertNotIn("TODO", instructions)
        self.assertIn("alfred-weather", instructions)
        self.assertIn("MET Norway", instructions)
        self.assertIn("OpenStreetMap", instructions)
        self.assertIn("time_zone", instructions)
        self.assertIn("IANA", instructions)
        self.assertIn("allow_implicit_invocation: true", metadata)

    def test_makefile_has_repeatable_agent_installation(self):
        makefile = (ROOT / "Makefile").read_text()

        self.assertIn("install-agent:", makefile)
        self.assertIn("$(UV) tool install --force --editable", makefile)
        self.assertIn("alfred_tools.install_skill", makefile)
        self.assertIn("alfred-personal-memory", makefile)
        self.assertIn("alfred-weather", makefile)
        self.assertIn("ALFRED_VOICE_MARKER", makefile)
        self.assertIn("test -f $(ALFRED_VOICE_MARKER)", makefile)
        self.assertIn("install -D -m 600 /dev/null $(ALFRED_VOICE_MARKER)", makefile)

        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn('alfred-notes = "alfred_tools.notes.cli:main"', pyproject)
        self.assertIn('alfred-serve = "alfred_tools.orchestrator.server:main"', pyproject)
        self.assertIn('alfred-weather = "alfred_tools.weather.forecast:main"', pyproject)
        self.assertIn('voice = ["faster-whisper>=1,<2"]', pyproject)


if __name__ == "__main__":
    unittest.main()
