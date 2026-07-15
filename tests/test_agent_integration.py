import tempfile
import unittest
from pathlib import Path

from alfred_tools.install_skill import install_skill

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "alfred-search-web"


class SkillInstallerTests(unittest.TestCase):
    def test_installs_skill_and_agent_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "skills" / "alfred-search-web"

            install_skill(SKILL, destination)

            self.assertEqual(
                (destination / "SKILL.md").read_text(), (SKILL / "SKILL.md").read_text()
            )
            self.assertEqual(
                (destination / "agents" / "openai.yaml").read_text(),
                (SKILL / "agents" / "openai.yaml").read_text(),
            )

    def test_rejects_a_source_without_skill_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "not-a-skill"
            source.mkdir()

            with self.assertRaisesRegex(ValueError, "SKILL.md"):
                install_skill(source, Path(directory) / "destination")


class AgentIntegrationContractTests(unittest.TestCase):
    def test_skill_is_complete_and_implicitly_triggerable(self):
        instructions = (SKILL / "SKILL.md").read_text()
        metadata = (SKILL / "agents" / "openai.yaml").read_text()

        self.assertNotIn("TODO", instructions)
        self.assertIn("alfred-web-search", instructions)
        self.assertIn("alfred-web-fetch", instructions)
        self.assertIn("alfred-web-research", instructions)
        self.assertIn("allow_implicit_invocation: true", metadata)

    def test_makefile_has_repeatable_agent_installation(self):
        makefile = (ROOT / "Makefile").read_text()

        self.assertIn("install-agent:", makefile)
        self.assertIn("$(UV) tool install --force --editable", makefile)
        self.assertIn("alfred_tools.install_skill", makefile)


if __name__ == "__main__":
    unittest.main()
