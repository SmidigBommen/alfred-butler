import tempfile
import unittest
from pathlib import Path

from alfred_tools.config import get_preference, load_preferences


class LocalPreferenceTests(unittest.TestCase):
    def test_reads_only_allowlisted_preferences_without_shell_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.env"
            path.write_text(
                "# Local Alfred defaults\n"
                "export ALFRED_TIME_ZONE=Europe/Oslo\n"
                "ALFRED_WEATHER_LOCATION=Configured municipality, Norway\n"
                "OPENAI_API_KEY=must-not-be-loaded-here\n"
                "MALFORMED\n"
            )

            preferences = load_preferences(path)

        self.assertEqual(
            preferences,
            {
                "ALFRED_TIME_ZONE": "Europe/Oslo",
                "ALFRED_WEATHER_LOCATION": "Configured municipality, Norway",
            },
        )

    def test_process_environment_wins_over_the_local_preferences_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.env"
            path.write_text("ALFRED_TIME_ZONE=UTC\n")

            value = get_preference(
                "ALFRED_TIME_ZONE",
                "fallback",
                environment={"ALFRED_TIME_ZONE": "Europe/Oslo"},
                path=path,
            )

        self.assertEqual(value, "Europe/Oslo")


if __name__ == "__main__":
    unittest.main()
