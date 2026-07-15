import tempfile
import unittest
from pathlib import Path

from alfred_tools.notes.store import NoteStore, SensitiveContentError
from alfred_tools.orchestrator.tools import build_alfred_tools


class FakeSearchClient:
    def __init__(self):
        self.requests = []

    def search(self, request):
        self.requests.append(request)
        return {"query": request.query, "results": []}


class FakeFetchClient:
    def fetch(self, request):
        return {"url": request.url, "text": "evidence"}


class FakeResearchClient:
    def research(self, request):
        return {"queries": list(request.queries), "sources": []}


class AlfredToolTests(unittest.TestCase):
    def test_builds_only_bounded_registered_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = build_alfred_tools(
                notes=NoteStore(Path(directory)),
                search=FakeSearchClient(),
                fetch=FakeFetchClient(),
                research=FakeResearchClient(),
            )

        self.assertEqual(
            {tool.name for tool in tools},
            {"memory_search", "memory_capture", "web_search", "web_fetch", "web_research"},
        )
        self.assertNotIn("shell", {tool.name for tool in tools})

    def test_memory_capture_is_automatic_and_sensitive_content_stays_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = build_alfred_tools(
                notes=NoteStore(Path(directory)),
                search=FakeSearchClient(),
                fetch=FakeFetchClient(),
                research=FakeResearchClient(),
            )
            capture = next(tool for tool in tools if tool.name == "memory_capture")
            note = capture.handler(
                {
                    "title": "Preferred editor",
                    "body": "Use Helix for quick edits.",
                    "kind": "preference",
                    "labels": ["tools"],
                    "importance": 0.6,
                }
            )

            self.assertEqual(note["review_status"], "pending")
            with self.assertRaises(SensitiveContentError):
                capture.handler(
                    {
                        "title": "Credential",
                        "body": "api_key=do-not-store",
                        "kind": "fact",
                        "labels": [],
                        "importance": 0.5,
                    }
                )

    def test_web_search_is_forced_to_english_and_a_small_result_limit(self):
        search_client = FakeSearchClient()
        with tempfile.TemporaryDirectory() as directory:
            tools = build_alfred_tools(
                notes=NoteStore(Path(directory)),
                search=search_client,
                fetch=FakeFetchClient(),
                research=FakeResearchClient(),
            )
            search = next(tool for tool in tools if tool.name == "web_search")
            search.handler({"query": "current docs"})

        request = search_client.requests[0]
        self.assertEqual(request.language, "en")
        self.assertEqual(request.limit, 5)


if __name__ == "__main__":
    unittest.main()
