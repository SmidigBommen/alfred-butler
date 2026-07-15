"""Safely retrieve compact, readable evidence from public web pages."""

from __future__ import annotations

import argparse
import codecs
import hashlib
import http.client
import json
import os
import re
import socket
import sqlite3
import ssl
import sys
import time
import urllib.parse
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Protocol, TextIO

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_CHARS = 50_000
DEFAULT_MAX_REDIRECTS = 5
MAX_URL_LENGTH = 4_096
READABLE_CONTENT_TYPES = {
    "application/json",
    "application/xhtml+xml",
    "text/html",
    "text/plain",
}


class FetchPolicyError(RuntimeError):
    """Raised when a URL or response violates the fetch safety policy."""


class FetchBackendError(RuntimeError):
    """Raised when a remote server cannot provide a usable response."""


@dataclass(frozen=True, slots=True)
class FetchRequest:
    url: str
    max_chars: int = DEFAULT_MAX_CHARS
    max_bytes: int = DEFAULT_MAX_BYTES
    max_redirects: int = DEFAULT_MAX_REDIRECTS

    def __post_init__(self) -> None:
        url = self.url.strip()
        if not url:
            raise ValueError("URL must not be empty")
        if not 1 <= self.max_chars <= 200_000:
            raise ValueError("max_chars must be between 1 and 200000")
        if not 1 <= self.max_bytes <= 10_000_000:
            raise ValueError("max_bytes must be between 1 and 10000000")
        if not 0 <= self.max_redirects <= 10:
            raise ValueError("max_redirects must be between 0 and 10")
        object.__setattr__(self, "url", url)


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]
    request_target: str


@dataclass(frozen=True, slots=True)
class RawResponse:
    status: int
    headers: dict[str, str]
    body: bytes


Resolver = Callable[[str, int], list[str]]


def _default_resolver(host: str, port: int) -> list[str]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise FetchBackendError(f"DNS resolution failed for {host}: {error}") from error
    return list(dict.fromkeys(answer[4][0] for answer in answers))


