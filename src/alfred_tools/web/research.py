"""Collect a bounded, diverse web evidence bundle for AI synthesis."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from alfred_tools.web.fetch import (
    FetchBackendError,
    FetchClient,
    FetchPolicyError,
    FetchRequest,
    SQLiteFetchCache,
)
from alfred_tools.web.search import (
    DEFAULT_BASE_URL,
    SearchBackendError,
    SearchClient,
    SearchRequest,
    SQLiteSearchCache,
)

MIN_EVIDENCE_CHARS = 200
CHALLENGE_MARKERS = (
    "checking your browser",
    "enable javascript and cookies to continue",
    "required part of this site couldn’t load",
    "verify you are human",
)


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    queries: tuple[str, ...]
    max_sources: int = 4
    results_per_query: int = 5
    max_chars_per_source: int = 12_000
    category: str = "general"
    language: str | None = None
    time_range: str | None = None
    safe_search: int = 0

    def __post_init__(self) -> None:
        queries = tuple(dict.fromkeys(query.strip() for query in self.queries if query.strip()))
        if not queries:
            raise ValueError("at least one research query is required")
        if len(queries) > 8:
            raise ValueError("at most eight research queries are allowed")
        if any(len(query) > 500 for query in queries):
            raise ValueError("research queries must contain at most 500 characters")
        if not 1 <= self.max_sources <= 10:
            raise ValueError("max_sources must be between 1 and 10")
        if not 1 <= self.results_per_query <= 20:
            raise ValueError("results_per_query must be between 1 and 20")
        if not MIN_EVIDENCE_CHARS <= self.max_chars_per_source <= 50_000:
            raise ValueError("max_chars_per_source must be between 200 and 50000")
        if not self.category.strip() or "," in self.category:
            raise ValueError("category must be one non-empty category name")
        if self.safe_search not in {0, 1, 2}:
            raise ValueError("safe_search must be 0, 1, or 2")
        if self.time_range not in {None, "day", "month", "year"}:
            raise ValueError("time_range must be day, month, or year")
        object.__setattr__(self, "queries", queries)
        object.__setattr__(self, "category", self.category.strip())


def _canonical_url(url: str) -> tuple[str, str] | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    default_port = 443 if parsed.scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    path = parsed.path or "/"
    canonical = urllib.parse.urlunsplit((parsed.scheme.lower(), authority, path, parsed.query, ""))
    return canonical, authority


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _evidence_quality_error(fetched: dict[str, Any]) -> str | None:
    evidence_text = str(fetched.get("text") or "").strip()
    title = str(fetched.get("title") or "").strip().lower()
    sample = evidence_text[:1000].lower()
    if "client challenge" in title or any(marker in sample for marker in CHALLENGE_MARKERS):
        return "challenge page did not provide source evidence"
    if len(evidence_text) < MIN_EVIDENCE_CHARS:
        return f"extracted evidence is too short ({len(evidence_text)} characters)"
    return None


class ResearchClient:
    def __init__(self, *, search_client: Any, fetch_client: Any):
        self.search_client = search_client
        self.fetch_client = fetch_client

    def research(self, request: ResearchRequest) -> dict[str, Any]:
        candidates: dict[str, dict[str, Any]] = {}
        warnings: list[dict[str, Any]] = []
        for query in request.queries:
            search_request = SearchRequest(
                query=query,
                categories=(request.category,),
                language=request.language,
                time_range=request.time_range,
                safe_search=request.safe_search,
                limit=request.results_per_query,
            )
            try:
                response = self.search_client.search(search_request)
            except SearchBackendError as error:
                warnings.append(
                    {"stage": "search", "query": query, "type": "backend", "error": str(error)}
                )
                continue
            for warning in response.get("warnings", []):
                if isinstance(warning, dict):
                    warnings.append(
                        {
                            "stage": "search",
                            "query": query,
                            "engine": str(warning.get("engine", "unknown")),
                            "error": str(warning.get("error", "unknown error")),
                        }
                    )
            for result in response.get("results", []):
                if not isinstance(result, dict):
                    continue
                url = str(result.get("url", ""))
                normalized = _canonical_url(url)
                if normalized is None:
                    continue
                canonical, domain = normalized
                engines = [str(engine) for engine in result.get("engines", []) if engine]
                if canonical in candidates:
                    candidate = candidates[canonical]
                    candidate["queries"] = list(dict.fromkeys([*candidate["queries"], query]))
                    candidate["engines"] = list(dict.fromkeys([*candidate["engines"], *engines]))
                    candidate["score"] = max(candidate["score"], _score(result.get("score")))
                    continue
                candidates[canonical] = {
                    "url": url,
                    "domain": domain,
                    "title": str(result.get("title") or "").strip(),
                    "snippet": str(result.get("snippet") or "").strip(),
                    "score": _score(result.get("score")),
                    "engines": list(dict.fromkeys(engines)),
                    "queries": [query],
                }

        ranked = sorted(
            candidates.values(),
            key=lambda candidate: (
                -len(candidate["queries"]),
                -candidate["score"],
                -len(candidate["engines"]),
                candidate["url"],
            ),
        )
        attempt_order: list[dict[str, Any]] = []
        seen_domains: set[str] = set()
        for candidate in ranked:
            if candidate["domain"] not in seen_domains:
                attempt_order.append(candidate)
                seen_domains.add(candidate["domain"])
        attempt_order.extend(candidate for candidate in ranked if candidate not in attempt_order)

        sources: list[dict[str, Any]] = []
        max_attempts = min(len(attempt_order), request.max_sources * 3)
        for candidate in attempt_order[:max_attempts]:
            if len(sources) >= request.max_sources:
                break
            try:
                fetched = self.fetch_client.fetch(
                    FetchRequest(
                        candidate["url"],
                        max_chars=request.max_chars_per_source,
                    )
                )
            except FetchPolicyError as error:
                warnings.append(
                    {
                        "stage": "fetch",
                        "url": candidate["url"],
                        "type": "policy",
                        "error": str(error),
                    }
                )
                continue
            except FetchBackendError as error:
                warnings.append(
                    {
                        "stage": "fetch",
                        "url": candidate["url"],
                        "type": "backend",
                        "error": str(error),
                    }
                )
                continue
            quality_error = _evidence_quality_error(fetched)
            if quality_error is not None:
                warnings.append(
                    {
                        "stage": "fetch",
                        "url": candidate["url"],
                        "type": "quality",
                        "error": quality_error,
                    }
                )
                continue
            source = dict(fetched)
            source["search"] = {
                "title": candidate["title"],
                "snippet": candidate["snippet"],
                "score": candidate["score"],
                "engines": candidate["engines"],
                "queries": candidate["queries"],
            }
            sources.append(source)

        return {
            "queries": list(request.queries),
            "sources": sources,
            "warnings": warnings,
            "generated_at": datetime.now(UTC).isoformat(),
            "untrusted": True,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect web evidence for Alfred research")
    parser.add_argument("--query", action="append", required=True, dest="queries")
    parser.add_argument("--max-sources", type=int, default=4)
    parser.add_argument("--results-per-query", type=int, default=5)
    parser.add_argument("--max-chars-per-source", type=int, default=12_000)
    parser.add_argument("--category", default="general")
    parser.add_argument("--language")
    parser.add_argument("--time-range", choices=("day", "month", "year"))
    parser.add_argument("--safe-search", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--timeout", type=float, default=15.0)
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
        request = ResearchRequest(
            queries=tuple(args.queries),
            max_sources=args.max_sources,
            results_per_query=args.results_per_query,
            max_chars_per_source=args.max_chars_per_source,
            category=args.category,
            language=args.language,
            time_range=args.time_range,
            safe_search=args.safe_search,
        )
        if client is None:
            cache_root = Path.cwd() / ".cache" / "alfred"
            search_cache_path = Path(
                os.environ.get("ALFRED_SEARCH_CACHE", cache_root / "search.sqlite3")
            )
            fetch_cache_path = Path(
                os.environ.get("ALFRED_FETCH_CACHE", cache_root / "fetch.sqlite3")
            )
            search_cache = None if args.no_cache else SQLiteSearchCache(search_cache_path)
            fetch_cache = None if args.no_cache else SQLiteFetchCache(fetch_cache_path)
            search_client = SearchClient(
                os.environ.get("ALFRED_SEARXNG_URL", DEFAULT_BASE_URL),
                timeout=args.timeout,
                cache=search_cache,
            )
            fetch_client = FetchClient(timeout=args.timeout, cache=fetch_cache)
            client = ResearchClient(search_client=search_client, fetch_client=fetch_client)
        result = client.research(request)
    except ValueError as error:
        json.dump({"error": str(error), "type": "input"}, stderr, ensure_ascii=False)
        stderr.write("\n")
        return 1

    json.dump(result, stdout, ensure_ascii=False, indent=2)
    stdout.write("\n")
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
