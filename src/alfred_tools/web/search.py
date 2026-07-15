"""A small, stable search interface over a private SearXNG instance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Protocol, TextIO

DEFAULT_BASE_URL = "http://127.0.0.1:8888"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 2_000_000
VALID_TIME_RANGES = {"day", "month", "year"}


class SearchBackendError(RuntimeError):
    """Raised when SearXNG cannot provide a usable response."""


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    categories: tuple[str, ...] = ()
    language: str | None = None
    page: int = 1
    time_range: str | None = None
    safe_search: int = 0
    limit: int = 10

    def __post_init__(self) -> None:
        query = self.query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if len(query) > 500:
            raise ValueError("query must contain at most 500 characters")
        if not 1 <= self.limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if self.page < 1:
            raise ValueError("page must be at least 1")
        if self.safe_search not in {0, 1, 2}:
            raise ValueError("safe_search must be 0, 1, or 2")
        if self.time_range is not None and self.time_range not in VALID_TIME_RANGES:
            raise ValueError("time_range must be day, month, or year")
        if any(not category.strip() or "," in category for category in self.categories):
            raise ValueError("categories must be non-empty names without commas")
        object.__setattr__(self, "query", query)
        object.__setattr__(
            self, "categories", tuple(category.strip() for category in self.categories)
        )

    def to_parameters(self) -> dict[str, str]:
        parameters = {
            "q": self.query,
            "format": "json",
            "pageno": str(self.page),
            "safesearch": str(self.safe_search),
        }
        if self.categories:
            parameters["categories"] = ",".join(self.categories)
        if self.language:
            parameters["language"] = self.language
        if self.time_range:
            parameters["time_range"] = self.time_range
        return parameters


class SearchTransport(Protocol):
    def search(
        self, base_url: str, parameters: dict[str, str], timeout: float
    ) -> dict[str, Any]: ...


class UrllibSearchTransport:
    """POST form data to SearXNG using only the Python standard library."""

    def search(self, base_url: str, parameters: dict[str, str], timeout: float) -> dict[str, Any]:
        data = urllib.parse.urlencode(parameters).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "AlfredWebSearch/0.1",
        }
        if _is_loopback_host(urllib.parse.urlsplit(base_url).hostname):
            headers["X-Real-IP"] = "127.0.0.1"
        request = urllib.request.Request(
            f"{base_url}/search",
            data=data,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            detail = error.read(512).decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise SearchBackendError(f"SearXNG returned HTTP {error.code}{suffix}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise SearchBackendError(f"could not reach SearXNG: {error}") from error

        if len(payload) > MAX_RESPONSE_BYTES:
            raise SearchBackendError("SearXNG response exceeded 2 MB")
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SearchBackendError("SearXNG returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise SearchBackendError("SearXNG returned an unexpected JSON value")
        return decoded


class SQLiteSearchCache:
    def __init__(self, path: Path, ttl_seconds: int = 300):
        if ttl_seconds < 0:
            raise ValueError("cache TTL must not be negative")
        self.path = path
        self.ttl_seconds = ttl_seconds

    def get(self, key: str) -> dict[str, Any] | None:
        if self.ttl_seconds == 0 or not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT value, created_at FROM search_cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if time.time() - row[1] > self.ttl_seconds:
                connection.execute("DELETE FROM search_cache WHERE key = ?", (key,))
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
                "CREATE TABLE IF NOT EXISTS search_cache "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO search_cache (key, value, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )
            connection.commit()


class SearchClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: SearchTransport | None = None,
        cache: SQLiteSearchCache | None = None,
    ):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("SearXNG URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("SearXNG URL must not contain credentials, a query, or a fragment")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or UrllibSearchTransport()
        self.cache = cache

    def search(self, request: SearchRequest) -> dict[str, Any]:
        cache_key = self._cache_key(request)
        if self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                cached["cached"] = True
                return cached

        try:
            raw = self.transport.search(self.base_url, request.to_parameters(), self.timeout)
        except SearchBackendError:
            raise
        except Exception as error:
            raise SearchBackendError(str(error)) from error

        response = _normalize_response(raw, request)
        if self.cache:
            self.cache.set(cache_key, response)
        return response

    def _cache_key(self, request: SearchRequest) -> str:
        payload = json.dumps(
            {"base_url": self.base_url, "request": asdict(request)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _normal_url_key(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        return None
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), hostname + port, path, parsed.query, ""))


def _result_engines(result: dict[str, Any]) -> list[str]:
    engines = result.get("engines")
    if not isinstance(engines, list):
        engines = [result.get("engine")] if result.get("engine") else []
    return list(dict.fromkeys(str(engine) for engine in engines if engine))


def _normalize_response(raw: dict[str, Any], request: SearchRequest) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    by_url: dict[str, dict[str, Any]] = {}
    raw_results = raw.get("results") if isinstance(raw.get("results"), list) else []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        url_key = _normal_url_key(url)
        if not url_key:
            continue
        if url_key in by_url:
            existing = by_url[url_key]
            existing["engines"] = list(
                dict.fromkeys([*existing["engines"], *_result_engines(item)])
            )
            continue
        normalized = {
            "title": str(item.get("title") or "").strip(),
            "url": url,
            "snippet": str(item.get("content") or "").strip(),
            "engines": _result_engines(item),
            "score": item.get("score"),
            "category": item.get("category"),
            "published_at": item.get("publishedDate") or item.get("published_at"),
        }
        by_url[url_key] = normalized
        results.append(normalized)
        if len(results) >= request.limit:
            break

    warnings: list[dict[str, str]] = []
    raw_warnings = raw.get("unresponsive_engines")
    if isinstance(raw_warnings, list):
        for warning in raw_warnings:
            if isinstance(warning, (list, tuple)) and len(warning) >= 2:
                warnings.append({"engine": str(warning[0]), "error": str(warning[1])})

    def list_value(name: str) -> list[Any]:
        value = raw.get(name)
        return value if isinstance(value, list) else []

    return {
        "query": str(raw.get("query") or request.query),
        "results": results,
        "answers": list_value("answers"),
        "corrections": list_value("corrections"),
        "suggestions": list_value("suggestions"),
        "warnings": warnings,
        "fetched_at": datetime.now(UTC).isoformat(),
        "cached": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search the web through Alfred's SearXNG instance")
    parser.add_argument("--query", "-q", required=True)
    parser.add_argument("--category", action="append", default=[], dest="categories")
    parser.add_argument("--language")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--time-range", choices=sorted(VALID_TIME_RANGES))
    parser.add_argument("--safe-search", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--base-url", default=os.environ.get("ALFRED_SEARXNG_URL", DEFAULT_BASE_URL)
    )
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
        request = SearchRequest(
            query=args.query,
            categories=tuple(args.categories),
            language=args.language,
            page=args.page,
            time_range=args.time_range,
            safe_search=args.safe_search,
            limit=args.limit,
        )
        if client is None:
            cache_path = Path(
                os.environ.get(
                    "ALFRED_SEARCH_CACHE",
                    Path.cwd() / ".cache" / "alfred" / "search.sqlite3",
                )
            )
            cache = None if args.no_cache else SQLiteSearchCache(cache_path)
            client = SearchClient(args.base_url, timeout=args.timeout, cache=cache)
        response = client.search(request)
    except ValueError as error:
        json.dump({"error": str(error), "type": "input"}, stderr, ensure_ascii=False)
        stderr.write("\n")
        return 1
    except SearchBackendError as error:
        json.dump({"error": str(error), "type": "backend"}, stderr, ensure_ascii=False)
        stderr.write("\n")
        return 2

    json.dump(response, stdout, ensure_ascii=False, indent=2)
    stdout.write("\n")
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
