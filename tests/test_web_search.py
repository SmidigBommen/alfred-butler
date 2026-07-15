import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alfred_tools.web.search import (
    SearchBackendError,
    SearchClient,
    SearchRequest,
    SQLiteSearchCache,
    UrllibSearchTransport,
    run_cli,
)


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def search(self, base_url, parameters, timeout):
        self.calls.append((base_url, parameters, timeout))
        if self.error:
            raise self.error
        return self.response


class SearchRequestTests(unittest.TestCase):
    def test_rejects_invalid_inputs(self):
        invalid = (
            {"query": "   "},
            {"query": "test", "limit": 0},
            {"query": "test", "limit": 51},
            {"query": "test", "page": 0},
            {"query": "test", "safe_search": 3},
            {"query": "test", "time_range": "week"},
        )

        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                SearchRequest(**values)

    def test_encodes_only_supported_searxng_parameters(self):
        request = SearchRequest(
            query="site:example.com local tools",
            categories=("general", "it"),
            language="en-US",
            page=2,
            time_range="month",
            safe_search=1,
            limit=7,
        )

        self.assertEqual(
            request.to_parameters(),
            {
                "q": "site:example.com local tools",
                "format": "json",
                "categories": "general,it",
                "language": "en-US",
                "pageno": "2",
                "time_range": "month",
                "safesearch": "1",
            },
        )


class SearchClientTests(unittest.TestCase):
    def test_normalizes_deduplicates_and_limits_results(self):
        transport = FakeTransport(
            {
                "query": "alfred",
                "results": [
                    {
                        "title": " First result ",
                        "url": "https://example.com/page#section",
                        "content": " Useful snippet ",
                        "engines": ["brave", "duckduckgo"],
                        "score": 4.25,
                        "category": "general",
                        "publishedDate": "2026-07-14T12:00:00",
                    },
                    {
                        "title": "Duplicate",
                        "url": "https://example.com/page#other",
                        "content": "duplicate",
                        "engine": "google",
                    },
                    {
                        "title": "Second result",
                        "url": "https://example.org/",
                        "content": None,
                        "engine": "startpage",
                    },
                ],
                "answers": [{"answer": "forty-two"}],
                "corrections": ["Alfred"],
                "suggestions": ["alfred assistant"],
                "unresponsive_engines": [["qwant", "timeout"]],
            }
        )
        client = SearchClient("http://127.0.0.1:8888/", transport=transport)

        response = client.search(SearchRequest(query="alfred", limit=2))

        self.assertEqual(response["query"], "alfred")
        self.assertEqual(len(response["results"]), 2)
        self.assertEqual(
            response["results"][0],
            {
                "title": "First result",
                "url": "https://example.com/page#section",
                "snippet": "Useful snippet",
                "engines": ["brave", "duckduckgo", "google"],
                "score": 4.25,
                "category": "general",
                "published_at": "2026-07-14T12:00:00",
            },
        )
        self.assertEqual(response["warnings"], [{"engine": "qwant", "error": "timeout"}])
        self.assertEqual(response["answers"], [{"answer": "forty-two"}])
        self.assertFalse(response["cached"])
        self.assertEqual(transport.calls[0][0], "http://127.0.0.1:8888")

    def test_rejects_non_http_result_urls(self):
        transport = FakeTransport(
            {
                "query": "unsafe",
                "results": [
                    {"title": "Bad", "url": "javascript:alert(1)"},
                    {"title": "Bad port", "url": "https://example.com:not-a-port/"},
                    {"title": "Good", "url": "https://example.com"},
                ],
            }
        )
        client = SearchClient("http://127.0.0.1:8888", transport=transport)

        response = client.search(SearchRequest(query="unsafe"))

        self.assertEqual([item["title"] for item in response["results"]], ["Good"])

    def test_wraps_transport_failures(self):
        client = SearchClient(
            "http://127.0.0.1:8888",
            transport=FakeTransport(error=TimeoutError("too slow")),
        )

        with self.assertRaisesRegex(SearchBackendError, "too slow"):
            client.search(SearchRequest(query="test"))

    def test_uses_sqlite_cache_for_identical_requests(self):
        transport = FakeTransport({"query": "cached", "results": []})
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteSearchCache(Path(directory) / "search.sqlite3", ttl_seconds=60)
            client = SearchClient("http://127.0.0.1:8888", transport=transport, cache=cache)

            first = client.search(SearchRequest(query="cached"))
            second = client.search(SearchRequest(query="cached"))

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(transport.calls), 1)


class UrllibSearchTransportTests(unittest.TestCase):
    def test_identifies_local_client_to_searxng_proxy_middleware(self):
        with patch("alfred_tools.web.search.urllib.request.urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = b'{"query":"test","results":[]}'

            UrllibSearchTransport().search(
                "http://127.0.0.1:8888", {"q": "test", "format": "json"}, 1
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-real-ip"), "127.0.0.1")

    def test_does_not_claim_loopback_identity_to_remote_instance(self):
        with patch("alfred_tools.web.search.urllib.request.urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = b'{"query":"test","results":[]}'

            UrllibSearchTransport().search(
                "https://search.example.com", {"q": "test", "format": "json"}, 1
            )

        request = urlopen.call_args.args[0]
        self.assertIsNone(request.get_header("X-real-ip"))


class CliTests(unittest.TestCase):
    def test_cli_prints_machine_readable_json(self):
        class FakeClient:
            def search(self, request):
                return {"query": request.query, "results": [], "cached": False}

        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli(
            ["--query", "local search", "--category", "it", "--limit", "3"],
            client=FakeClient(),
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["query"], "local search")
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_emits_json_error_and_nonzero_exit(self):
        class FailingClient:
            def search(self, request):
                raise SearchBackendError("offline")

        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli(
            ["--query", "test"], client=FailingClient(), stdout=stdout, stderr=stderr
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "offline", "type": "backend"})
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
