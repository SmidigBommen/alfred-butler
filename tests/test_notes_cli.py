import io
import json
import tempfile
import unittest
from pathlib import Path

from alfred_tools.notes.cli import run_cli


class NotesCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "notes"

    def run_command(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli(
            [*arguments],
            root=self.root,
            stdout=stdout,
            stderr=stderr,
        )
        output = json.loads(stdout.getvalue()) if stdout.getvalue() else None
        error = json.loads(stderr.getvalue()) if stderr.getvalue() else None
        return exit_code, output, error

    def test_capture_search_show_and_related_commands_emit_json(self):
        exit_code, first, error = self.run_command(
            "capture",
            "--title",
            "Alfred principle",
            "--body",
            "Always improve and have fun.",
            "--label",
            "personal",
            "--kind",
            "preference",
            "--importance",
            "1",
        )
        self.assertEqual((exit_code, error), (0, None))

        _, second, _ = self.run_command(
            "capture",
            "--title",
            "Improvement loop",
            "--body",
            "Review, improve, and repeat.",
            "--label",
            "personal",
        )
        exit_code, linked, error = self.run_command("link", first["id"], second["id"])
        self.assertEqual((exit_code, error), (0, None))
        self.assertIn(f"[[{second['id']}|Improvement loop]]", linked["body"])

        exit_code, results, error = self.run_command(
            "search", "--query", "improve", "--label", "personal", "--limit", "5"
        )
        self.assertEqual((exit_code, error), (0, None))
        self.assertEqual(results["query"], "improve")
        self.assertTrue(results["results"])
        self.assertIn("score_components", results["results"][0])

        exit_code, related, error = self.run_command("related", first["id"])
        self.assertEqual((exit_code, error), (0, None))
        self.assertEqual(related["results"][0]["note"]["id"], second["id"])

        exit_code, shown, error = self.run_command("show", first["id"])
        self.assertEqual((exit_code, error), (0, None))
        self.assertEqual(shown["title"], "Alfred principle")

    def test_automatic_review_update_archive_rebuild_and_export(self):
        _, note, _ = self.run_command(
            "capture",
            "--title",
            "Preferred format",
            "--body",
            "Markdown",
            "--automatic",
        )

        _, review, _ = self.run_command("review")
        self.assertEqual([item["id"] for item in review["notes"]], [note["id"]])

        _, accepted, _ = self.run_command("update", note["id"], "--accept")
        self.assertEqual(accepted["review_status"], "accepted")

        _, rebuilt, _ = self.run_command("rebuild-index")
        self.assertEqual(rebuilt["notes"], 1)

        _, exported, _ = self.run_command("export")
        self.assertEqual(exported["schema_version"], 1)
        self.assertEqual(exported["notes"][0]["id"], note["id"])

        _, archived, _ = self.run_command("archive", note["id"])
        self.assertEqual(archived["status"], "archived")

    def test_privacy_error_is_machine_readable(self):
        exit_code, output, error = self.run_command(
            "capture",
            "--title",
            "Login",
            "--body",
            "password=do-not-store-me",
        )

        self.assertEqual(exit_code, 3)
        self.assertIsNone(output)
        self.assertEqual(error["type"], "privacy")
        self.assertEqual(error["category"], "credential")
        self.assertFalse(error["storable"])

    def test_delete_is_confirmation_gated(self):
        _, note, _ = self.run_command(
            "capture", "--title", "Temporary", "--body", "Delete this note."
        )

        exit_code, output, error = self.run_command("delete", note["id"])
        self.assertEqual(exit_code, 1)
        self.assertIsNone(output)
        self.assertIn("confirmation", error["error"])

        exit_code, output, error = self.run_command("delete", note["id"], "--confirm")
        self.assertEqual((exit_code, error), (0, None))
        self.assertEqual(output, {"deleted": note["id"]})


if __name__ == "__main__":
    unittest.main()