class UrlGuard:
    def __init__(self, resolver: Resolver | None = None):
        self.resolver = resolver or _default_resolver

    def validate(self, url: str) -> ResolvedTarget:
        if len(url) > MAX_URL_LENGTH:
            raise FetchPolicyError("URL exceeds 4096 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in url):
            raise FetchPolicyError("URL contains control characters")
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise FetchPolicyError(f"invalid URL: {error}") from error
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise FetchPolicyError("only HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise FetchPolicyError("URL credentials are not allowed")
        if not parsed.hostname:
            raise FetchPolicyError("URL must include a hostname")

        try:
            host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as error:
            raise FetchPolicyError("URL hostname is not valid IDNA") from error
        if not host or host == "localhost" or host.endswith(".localhost"):
            raise FetchPolicyError("localhost destinations are blocked")
        port = port or (443 if scheme == "https" else 80)

        try:
            literal = ip_address(host)
        except ValueError:
            addresses = self.resolver(host, port)
        else:
            addresses = [literal.compressed]
        if not addresses:
            raise FetchBackendError(f"DNS resolution returned no addresses for {host}")

        normalized_addresses: list[str] = []
        for value in addresses:
            try:
                address = ip_address(value)
            except ValueError as error:
                raise FetchBackendError(f"DNS returned an invalid address for {host}") from error
            if not address.is_global:
                raise FetchPolicyError(f"non-public destination is blocked: {address.compressed}")
            normalized_addresses.append(address.compressed)

        path = urllib.parse.quote(
            parsed.path or "/",
            safe="/%:@!$&'()*+,;=-._~",
        )
        query = urllib.parse.quote(
            parsed.query,
            safe="=&;%:@/?+,$-_.!~*'()",
        )
        default_port = 443 if scheme == "https" else 80
        display_host = f"[{host}]" if ":" in host else host
        authority = display_host if port == default_port else f"{display_host}:{port}"
        normalized_url = urllib.parse.urlunsplit((scheme, authority, path, query, ""))
        request_target = path + (f"?{query}" if query else "")
        return ResolvedTarget(
            url=normalized_url,
            scheme=scheme,
            host=host,
            port=port,
            addresses=tuple(dict.fromkeys(normalized_addresses)),
            request_target=request_target,
        )


class FetchTransport(Protocol):
    def request(self, target: ResolvedTarget, timeout: float, max_bytes: int) -> RawResponse: ...


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, address, timeout)
        self._context = ssl.create_default_context()
        self._context.set_alpn_protocols(["http/1.1"])

    def connect(self) -> None:
        super().connect()
        assert self.sock is not None
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class PinnedHttpTransport:
    """Connect to a policy-checked IP while preserving HTTP Host and TLS SNI."""

    def request(self, target: ResolvedTarget, timeout: float, max_bytes: int) -> RawResponse:
        default_port = 443 if target.scheme == "https" else 80
        host_header = target.host if target.port == default_port else f"{target.host}:{target.port}"
        headers = {
            "Accept": "text/html, application/xhtml+xml, text/plain, application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "Host": host_header,
            "User-Agent": "AlfredWebFetch/0.1",
        }
        failures: list[str] = []
        for address in target.addresses:
            connection_class = (
                _PinnedHTTPSConnection if target.scheme == "https" else _PinnedHTTPConnection
            )
            connection = connection_class(target.host, target.port, address, timeout)
            try:
                connection.request("GET", target.request_target, headers=headers)
                response = connection.getresponse()
                content_length = response.getheader("Content-Length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    raise FetchBackendError("response exceeds configured byte limit")
                body = response.read(max_bytes + 1)
                response_headers = {key.lower(): value for key, value in response.getheaders()}
                return RawResponse(response.status, response_headers, body)
            except FetchBackendError:
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException) as error:
                failures.append(f"{address}: {error}")
            finally:
                connection.close()
        detail = "; ".join(failures) if failures else "no address was attempted"
        raise FetchBackendError(f"request failed for {target.host}: {detail}")


class _ReadableHTMLParser(HTMLParser):
    _ignored_tags = {
        "aside",
        "dialog",
        "footer",
        "form",
        "nav",
        "noscript",
        "script",
        "style",
        "svg",
        "template",
    }
    _block_tags = {
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "main",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._focus_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._focused_parts: list[str] = []
        self.description: str | None = None
        self.author: str | None = None
        self.published_at: str | None = None

    @property
    def title(self) -> str | None:
        title = _collapse_text(" ".join(self._title_parts))
        return title or None

    @property
    def text(self) -> str:
        focused = _collapse_text(" ".join(self._focused_parts))
        return focused or _collapse_text(" ".join(self._text_parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._ignored_tags:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"article", "main"}:
            self._focus_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self._block_tags:
            self._text_parts.append(" ")
            if self._focus_depth:
                self._focused_parts.append(" ")
        if tag == "meta":
            values = {key.lower(): value for key, value in attrs if value is not None}
            name = (values.get("name") or values.get("property") or "").lower()
            content = _collapse_text(values.get("content", ""))
            if not content:
                return
            if name in {"description", "og:description", "twitter:description"}:
                self.description = self.description or content
            elif name in {"author", "article:author"}:
                self.author = self.author or content
            elif name in {
                "article:published_time",
                "date",
                "datepublished",
                "publish-date",
            }:
                self.published_at = self.published_at or content

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._ignored_tags and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag in self._block_tags:
            self._text_parts.append(" ")
            if self._focus_depth:
                self._focused_parts.append(" ")
        if tag in {"article", "main"} and self._focus_depth:
            self._focus_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        else:
            self._text_parts.append(data)
            if self._focus_depth:
                self._focused_parts.append(data)


def _collapse_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _content_type(headers: dict[str, str]) -> tuple[str, str]:
    header = headers.get("content-type", "")
    media_type = header.split(";", 1)[0].strip().lower()
    charset_match = re.search(r"charset\s*=\s*[\"']?([^;\"']+)", header, re.IGNORECASE)
    charset = charset_match.group(1).strip() if charset_match else "utf-8"
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = "utf-8"
    return media_type, charset


class SQLiteFetchCache:
    def __init__(self, path: Path, ttl_seconds: int = 3_600):
        if ttl_seconds < 0:
            raise ValueError("cache TTL must not be negative")
        self.path = path
        self.ttl_seconds = ttl_seconds

    def get(self, key: str) -> dict[str, Any] | None:
        if self.ttl_seconds == 0 or not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT value, created_at FROM fetch_cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if time.time() - row[1] > self.ttl_seconds:
                connection.execute("DELETE FROM fetch_cache WHERE key = ?", (key,))
                connection.commit()
                return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        if self.ttl_seconds == 0:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS fetch_cache "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO fetch_cache (key, value, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )
            connection.commit()


class FetchClient:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        guard: UrlGuard | None = None,
        transport: FetchTransport | None = None,
        cache: SQLiteFetchCache | None = None,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self.guard = guard or UrlGuard()
        self.transport = transport or PinnedHttpTransport()
        self.cache = cache

    def fetch(self, request: FetchRequest) -> dict[str, Any]:
        cache_key = hashlib.sha256(
            json.dumps(asdict(request), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                cached["cached"] = True
                return cached

        current_url = request.url
        seen: set[str] = set()
        redirects: list[str] = []
        for redirect_count in range(request.max_redirects + 1):
            target = self.guard.validate(current_url)
            if target.url in seen:
                raise FetchBackendError("redirect loop detected")
            seen.add(target.url)
            try:
                raw = self.transport.request(target, self.timeout, request.max_bytes)
            except (FetchBackendError, FetchPolicyError):
                raise
            except Exception as error:
                raise FetchBackendError(str(error)) from error
            if len(raw.body) > request.max_bytes:
                raise FetchBackendError("response exceeds configured byte limit")

            headers = {key.lower(): value for key, value in raw.headers.items()}
            if 300 <= raw.status < 400:
                location = headers.get("location")
                if not location:
                    raise FetchBackendError(f"HTTP {raw.status} redirect omitted Location")
                if redirect_count >= request.max_redirects:
                    raise FetchBackendError("redirect limit exceeded")
                redirects.append(target.url)
                current_url = urllib.parse.urljoin(target.url, location)
                continue
            if not 200 <= raw.status < 300:
                raise FetchBackendError(f"remote server returned HTTP {raw.status}")

            media_type, charset = _content_type(headers)
            if media_type not in READABLE_CONTENT_TYPES:
                label = media_type or "missing Content-Type"
                raise FetchPolicyError(f"unsupported content type: {label}")
            content_encoding = headers.get("content-encoding", "identity").lower().strip()
            if content_encoding not in {"", "identity"}:
                raise FetchPolicyError(f"unsupported content encoding: {content_encoding}")
            decoded = raw.body.decode(charset, errors="replace")
            title = description = author = published_at = None
            if media_type in {"text/html", "application/xhtml+xml"}:
                parser = _ReadableHTMLParser()
                try:
                    parser.feed(decoded)
                    parser.close()
                except Exception as error:
                    raise FetchBackendError(f"HTML parsing failed: {error}") from error
                text = parser.text
                title = parser.title
                description = parser.description
                author = parser.author
                published_at = parser.published_at
            else:
                text = _collapse_text(decoded)

            truncated = len(text) > request.max_chars
            result = {
                "requested_url": request.url,
                "url": target.url,
                "status": raw.status,
                "content_type": media_type,
                "title": title,
                "description": description,
                "author": author,
                "published_at": published_at,
                "text": text[: request.max_chars],
                "truncated": truncated,
                "redirects": redirects,
                "content_sha256": hashlib.sha256(raw.body).hexdigest(),
                "fetched_at": datetime.now(UTC).isoformat(),
                "cached": False,
                "untrusted": True,
            }
            if self.cache:
                self.cache.set(cache_key, result)
            return result
        raise FetchBackendError("redirect limit exceeded")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely fetch readable public web content")
    parser.add_argument("--url", required=True)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-redirects", type=int, default=DEFAULT_MAX_REDIRECTS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--no-cache", action="store_true")
    return parser


def run_cli(
    argv: list[str] | None = None,
    *,
    client: Any | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    try:
        request = FetchRequest(
            url=args.url,
            max_chars=args.max_chars,
            max_bytes=args.max_bytes,
            max_redirects=args.max_redirects,
        )
        if client is None:
            cache_path = Path(
                os.environ.get(
                    "ALFRED_FETCH_CACHE",
                    Path.cwd() / ".cache" / "alfred" / "fetch.sqlite3",
                )
            )
            cache = None if args.no_cache else SQLiteFetchCache(cache_path)
            client = FetchClient(timeout=args.timeout, cache=cache)
        result = client.fetch(request)
    except ValueError as error:
        json.dump({"error": str(error), "type": "input"}, stderr, ensure_ascii=False)
        stderr.write("\n")
        return 1
    except FetchBackendError as error:
        json.dump({"error": str(error), "type": "backend"}, stderr, ensure_ascii=False)
        stderr.write("\n")
        return 2
    except FetchPolicyError as error:
        json.dump({"error": str(error), "type": "policy"}, stderr, ensure_ascii=False)
        stderr.write("\n")
        return 3

    json.dump(result, stdout, ensure_ascii=False, indent=2)
    stdout.write("\n")
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
