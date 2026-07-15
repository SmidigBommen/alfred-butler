"""Allowlisted adapters from model tool calls to Alfred's deterministic tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from alfred_tools.notes.cli import default_notes_root
from alfred_tools.notes.store import NoteStore
from alfred_tools.orchestrator.engine import Tool
from alfred_tools.web.fetch import FetchClient, FetchRequest, SQLiteFetchCache
from alfred_tools.web.research import ResearchClient, ResearchRequest
from alfred_tools.web.search import (
    DEFAULT_BASE_URL,
    SearchClient,
    SearchRequest,
    SQLiteSearchCache,
)


def build_alfred_tools(*, notes: Any, search: Any, fetch: Any, research: Any) -> tuple[Tool, ...]:
    def memory_search(arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments["query"])
        return {"query": query, "results": notes.search(query, limit=8)}

    def memory_capture(arguments: dict[str, Any]) -> dict[str, Any]:
        return notes.capture(
            title=str(arguments["title"]),
            body=str(arguments["body"]),
            kind=str(arguments["kind"]),
            labels=tuple(str(label) for label in arguments["labels"]),
            importance=float(arguments["importance"]),
            sensitivity="normal",
            automatic=True,
        ).to_dict()

    def web_search(arguments: dict[str, Any]) -> dict[str, Any]:
        return search.search(
            SearchRequest(
                query=str(arguments["query"]),
                categories=("general",),
                language="en",
                limit=5,
            )
        )

    def web_fetch(arguments: dict[str, Any]) -> dict[str, Any]:
        return fetch.fetch(FetchRequest(url=str(arguments["url"]), max_chars=12_000))

    def web_research(arguments: dict[str, Any]) -> dict[str, Any]:
        queries = tuple(str(query) for query in arguments["queries"])
        return research.research(
            ResearchRequest(
                queries=queries,
                max_sources=4,
                results_per_query=5,
                max_chars_per_source=12_000,
                language="en",
            )
        )

    return (
        Tool(
            name="memory_search",
            description="Search Alfred's private local memory for relevant personal context.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 500}},
                "required": ["query"],
                "additionalProperties": False,
            },
            permission="read",
            handler=memory_search,
            remote_allowed=False,
        ),
        Tool(
            name="memory_capture",
            description=(
                "Save a durable, non-sensitive preference, decision, idea, correction, or "
                "procedure to private local memory. Never use for credentials or sensitive data."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 200},
                    "body": {"type": "string", "maxLength": 10000},
                    "kind": {
                        "type": "string",
                        "enum": ["note", "preference", "fact", "decision", "idea", "procedure"],
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 12,
                    },
                    "importance": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["title", "body", "kind", "labels", "importance"],
                "additionalProperties": False,
            },
            permission="local_write",
            handler=memory_capture,
            remote_allowed=False,
        ),
        Tool(
            name="web_search",
            description="Search the live web for a small set of candidate sources.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 500}},
                "required": ["query"],
                "additionalProperties": False,
            },
            permission="network_read",
            handler=web_search,
        ),
        Tool(
            name="web_fetch",
            description="Safely fetch readable evidence from one public HTTP(S) URL.",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string", "maxLength": 4096}},
                "required": ["url"],
                "additionalProperties": False,
            },
            permission="network_read",
            handler=web_fetch,
        ),
        Tool(
            name="web_research",
            description="Collect a diverse evidence bundle for up to four focused web queries.",
            input_schema={
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 4,
                    }
                },
                "required": ["queries"],
                "additionalProperties": False,
            },
            permission="network_read",
            handler=web_research,
        ),
    )


def default_alfred_tools() -> tuple[Tool, ...]:
    cache_root = Path(
        os.environ.get("ALFRED_CACHE_DIR", Path.home() / ".cache" / "alfred")
    ).expanduser()
    search = SearchClient(
        os.environ.get("ALFRED_SEARXNG_URL", DEFAULT_BASE_URL),
        cache=SQLiteSearchCache(cache_root / "search.sqlite3"),
    )
    fetch = FetchClient(cache=SQLiteFetchCache(cache_root / "fetch.sqlite3"))
    research = ResearchClient(search_client=search, fetch_client=fetch)
    return build_alfred_tools(
        notes=NoteStore(default_notes_root()),
        search=search,
        fetch=fetch,
        research=research,
    )
