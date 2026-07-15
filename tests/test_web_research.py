import io
import json
import unittest

from alfred_tools.web.fetch import FetchBackendError, FetchPolicyError
from alfred_tools.web.research import ResearchClient, ResearchRequest, run_cli


class FakeSearchClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def search(self, request):
        self.calls.append(request)
        response = self.responses[request.query]
        if isinstance(response, Exception):
            raise response
        return response


class FakeFetchClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, request):
        self.calls.append(request)
        response = self.responses[request.url]
        if isinstance(response, Exception):
            raise response
        return response


def fetched(url, title):
    return {
        "requested_url": url,
        "url": url,
        "status": 200,
        "content_type": "text/html",
        "title": title,
        "description": None,
        "author": None,
        "published_at": None,
        "text": (f"Evidence from {title}. " * 20).strip(),
        "truncated": False,
        "redirects": [],
        "content_sha256": f"hash-{title}",
        "fetched_at": "2026-07-15T12:00:00+00:00",
        "cached": False,
        "untrusted": True,
    }


class ResearchRequestTests(unittest.TestCase):
    def test_rejects_invalid_bounds_and_empty_queries(self):
        invalid = (
            {"queries": ()},
            {"queries": ("test",), "max_sources": 0},
            {"queries": ("test",), "results_per_query": 0},
            {"queries": ("test",), "max_chars_per_source": 0},
            {"queries": ("test",), "max_chars_per_source": 199},
        )

        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ResearchRequest(**values)


class ResearchClientTests(unittest.TestCase):
    def test_deduplicates_ranks_and_diversifies_sources(self):
        search = FakeSearchClient(
            {
                "query one": {
                    "results": [
                        {
                            "title": "A primary",
                            "url": "https://a.example/primary#section",
                            "score": 10.0,
                            "engines": ["engine-one"],
                        },
                        {
                            "title": "A second",
                            "url": "https://a.example/second",
                            "score": 9.0,
                            "engines": ["engine-one"],
                        },
                        {
                            "title": "B source",
                            "url": "https://b.example/source",
                            "score": 8.0,
                            "engines": ["engine-two"],
                        },
                    ],
                    "warnings": [{"engine": "slow", "error": "timeout"}],
                },
                "query two": {
                    "results": [
                        {
                            "title": "A primary duplicate",
                            "url": "https://a.example/primary#other",
                            "score": 7.0,
                            "engines": ["engine-three"],
                        },
                        {
                            "title": "C source",
                            "url": "https://c.example/source",
                            "score": 6.0,
                            "engines": ["engine-two"],
                        },
                    ],
                    "warnings": [],
                },
            }
        )
        fetch = FakeFetchClient(
            {
                "https://a.example/primary#section": fetched(
                    "https://a.example/primary#section", "A primary"
                ),
                "https://b.example/source": fetched("https://b.example/source", "B source"),
                "https://c.example/source": fetched("https://c.example/source", "C source"),
            }
        )
        client = ResearchClient(search_client=search, fetch_client=fetch)

        result = client.research(ResearchRequest(queries=("query one", "query two"), max_sources=2))

        self.assertEqual(
            [source["url"] for source in result["sources"]],
            [
                "https://a.example/primary#section",
                "https://b.example/source",
            ],
        )
        self.assertEqual(result["sources"][0]["search"]["queries"], ["query one", "query two"])
        self.assertEqual(result["sources"][0]["search"]["engines"], ["engine-one", "engine-three"])
        self.assertEqual(
            result["warnings"][0],
            {"stage": "search", "query": "query one", "engine": "slow", "error": "timeout"},
        )
        self.assertTrue(result["untrusted"])

    def test_fetch_failures_are_reported_and_replaced(self):
        search = FakeSearchClient(
            {
                "query": {
                    "results": [
                        {"title": "Blocked", "url": "https://blocked.example", "score": 10},
                        {"title": "Broken", "url": "https://broken.example", "score": 9},
                        {"title": "Good", "url": "https://good.example", "score": 8},
                    ],
                    "warnings": [],
                }
            }
        )
        fetch = FakeFetchClient(
            {
                "https://blocked.example": FetchPolicyError("private redirect"),
                "https://broken.example": FetchBackendError("HTTP 503"),
                "https://good.example": fetched("https://good.example", "Good"),
            }
        )
        client = ResearchClient(search_client=search, fetch_client=fetch)

        result = client.research(ResearchRequest(queries=("query",), max_sources=1))

        self.assertEqual([source["url"] for source in result["sources"]], ["https://good.example"])
        self.assertEqual([warning["type"] for warning in result["warnings"]], ["policy", "backend"])
        self.assertEqual(len(fetch.calls), 3)

    def test_skips_challenge_pages_with_insufficient_evidence(self):
        search = FakeSearchClient(
            {
                "query": {
                    "results": [
                        {"title": "Challenge", "url": "https://challenge.example", "score": 10},
                        {"title": "Evidence", "url": "https://evidence.example", "score": 9},
                    ],
                    "warnings": [],
                }
            }
        )
        challenge = fetched("https://challenge.example", "Client Challenge")
        challenge["text"] = (
            "A required part of this site couldn’t load. Enable JavaScript to continue. " * 10
        )
        fetch = FakeFetchClient(
            {
                "https://challenge.example": challenge,
                "https://evidence.example": fetched("https://evidence.example", "Evidence"),
            }
        )
        client = ResearchClient(search_client=search, fetch_client=fetch)

        result = client.research(ResearchRequest(queries=("query",), max_sources=1))

        self.assertEqual(
            [source["url"] for source in result["sources"]], ["https://evidence.example"]
        )
        self.assertEqual(result["warnings"][0]["type"], "quality")


class ResearchCliTests(unittest.TestCase):
    def test_cli_accepts_repeated_queries_and_prints_json(self):
        class FakeClient:
            def research(self, request):
                return {"queries": list(request.queries), "sources": [], "warnings": []}

        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["--query", "first", "--query", "second", "--max-sources", "3"],
            client=FakeClient(),
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["queries"], ["first", "second"])
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
