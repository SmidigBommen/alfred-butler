import os
import unittest

from alfred_tools.web.fetch import FetchClient, FetchRequest
from alfred_tools.web.research import ResearchClient, ResearchRequest
from alfred_tools.web.search import SearchClient, SearchRequest


@unittest.skipUnless(os.environ.get("ALFRED_LIVE_TESTS") == "1", "set ALFRED_LIVE_TESTS=1")
class LiveSearchTests(unittest.TestCase):
    def test_private_searxng_returns_normalized_web_results(self):
        response = SearchClient().search(
            SearchRequest(query="SearXNG official documentation", limit=3)
        )

        self.assertEqual(response["query"], "SearXNG official documentation")
        self.assertTrue(response["results"])
        self.assertLessEqual(len(response["results"]), 3)
        self.assertTrue(
            all(item["url"].startswith(("http://", "https://")) for item in response["results"])
        )

    def test_public_page_fetch_returns_bounded_untrusted_evidence(self):
        response = FetchClient(timeout=8).fetch(
            FetchRequest(
                "https://docs.python.org/3/library/http.client.html",
                max_chars=1_000,
            )
        )

        self.assertEqual(response["status"], 200)
        self.assertIn("HTTP protocol client", response["title"])
        self.assertTrue(response["text"])
        self.assertLessEqual(len(response["text"]), 1_000)
        self.assertTrue(response["untrusted"])

    def test_research_composes_live_search_and_fetch(self):
        response = ResearchClient(
            search_client=SearchClient(timeout=8),
            fetch_client=FetchClient(timeout=8),
        ).research(
            ResearchRequest(
                queries=("SearXNG official documentation",),
                max_sources=1,
                results_per_query=5,
                max_chars_per_source=1_000,
            )
        )

        self.assertEqual(response["queries"], ["SearXNG official documentation"])
        self.assertTrue(response["sources"])
        self.assertTrue(response["sources"][0]["text"])
        self.assertTrue(response["untrusted"])


if __name__ == "__main__":
    unittest.main()
