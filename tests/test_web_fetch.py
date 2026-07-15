import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from alfred_tools.web.fetch import (
    FetchBackendError,
    FetchClient,
    FetchPolicyError,
    FetchRequest,
    RawResponse,
    SQLiteFetchCache,
    UrlGuard,
    run_cli,
)

PUBLIC_IP = "93.184.216.34"


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, target, timeout, max_bytes):
        self.calls.append((target, timeout, max_bytes))
        response = self.responses[target.url]
        if isinstance(response, Exception):
            raise response
        return response


def public_guard(hosts=None):
    hosts = hosts or {}

    def resolve(host, port):
        return hosts.get(host, [PUBLIC_IP])

    return UrlGuard(resolver=resolve)


class UrlGuardTests(unittest.TestCase):
    def test_rejects_unsafe_url_forms_and_literal_private_addresses(self):
        guard = public_guard()
        unsafe = (
            "ftp://example.com/file",
            "http://user:secret@example.com/",
            "http://127.0.0.1/private",
            "http://[::1]/private",
            "http://localhost/private",
            "https://example.com:bad/",
        )

        for url in unsafe:
            with self.subTest(url=url), self.assertRaises(FetchPolicyError):
                guard.validate(url)

    def test_rejects_dns_answers_containing_any_non_public_address(self):
        guard = public_guard(
            {
                "private.example": ["10.0.0.5"],
                "mixed.example": [PUBLIC_IP, "169.254.169.254"],
            }
        )

        for url in ("https://private.example", "https://mixed.example"):
            with self.subTest(url=url), self.assertRaises(FetchPolicyError):
                guard.validate(url)

    def test_returns_normalized_target_with_pinned_public_addresses(self):
        target = public_guard().validate("https://Example.COM:443/a path?q=hello#fragment")

        self.assertEqual(target.url, "https://example.com/a%20path?q=hello")
        self.assertEqual(target.host, "example.com")
        self.assertEqual(target.port, 443)
        self.assertEqual(target.addresses, (PUBLIC_IP,))
        self.assertEqual(target.request_target, "/a%20path?q=hello")


class FetchClientTests(unittest.TestCase):
    def test_extracts_readable_html_and_metadata(self):
        body = b"""<!doctype html><html><head>
        <title> Example title </title>
        <meta name="description" content="A useful description">
        <meta name="author" content="Ada Lovelace">
        <meta property="article:published_time" content="2026-07-15T12:00:00Z">
        <style>hidden style</style><script>ignore_this()</script></head>
        <body><nav>Navigation</nav><main><h1>Hello &amp; welcome</h1>
        <p>This is the useful article text.</p></main></body></html>"""
        transport = FakeTransport(
            {
                "https://example.com/article": RawResponse(
                    status=200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    body=body,
                )
            }
        )
        client = FetchClient(guard=public_guard(), transport=transport)

        result = client.fetch(FetchRequest("https://example.com/article"))

        self.assertEqual(result["title"], "Example title")
        self.assertEqual(result["description"], "A useful description")
        self.assertEqual(result["author"], "Ada Lovelace")
        self.assertEqual(result["published_at"], "2026-07-15T12:00:00Z")
        self.assertIn("Hello & welcome", result["text"])
        self.assertIn("This is the useful article text.", result["text"])
        self.assertNotIn("Navigation", result["text"])
        self.assertNotIn("ignore_this", result["text"])
        self.assertNotIn("hidden style", result["text"])
        self.assertEqual(result["content_sha256"], hashlib.sha256(body).hexdigest())
        self.assertTrue(result["untrusted"])
        self.assertFalse(result["cached"])

    def test_revalidates_redirects_and_blocks_private_destination(self):
        transport = FakeTransport(
            {
                "https://example.com/start": RawResponse(
                    status=302,
                    headers={"location": "http://127.0.0.1/admin"},
                    body=b"",
                )
            }
        )
        client = FetchClient(guard=public_guard(), transport=transport)

        with self.assertRaises(FetchPolicyError):
            client.fetch(FetchRequest("https://example.com/start"))

        self.assertEqual(len(transport.calls), 1)

    def test_rejects_binary_content_and_oversized_body(self):
        responses = {
            "https://example.com/image": RawResponse(
                status=200, headers={"content-type": "image/png"}, body=b"png"
            ),
            "https://example.com/huge": RawResponse(
                status=200,
                headers={"content-type": "text/plain"},
                body=b"x" * 101,
            ),
        }
        client = FetchClient(guard=public_guard(), transport=FakeTransport(responses))

        with self.assertRaises(FetchPolicyError):
            client.fetch(FetchRequest("https://example.com/image"))
        with self.assertRaises(FetchBackendError):
            client.fetch(FetchRequest("https://example.com/huge", max_bytes=100))

    def test_truncates_extracted_text_to_requested_character_limit(self):
        transport = FakeTransport(
            {
                "https://example.com/text": RawResponse(
                    status=200,
                    headers={"content-type": "text/plain; charset=utf-8"},
                    body=b"one two three four five",
                )
            }
        )
        client = FetchClient(guard=public_guard(), transport=transport)

        result = client.fetch(FetchRequest("https://example.com/text", max_chars=10))

        self.assertEqual(result["text"], "one two th")
        self.assertTrue(result["truncated"])

    def test_uses_sqlite_cache_for_identical_requests(self):
        transport = FakeTransport(
            {
                "https://example.com/": RawResponse(
                    status=200,
                    headers={"content-type": "text/plain"},
                    body=b"cached page",
                )
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteFetchCache(Path(directory) / "fetch.sqlite3", ttl_seconds=60)
            client = FetchClient(guard=public_guard(), transport=transport, cache=cache)

            first = client.fetch(FetchRequest("https://example.com/"))
            second = client.fetch(FetchRequest("https://example.com/"))

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(transport.calls), 1)


class FetchCliTests(unittest.TestCase):
    def test_cli_prints_json(self):
        class FakeClient:
            def fetch(self, request):
                return {"url": request.url, "text": "example", "cached": False}

        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["--url", "https://example.com", "--max-chars", "100"],
            client=FakeClient(),
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["text"], "example")
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_distinguishes_policy_errors(self):
        class FailingClient:
            def fetch(self, request):
                raise FetchPolicyError("private address blocked")

        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["--url", "http://127.0.0.1"],
            client=FailingClient(),
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 3)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"error": "private address blocked", "type": "policy"},
        )
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
