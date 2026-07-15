import gc
import json
import sqlite3
import tempfile
import unittest
import uuid
import warnings
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from alfred_tools.notes import NoteStore, SensitiveContentError


class NoteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "notes"
        self.store = NoteStore(self.root)

    def test_capture_writes_portable_markdown_and_round_trips_metadata(self):
        note = self.store.capture(
            title="How I like to work",
            body="Always improve and have fun doing it.",
            labels=("Personal", "principles", "personal"),
            kind="preference",
            provenance="user-stated",
            importance=0.9,
        )

        path = self.root / f"{note.id}.md"
        markdown = path.read_text()
        loaded = self.store.get(note.id)

        self.assertTrue(markdown.startswith("+++\n"))
        self.assertIn(f'id = "{note.id}"', markdown)
        self.assertIn('labels = ["personal", "principles"]', markdown)
        self.assertIn("Always improve and have fun doing it.", markdown)
        self.assertEqual(loaded.title, "How I like to work")
        self.assertEqual(loaded.labels, ("personal", "principles"))
        self.assertEqual(loaded.kind, "preference")
        self.assertEqual(loaded.provenance, "user-stated")
        self.assertEqual(loaded.importance, 0.9)

    def test_graph_index_weights_explicit_links_above_shared_labels(self):
        source = self.store.capture(
            title="Alfred principles",
            body="See [[Continuous improvement]].",
            labels=("alfred", "principles"),
        )
        explicit = self.store.capture(
            title="Continuous improvement",
            body="Review what worked and make the next iteration better.",
            labels=("improvement",),
        )
        inferred = self.store.capture(
            title="Assistant personality",
            body="Keep the work enjoyable.",
            labels=("alfred", "principles"),
        )

        related = self.store.related(source.id, limit=5)

        self.assertEqual(related[0]["note"]["id"], explicit.id)
        self.assertEqual(related[0]["score_components"]["explicit"], 1.0)
        inferred_result = next(item for item in related if item["note"]["id"] == inferred.id)
        self.assertGreater(inferred_result["score_components"]["shared_labels"], 0)
        self.assertGreater(related[0]["score"], inferred_result["score"])

        with closing(sqlite3.connect(self.store.index_path)) as connection:
            origins = {row[0] for row in connection.execute("SELECT DISTINCT origin FROM edges")}
        self.assertEqual(origins, {"explicit", "inferred"})

    def test_link_uses_stable_id_when_titles_are_duplicated_later(self):
        source = self.store.capture(title="Source", body="Durable relationship.")
        intended = self.store.capture(title="Repeated title", body="Intended target.")

        linked = self.store.link(source.id, intended.id)
        duplicate = self.store.capture(title="Repeated title", body="A later duplicate.")
        related_ids = [item["note"]["id"] for item in self.store.related(source.id)]

        self.assertIn(f"[[{intended.id}|Repeated title]]", linked.body)
        self.assertIn(intended.id, related_ids)
        self.assertNotIn(duplicate.id, related_ids)

    def test_ambiguous_title_only_wikilink_does_not_choose_a_target(self):
        source = self.store.capture(title="Source", body="See [[Repeated title]].")
        self.store.capture(title="Repeated title", body="First possible target.")
        self.store.capture(title="Repeated title", body="Second possible target.")

        self.assertEqual(self.store.related(source.id), [])

    def test_shared_label_graph_uses_label_nodes_instead_of_dense_pair_edges(self):
        notes = [
            self.store.capture(
                title=f"Note {number}", body="Connected by context.", labels=("personal",)
            )
            for number in range(5)
        ]

        with closing(sqlite3.connect(self.store.index_path)) as connection:
            inferred_edges = connection.execute(
                "SELECT COUNT(*) FROM edges WHERE origin = 'inferred'"
            ).fetchone()[0]
            label_nodes = connection.execute(
                "SELECT COUNT(*) FROM nodes WHERE node_type = 'label'"
            ).fetchone()[0]

        self.assertEqual(inferred_edges, 5)
        self.assertEqual(label_nodes, 1)
        self.assertEqual(len(self.store.related(notes[0].id)), 4)

    def test_search_is_explainable_label_aware_and_rebuilds_after_external_edit(self):
        principle = self.store.capture(
            title="Continuous improvement",
            body="Always improve and have fun doing it.",
            labels=("personal", "principles"),
            importance=1.0,
        )
        self.store.capture(
            title="Groceries",
            body="Milk and coffee.",
            labels=("personal", "errands"),
        )

        results = self.store.search("improve", labels=("principles",), limit=5)

        self.assertEqual([item["note"]["id"] for item in results], [principle.id])
        self.assertEqual(
            set(results[0]["score_components"]),
            {"text", "labels", "graph", "importance", "recency"},
        )
        self.assertAlmostEqual(results[0]["score"], sum(results[0]["score_components"].values()))

        path = self.root / f"{principle.id}.md"
        path.write_text(path.read_text().replace("Always improve", "Iterate deliberately"))

        self.assertEqual(self.store.search("improve"), [])
        self.assertEqual(self.store.search("deliberately")[0]["note"]["id"], principle.id)

    def test_search_uses_fts_relevance_before_explainable_score_components(self):
        with patch(
            "alfred_tools.notes.store.uuid.uuid4",
            side_effect=(
                uuid.UUID("00000000-0000-0000-0000-000000000001"),
                uuid.UUID("00000000-0000-0000-0000-000000000010"),
                uuid.UUID("00000000-0000-0000-0000-000000000002"),
                uuid.UUID("00000000-0000-0000-0000-000000000011"),
            ),
        ):
            self.store.capture(title="Miscellaneous", body="A passing mention of graph storage.")
            exact = self.store.capture(
                title="Graph memory", body="Weighted personal knowledge graph."
            )

        results = self.store.search("graph")

        self.assertEqual(results[0]["note"]["id"], exact.id)
        self.assertGreater(
            results[0]["score_components"]["text"],
            results[1]["score_components"]["text"],
        )

    def test_automatic_capture_enters_review_and_archive_removes_it_from_recall(self):
        note = self.store.capture(
            title="Preferred note format",
            body="Markdown works well.",
            labels=("preferences",),
            automatic=True,
        )

        self.assertEqual(note.review_status, "pending")
        self.assertEqual([item.id for item in self.store.review()], [note.id])

        accepted = self.store.update(note.id, accept=True)
        self.assertEqual(accepted.review_status, "accepted")
        self.assertEqual(self.store.review(), [])

        archived = self.store.archive(note.id)
        self.assertEqual(archived.status, "archived")
        self.assertEqual(self.store.search("Markdown"), [])

    def test_delete_requires_confirmation_and_removes_source_and_graph_data(self):
        note = self.store.capture(title="Forget me", body="Temporary personal context.")

        with self.assertRaisesRegex(ValueError, "confirmation"):
            self.store.delete(note.id)

        result = self.store.delete(note.id, confirm=True)

        self.assertEqual(result, {"deleted": note.id})
        self.assertFalse((self.root / f"{note.id}.md").exists())
        with self.assertRaises(KeyError):
            self.store.get(note.id)
        with closing(sqlite3.connect(self.store.index_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM nodes WHERE id = ?", (note.id,)
                ).fetchone()[0],
                0,
            )

    def test_rejects_credentials_and_requires_confirmation_for_sensitive_notes(self):
        with self.assertRaises(SensitiveContentError) as caught:
            self.store.capture(title="Login", body="password = swordfish")
        self.assertEqual(caught.exception.category, "credential")
        self.assertFalse(caught.exception.storable)

        with self.assertRaises(SensitiveContentError) as caught:
            self.store.capture(title="Health", body="My medical diagnosis and treatment notes")
        self.assertEqual(caught.exception.category, "sensitive-personal")
        self.assertTrue(caught.exception.storable)

        note = self.store.capture(
            title="Health",
            body="My medical diagnosis and treatment notes",
            sensitivity="sensitive",
            confirm_sensitive=True,
        )
        self.assertEqual(note.sensitivity, "sensitive")

    def test_export_contains_notes_and_weighted_graph(self):
        first = self.store.capture(title="First", body="See [[Second]].")
        second = self.store.capture(title="Second", body="Connected note.")

        exported = self.store.export()

        self.assertEqual({item["id"] for item in exported["notes"]}, {first.id, second.id})
        self.assertEqual(exported["schema_version"], 1)
        self.assertEqual(exported["edges"][0]["origin"], "explicit")
        json.dumps(exported)

    def test_operations_close_every_sqlite_connection(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            note = self.store.capture(title="Connection test", body="SQLite graph index")
            self.store.search("SQLite")
            self.store.related(note.id)
            self.store.export()
            gc.collect()

        resource_warnings = [item for item in caught if item.category is ResourceWarning]
        self.assertEqual(resource_warnings, [])


if __name__ == "__main__":
    unittest.main()
